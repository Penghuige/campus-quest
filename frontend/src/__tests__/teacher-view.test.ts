/**
 * Task 9 (Plan 09): pure teacher-view pins — the lifecycle confirmation
 * model, the create-form band mirrors, the §7.1 import preview view, the
 * review-queue copy, and the moderation privacy choke point.
 *
 * The binding constraints under test:
 * - lifecycle actions: which verbs render per status, and that
 *   publish/close/archive REQUIRE an explicit confirmation with
 *   consequence copy (design §9/§10);
 * - import preview: row-level vs file-level errors, counts text, and
 *   confirm GATING (valid rows > 0 AND a live token — an expired preview
 *   can never confirm);
 * - invalidate warning: the prominent 判无效 warning names the cancelled
 *   lock and the non-deletable audit trail;
 * - moderation rows: Object.keys of the view are EXACTLY the display
 *   fields — no student number/phone/email/login identifier can exist
 *   (the privacy pin).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";
import type {
  ImportPreviewDto,
  ModerationCommentDto,
  TeacherTaskDto,
} from "../features/admin/teacherApi";
import {
  capRowErrors,
  COLLABORATOR_PERMISSIONS,
  describeEditError,
  envelopeFields,
  EMPTY_TASK_FORM,
  frozenFieldText,
  importPreviewView,
  INVALIDATE_WARNING_TEXT,
  lifecycleActions,
  lockedTierText,
  moderationRowView,
  parseSubmissionSchema,
  permissionLabels,
  reportStatusView,
  reviewStatusView,
  reviewTextReady,
  rewardLockView,
  taskEditable,
  taskFormFromTask,
  taskFormToBody,
  taskFormToUpdateBody,
  toDatetimeLocalValue,
  taskStatusView,
  validateTaskForm,
  versionText,
} from "../features/admin/teacherView";

const NOW = Date.parse("2026-09-20T12:00:00Z");

function form(
  overrides: Partial<typeof EMPTY_TASK_FORM> = {},
): typeof EMPTY_TASK_FORM {
  return { ...EMPTY_TASK_FORM, ...overrides };
}

/** A create-ready FIXED form (baseline for the band tests). */
function readyForm() {
  return form({
    title: "图书馆书影采集",
    description: "拍摄图书馆藏书",
    baseRewardPoints: "160",
    fixedDeadlineLocal: "2026-10-30T18:00",
    fileTypes: ["CSV"],
  });
}

/** A schema-less FIXED DRAFT — exactly what the create dialog can produce. */
function draftTask(overrides: Partial<TeacherTaskDto> = {}): TeacherTaskDto {
  return {
    id: "t1",
    title: "图书馆书影采集",
    description: "拍摄图书馆藏书",
    task_type: "DATA_CRAWL",
    rarity: "NORMAL",
    base_reward_points: 160,
    status: "DRAFT",
    deadline_mode: "FIXED",
    fixed_deadline_at: "2026-10-30T10:00:00.000Z",
    duration_minutes: null,
    claim_cutoff_minutes: 240,
    grace_period_minutes: 1440,
    submission_schema: null,
    submission_schema_version: null,
    allowed_file_types: ["CSV"],
    max_file_size_bytes: 50 * 1024 * 1024,
    notify_24h: true,
    notify_4h: true,
    notification_channels: ["SMS", "EMAIL", "IN_APP"],
    published_at: null,
    closed_at: null,
    created_at: "2026-09-20T12:00:00Z",
    ...overrides,
  } as TeacherTaskDto;
}

