/**
 * Pure views for the Admin workspace (Plan 09 Task 10; design-system §8
 * Admin; patterns §3/§6/§14 — the teacherView precedent).
 *
 * Everything here is a pure function of SERVER data — the unit-testable
 * core the admin components render. Client mirrors of server rules are
 * CONVENIENCE ONLY: the backend's `require_admin_actor` guard and its
 * typed envelopes (VALIDATION_ERROR 4xx / CONFLICT 409) stay the single
 * authority; the affordances below just keep obvious mistakes from
 * leaving the browser.
 *
 * The 409/422 contract the brief pins: `CONFLICT` (registered Plan 08
 * T9) means concurrent/state conflict — another admin changed the row,
 * a digest replay failed, a transition is illegal from the observed
 * state; `VALIDATION_ERROR` means the payload itself is wrong. The two
 * take different copy through `describeAdminMutationError`, driven by
 * `error.code` only (never the Chinese message).
 */
import { isApiError } from "@/lib/errors";

import type {
  AdminUserDto,
  RewardCatalogueDto,
  RewardCreateBody,
  RewardUpdateBody,
  WhitelistCountsDto,
  WhitelistPreviewDto,
} from "./adminApi";
import type { StaffTone } from "./teacherView";

// --- mutation-error mapping (the CONFLICT-vs-validation contract) ------------------------

export interface AdminMutationErrorView {
  message: string;
  requestId: string | null;
  /** True for the typed 409 `CONFLICT` — concurrent/state conflict copy. */
  conflict: boolean;
}

export const ADMIN_NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";

/**
 * The shared mutation-failure view for every admin dialog: 409 CONFLICT
 * gets the "state changed under you — refresh and retry" line, 4xx
 * validation shows the backend's own message, transport failures show
 * the connectivity line. The request id rides every server refusal.
 */
export function describeAdminMutationError(
  error: unknown,
  fallback: string,
): AdminMutationErrorView {
  if (!isApiError(error)) {
    return { message: ADMIN_NETWORK_ERROR_TEXT, requestId: null, conflict: false };
  }
  if (error.code === "CONFLICT") {
    return {
      message: `${error.message || fallback}（与当前状态冲突：可能已被其他管理员变更或状态已流转，请刷新后重试）`,
      requestId: error.requestId,
      conflict: true,
    };
  }
  return {
    message: error.message || fallback,
    requestId: error.requestId,
    conflict: false,
  };
}

/**
 * The colliding student numbers a whitelist-import 409 carries in
 * `details.student_numbers` (the backend's deterministic sorted list) —
 * empty for any other failure shape.
 */
export function conflictStudentNumbers(error: unknown): string[] {
  if (!isApiError(error) || error.code !== "CONFLICT") {
    return [];
  }
  const details = error.details;
  if (typeof details !== "object" || details === null) {
    return [];
  }
  const numbers = (details as { student_numbers?: unknown }).student_numbers;
  if (!Array.isArray(numbers)) {
    return [];
  }
  return numbers.filter((value): value is string => typeof value === "string");
}

// --- account directory (spec §5.7) ---------------------------------------------------------

export interface UserStatusView {
  label: string;
  tone: StaffTone;
}

const USER_STATUS_VIEWS: Record<string, UserStatusView> = {
  PENDING_PHONE: { label: "待验证", tone: "muted" },
  ACTIVE: { label: "正常", tone: "success" },
  SUSPENDED: { label: "已停用", tone: "warning" },
  BANNED: { label: "已封禁", tone: "danger" },
};

/** Status wording + tone; unknown values (drift) degrade, never throw. */
export function userStatusView(status: string): UserStatusView {
  return USER_STATUS_VIEWS[status] ?? { label: "未知状态", tone: "muted" };
}

