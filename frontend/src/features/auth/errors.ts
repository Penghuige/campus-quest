/**
 * Auth-surface error presentation (spec §29 envelope; patterns §6, §15).
 *
 * Branching rule: every branch keys on `error.code` — the Chinese
 * `error.message` from the backend is DISPLAY fallback text and is never
 * parsed. Three tiers:
 *
 * 1. `VALIDATION_ERROR` — the envelope may carry field errors in `details`.
 *    The backend emits two shapes and both are honored:
 *    - service rejections: `details = { field: "student_number", reason }`
 *      (identity/service.py, profile_service.py);
 *    - the framework's 422 handler: `details = { field: [messages] }`
 *      (core/errors.py `_validation_field_errors`).
 *    A mapped field gets OUR stable zh-CN text (the rules mirror the backend
 *    band-for-band); `reason`/`msg` strings are English developer text and
 *    are deliberately not shown.
 * 2. auth-mapped business codes — stable zh-CN copy below.
 * 3. everything else — a known business code outside this surface falls back
 *    to the backend's own Chinese message; system codes and UNKNOWN codes
 *    (registry drift) degrade to a safe generic line plus the request id.
 */
import { isKnownErrorCode, isSystemErrorCode, type ApiError } from "../../lib/errors";

/** Form field keys an auth form can render errors for. */
export type AuthFieldName =
  | "username"
  | "student_number"
  | "nickname"
  | "phone"
  | "code"
  | "password"
  // Account-settings fields (T5): the same envelope shapes serve the
  // profile forms, with their own field names from profile_service.
  | "new_phone"
  | "email"
  | "token"
  | "current_password"
  | "new_password";

/** Presentation-ready view of one failed auth mutation. */
export interface AuthErrorView {
  /** Line for the error summary region; `null` when field errors suffice. */
  summary: string | null;
  /** Field-keyed error texts (envelope field names; forms render subsets). */
  fieldErrors: Partial<Record<AuthFieldName, string>>;
  /** Request id to surface for unexpected/system failures (§10 Error). */
  requestId: string | null;
}

/** Stable zh-CN copy per auth-relevant envelope code. */
export const AUTH_ERROR_TEXT: Partial<Record<string, string>> = {
  AUTHENTICATION_REQUIRED: "学号或密码不正确",
  ACCOUNT_NOT_ACTIVE: "该账号当前无法登录，请联系管理员",
  STUDENT_NOT_WHITELISTED: "该学号不在注册白名单中，无法注册",
  USERNAME_ALREADY_EXISTS: "该学号已注册，请直接登录",
  PHONE_ALREADY_BOUND: "该手机号已绑定其他账号",
  OTP_CODE_INVALID: "验证码不正确",
  OTP_TOO_MANY_ATTEMPTS: "验证码尝试次数过多，请重新获取",
  OTP_CHALLENGE_EXPIRED: "验证码已过期，请重新获取",
  OTP_CHALLENGE_CONSUMED: "验证码已使用，请重新获取",
  OTP_CHALLENGE_INVALID: "验证码已失效，请重新获取",
  OTP_TOKEN_INVALID: "手机验证已失效，请重新获取验证码",
  OTP_RESEND_COOLDOWN: "验证码发送过于频繁，请稍后再试",
  // Account-settings codes (T5): email binding + the re-auth failures
  // the profile endpoints answer (spec §5.5/§5.6).
  EMAIL_ALREADY_BOUND: "该邮箱已绑定其他账号",
  INVALID_EMAIL_TOKEN: "邮箱验证已失效，请重新发送验证邮件",
  RATE_LIMITED: "操作过于频繁，请稍后再试",
};

const SYSTEM_ERROR_TEXT = "服务暂时不可用，请稍后重试";
const GENERIC_ERROR_TEXT = "操作失败，请稍后重试";
const VALIDATION_SUMMARY_TEXT = "请检查表单填写后重试";

/**
 * Stable zh-CN text for a `VALIDATION_ERROR` detail field. Only fields whose
 * rules the client mirrors get text here; anything else falls to the summary.
 */
const VALIDATION_FIELD_TEXT: Partial<Record<AuthFieldName, string>> = {
  student_number: `学号需为 6-20 位数字`,
  username: `学号需为 6-20 位数字`,
  nickname: `昵称最长 16 个字符`,
  password: `密码长度需为 10-128 个字符`,
  phone: "请输入正确的手机号",
  code: `验证码为 6 位数字`,
  // Account-settings fields (T5).
  new_phone: "请输入正确的新手机号",
  email: "请输入正确的邮箱地址",
  token: "请输入邮件中的验证码",
  new_password: `密码长度需为 10-128 个字符`,
};

/**
 * Which form field carries each business code's message when the form wants
 * placement instead of a summary (e.g. whitelist rejection belongs next to
 * the student-number input, §9 Forms: "errors appear close to the field").
 */
export const FIELD_FOR_CODE: Partial<Record<string, AuthFieldName>> = {
  STUDENT_NOT_WHITELISTED: "student_number",
  USERNAME_ALREADY_EXISTS: "student_number",
  PHONE_ALREADY_BOUND: "phone",
  OTP_CODE_INVALID: "code",
  OTP_TOO_MANY_ATTEMPTS: "code",
  OTP_CHALLENGE_EXPIRED: "code",
  OTP_CHALLENGE_CONSUMED: "code",
  OTP_CHALLENGE_INVALID: "code",
  OTP_TOKEN_INVALID: "code",
};