describe("task edit surface (spec §6.2 V1 edit rule)", () => {
  test("taskEditable: DRAFT/PUBLISHED/PAUSED yes; CLOSED/ARCHIVED never", () => {
    assert.equal(taskEditable("DRAFT"), true);
    assert.equal(taskEditable("PUBLISHED"), true);
    assert.equal(taskEditable("PAUSED"), true);
    assert.equal(taskEditable("CLOSED"), false);
    assert.equal(taskEditable("ARCHIVED"), false);
  });

  test("taskFormFromTask round-trips the editable values (ISO -> datetime-local)", () => {
    const values = taskFormFromTask(draftTask());
    assert.equal(values.title, "图书馆书影采集");
    assert.equal(values.baseRewardPoints, "160");
    assert.equal(values.deadlineMode, "FIXED");
    // Zone-independent symmetry: the datetime-local value is LOCAL wall
    // time (the input's semantics), so re-parsing it yields the task's
    // UTC instant on ANY host timezone — no phantom deadline diff.
    assert.equal(
      new Date(values.fixedDeadlineLocal).getTime(),
      Date.parse("2026-10-30T10:00:00.000Z"),
    );
    assert.equal(
      toDatetimeLocalValue(Date.parse("2026-10-30T10:00:00.000Z")),
      values.fixedDeadlineLocal,
    );
    assert.equal(values.maxFileSizeMb, "50");
    assert.deepEqual(values.fileTypes, ["CSV"]);
    assert.equal(values.submissionSchema, "");
    assert.equal(values.submissionSchemaVersion, "");
  });

  test("DRAFT: changed fields ride the PATCH; unchanged fields stay home", () => {
    const values = {
      ...taskFormFromTask(draftTask()),
      submissionSchema: '{"columns":["platform","keyword"]}',
      submissionSchemaVersion: "1",
    };
    const result = taskFormToUpdateBody(values, draftTask(), NOW);
    assert.equal(result.ok, true);
    assert.deepEqual(result.errors, {});
    // ONLY the two schema fields changed -> only they ride the body.
    assert.deepEqual(result.body, {
      submission_schema: { columns: ["platform", "keyword"] },
      submission_schema_version: 1,
    });
  });

  test("DRAFT: no diff -> empty body (the dialog closes without a request)", () => {
    const result = taskFormToUpdateBody(taskFormFromTask(draftTask()), draftTask(), NOW);
    assert.equal(result.ok, true);
    assert.deepEqual(result.body, {});
  });

  test("PUBLISHED: contract diffs are EXCLUDED — an unchanged value must not ride either", () => {
    const task = draftTask({ status: "PUBLISHED", published_at: "2026-09-21T00:00:00Z" });
    const values = {
      ...taskFormFromTask(task),
      title: "更新后的标题",
      // Contract-looking changes on a published task...
      baseRewardPoints: "999",
      submissionSchema: '{"columns":["x"]}',
    };
    const result = taskFormToUpdateBody(values, task, NOW);
    assert.equal(result.ok, true);
    // ...never enter the body; only the presentation change rides.
    assert.deepEqual(result.body, { title: "更新后的标题" });
  });

  test("provided-only validation: an unchanged invalid band never blocks the save", () => {
    // A DRAFT whose deadline is already in the past (legal as a draft):
    // editing ONLY the title must not be blocked by the deadline band.
    const task = draftTask({ fixed_deadline_at: "2020-01-01T00:00:00.000Z" });
    const values = { ...taskFormFromTask(task), title: "仅改标题" };
    const result = taskFormToUpdateBody(values, task, NOW);
    assert.equal(result.ok, true);
    assert.deepEqual(result.body, { title: "仅改标题" });
  });

  test("a CHANGED field must pass its band (reward 0 refused client-side)", () => {
    const values = { ...taskFormFromTask(draftTask()), baseRewardPoints: "0" };
    const result = taskFormToUpdateBody(values, draftTask(), NOW);
    assert.equal(result.ok, false);
    assert.equal(result.errors.baseRewardPoints, "基础奖励积分必须大于 0");
  });

  test("a changed-but-unparseable schema text is refused client-side", () => {
    const values = { ...taskFormFromTask(draftTask()), submissionSchema: "{bad" };
    const result = taskFormToUpdateBody(values, draftTask(), NOW);
    assert.equal(result.ok, false);
    assert.equal(
      result.errors.submissionSchema,
      "提交校验 schema 必须是非空 JSON 对象",
    );
  });

  test("the load-bearing chain: create schema-less -> edit-in schema -> publish", () => {
    // Step 1: the create dialog accepts a schema-less DRAFT (DRAFT-legal).
    const createResult = taskFormToBody(readyForm(), NOW);
    assert.equal(createResult.ok, true);
    assert.equal(createResult.body?.submission_schema, null);

    // Step 2 (server): the created DRAFT echoes back without a schema.
    const task = draftTask();

    // Step 3: the edit dialog adds the schema; the diff body carries it.
    const editResult = taskFormToUpdateBody(
      {
        ...taskFormFromTask(task),
        submissionSchema: '{"columns":["platform","keyword"]}',
        submissionSchemaVersion: "1",
      },
      task,
      NOW,
    );
    assert.equal(editResult.ok, true);
    assert.deepEqual(editResult.body?.submission_schema, {
      columns: ["platform", "keyword"],
    });
    assert.equal(editResult.body?.submission_schema_version, 1);

    // Step 4: publish becomes available and is confirm-gated.
    const publish = lifecycleActions("DRAFT")[0];
    assert.equal(publish.verb, "publish");
    assert.equal(publish.confirmRequired, true);
  });

  test("frozenFieldText branches on details.fields, never the message", () => {
    const error = new ApiError({
      code: "VALIDATION_ERROR",
      message: "some unrelated message text",
      status: 400,
      details: { fields: ["base_reward_points", "deadline_mode"], status: "PUBLISHED" },
    });
    assert.equal(
      frozenFieldText(error),
      "以下字段在发布后已冻结，不可修改：基础奖励积分、截止模式",
    );
    // Any other failure shape: null (the generic path handles it).
    assert.equal(
      frozenFieldText(
        new ApiError({ code: "VALIDATION_ERROR", message: "x", status: 400, details: null }),
      ),
      null,
    );
    assert.equal(
      frozenFieldText(
        new ApiError({ code: "TASK_NOT_CLAIMABLE", message: "x", status: 409, details: null }),
      ),
      null,
    );
    assert.equal(frozenFieldText(new Error("network")), null);
    assert.deepEqual(envelopeFields(null), []);
    assert.deepEqual(envelopeFields({ fields: ["a", 3, "b"] }), ["a", "b"]);
  });

  test("describeEditError: frozen line > envelope message > network", () => {
    const frozen = describeEditError(
      new ApiError({
        code: "VALIDATION_ERROR",
        message: "server text",
        status: 400,
        details: { fields: ["allowed_file_types"] },
        requestId: "req-1",
      }),
    );
    assert.equal(frozen.summary, "以下字段在发布后已冻结，不可修改：允许的文件类型");
    assert.equal(frozen.requestId, "req-1");

    const generic = describeEditError(
      new ApiError({ code: "VALIDATION_ERROR", message: "任务标题不能为空", status: 400 }),
    );
    assert.equal(generic.summary, "任务标题不能为空");

    const network = describeEditError(new TypeError("offline"));
    assert.equal(network.summary, "网络异常，请检查连接后重试");
  });
});

