/**
 * Pure views for the Teacher workbench (spec §41; design-system §8
 * Teacher / §9 badges; patterns §3/§6/§14).
 *
 * Everything here is a pure function of SERVER data (plus an explicit
 * `nowMs` where time matters) — the unit-testable core the workbench
 * components render. The client mirrors of server rules are CONVENIENCE
 * ONLY (patterns §6): the create-form band checks catch obvious mistakes
 * before a request leaves; every business decision stays with the
 * backend's VALIDATION_ERROR / TASK_NOT_CLAIMABLE envelopes.
 *
 * PRIVACY PIN (binding constraint, spec §40/§21.4): `moderationRowView`
 * is the single choke point every rendered moderation row goes through —
 * it copies EXACTLY the display fields and cannot carry a student
 * number, phone, email, or login identifier even if a future DTO grew
 * one (the Object.keys shape is pinned by unit test).
 */
import { formatFileSize } from "@/features/submissions/api";
import type { AuthErrorView } from "@/features/auth/errors";
import { isApiError } from "@/lib/errors";

import type {
  ImportPreviewDto,
  ImportPreviewErrorDto,
  ModerationCommentDto,
  TaskCreateBody,
  TaskFileTypeKey,
  TaskLifecycleVerb,
  TaskUpdateBody,
  TeacherTaskDto,
} from "./teacherApi";

// --- task status (design §9: product wording, color supplemental) ---------------------

export type StaffTone = "info" | "success" | "warning" | "muted" | "danger";

export interface TaskStatusView {
  label: string;
  tone: StaffTone;
}

const TASK_STATUS_VIEWS: Record<string, TaskStatusView> = {
  DRAFT: { label: "草稿", tone: "muted" },
  PUBLISHED: { label: "已发布", tone: "success" },
  PAUSED: { label: "已暂停", tone: "warning" },
  CLOSED: { label: "已关闭", tone: "muted" },
  ARCHIVED: { label: "已归档", tone: "muted" },
};

/** Status wording + tone; unknown values (drift) degrade, never throw. */
export function taskStatusView(status: string): TaskStatusView {
  return TASK_STATUS_VIEWS[status] ?? { label: "未知状态", tone: "muted" };
}

// --- lifecycle model (spec §6.2 transition table, mirrored for the UI) ----------------

export interface LifecycleActionView {
  verb: TaskLifecycleVerb;
  /** Button copy (设计 §9: consequences, not jargon). */
  label: string;
  /** `btn-*` class — one primary max per row (design §9 Buttons). */
  buttonClass: "btn-primary" | "btn-secondary" | "btn-danger";
  /** Destructive/irreversible or otherwise consequential: explicit confirm first. */
  confirmRequired: boolean;
  confirmTitle: string;
  /** What the verb does to the task — the dialog's consequence copy. */
  confirmBody: string;
  confirmLabel: string;
}

const PUBLISH_ACTION: LifecycleActionView = {
  verb: "publish",
  label: "发布",
  buttonClass: "btn-primary",
  confirmRequired: true,
  confirmTitle: "发布任务",
  confirmBody:
    "发布后任务立即对学生可见并可领取；奖励积分、截止模式、文件策略等契约字段将从发布一刻起冻结，之后只能暂停或关闭。发布前请确认已导入任务单元并通过服务端校验。",
  confirmLabel: "确认发布",
};

const CLOSE_ACTION: LifecycleActionView = {
  verb: "close",
  label: "关闭任务",
  buttonClass: "btn-danger",
  confirmRequired: true,
  confirmTitle: "关闭任务",
  confirmBody:
    "关闭后学生不能再领取或提交，该操作不可撤销；进行中的领取与提交以服务端截止规则结算。仅在确认不再接收工作时关闭。",
  confirmLabel: "确认关闭",
};

const ARCHIVE_ACTION: LifecycleActionView = {
  verb: "archive",
  label: "归档",
  buttonClass: "btn-danger",
  confirmRequired: true,
  confirmTitle: "归档任务",
  confirmBody:
    "归档后任务退出工作台主列表，历史数据保留但不可再变更。该操作不可撤销。",
  confirmLabel: "确认归档",
};

