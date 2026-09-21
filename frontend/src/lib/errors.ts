/**
 * Error-code registry and the typed `ApiError` for the CampusQuest API.
 *
 * The string unions below MIRROR the frozen registry in
 * `docs/architecture/interfaces.md` ("Error-Code Registry", spec §29):
 * business codes first, then the system/framework codes. Membership there is
 * frozen by `backend/tests/unit/core/test_error_codes.py`; a new code must be
 * added to that document before it can appear here.
 *
 * Branching rule (spec §29): consumers switch on `error.code`, NEVER by
 * parsing the human-readable (Chinese) `message`. The message is display
 * material only.
 */

/** Business error codes (interfaces.md, business table). */
export const BUSINESS_ERROR_CODES = [
  "VALIDATION_ERROR",
  "AUTHENTICATION_REQUIRED",
  "PERMISSION_DENIED",
  "ACCOUNT_NOT_ACTIVE",
  "STUDENT_NOT_WHITELISTED",
  "PHONE_ALREADY_BOUND",
  "USERNAME_ALREADY_EXISTS",
  "TASK_NOT_CLAIMABLE",
  "NO_ASSIGNMENT_AVAILABLE",
  "ASSIGNMENT_LIMIT_REACHED",
  "TASK_ACTIVE_CLAIM_EXISTS",
  "CLAIM_CUTOFF_REACHED",
  "ABANDON_LIMIT_REACHED",
  "CLAIM_NOT_ABANDONABLE",
  "CLAIM_NOT_SUBMITTABLE",
  "SUBMISSION_WINDOW_CLOSED",
  "FILE_TOO_LARGE",
  "FILE_TYPE_NOT_ALLOWED",
  "SUBMISSION_VALIDATION_FAILED",
  "ALREADY_REVIEWED",
  "INSUFFICIENT_POINTS",
  "REWARD_OUT_OF_STOCK",
  "REDEMPTION_LIMIT_REACHED",
  "RATING_NOT_ELIGIBLE",
  "TOTP_SETUP_REQUIRED",
  "EMAIL_ALREADY_BOUND",
  "INVALID_EMAIL_TOKEN",
  "OTP_CODE_INVALID",
  "OTP_TOO_MANY_ATTEMPTS",
  "OTP_CHALLENGE_EXPIRED",
  "OTP_CHALLENGE_CONSUMED",
  "OTP_CHALLENGE_INVALID",
  "OTP_TOKEN_INVALID",
  "OTP_RESEND_COOLDOWN",
  "RATE_LIMITED",
] as const;

export type BusinessErrorCode = (typeof BUSINESS_ERROR_CODES)[number];

/**
 * System / framework codes (interfaces.md, system table). The backend
 * reuses the same envelope shape for these, but they signal transport or
 * framework failure — the frontend must treat them as infrastructure
 * errors, never as domain branches.
 */
export const SYSTEM_ERROR_CODES = [
  "INTERNAL_ERROR",
  "NOT_FOUND",
  "METHOD_NOT_ALLOWED",
  "HTTP_ERROR",
] as const;

export type SystemErrorCode = (typeof SYSTEM_ERROR_CODES)[number];

/** Every code the frozen registry defines. */
export type ErrorCode = BusinessErrorCode | SystemErrorCode;

const KNOWN_ERROR_CODES: ReadonlySet<string> = new Set<string>([
  ...BUSINESS_ERROR_CODES,
  ...SYSTEM_ERROR_CODES,
]);

/** True when `code` is part of the frozen registry above. */
export function isKnownErrorCode(code: string): boolean {
  return KNOWN_ERROR_CODES.has(code);
}

/** True for the framework/system codes (transport errors, not domain). */
export function isSystemErrorCode(code: string): code is SystemErrorCode {
  return (SYSTEM_ERROR_CODES as readonly string[]).includes(code);
}