describe("import row-error table cap (F4)", () => {
  test("caps at 50 with an accurate hidden count; data itself untouched", () => {
    const rows = Array.from({ length: 60 }, (_, index) => ({ n: index }));
    const { visible, hiddenCount } = capRowErrors(rows);
    assert.equal(visible.length, 50);
    assert.equal(visible[0].n, 0);
    assert.equal(hiddenCount, 10);
    assert.equal(rows.length, 60, "the in-memory list stays whole");
  });

  test("short lists pass through untouched", () => {
    const rows = [{ n: 1 }, { n: 2 }];
    const { visible, hiddenCount } = capRowErrors(rows);
    assert.equal(visible.length, 2);
    assert.equal(hiddenCount, 0);
  });
});

describe("taskStatusView (design §9: product wording, never raw enums)", () => {
  test("every status maps to zh-CN wording", () => {
    assert.deepEqual(taskStatusView("DRAFT"), { label: "草稿", tone: "muted" });
    assert.equal(taskStatusView("PUBLISHED").label, "已发布");
    assert.equal(taskStatusView("PAUSED").label, "已暂停");
    assert.equal(taskStatusView("CLOSED").label, "已关闭");
    assert.equal(taskStatusView("ARCHIVED").label, "已归档");
  });

  test("unknown statuses degrade, never throw", () => {
    assert.equal(taskStatusView("SOMETHING_NEW").label, "未知状态");
  });
});