const ROLE_LABELS: Record<string, string> = {
  STUDENT: "学生",
  TEACHER: "教师",
  ADMIN: "管理员",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** The three admin verbs (spec §5.7 transition table, mirrored for affordances). */
export type AccountActionKind = "suspend" | "ban" | "reactivate";

export interface AccountActionView {
  kind: AccountActionKind;
  label: string;
  buttonClass: "btn-secondary" | "btn-danger";
  title: string;
  /** The dialog's consequence copy (design §9: consequences, not jargon). */
  body: (user: AdminUserDto) => string;
  confirmLabel: string;
}

const ACCOUNT_ACTIONS: Record<AccountActionKind, AccountActionView> = {
  suspend: {
    kind: "suspend",
    label: "停用",
    buttonClass: "btn-secondary",
    title: "停用账号",
    body: (user) =>
      `停用后 ${user.nickname}（${user.username}）将无法登录和使用平台功能，可随时重新启用。停用原因必填并记入审计日志。`,
    confirmLabel: "确认停用",
  },
  ban: {
    kind: "ban",
    label: "封禁",
    buttonClass: "btn-danger",
    title: "封禁账号",
    body: (user) =>
      `封禁后 ${user.nickname}（${user.username}）将无法登录；解封需要管理员手动恢复。封禁原因必填并记入审计日志。`,
    confirmLabel: "确认封禁",
  },
  reactivate: {
    kind: "reactivate",
    label: "恢复",
    buttonClass: "btn-secondary",
    title: "恢复账号",
    body: (user) =>
      `恢复后 ${user.nickname}（${user.username}）将回到正常状态并可重新登录（停用与封禁均可恢复）。恢复原因必填并记入审计日志。`,
    confirmLabel: "确认恢复",
  },
};

/**
 * The verbs offered from one status: ACTIVE offers suspend/ban,
 * SUSPENDED/BANNED offer reactivate; PENDING_PHONE has no admin
 * transition in §5.7 (the backend refuses it as the typed 409 either
 * way — this mirror only keeps the dead button off the page).
 */
export function accountActions(status: string): AccountActionView[] {
  switch (status) {
    case "ACTIVE":
      return [ACCOUNT_ACTIONS.suspend, ACCOUNT_ACTIONS.ban];
    case "SUSPENDED":
    case "BANNED":
      return [ACCOUNT_ACTIONS.reactivate];
    default:
      return [];
  }
}

export function accountActionView(kind: AccountActionKind): AccountActionView {
  return ACCOUNT_ACTIONS[kind];
}

/** Blank-after-trim mirror of the transport rule (reason mandatory at §5.7). */
export function adminReasonReady(reason: string): boolean {
  return reason.trim().length > 0;
}

// --- whitelist import preview ------------------------------------------------------------

export interface WhitelistCodeView {
  label: string;
  tone: StaffTone;
}

const WHITELIST_CODE_VIEWS: Record<string, WhitelistCodeView> = {
  IMPORTABLE: { label: "可导入", tone: "success" },
  DUPLICATE_IN_FILE: { label: "文件内重复", tone: "warning" },
  DUPLICATE_IN_DB: { label: "已在白名单", tone: "muted" },
  FULL_WIDTH_DIGITS: { label: "全角数字", tone: "warning" },
  INVALID_CHARACTERS: { label: "含非法字符", tone: "danger" },
  INVALID_LENGTH: { label: "长度不合法", tone: "warning" },
};

/** Row-code wording (the backend `WhitelistRowCode` vocabulary, T9's renderer). */
export function whitelistCodeView(code: string): WhitelistCodeView {
  return WHITELIST_CODE_VIEWS[code] ?? { label: code, tone: "muted" };
}

/** "共 12 行：可导入 9 行，文件内重复 1 行…" — non-zero problem classes only. */
export function whitelistCountsText(counts: WhitelistCountsDto): string {
  const parts = [`可导入 ${counts.importable} 行`];
  const problems: [string, number][] = [
    ["文件内重复", counts.duplicate_in_file],
    ["已在白名单", counts.duplicate_in_db],
    ["全角数字", counts.full_width_digits],
    ["非法字符", counts.invalid_characters],
    ["长度不合法", counts.invalid_length],
  ];
  for (const [label, value] of problems) {
    if (value > 0) {
      parts.push(`${label} ${value} 行`);
    }
  }
  return `共 ${counts.total_rows} 行：${parts.join("，")}`;
}

/** Confirm requires a non-empty previewed importable set bound by its digest. */
export function canConfirmWhitelistImport(preview: WhitelistPreviewDto): boolean {
  return preview.importable.length > 0 && preview.confirm_token.trim().length > 0;
}

// --- redemption queue (spec §16.1/§16.2) ----------------------------------------------------

export interface RedemptionStatusView {
  label: string;
  tone: StaffTone;
}

const REDEMPTION_STATUS_VIEWS: Record<string, RedemptionStatusView> = {
  REQUESTED: { label: "待审核", tone: "info" },
  UNDER_REVIEW: { label: "审核中", tone: "info" },
  APPROVED: { label: "已批准 · 待发放", tone: "warning" },
  FULFILLED: { label: "已发放", tone: "success" },
  REJECTED: { label: "已拒绝", tone: "danger" },
};

export function redemptionStatusView(status: string): RedemptionStatusView {
  return REDEMPTION_STATUS_VIEWS[status] ?? { label: "待处理", tone: "info" };
}

/** The decision verbs offered from one row status (the queue lists the pending set). */
export type RedemptionActionKind = "approve" | "reject" | "fulfill";

export function redemptionActions(status: string): RedemptionActionKind[] {
  switch (status) {
    case "REQUESTED":
    case "UNDER_REVIEW":
      return ["approve", "reject"];
    case "APPROVED":
      return ["fulfill"];
    default:
      return [];
  }
}

// --- system settings (typed keys; storage forms from backend system/service.py) -------------

export const SETTING_KEY_CURRENT_ACADEMIC_TERM = "CURRENT_ACADEMIC_TERM";
export const SETTING_KEY_EMOJI_WHITELIST = "EMOJI_WHITELIST";
export const SETTING_KEY_ABANDON_DAILY_LIMIT = "ABANDON_DAILY_LIMIT";
export const SETTING_KEY_MANAGEMENT_NETWORK_ENABLED =
  "MANAGEMENT_NETWORK_ENABLED";
export const SETTING_KEY_MANAGEMENT_NETWORK_CIDRS = "MANAGEMENT_NETWORK_CIDRS";

export interface SettingKeyView {
  key: string;
  title: string;
  hint: string;
}

/** The five registered keys, registry order (the GET /admin/settings order). */
export const SETTING_KEY_VIEWS: readonly SettingKeyView[] = [
  {
    key: SETTING_KEY_CURRENT_ACADEMIC_TERM,
    title: "当前学期",
    hint: "兑换记录创建时会快照该学期；已创建的兑换保持原学期不变。",
  },
  {
    key: SETTING_KEY_EMOJI_WHITELIST,
    title: "表情白名单",
    hint: "每行一个表情（1-8 个码点）；清空表示禁用全部表情。",
  },
  {
    key: SETTING_KEY_ABANDON_DAILY_LIMIT,
    title: "每日放弃上限",
    hint: "每个自然日最多可放弃的领取数；0 表示禁止放弃。",
  },
  {
    key: SETTING_KEY_MANAGEMENT_NETWORK_ENABLED,
    title: "管理网络限制",
    hint: "开启后，管理端操作只允许来自下方 CIDR 列表内的地址。",
  },
  {
    key: SETTING_KEY_MANAGEMENT_NETWORK_CIDRS,
    title: "管理网络 CIDR 列表",
    hint: "每行一个 CIDR（如 10.0.0.0/8）；限制开启时列表不能为空。",
  },
];

/** "未设置" copy for a no-row value (the deployment seed decides — G7). */
export const SETTING_UNSET_TEXT = "未设置（使用部署默认值）";

/**
 * EMOJI_WHITELIST stored form -> the emoji list. Stored values are the
 * backend's canonical JSON array; anything else is registry drift and
 * degrades to an empty list (the edit form is still usable — the PUT
 * replaces the value wholesale).
 */
export function parseEmojiSettingValue(value: string | null): string[] {
  if (value === null) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter((item): item is string => typeof item === "string");
  } catch {
    return [];
  }
}