/** Shape of the spec §29 error envelope body: `{"error": {...}}`. */
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: unknown;
    request_id?: string | null;
  };
}

function isEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const inner = (value as { error: unknown }).error;
  return (
    typeof inner === "object" &&
    inner !== null &&
    "code" in inner &&
    typeof (inner as { code: unknown }).code === "string" &&
    "message" in inner &&
    typeof (inner as { message: unknown }).message === "string"
  );
}

/**
 * Typed error thrown for every non-2xx CampusQuest API response.
 *
 * - `code` — branch on this (see registry above).
 * - `message` — server-provided human text; display only, never parsed.
 * - `details` — opaque envelope payload (e.g. `{ limit: 3 }`).
 * - `requestId` — from the envelope's `request_id`, falling back to the
 *   response `X-Request-ID` header; shown to users and logged on
 *   unexpected failures.
 * - `status` — HTTP status code.
 *
 * Unknown codes (registry drift between backend and this mirror) keep the
 * raw server string at runtime; `isKnownErrorCode` distinguishes them.
 */
export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly status: number;
  readonly details: unknown;
  readonly requestId: string | null;

  constructor(options: {
    code: string;
    message: string;
    status: number;
    details?: unknown;
    requestId?: string | null;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.code = options.code as ErrorCode;
    this.status = options.status;
    this.details = options.details;
    this.requestId = options.requestId ?? null;
  }
}

/** Narrows `unknown` failures caught around API calls. */
export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/**
 * Build the `ApiError` for a failed response.
 *
 * When the body is a valid §29 envelope, every field is preserved
 * (code/message/details/request_id). Anything else (proxy HTML, empty body,
 * malformed JSON) degrades to the system `HTTP_ERROR` code with a safe
 * message — never raw response text — so callers always get an `ApiError`
 * they can branch on.
 */
export function toApiError(
  status: number,
  body: unknown,
  requestIdHeader: string | null,
): ApiError {
  if (isEnvelope(body)) {
    const envelope = body.error;
    return new ApiError({
      code: envelope.code,
      message: envelope.message,
      status,
      details: envelope.details,
      requestId: envelope.request_id ?? requestIdHeader,
    });
  }
  return new ApiError({
    code: "HTTP_ERROR",
    message: `请求失败（HTTP ${status}）`,
    status,
    requestId: requestIdHeader,
  });
}

/*
 * Section-level error presentation for read surfaces (patterns §10
 * Error, §15): human-readable line + retry affordance, request id on
 * unexpected server failures.
 *
 * Tiering mirrors the auth surface's rules but stays transport-generic so
 * any feature section (dashboard, task list, task detail) renders the
 * same shape:
 * - network/transport failure -> connectivity line;
 * - system codes and UNKNOWN codes (registry drift) -> safe generic +
 *   request id;
 * - a known business code the section does not specifically handle -> the
 *   backend's own Chinese message as fallback display text (never
 *   parsed).
 */

/** Presentation view of one failed section load. */
export interface SectionErrorView {
  message: string;
  requestId: string | null;
}

export const SECTION_NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";
const SECTION_SYSTEM_ERROR_TEXT = "服务暂时不可用，请稍后重试";
const SECTION_GENERIC_ERROR_TEXT = "加载失败，请稍后重试";

/** Derive a section error view. Pure: same failure in, same view out. */
export function describeSectionError(error: unknown): SectionErrorView {
  if (!isApiError(error)) {
    return { message: SECTION_NETWORK_ERROR_TEXT, requestId: null };
  }
  if (isSystemErrorCode(error.code)) {
    return { message: SECTION_SYSTEM_ERROR_TEXT, requestId: error.requestId };
  }
  if (isKnownErrorCode(error.code)) {
    return { message: error.message || SECTION_GENERIC_ERROR_TEXT, requestId: null };
  }
  return { message: SECTION_GENERIC_ERROR_TEXT, requestId: error.requestId };
}