describe("lifecycleActions (spec §6.2 table, mirrored for affordances)", () => {
  test("DRAFT offers only publish, behind an explicit confirm", () => {
    const actions = lifecycleActions("DRAFT");
    assert.equal(actions.length, 1);
    assert.equal(actions[0].verb, "publish");
    assert.equal(actions[0].confirmRequired, true);
    assert.match(actions[0].confirmBody, /立即对学生可见/);
    assert.match(actions[0].confirmBody, /冻结/);
  });

  test("PUBLISHED offers pause (direct) + close (danger confirm)", () => {
    const verbs = lifecycleActions("PUBLISHED").map((a) => a.verb);
    assert.deepEqual(verbs, ["pause", "close"]);
    const pause = lifecycleActions("PUBLISHED")[0];
    const close = lifecycleActions("PUBLISHED")[1];
    assert.equal(pause.confirmRequired, false);
    assert.equal(close.confirmRequired, true);
    assert.equal(close.buttonClass, "btn-danger");
    assert.match(close.confirmBody, /不可撤销/);
  });

  test("PAUSED offers resume + close; CLOSED offers archive only", () => {
    assert.deepEqual(
      lifecycleActions("PAUSED").map((a) => a.verb),
      ["resume", "close"],
    );
    const archive = lifecycleActions("CLOSED");
    assert.equal(archive.length, 1);
    assert.equal(archive[0].verb, "archive");
    assert.equal(archive[0].buttonClass, "btn-danger");
    assert.equal(archive[0].confirmRequired, true);
  });

  test("ARCHIVED is terminal: no actions render", () => {
    assert.deepEqual(lifecycleActions("ARCHIVED"), []);
  });

  test("button hierarchy (design §9): at most one primary; danger = destructive", () => {
    for (const status of ["DRAFT", "PUBLISHED", "PAUSED", "CLOSED"]) {
      const actions = lifecycleActions(status);
      const primaries = actions.filter(
        (action) => action.buttonClass === "btn-primary",
      );
      assert.ok(
        primaries.length <= 1,
        `${status} must not field competing primary buttons`,
      );
      for (const action of actions) {
        if (action.buttonClass === "btn-danger") {
          assert.equal(action.confirmRequired, true, `${status}/${action.verb}`);
        }
      }
    }
    // The one primary the workbench fields: publish from DRAFT.
    assert.equal(lifecycleActions("DRAFT")[0].buttonClass, "btn-primary");
  });
});