/** ABANDON_DAILY_LIMIT stored form (decimal text) -> the number, else null. */
export function parseAbandonLimitValue(value: string | null): number | null {
  if (value === null) {
    return null;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? null : parsed;
}

/** MANAGEMENT_NETWORK_ENABLED stored form ("true"/"false") -> the flag, else null. */
export function parseNetworkEnabledValue(value: string | null): boolean | null {
  if (value === "true") {
    return true;
  }
  if (value === "false") {
    return false;
  }
  return null;
}

/** MANAGEMENT_NETWORK_CIDRS stored form (comma-separated canonical) -> the list. */
export function parseCidrsSettingValue(value: string | null): string[] {
  if (value === null) {
    return [];
  }
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
}

/** One setting's human value line for the read-only card. */
export function settingValueText(key: string, value: string | null): string {
  if (value === null) {
    return SETTING_UNSET_TEXT;
  }
  switch (key) {
    case SETTING_KEY_EMOJI_WHITELIST: {
      const emojis = parseEmojiSettingValue(value);
      return emojis.length > 0 ? emojis.join(" ") : "（空列表：全部表情禁用）";
    }
    case SETTING_KEY_MANAGEMENT_NETWORK_ENABLED:
      return parseNetworkEnabledValue(value) === true ? "已开启" : "已关闭";
    case SETTING_KEY_MANAGEMENT_NETWORK_CIDRS: {
      const cidrs = parseCidrsSettingValue(value);
      return cidrs.length > 0 ? cidrs.join("，") : "（空列表）";
    }
    default:
      return value;
  }
}

/** Version line for the read-only card ("当前版本 v3 · 更新于 9月20日 14:05" 的数据部分). */
export function settingVersionText(
  version: number | null,
  updatedAt: string | null,
  formatInstant: (iso: string) => string,
): string {
  const versionPart = version === null ? "尚未写入" : `版本 v${version}`;
  if (updatedAt === null) {
    return versionPart;
  }
  return `${versionPart} · 更新于 ${formatInstant(updatedAt)}`;
}

/** The old -> new diff line the settings confirm dialog shows before a PUT. */
export function settingDiffText(
  currentValue: string | null,
  newValue: string,
): string {
  const from =
    currentValue === null ? SETTING_UNSET_TEXT : `“${currentValue}”`;
  return `${from} → “${newValue}”`;
}

// --- notification templates -----------------------------------------------------------------

export const TEMPLATE_EVENT_TYPE_OPTIONS: readonly {
  value: string;
  label: string;
}[] = [
  { value: "ASSIGNMENT_DEADLINE_24H", label: "任务截止前 24 小时" },
  { value: "ASSIGNMENT_DEADLINE_4H", label: "任务截止前 4 小时" },
  { value: "REVISION_REQUIRED", label: "提交被退回修改" },
  { value: "SUBMISSION_APPROVED", label: "提交审核通过" },
  { value: "SUBMISSION_VALIDATION_FAILED", label: "提交机器校验失败" },
  { value: "REWARD_REDEMPTION_APPROVED", label: "兑换已批准" },
  { value: "REWARD_REDEMPTION_REJECTED", label: "兑换被拒绝" },
  { value: "ACCOUNT_SECURITY", label: "账号安全" },
];

export const TEMPLATE_CHANNEL_OPTIONS: readonly {
  value: string;
  label: string;
}[] = [
  { value: "SMS", label: "短信" },
  { value: "EMAIL", label: "邮件" },
  { value: "IN_APP", label: "站内" },
];

// --- reward catalogue form ------------------------------------------------------------------

export interface RewardFormValues {
  name: string;
  pointCost: string;
  description: string;
  stock: string;
  perUserTermLimit: string;
  /** `datetime-local` values; interpreted in the browser zone (patterns §14). */
  availableFromLocal: string;
  availableUntilLocal: string;
  requiresManualReview: boolean;
  fulfillmentInstructions: string;
  reason: string;
}

export const EMPTY_REWARD_FORM: RewardFormValues = {
  name: "",
  pointCost: "",
  description: "",
  stock: "",
  perUserTermLimit: "",
  availableFromLocal: "",
  availableUntilLocal: "",
  requiresManualReview: false,
  fulfillmentInstructions: "",
  reason: "",
};

/** Field set shared by the catalogue row and the admin verdict row (structural). */
export type RewardEditSource = Pick<
  RewardCatalogueDto,
  | "id"
  | "name"
  | "point_cost"
  | "description"
  | "stock"
  | "per_user_term_limit"
  | "available_from"
  | "available_until"
>;

/** Prefill from a catalogue or verdict row (instructions/review-flag stay create-only). */
export function rewardFormFromCatalogue(
  row: RewardEditSource,
  toLocalValue: (ms: number) => string,
): RewardFormValues {
  return {
    ...EMPTY_REWARD_FORM,
    name: row.name,
    pointCost: String(row.point_cost),
    description: row.description ?? "",
    stock: row.stock === null ? "" : String(row.stock),
    perUserTermLimit:
      row.per_user_term_limit === null ? "" : String(row.per_user_term_limit),
    availableFromLocal:
      row.available_from === null ? "" : toLocalValue(Date.parse(row.available_from)),
    availableUntilLocal:
      row.available_until === null ? "" : toLocalValue(Date.parse(row.available_until)),
  };
}

export type RewardFormField =
  | "name"
  | "pointCost"
  | "stock"
  | "perUserTermLimit"
  | "window";

export type RewardFormErrors = Partial<Record<RewardFormField, string>>;

function parseIntOrNaN(value: string): number {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) {
    return Number.NaN;
  }
  return Number.parseInt(trimmed, 10);
}