const PAUSE_ACTION: LifecycleActionView = {
  verb: "pause",
  label: "暂停领取",
  buttonClass: "btn-secondary",
  confirmRequired: false,
  confirmTitle: "",
  confirmBody: "",
  confirmLabel: "",
};

const RESUME_ACTION: LifecycleActionView = {
  verb: "resume",
  label: "恢复领取",
  buttonClass: "btn-secondary",
  confirmRequired: false,
  confirmTitle: "",
  confirmBody: "",
  confirmLabel: "",
};

/**
 * The verbs available from one status (the backend's transition table,
 * mirrored for affordances only — an illegal POST answers the typed
 * VALIDATION_ERROR envelope either way).
 */
export function lifecycleActions(status: string): LifecycleActionView[] {
  switch (status) {
    case "DRAFT":
      return [PUBLISH_ACTION];
    case "PUBLISHED":
      return [PAUSE_ACTION, CLOSE_ACTION];
    case "PAUSED":
      return [RESUME_ACTION, CLOSE_ACTION];
    case "CLOSED":
      return [ARCHIVE_ACTION];
    default:
      return [];
  }
}

// --- create-task form (client convenience mirror; server authoritative) ---------------

export const TASK_RARITY_OPTIONS: readonly {
  value: string;
  label: string;
}[] = [
  { value: "NORMAL", label: "普通" },
  { value: "RARE", label: "稀有" },
  { value: "EPIC", label: "史诗" },
  { value: "LEGENDARY", label: "传说" },
];

export const DEADLINE_MODE_OPTIONS: readonly {
  value: "FIXED" | "RELATIVE";
  label: string;
}[] = [
  { value: "FIXED", label: "固定截止时间" },
  { value: "RELATIVE", label: "领取后计时" },
];

export type DeadlineModeKey = "FIXED" | "RELATIVE";

/** The publish-validation fields the dialog collects (binding constraint). */
export interface TaskFormValues {
  title: string;
  description: string;
  baseRewardPoints: string;
  deadlineMode: DeadlineModeKey;
  /** `datetime-local` value; interpreted in the browser zone (patterns §14). */
  fixedDeadlineLocal: string;
  durationMinutes: string;
  claimCutoffMinutes: string;
  rarity: string;
  fileTypes: TaskFileTypeKey[];
  maxFileSizeMb: string;
  /** Raw JSON textarea; draft-legal to leave empty (publish requires it). */
  submissionSchema: string;
  submissionSchemaVersion: string;
}

export const EMPTY_TASK_FORM: TaskFormValues = {
  title: "",
  description: "",
  baseRewardPoints: "",
  deadlineMode: "FIXED",
  fixedDeadlineLocal: "",
  durationMinutes: "",
  claimCutoffMinutes: "240",
  rarity: "NORMAL",
  fileTypes: ["CSV"],
  maxFileSizeMb: "50",
  submissionSchema: "",
  submissionSchemaVersion: "",
};

export type TaskFormField =
  | "title"
  | "description"
  | "baseRewardPoints"
  | "deadline"
  | "claimCutoffMinutes"
  | "fileTypes"
  | "maxFileSizeMb"
  | "submissionSchema";

export type TaskFormErrors = Partial<Record<TaskFormField, string>>;

function parseIntOrNaN(value: string): number {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) {
    return Number.NaN;
  }
  return Number.parseInt(trimmed, 10);
}

/**
 * Light client mirror of the create/publish band rules (backend
 * `TaskService`): obvious mistakes never leave the browser; everything
 * else is the server's VALIDATION_ERROR to explain. Returns `{}` when
 * the form is create-ready.
 */