describe("create-form band mirrors (server authoritative)", () => {
  test("a ready FIXED form validates clean and maps to the wire body", () => {
    const result = taskFormToBody(readyForm(), NOW);
    assert.equal(result.ok, true);
    assert.deepEqual(result.errors, {});
    const body = result.body!;
    assert.equal(body.base_reward_points, 160);
    assert.equal(body.deadline_mode, "FIXED");
    assert.equal(body.max_file_size_bytes, 50 * 1024 * 1024);
    assert.equal(body.submission_schema, null);
    assert.equal(body.submission_schema_version, null);
    assert.equal(body.fixed_deadline_at, new Date("2026-10-30T18:00").toISOString());
    assert.equal(body.duration_minutes, null);
    assert.equal(body.claim_cutoff_minutes, 240);
    assert.deepEqual(body.notification_channels, ["SMS", "EMAIL", "IN_APP"]);
  });

  test("RELATIVE maps duration and nulls the fixed deadline", () => {
    const result = taskFormToBody(
      form({
        ...readyForm(),
        deadlineMode: "RELATIVE",
        durationMinutes: "90",
      }),
      NOW,
    );
    assert.equal(result.ok, true);
    assert.equal(result.body?.duration_minutes, 90);
    assert.equal(result.body?.fixed_deadline_at, null);
  });

  test("empty title / description / non-positive reward are caught", () => {
    const errors = validateTaskForm(form({ title: "  ", description: "", baseRewardPoints: "0" }), NOW);
    assert.equal(errors.title, "请填写任务标题");
    assert.equal(errors.description, "请填写任务描述");
    assert.equal(errors.baseRewardPoints, "基础奖励积分必须大于 0");
  });

  test("over-255 title is caught; non-numeric reward is caught", () => {
    const errors = validateTaskForm(
      form({ title: "长".repeat(256), baseRewardPoints: "abc" }),
      NOW,
    );
    assert.match(errors.title ?? "", /255/);
    assert.equal(errors.baseRewardPoints, "基础奖励积分必须大于 0");
  });

  test("FIXED requires a future deadline; RELATIVE requires positive duration", () => {
    const missing = validateTaskForm(form({ fixedDeadlineLocal: "" }), NOW);
    assert.equal(missing.deadline, "固定截止模式必须设置截止时间");
    const past = validateTaskForm(form({ fixedDeadlineLocal: "2020-01-01T10:00" }), NOW);
    assert.equal(past.deadline, "截止时间必须晚于当前时间");
    const relative = validateTaskForm(
      form({ deadlineMode: "RELATIVE", durationMinutes: "0" }),
      NOW,
    );
    assert.equal(relative.deadline, "领取后计时模式必须设置正的时长（分钟）");
  });

  test("cutoff band, file-type set, size cap, schema JSON bands", () => {
    const errors = validateTaskForm(
      form({
        claimCutoffMinutes: "-1",
        fileTypes: [],
        maxFileSizeMb: "300",
        submissionSchema: "{not json",
      }),
      NOW,
    );
    assert.match(errors.claimCutoffMinutes ?? "", /不小于 0/);
    assert.equal(errors.fileTypes, "请至少选择一种允许的文件类型");
    assert.match(errors.maxFileSizeMb ?? "", /200 MB/);
    assert.equal(errors.submissionSchema, "提交校验 schema 必须是非空 JSON 对象");
  });

  test("schema parser accepts non-empty objects, rejects empties/arrays/scalars", () => {
    assert.deepEqual(parseSubmissionSchema('{"columns":["a"]}'), { columns: ["a"] });
    assert.equal(parseSubmissionSchema("{}"), null);
    assert.equal(parseSubmissionSchema("[]"), null);
    assert.equal(parseSubmissionSchema('"x"'), null);
    assert.equal(parseSubmissionSchema("{bad"), null);
  });
});

describe("importPreviewView (spec §7.1 step 4)", () => {
  function preview(overrides: Partial<ImportPreviewDto> = {}): ImportPreviewDto {
    return {
      task_id: "t1",
      total_rows: 12,
      valid_count: 9,
      error_count: 3,
      errors: [
        {
          code: "EMPTY_KEYWORD",
          message: "keyword 不能为空",
          row_number: 3,
          platform: "weibo",
          keyword: "",
        },
        {
          code: "DUPLICATE_IN_FILE",
          message: "文件内重复的 platform + keyword 组合",
          row_number: 2,
          platform: "weibo",
          keyword: "图书馆",
        },
        {
          code: "INVALID_ENCODING",
          message: "文件编码必须是 UTF-8",
          row_number: null,
        },
      ],
      preview_token: "tok",
      expires_at: "2026-09-20T12:15:00Z",
      ...overrides,
    };
  }

  test("counts text mirrors the server counts exactly", () => {
    const view = importPreviewView(preview());
    assert.equal(view.countsText, "共 12 行：可导入 9 行，存在问题 3 行");
  });

  test("row errors separate from file errors and sort by row number", () => {
    const view = importPreviewView(preview());
    assert.equal(view.rowErrors.length, 2);
    assert.equal(view.fileErrors.length, 1);
    // Arrived 3-before-2; the view renders 2 first (sorted by row).
    assert.equal(view.rowErrors[0].where, "第 2 行");
    assert.equal(view.rowErrors[1].where, "第 3 行");
    assert.equal(view.fileErrors[0].where, "文件");
  });

  test("row errors carry the offending pair for the table columns", () => {
    const view = importPreviewView(preview());
    assert.equal(view.rowErrors[0].platform, "weibo");
    assert.equal(view.rowErrors[0].keyword, "图书馆");
  });

  test("confirm gating: valid rows + live token", () => {
    assert.equal(importPreviewView(preview()).canConfirm, true);

    const noValid = importPreviewView(preview({ valid_count: 0, preview_token: "tok" }));
    assert.equal(noValid.canConfirm, false);
    assert.equal(noValid.confirmHint, "没有可导入的行：请修正问题后重新上传。");

    const expired = importPreviewView(
      preview({ preview_token: null, expires_at: null }),
    );
    assert.equal(expired.canConfirm, false);
    assert.equal(expired.confirmHint, "预览已过期，请重新上传文件获取新的预览。");
  });
});