/** Light client band checks (server VALIDATION_ERROR stays the verdict). */
export function validateRewardForm(values: RewardFormValues): RewardFormErrors {
  const errors: RewardFormErrors = {};
  if (values.name.trim().length === 0) {
    errors.name = "请填写奖励名称";
  }
  const cost = parseIntOrNaN(values.pointCost);
  if (Number.isNaN(cost) || cost <= 0) {
    errors.pointCost = "兑换积分必须大于 0";
  }
  if (values.stock.trim().length > 0 && Number.isNaN(parseIntOrNaN(values.stock))) {
    errors.stock = "库存必须是不小于 0 的整数";
  }
  if (
    values.perUserTermLimit.trim().length > 0 &&
    Number.isNaN(parseIntOrNaN(values.perUserTermLimit))
  ) {
    errors.perUserTermLimit = "学期限购必须是不小于 0 的整数";
  }
  const from = values.availableFromLocal.trim();
  const until = values.availableUntilLocal.trim();
  if (from.length > 0 && Number.isNaN(new Date(from).getTime())) {
    errors.window = "可用时间起点格式不正确";
  } else if (until.length > 0 && Number.isNaN(new Date(until).getTime())) {
    errors.window = "可用时间终点格式不正确";
  } else if (
    from.length > 0 &&
    until.length > 0 &&
    new Date(from).getTime() >= new Date(until).getTime()
  ) {
    errors.window = "可用时间起点必须早于终点";
  }
  return errors;
}