export function validateTaskForm(
  values: TaskFormValues,
  nowMs: number,
): TaskFormErrors {
  const errors: TaskFormErrors = {};

  if (values.title.trim().length === 0) {
    errors.title = "请填写任务标题";
  } else if (values.title.trim().length > 255) {
    errors.title = "标题不能超过 255 个字符";
  }

  if (values.description.trim().length === 0) {
    errors.description = "请填写任务描述";
  }

  const reward = parseIntOrNaN(values.baseRewardPoints);
  if (Number.isNaN(reward) || reward <= 0) {
    errors.baseRewardPoints = "基础奖励积分必须大于 0";
  }

  if (values.deadlineMode === "FIXED") {
    if (values.fixedDeadlineLocal.trim().length === 0) {
      errors.deadline = "固定截止模式必须设置截止时间";
    } else {
      const deadlineMs = new Date(values.fixedDeadlineLocal).getTime();
      if (Number.isNaN(deadlineMs)) {
        errors.deadline = "截止时间格式不正确";
      } else if (deadlineMs <= nowMs) {
        errors.deadline = "截止时间必须晚于当前时间";
      }
    }
  } else {
    const duration = parseIntOrNaN(values.durationMinutes);
    if (Number.isNaN(duration) || duration <= 0) {
      errors.deadline = "领取后计时模式必须设置正的时长（分钟）";
    }
  }

  const cutoff = parseIntOrNaN(values.claimCutoffMinutes);
  if (Number.isNaN(cutoff) || cutoff < 0) {
    errors.claimCutoffMinutes = "领取截止须为不小于 0 的分钟数";
  }

  if (values.fileTypes.length === 0) {
    errors.fileTypes = "请至少选择一种允许的文件类型";
  }

  const sizeMb = Number.parseFloat(values.maxFileSizeMb);
  if (!Number.isFinite(sizeMb) || sizeMb <= 0 || sizeMb > 200) {
    errors.maxFileSizeMb = "单文件上限须大于 0 且不超过 200 MB";
  }

  if (values.submissionSchema.trim().length > 0) {
    const parsed = parseSubmissionSchema(values.submissionSchema);
    if (parsed === null) {
      errors.submissionSchema = "提交校验 schema 必须是非空 JSON 对象";
    }
  }

  return errors;
}