describe("review-queue copy", () => {
  test("claim/claim-lock wording never renders raw enums (design §9)", () => {
    assert.equal(reviewStatusView("PENDING_REVIEW").label, "待审核");
    assert.equal(reviewStatusView("REVISION_REQUIRED").label, "已退回");
    assert.equal(reviewStatusView("NOVEL_CODE").label, "待处理");
    assert.equal(rewardLockView("PROVISIONAL").label, "已锁定（待确认）");
    assert.equal(rewardLockView("INVALIDATED").label, "已作废");
  });

  test("locked tier line composes tier + points; nulls read 未锁定", () => {
    assert.equal(
      lockedTierText({ reward_tier_locked: 1, locked_reward_points: 160 }),
      "档位 T1 · 160 积分",
    );
    assert.equal(
      lockedTierText({ reward_tier_locked: null, locked_reward_points: null }),
      "未锁定",
    );
    assert.equal(versionText(2), "第 2 版");
  });

  test("the invalidate warning is prominent AND names the consequences", () => {
    assert.match(INVALIDATE_WARNING_TEXT, /取消/);
    assert.match(INVALIDATE_WARNING_TEXT, /奖励档位与积分/);
    assert.match(INVALIDATE_WARNING_TEXT, /审计信息不可删除/);
  });

  test("note/reason blank mirror: whitespace-only text is not ready", () => {
    assert.equal(reviewTextReady("  "), false);
    assert.equal(reviewTextReady("　"), false); // full-width space
    assert.equal(reviewTextReady(" x "), true);
  });

  test("report status wording maps the §23 lifecycle", () => {
    assert.deepEqual(reportStatusView("OPEN"), { label: "待处理", tone: "warning" });
    assert.equal(reportStatusView("HANDLED").label, "已处理");
    assert.equal(reportStatusView("DISMISSED").label, "已驳回");
  });
});

describe("moderationRowView (privacy choke point, spec §21.4/§40)", () => {
  const dto: ModerationCommentDto = {
    id: "c1",
    task_id: "t1",
    parent_id: null,
    content: "匿名评论内容",
    is_anonymous: true,
    author_display: "匿名用户",
    created_at: "2026-09-20T10:00:00Z",
    updated_at: "2026-09-20T10:00:00Z",
    edited: false,
    deleted: false,
    moderation_key: "mk-123",
    hard_hidden: false,
  };

  test("view shape is EXACTLY the display fields (no identity seam)", () => {
    const view = moderationRowView(dto, (iso) => Date.parse(iso));
    assert.deepEqual(Object.keys(view).sort(), [
      "authorDisplay",
      "content",
      "createdAtMs",
      "deleted",
      "edited",
      "hardHidden",
      "id",
      "isAnonymous",
      "moderationKey",
    ]);
    assert.equal(view.authorDisplay, "匿名用户");
    assert.equal(view.moderationKey, "mk-123");
    assert.equal(view.createdAtMs, Date.parse("2026-09-20T10:00:00Z"));
  });

  test("tombstones carry null content with the flags intact", () => {
    const view = moderationRowView(
      { ...dto, content: null, deleted: true },
      (iso) => Date.parse(iso),
    );
    assert.equal(view.content, null);
    assert.equal(view.deleted, true);
  });
});

describe("collaborator permission labels (spec §4.2)", () => {
  test("the closed set has the four frozen capabilities", () => {
    assert.deepEqual(
      COLLABORATOR_PERMISSIONS.map((option) => option.value),
      ["VIEW_TASK", "MANAGE_ASSIGNMENTS", "REVIEW_SUBMISSIONS", "MODERATE_COMMUNITY"],
    );
  });

  test("labels resolve; unknown codes degrade to the raw code", () => {
    assert.deepEqual(permissionLabels(["VIEW_TASK", "REVIEW_SUBMISSIONS"]), [
      "查看任务",
      "审核提交",
    ]);
    assert.deepEqual(permissionLabels(["FUTURE_CODE"]), ["FUTURE_CODE"]);
  });
});
