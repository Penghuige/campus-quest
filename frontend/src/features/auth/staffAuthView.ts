/**
 * Staff 2FA surface — pure presentation logic (spec §5.8; patterns §6/§15).
 *
 * Everything here is a pure function over backend-returned data: the unit
 * suite pins each rule without React. The three concerns:
 *
 * 1. client convenience validation (band mirrors only — the backend stays
 *    the single authority, identity/staff_service.py + totp.py);
 * 2. per-surface error mapping: `AUTHENTICATION_REQUIRED` is one envelope
 *    code covering THREE different user-facing failures on the staff
 *    surface (dead invitation token, wrong staff credentials, wrong TOTP
 *    confirm code). The §29 code is branched on — never the message — and
 *    the SURFACE (who asked) picks the stable zh-CN copy;
 * 3. the one-time recovery-codes display state machine: codes enter state
 *    exactly once, `confirmRecoveryCodesSeen` is the only exit, and no
 *    exported transition can re-enter "shown" or restore `codes` — the
 *    plaintext list exists nowhere client-side after confirm (no storage,
 *    no refetch path; the backend itself returns the list exactly once).
 *
 * QR display decision (documented as required): the setup step shows the
 * SECRET + the backend's `otpauth://` URI as copyable text — NO client-side
 * QR rendering. `TOTP_QR_STRATEGY` pins the choice; see its doc comment for
 * the rationale.
 */
import type { ApiError } from "../../lib/errors";

import { describeAuthError, type AuthErrorView } from "./errors";
import { isAsciiDigitString, validatePassword } from "./validation";

// --- client convenience validation ------------------------------------------

/** Backend RFC 6238 code length (pyotp default; identity/totp.py). */
export const TOTP_CODE_LENGTH = 6;

/** Backend `RECOVERY_CODE_PATTERN`: 5 hex chars, dash, 5 hex chars. */
const RECOVERY_CODE_PATTERN = /^[0-9a-f]{5}-[0-9a-f]{5}$/i;

/** Backend `_EMAIL_MAX_LENGTH` (identity/staff_service.py). */
const EMAIL_MAX_LENGTH = 320;

/**
 * Staff email convenience rule. The backend lowercases/strips and owns the
 * real mailbox check; this only catches obvious garbage so the submit does
 * not fire on a typo'd identifier (patterns §6).
 */
export function validateStaffEmail(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "请输入邮箱";
  }
  if (
    trimmed.length > EMAIL_MAX_LENGTH ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)
  ) {
    return "请输入正确的邮箱地址";
  }
  return null;
}

/**
 * Invitation password step: the shared 10-128 band (§5.6 — the backend
 * checks it BEFORE touching the one-time token) plus the repeat-field
 * match the backend cannot see.
 */
export function validateInvitePassword(password: string): string | null {
  return validatePassword(password);
}

/** The repeat field on the invitation form (client-only concern). */
export function validateInvitePasswordRepeat(
  password: string,
  repeat: string,
): string | null {
  if (repeat.length === 0) {
    return "请再次输入密码";
  }
  if (password !== repeat) {
    return "两次输入的密码不一致";
  }
  return null;
}

/**
 * TOTP code convenience rule for the setup CONFIRM step: exactly 6 ASCII
 * digits (the backend strips outer whitespace before verifying — mirrored).
 */
export function validateTotpCode(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "请输入动态验证码";
  }
  if (!isAsciiDigitString(trimmed) || trimmed.length !== TOTP_CODE_LENGTH) {
    return `动态验证码为 ${TOTP_CODE_LENGTH} 位数字`;
  }
  return null;
}

/**
 * Second-factor convenience rule for staff LOGIN: the field accepts a
 * current 6-digit TOTP code OR one unused recovery code (`0f1e2-d3c4b`
 * shape; the backend matches case-sensitively after strip, so uppercase
 * input is tolerated here and left to the server's uniform rejection —
 * convenience validation must not invent a stricter band than the backend).
 */
export function validateSecondFactor(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "请输入动态验证码或恢复代码";
  }
  if (validateTotpCode(trimmed) === null || RECOVERY_CODE_PATTERN.test(trimmed)) {
    return null;
  }
  return "请输入 6 位数字动态验证码，或恢复代码（格式如 0f1e2-d3c4b）";
}

// --- per-surface error mapping ----------------------------------------------

/** Which staff surface failed — picks the copy for shared envelope codes. */
export type StaffErrorSurface =
  | "invite-accept"
  | "staff-login"
  | "totp-confirm"
  | "totp-session";

/**
 * Stable zh-CN copy the staff surface OVERRIDES. `AUTHENTICATION_REQUIRED`
 * is deliberately uniform server-side (anti-enumeration): unknown, expired,
 * and used invitation tokens — and wrong email/password/second-factor at
 * login — all answer with the same envelope, so the client copy must stay
 * per-surface and equally uniform. Codes not listed fall through to the
 * shared auth mapping (features/auth/errors.ts tiers) — except where the
 * shared copy is student-worded (`USERNAME_ALREADY_EXISTS`) and the staff
 * surface needs its own line.
 */