/**
 * The create body: every managed field present (create has no diff
 * semantics — it writes the whole row). datetimes go out as ISO
 * instants (timezone-aware, the backend's G14 transport rule).
 */
export function rewardFormToCreateBody(
  values: RewardFormValues,
): { ok: boolean; errors: RewardFormErrors; body?: RewardCreateBody } {
  const errors = validateRewardForm(values);
  // The reason is dialog-mandatory (the audited-create copy); callers
  // gate the submit button on the same blank-after-trim mirror.
  if (Object.keys(errors).length > 0 || !adminReasonReady(values.reason)) {
    return { ok: false, errors };
  }
  return {
    ok: true,
    errors: {},
    body: {
      name: values.name.trim(),
      point_cost: parseIntOrNaN(values.pointCost),
      description: values.description.trim().length > 0 ? values.description.trim() : null,
      stock: values.stock.trim().length > 0 ? parseIntOrNaN(values.stock) : null,
      per_user_term_limit:
        values.perUserTermLimit.trim().length > 0
          ? parseIntOrNaN(values.perUserTermLimit)
          : null,
      available_from:
        values.availableFromLocal.trim().length > 0
          ? new Date(values.availableFromLocal).toISOString()
          : null,
      available_until:
        values.availableUntilLocal.trim().length > 0
          ? new Date(values.availableUntilLocal).toISOString()
          : null,
      requires_manual_review: values.requiresManualReview,
      fulfillment_instructions:
        values.fulfillmentInstructions.trim().length > 0
          ? values.fulfillmentInstructions.trim()
          : null,
      reason: values.reason.trim(),
    },
  };
}