/** Fields whose challenge is dead once the code is rejected (§33.2). */
export const OTP_CHALLENGE_RESET_CODES: ReadonlySet<string> = new Set([
  "OTP_TOO_MANY_ATTEMPTS",
  "OTP_CHALLENGE_EXPIRED",
  "OTP_CHALLENGE_CONSUMED",
  "OTP_CHALLENGE_INVALID",
  "OTP_TOKEN_INVALID",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Map a VALIDATION_ERROR `details` payload onto field-keyed error texts. */
export function extractValidationFieldErrors(
  details: unknown,
): Partial<Record<AuthFieldName, string>> {
  if (!isRecord(details)) {
    return {};
  }
  const mapped: Partial<Record<AuthFieldName, string>> = {};

  // Shape 1 — service rejection: { field: "nickname", reason: "..." }.
  if (typeof details.field === "string") {
    const field = details.field as AuthFieldName;
    const text = VALIDATION_FIELD_TEXT[field];
    if (text !== undefined) {
      mapped[field] = text;
    }
    return mapped;
  }

  // Shape 2 — framework 422: { field: [msg, ...], ... }.
  for (const [key, value] of Object.entries(details)) {
    const field = key as AuthFieldName;
    const text = VALIDATION_FIELD_TEXT[field];
    if (text !== undefined && Array.isArray(value) && value.length > 0) {
      mapped[field] = text;
    }
  }
  return mapped;
}

/**
 * Derive the presentation view of a failed auth mutation. Pure: same envelope
 * in, same view out, message text never parsed.
 */
export function describeAuthError(error: ApiError): AuthErrorView {
  if (error.code === "VALIDATION_ERROR") {
    const fieldErrors = extractValidationFieldErrors(error.details);
    return {
      summary:
        Object.keys(fieldErrors).length > 0
          ? null
          : (error.message || VALIDATION_SUMMARY_TEXT),
      fieldErrors,
      requestId: null,
    };
  }

  const mapped = AUTH_ERROR_TEXT[error.code];
  if (mapped !== undefined) {
    return { summary: mapped, fieldErrors: {}, requestId: null };
  }

  if (isSystemErrorCode(error.code)) {
    return { summary: SYSTEM_ERROR_TEXT, fieldErrors: {}, requestId: error.requestId };
  }

  if (isKnownErrorCode(error.code)) {
    // Known business code from another surface: the backend's own Chinese UX
    // copy is the sanctioned fallback display text (never parsed).
    return {
      summary: error.message || GENERIC_ERROR_TEXT,
      fieldErrors: {},
      requestId: null,
    };
  }

  // Unknown code — registry drift between backend and frontend mirrors.
  return { summary: GENERIC_ERROR_TEXT, fieldErrors: {}, requestId: error.requestId };
}

/**
 * Default resend cooldown when the envelope does not say: mirrors backend
 * `Settings.otp_resend_cooldown_seconds` (identity/otp.py policy).
 */
export const DEFAULT_RESEND_COOLDOWN_SECONDS = 60;

const MAX_COOLDOWN_SECONDS = 900;

function clampSeconds(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) {
    return DEFAULT_RESEND_COOLDOWN_SECONDS;
  }
  return Math.min(Math.ceil(raw), MAX_COOLDOWN_SECONDS);
}

/**
 * Seconds to display on the resend countdown after an OTP-send failure.
 *
 * The current backend `ResendCooldownError` envelope carries NO retry hint
 * (`error_envelope(code, message, None, request_id)`), so the default mirrors
 * the backend cooldown knob. If the envelope ever grows a
 * `details.retry_after` (seconds), it is honored — numeric, clamped to
 * 1-900 — without any message parsing. Non-cooldown errors return 0 (start
 * nothing).
 */
export function cooldownSecondsFrom(
  error: ApiError,
  fallbackSeconds: number = DEFAULT_RESEND_COOLDOWN_SECONDS,
): number {
  if (error.code !== "OTP_RESEND_COOLDOWN") {
    return 0;
  }
  const details = error.details;
  if (!isRecord(details)) {
    return fallbackSeconds;
  }
  const raw = details.retry_after;
  if (typeof raw === "number") {
    return clampSeconds(raw);
  }
  if (typeof raw === "string" && raw.trim() !== "" && Number.isFinite(Number(raw))) {
    return clampSeconds(Number(raw));
  }
  return fallbackSeconds;
}

/**
 * Label for the OTP request/resend button driven by the countdown state —
 * the display half of "server cooldown envelope -> countdown". The remaining
 * seconds are always visible text, never color-only state.
 */
export function resendButtonLabel(
  remainingSeconds: number,
  idleLabel: string,
  requesting: boolean,
): string {
  if (requesting) {
    return "发送中…";
  }
  if (remainingSeconds > 0) {
    return `重新发送（${remainingSeconds} 秒）`;
  }
  return idleLabel;
}

/** Network/transport failure text for non-`ApiError` rejections. */
export const NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";

/**
 * Presentation helper for forms that render BOTH a summary region and field
 * errors: when the view carries only field errors, add the generic summary
 * line so the failure is announced (role="alert") and not just painted next
 * to the inputs (patterns §18).
 */
export function withFieldErrorSummary(view: AuthErrorView): AuthErrorView {
  if (view.summary === null && Object.keys(view.fieldErrors).length > 0) {
    return { ...view, summary: VALIDATION_SUMMARY_TEXT };
  }
  return view;
}