const STAFF_SUMMARY_TEXT: Record<StaffErrorSurface, Partial<Record<string, string>>> = {
  "invite-accept": {
    AUTHENTICATION_REQUIRED: "邀请链接无效或已被使用",
    // The shared map's copy is the STUDENT registration wording (学号);
    // the staff surface keeps the backend's own email wording.
    USERNAME_ALREADY_EXISTS: "该邮箱已被其他账号使用",
  },
  "staff-login": {
    AUTHENTICATION_REQUIRED: "邮箱、密码或动态验证码错误",
    // Raised only AFTER the password proved correct (staff_service):
    // the actionable panel in StaffLoginForm rides on this code.
    TOTP_SETUP_REQUIRED: "密码正确，但该账号尚未完成动态口令绑定",
  },
  "totp-confirm": {
    // Wrong code at confirm burns nothing server-side; retry is safe.
    AUTHENTICATION_REQUIRED: "动态验证码错误，请输入验证器当前显示的 6 位数字",
  },
  "totp-session": {
    // A dead/expired pending-session bearer at begin/confirm.
    AUTHENTICATION_REQUIRED: "绑定会话已失效，请重新打开邀请链接；若持续失败，请联系管理员",
  },
};

/**
 * Derive the presentation view of a failed staff mutation. Same contract
 * as `describeAuthError` (pure, code-branched, message never parsed) with
 * the staff surface's overrides applied first. Callers handle the
 * non-envelope (network) tier themselves, exactly like every other auth
 * form — hence the `ApiError` parameter type.
 */
export function describeStaffError(
  error: ApiError,
  surface: StaffErrorSurface,
): AuthErrorView {
  const overridden = STAFF_SUMMARY_TEXT[surface][error.code];
  if (overridden !== undefined) {
    return { summary: overridden, fieldErrors: {}, requestId: null };
  }
  // USERNAME_ALREADY_EXISTS at invite, VALIDATION_ERROR band/already-bound
  // envelopes, rate limits, and system/unknown codes keep the shared tiers.
  return describeAuthError(error);
}

// --- otpauth provisioning display -------------------------------------------

/**
 * How the TOTP credential is presented. Decision: TEXT + COPY, no QR.
 *
 * - package.json intentionally carries no QR dependency and this stream
 *   adds no runtime deps (patterns §17: do not ship libraries for tiny
 *   utilities); the ONE sanctioned fallback is a zero-dep renderer, but a
 *   correct QR encoder is a Reed-Solomon + masking implementation — far
 *   too much hand-rolled surface to risk for one screen;
 * - an external QR image service would leak the secret to a third party
 *   (the secret IS the credential) — never an option;
 * - every authenticator app supports manual key entry, and the backend
 *   already returns both the secret and the ready-to-paste `otpauth://`
 *   URI, so text + copy buttons is the dependency-free, offline, private
 *   presentation. If a vetted zero-dep QR module lands later, this
 *   constant is the single place to flip.
 */
export const TOTP_QR_STRATEGY = "text-and-copy" as const;

/** Presentation pieces of one `TotpSetupResponse` (displayed exactly once). */
export interface TotpProvisioningView {
  /** The provisioning strategy in force (see `TOTP_QR_STRATEGY`). */
  strategy: typeof TOTP_QR_STRATEGY;
  /** Base32 secret for the authenticator's manual-key-entry field. */
  secret: string;
  /** The backend's `otpauth://` URI, verbatim (copy-to-clipboard target). */
  otpauthUri: string;
  /**
   * `Issuer:account` label parsed from the URI path for the manual-entry
   * hint; `null` when the URI is not the expected shape (the raw URI
   * remains displayed either way — parsing is presentation-only).
   */
  accountLabel: string | null;
}

/**
 * Build the display view of a fresh TOTP credential. Pure; never mutates
 * or re-encodes the URI (a test pins it passes through byte-for-byte).
 */
export function totpProvisioningView(setup: {
  secret: string;
  otpauth_uri: string;
}): TotpProvisioningView {
  let accountLabel: string | null = null;
  try {
    const parsed = new URL(setup.otpauth_uri);
    const label = decodeURIComponent(parsed.pathname.replace(/^\/+/, ""));
    // pyotp emits `otpauth://totp/Issuer:account?...`; a bare host-less
    // path (or a non-otpauth scheme) is not worth guessing at.
    if (
      parsed.protocol === "otpauth:" &&
      label.length > 0 &&
      !label.includes("/") &&
      !label.includes("?")
    ) {
      accountLabel = label;
    }
  } catch {
    accountLabel = null;
  }
  return {
    strategy: TOTP_QR_STRATEGY,
    secret: setup.secret,
    otpauthUri: setup.otpauth_uri,
    accountLabel,
  };
}

// --- recovery codes: the one-time display state machine ----------------------

/**
 * The ONLY lifecycle the plaintext recovery codes have client-side:
 * `shown` (issued by the confirm response, held in React state) ->
 * `confirmed` (user pressed 确认已保存; codes dropped for good).
 *
 * Invariants the suite pins:
 * - `codes` is non-null ONLY in `shown`;
 * - no exported transition re-enters `shown` or restores `codes` — there
 *   is no refetch path (the backend returns the list exactly once) and no
 *   storage path (nothing may touch localStorage/sessionStorage).
 */
export interface RecoveryCodesState {
  phase: "shown" | "confirmed";
  codes: readonly string[] | null;
}

/** Enter the one-time display window with the freshly issued codes. */
export function recoveryCodesIssued(codes: string[]): RecoveryCodesState {
  return { phase: "shown", codes };
}

/**
 * The user's "I saved them" confirmation: exit the display window and
 * DROP the plaintext list. Idempotent (an already-confirmed state passes
 * through unchanged); irreversible by construction.
 */
export function confirmRecoveryCodesSeen(
  state: RecoveryCodesState,
): RecoveryCodesState {
  return state.phase === "shown" ? { phase: "confirmed", codes: null } : state;
}

/** The copy-all payload: one code per line, exactly as displayed. */
export function copyAllRecoveryCodesText(codes: readonly string[]): string {
  return codes.join("\n");
}