/** Parse the schema textarea into a non-empty JSON object, or null. */
export function parseSubmissionSchema(
  raw: string,
): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed) &&
      Object.keys(parsed).length > 0
    ) {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

export interface TaskFormToBodyResult {
  ok: boolean;
  body?: TaskCreateBody;
  errors: TaskFormErrors;
}

/** Validate + build the create body in one step (validation is reused verbatim). */
export function taskFormToBody(
  values: TaskFormValues,
  nowMs: number,
): TaskFormToBodyResult {
  const errors = validateTaskForm(values, nowMs);
  if (Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  const schema =
    values.submissionSchema.trim().length > 0
      ? parseSubmissionSchema(values.submissionSchema)
      : null;
  const versionText = values.submissionSchemaVersion.trim();
  return {
    ok: true,
    errors: {},
    body: {
      title: values.title.trim(),
      description: values.description.trim(),
      base_reward_points: parseIntOrNaN(values.baseRewardPoints),
      deadline_mode: values.deadlineMode,
      allowed_file_types: [...values.fileTypes],
      max_file_size_bytes: Math.round(
        Number.parseFloat(values.maxFileSizeMb) * 1024 * 1024,
      ),
      task_type: "DATA_CRAWL",
      rarity: values.rarity,
      fixed_deadline_at:
        values.deadlineMode === "FIXED"
          ? new Date(values.fixedDeadlineLocal).toISOString()
          : null,
      duration_minutes:
        values.deadlineMode === "RELATIVE"
          ? parseIntOrNaN(values.durationMinutes)
          : null,
      claim_cutoff_minutes: parseIntOrNaN(values.claimCutoffMinutes),
      submission_schema: schema,
      submission_schema_version:
        versionText.length > 0 ? parseIntOrNaN(versionText) : null,
      notify_24h: true,
      notify_4h: true,
      notification_channels: ["SMS", "EMAIL", "IN_APP"],
    },
  };
}

// --- task edit (spec §6.2 V1 edit rule; the create form reused) ------------------------

/** Form keys the create/edit bands and the edit rule key off. */
export type TaskFormFieldKey =
  | "title"
  | "description"
  | "baseRewardPoints"
  | "deadlineMode"
  | "fixedDeadlineLocal"
  | "durationMinutes"
  | "claimCutoffMinutes"
  | "rarity"
  | "fileTypes"
  | "maxFileSizeMb"
  | "submissionSchema"
  | "submissionSchemaVersion";

/**
 * The frozen set in form terms: the backend's `_CONTRACT_FIELDS` plus
 * rarity (which the update transport cannot carry at all). An
 * affordance only — the server's `ImmutableTaskFieldError` envelope
 * stays the verdict.
 */
export const POST_PUBLISH_FROZEN_FIELDS: ReadonlySet<TaskFormFieldKey> =
  new Set([
    "baseRewardPoints",
    "deadlineMode",
    "fixedDeadlineLocal",
    "durationMinutes",
    "claimCutoffMinutes",
    "submissionSchema",
    "submissionSchemaVersion",
    "fileTypes",
    "maxFileSizeMb",
    "rarity",
  ]);

/**
 * Whether the edit surface renders at all: DRAFT edits everything,
 * PUBLISHED/PAUSED edit presentation fields, CLOSED/ARCHIVED accept
 * nothing (a fieldless edit is a backend no-op — the UI offers no edit).
 */
export function taskEditable(status: string): boolean {
  return status === "DRAFT" || status === "PUBLISHED" || status === "PAUSED";
}

/**
 * Epoch ms -> a `datetime-local` input value in the BROWSER's zone.
 *
 * Symmetry is the contract: `new Date(value)` (how every consumer of a
 * datetime-local string parses it, including this module's own band
 * checks and body builders) interprets the string as LOCAL wall time,
 * so seeding the input must ALSO render local wall time — slicing the
 * UTC ISO string instead shifts every round-trip by the zone offset and
 * manufactures a phantom deadline diff (patterns §14).
 */
export function toDatetimeLocalValue(ms: number): string {
  const date = new Date(ms);
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** Initial form values from the task detail (ISO -> datetime-local). */
export function taskFormFromTask(task: TeacherTaskDto): TaskFormValues {
  return {
    title: task.title,
    description: task.description,
    baseRewardPoints: String(task.base_reward_points),
    deadlineMode: task.deadline_mode === "RELATIVE" ? "RELATIVE" : "FIXED",
    fixedDeadlineLocal:
      task.fixed_deadline_at !== null
        ? toDatetimeLocalValue(Date.parse(task.fixed_deadline_at))
        : "",
    durationMinutes:
      task.duration_minutes !== null ? String(task.duration_minutes) : "",
    claimCutoffMinutes: String(task.claim_cutoff_minutes),
    rarity: task.rarity,
    fileTypes: [...task.allowed_file_types] as TaskFileTypeKey[],
    maxFileSizeMb: String(Math.round(task.max_file_size_bytes / (1024 * 1024))),
    submissionSchema:
      task.submission_schema !== null
        ? JSON.stringify(task.submission_schema)
        : "",
    submissionSchemaVersion:
      task.submission_schema_version !== null
        ? String(task.submission_schema_version)
        : "",
  };
}

export interface TaskFormToUpdateResult {
  ok: boolean;
  /** Diff-only PATCH body; `{}` when nothing changed. */
  body?: TaskUpdateBody;
  errors: TaskFormErrors;
}

function sameSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  const a = [...left].sort();
  const b = [...right].sort();
  return a.every((value, index) => value === b[index]);
}

/**
 * Build the DIFF-ONLY update body (spec §6.2). This is not politeness —
 * it is the contract: the backend counts any non-null provided contract
 * field as PROVIDED, so an unchanged value sent on a PUBLISHED task is
 * still an `ImmutableTaskFieldError`. Contract fields therefore enter
 * the body only when the task is DRAFT and the value actually changed;
 * presentation fields ride whenever they differ.
 *
 * Validation mirrors the server's provided-only scope: a band error on
 * an UNCHANGED field does not block the save (a DRAFT may stay
 * incomplete); a changed field must pass its band; a schema text that
 * changed but does not parse is refused client-side.
 */
export function taskFormToUpdateBody(
  values: TaskFormValues,
  task: TeacherTaskDto,
  nowMs: number,
): TaskFormToUpdateResult {
  const allErrors = validateTaskForm(values, nowMs);
  const draft = task.status === "DRAFT";
  const body: TaskUpdateBody = {};

  if (values.title.trim() !== task.title) {
    body.title = values.title.trim();
  }
  if (values.description.trim() !== task.description) {
    body.description = values.description.trim();
  }

  if (draft) {
    const reward = parseIntOrNaN(values.baseRewardPoints);
    if (!Number.isNaN(reward) && reward !== task.base_reward_points) {
      body.base_reward_points = reward;
    }
    if (values.deadlineMode !== task.deadline_mode) {
      body.deadline_mode = values.deadlineMode;
    }
    if (values.deadlineMode === "FIXED" && values.fixedDeadlineLocal !== "") {
      const deadlineMs = new Date(values.fixedDeadlineLocal).getTime();
      if (
        !Number.isNaN(deadlineMs) &&
        (task.fixed_deadline_at === null ||
          deadlineMs !== Date.parse(task.fixed_deadline_at))
      ) {
        body.fixed_deadline_at = new Date(
          values.fixedDeadlineLocal,
        ).toISOString();
      }
    }
    const duration = parseIntOrNaN(values.durationMinutes);
    if (
      values.deadlineMode === "RELATIVE" &&
      !Number.isNaN(duration) &&
      duration !== task.duration_minutes
    ) {
      body.duration_minutes = duration;
    }
    const cutoff = parseIntOrNaN(values.claimCutoffMinutes);
    if (!Number.isNaN(cutoff) && cutoff !== task.claim_cutoff_minutes) {
      body.claim_cutoff_minutes = cutoff;
    }
    if (!sameSet(values.fileTypes, task.allowed_file_types)) {
      body.allowed_file_types = [...values.fileTypes];
    }
    const sizeBytes = Math.round(
      Number.parseFloat(values.maxFileSizeMb) * 1024 * 1024,
    );
    if (Number.isFinite(sizeBytes) && sizeBytes !== task.max_file_size_bytes) {
      body.max_file_size_bytes = sizeBytes;
    }
    if (values.submissionSchema.trim() !== "") {
      const schema = parseSubmissionSchema(values.submissionSchema);
      if (
        schema !== null &&
        JSON.stringify(schema) !== JSON.stringify(task.submission_schema ?? null)
      ) {
        body.submission_schema = schema;
      }
    }
    const versionText = values.submissionSchemaVersion.trim();
    if (versionText !== "") {
      const version = parseIntOrNaN(versionText);
      if (
        !Number.isNaN(version) &&
        version !== task.submission_schema_version
      ) {
        body.submission_schema_version = version;
      }
    }
  }

  // Validate PROVIDED fields only (the server's scope): map each body
  // field to its band error; an unparseable schema text never reaches
  // the body and is flagged when the text actually changed.
  const errors: TaskFormErrors = {};
  const pick = (formKey: keyof TaskFormErrors, provided: boolean) => {
    if (provided && allErrors[formKey] !== undefined) {
      errors[formKey] = allErrors[formKey];
    }
  };
  pick("title", body.title !== undefined);
  pick("description", body.description !== undefined);
  pick("baseRewardPoints", body.base_reward_points !== undefined);
  pick(
    "deadline",
    body.deadline_mode !== undefined ||
      body.fixed_deadline_at !== undefined ||
      body.duration_minutes !== undefined,
  );
  pick("claimCutoffMinutes", body.claim_cutoff_minutes !== undefined);
  pick("fileTypes", body.allowed_file_types !== undefined);
  pick("maxFileSizeMb", body.max_file_size_bytes !== undefined);
  const schemaChanged =
    values.submissionSchema.trim() !==
    (task.submission_schema !== null
      ? JSON.stringify(task.submission_schema)
      : "");
  pick(
    "submissionSchema",
    body.submission_schema !== undefined || schemaChanged,
  );

  return {
    ok: Object.keys(errors).length === 0,
    body,
    errors,
  };
}

// --- frozen-field envelope rendering (ImmutableTaskFieldError) -------------------------

const FROZEN_FIELD_LABELS: Record<string, string> = {
  base_reward_points: "基础奖励积分",
  deadline_mode: "截止模式",
  fixed_deadline_at: "固定截止时间",
  duration_minutes: "提交时限",
  claim_cutoff_minutes: "领取截止",
  submission_schema: "提交校验 schema",
  submission_schema_version: "schema 版本",
  allowed_file_types: "允许的文件类型",
  max_file_size_bytes: "单文件上限",
};

/** Wire field names an ImmutableTaskFieldError envelope carries, if any. */
export function envelopeFields(details: unknown): string[] {
  if (typeof details !== "object" || details === null) {
    return [];
  }
  const fields = (details as { fields?: unknown }).fields;
  if (!Array.isArray(fields)) {
    return [];
  }
  return fields.filter((value): value is string => typeof value === "string");
}

/**
 * The frozen-field line for an edit rejection: the backend's
 * `ImmutableTaskFieldError` renders VALIDATION_ERROR with
 * `details.fields` — branch on THAT (never the message text) and name
 * the frozen fields in product wording. Null for any other failure.
 */
export function frozenFieldText(error: unknown): string | null {
  if (!isApiError(error) || error.code !== "VALIDATION_ERROR") {
    return null;
  }
  const fields = envelopeFields(error.details);
  if (fields.length === 0) {
    return null;
  }
  return `以下字段在发布后已冻结，不可修改：${fields
    .map((field) => FROZEN_FIELD_LABELS[field] ?? field)
    .join("、")}`;
}

/**
 * Shared task-mutation error view (create + edit): network line, else
 * the envelope message with the request id. The frozen-field special
 * case belongs to the edit surface (`describeEditError`).
 */
export function describeTaskMutationError(
  error: unknown,
  fallback: string,
): AuthErrorView {
  if (!isApiError(error)) {
    return { summary: "网络异常，请检查连接后重试", fieldErrors: {}, requestId: null };
  }
  return {
    summary: error.message || fallback,
    fieldErrors: {},
    requestId: error.requestId,
  };
}

/** Edit-mutation error view: frozen-field line > envelope message > network. */
export function describeEditError(error: unknown): AuthErrorView {
  const frozen = frozenFieldText(error);
  if (frozen !== null) {
    const requestId =
      isApiError(error) && error.requestId !== null ? error.requestId : null;
    return { summary: frozen, fieldErrors: {}, requestId };
  }
  return describeTaskMutationError(error, "保存失败，请稍后重试");
}

// --- assignment import preview (spec §7.1 steps 1-6) ----------------------------------

export interface ImportErrorRowView {
  key: string;
  /** "第 3 行" for row errors; "文件" for file-level errors. */
  where: string;
  /** The server's Chinese message, verbatim display (never parsed). */
  message: string;
  /** The offending pair when the row carried one (display only). */
  platform: string | null;
  keyword: string | null;
}

export interface ImportPreviewView {
  /** File-level errors (row_number === null): encoding, header, size, ... */
  fileErrors: ImportErrorRowView[];
  /** Row-level errors sorted by row number. */
  rowErrors: ImportErrorRowView[];
  /** "共 12 行：可导入 9 行，存在问题 3 行" (design §14 copy). */
  countsText: string;
  /** Confirm requires at least one importable row AND a live token. */
  canConfirm: boolean;
  /** Why confirm is unavailable, when it is. */
  confirmHint: string | null;
}

function errorWhere(error: ImportPreviewErrorDto): string {
  return error.row_number === null || error.row_number === undefined
    ? "文件"
    : `第 ${error.row_number} 行`;
}

/**
 * Preview presentation (spec §7.1 step 4): the valid rows themselves are
 * NOT echoed by the backend — the counts are the contract, the errors
 * are per-row, and the token names the exact rows confirm will insert.
 */
export function importPreviewView(preview: ImportPreviewDto): ImportPreviewView {
  const fileErrors: ImportErrorRowView[] = [];
  const rowErrors: ImportErrorRowView[] = [];
  preview.errors.forEach((error, index) => {
    const row: ImportErrorRowView = {
      key: `${error.code}-${error.row_number ?? "file"}-${index}`,
      where: errorWhere(error),
      message: error.message,
      platform: error.platform ?? null,
      keyword: error.keyword ?? null,
    };
    if (error.row_number === null || error.row_number === undefined) {
      fileErrors.push(row);
    } else {
      rowErrors.push(row);
    }
  });
  rowErrors.sort((a, b) => parseRowNumber(a.where) - parseRowNumber(b.where));

  const hasToken = preview.preview_token !== null;
  const canConfirm = preview.valid_count > 0 && hasToken;
  let confirmHint: string | null = null;
  if (!hasToken && preview.valid_count > 0) {
    confirmHint = "预览已过期，请重新上传文件获取新的预览。";
  } else if (preview.valid_count === 0) {
    confirmHint = "没有可导入的行：请修正问题后重新上传。";
  }

  return {
    fileErrors,
    rowErrors,
    countsText: `共 ${preview.total_rows} 行：可导入 ${preview.valid_count} 行，存在问题 ${preview.error_count} 行`,
    canConfirm,
    confirmHint,
  };
}

function parseRowNumber(where: string): number {
  const match = where.match(/^第 (\d+) 行$/);
  return match === null ? Number.MAX_SAFE_INTEGER : Number.parseInt(match[1], 10);
}

/** Render cap for the import row-error table (display only; data stays whole). */
export const IMPORT_ERROR_TABLE_CAP = 50;

/**
 * Cap the error table at `cap` rows, reporting how many are hidden — a
 * 5000-row import with thousands of errors must not freeze the page on
 * table layout. The FULL list stays in memory; the counts line already
 * carries the server totals.
 */
export function capRowErrors<T>(
  rows: readonly T[],
  cap: number = IMPORT_ERROR_TABLE_CAP,
): { visible: T[]; hiddenCount: number } {
  return {
    visible: rows.slice(0, cap),
    hiddenCount: Math.max(rows.length - cap, 0),
  };
}

// --- review queue (spec §12.4 UI list, §11.3 decisions) --------------------------------

export interface RewardLockView {
  label: string;
  tone: StaffTone;
}

const REWARD_LOCK_VIEWS: Record<string, RewardLockView> = {
  NONE: { label: "未锁定", tone: "muted" },
  PROVISIONAL: { label: "已锁定（待确认）", tone: "info" },
  CONFIRMED: { label: "已确认", tone: "success" },
  INVALIDATED: { label: "已作废", tone: "danger" },
};

export function rewardLockView(status: string): RewardLockView {
  return REWARD_LOCK_VIEWS[status] ?? { label: "未知", tone: "muted" };
}

export interface ReviewStatusView {
  label: string;
  tone: StaffTone;
}

const REVIEW_STATUS_VIEWS: Record<string, ReviewStatusView> = {
  PENDING_REVIEW: { label: "待审核", tone: "info" },
  UNDER_REVIEW: { label: "审核中", tone: "info" },
  APPROVED: { label: "已通过", tone: "success" },
  REVISION_REQUIRED: { label: "已退回", tone: "warning" },
};

export function reviewStatusView(status: string): ReviewStatusView {
  return REVIEW_STATUS_VIEWS[status] ?? { label: "待处理", tone: "info" };
}

/** The locked-tier line for a queue row / detail ("档位 T1 · 160 积分"). */
export function lockedTierText(item: {
  reward_tier_locked: number | null;
  locked_reward_points: number | null;
}): string {
  const tier = item.reward_tier_locked;
  const points = item.locked_reward_points;
  if (tier === null && points === null) {
    return "未锁定";
  }
  const parts: string[] = [];
  if (tier !== null) {
    parts.push(`档位 T${tier}`);
  }
  if (points !== null) {
    parts.push(`${points} 积分`);
  }
  return parts.join(" · ");
}

/** Version copy ("第 2 版") — versions are separate queue rows on one claim. */
export function versionText(version: number): string {
  return `第 ${version} 版`;
}

/** File facts line for the detail head (filename · type · size). */
export function fileFactsText(item: {
  original_filename: string;
  declared_type: string;
  detected_type: string | null;
  file_size: number;
}): string {
  const type = item.detected_type ?? item.declared_type;
  return `${item.original_filename} · ${type} · ${formatFileSize(item.file_size)}`;
}

/**
 * The prominent warning on the invalidate action (spec §11.3 判无效): the
 * PROVISIONAL lock is cancelled — the student must resubmit and the
 * reward is re-settled. Rendered BEFORE the reason field so the consequence
 * is read first (design §9 Forms: help text explains consequences).
 */
export const INVALIDATE_WARNING_TEXT =
  "判无效将取消该学生当前锁定的奖励档位与积分，之后需要重新提交并重新核算；已记录的审计信息不可删除。请确认提交内容确属无效后再操作。";

/** Blank-after-trim mirror of the transport rule (the note/reason is mandatory). */
export function reviewTextReady(text: string): boolean {
  return text.trim().length > 0;
}

// --- moderation rows (spec §21.4; the privacy choke point) ------------------------------

export interface ModerationRowView {
  id: string;
  /** author_display verbatim (nickname | 匿名用户) — NO identity derivation. */
  authorDisplay: string;
  isAnonymous: boolean;
  /** null exactly on tombstones; components render the deleted marker. */
  content: string | null;
  deleted: boolean;
  edited: boolean;
  hardHidden: boolean;
  /** Pseudonymous key shown only on anonymous rows (§21.4 追溯线索). */
  moderationKey: string | null;
  /** Epoch ms (created_at parsed once, here). */
  createdAtMs: number;
}

/**
 * The display row for one moderation comment. This function is the single
 * choke point every rendered moderation row goes through (the
 * communityView precedent): it copies EXACTLY the display fields — a
 * student number, phone, email, or login identifier has no field to land
 * in, even if the DTO grew one. Shape pinned by unit test (Object.keys).
 */
export function moderationRowView(
  comment: ModerationCommentDto,
  parseInstant: (iso: string) => number,
): ModerationRowView {
  return {
    id: comment.id,
    authorDisplay: comment.author_display,
    isAnonymous: comment.is_anonymous,
    content: comment.content,
    deleted: comment.deleted,
    edited: comment.edited,
    hardHidden: comment.hard_hidden,
    moderationKey: comment.moderation_key,
    createdAtMs: parseInstant(comment.created_at),
  };
}

/** Report-status wording for the report queue (§23 lifecycle). */
export function reportStatusView(status: string): { label: string; tone: StaffTone } {
  switch (status) {
    case "OPEN":
      return { label: "待处理", tone: "warning" };
    case "HANDLED":
      return { label: "已处理", tone: "success" };
    case "DISMISSED":
      return { label: "已驳回", tone: "muted" };
    default:
      return { label: "待处理", tone: "warning" };
  }
}

// --- collaborator permissions (spec §4.2; closed set, canonical labels) ----------------

export const COLLABORATOR_PERMISSIONS: readonly {
  value: string;
  label: string;
  hint: string;
}[] = [
  {
    value: "VIEW_TASK",
    label: "查看任务",
    hint: "查看任务详情与统计",
  },
  {
    value: "MANAGE_ASSIGNMENTS",
    label: "管理任务单元",
    hint: "导入与管理 Assignment",
  },
  {
    value: "REVIEW_SUBMISSIONS",
    label: "审核提交",
    hint: "处理审核队列中的提交",
  },
  {
    value: "MODERATE_COMMUNITY",
    label: "社区管理",
    hint: "处理评论与举报",
  },
];

/** Canonical permission labels for a granted set (unknown codes degrade gracefully). */
export function permissionLabels(permissions: readonly string[]): string[] {
  const known = new Map(
    COLLABORATOR_PERMISSIONS.map((option) => [option.value, option.label]),
  );
  return permissions.map((code) => known.get(code) ?? code);
}