/**
 * The DIFF-ONLY update body (the backend's presence semantics: absent =
 * unchanged, explicit null clears a nullable bound). An empty input for
 * a bound the row HELD means "clear it"; for a bound the row never had,
 * empty stays absent. name/point_cost always ride (non-nullable).
 * fulfillment_instructions / requires_manual_review are deliberately
 * NOT manageable on edit: the student-listing read cannot prefill them,
 * and sending a blind value would clobber the stored one.
 */
export function rewardFormToUpdateBody(
  values: RewardFormValues,
  row: RewardEditSource,
): { ok: boolean; errors: RewardFormErrors; body?: RewardUpdateBody } {
  const errors = validateRewardForm(values);
  if (Object.keys(errors).length > 0) {
    return { ok: false, errors };
  }
  const body: RewardUpdateBody = {};
  if (values.name.trim() !== row.name) {
    body.name = values.name.trim();
  }
  const cost = parseIntOrNaN(values.pointCost);
  if (cost !== row.point_cost) {
    body.point_cost = cost;
  }
  const description = values.description.trim();
  if (description !== (row.description ?? "")) {
    body.description = description.length > 0 ? description : null;
  }
  if (values.stock.trim().length === 0) {
    if (row.stock !== null) {
      body.stock = null;
    }
  } else {
    const stock = parseIntOrNaN(values.stock);
    if (stock !== row.stock) {
      body.stock = stock;
    }
  }
  if (values.perUserTermLimit.trim().length === 0) {
    if (row.per_user_term_limit !== null) {
      body.per_user_term_limit = null;
    }
  } else {
    const limit = parseIntOrNaN(values.perUserTermLimit);
    if (limit !== row.per_user_term_limit) {
      body.per_user_term_limit = limit;
    }
  }
  const fromMs =
    values.availableFromLocal.trim().length > 0
      ? new Date(values.availableFromLocal).getTime()
      : null;
  if (fromMs === null) {
    if (row.available_from !== null) {
      body.available_from = null;
    }
  } else if (row.available_from === null || fromMs !== Date.parse(row.available_from)) {
    body.available_from = new Date(fromMs).toISOString();
  }
  const untilMs =
    values.availableUntilLocal.trim().length > 0
      ? new Date(values.availableUntilLocal).getTime()
      : null;
  if (untilMs === null) {
    if (row.available_until !== null) {
      body.available_until = null;
    }
  } else if (row.available_until === null || untilMs !== Date.parse(row.available_until)) {
    body.available_until = new Date(untilMs).toISOString();
  }
  if (!adminReasonReady(values.reason)) {
    return { ok: false, errors };
  }
  body.reason = values.reason.trim();
  return { ok: true, errors: {}, body };
}

// --- misc copy ------------------------------------------------------------------------------

/** Window facts line for a catalogue row ("9月1日 08:00 – 9月30日 22:00 / 长期有效"). */
export function rewardWindowText(
  row: { available_from: string | null; available_until: string | null },
  formatInstant: (iso: string) => string,
): string {
  const from = row.available_from === null ? null : formatInstant(row.available_from);
  const until = row.available_until === null ? null : formatInstant(row.available_until);
  if (from === null && until === null) {
    return "长期有效";
  }
  if (until === null) {
    return `${from} 起长期有效`;
  }
  if (from === null) {
    return `截至 ${until}`;
  }
  return `${from} – ${until}`;
}
