/**
 * Pure account-settings views (spec §5.4-§5.6, §40; patterns §6).
 *
 * The panels here render the OWNER'S OWN contact values — MePublic
 * carries them precisely because this is the authenticated owner's
 * settings surface (spec §40); no other surface may show them. Client
 * validation mirrors the backend bands by REUSING the T2 validators in
 * `validation.ts` (single mirror, not a second copy).
 */
import type { MeDto } from "@/features/auth/api";

// --- email panel state -------------------------------------------------------------

export type EmailPanelState = "none" | "unverified" | "verified";

export interface EmailPanelView {
  state: EmailPanelState;
  /** The bound address (owner's own view; null while unbound). */
  email: string | null;
  /** Badge text for the binding state (color is supplemental). */
  statusLabel: string;
  /** One-line explanation of what the next action does. */
  hint: string;
}

/**
 * The email panel's state from the account view: bound+verified / bound
 * but the verification never completed / not bound at all. Takes just
 * the two email fields so narrow prop shapes type-check against it.
 */
export function emailPanelView(
  me: Pick<MeDto, "email_normalized" | "email_verified_at">,
): EmailPanelView {
  if (me.email_normalized === null) {
    return {
      state: "none",
      email: null,
      statusLabel: "未绑定",
      hint: "绑定邮箱后可以通过邮件接收验证与通知。",
    };
  }
  if (me.email_verified_at === null) {
    return {
      state: "unverified",
      email: me.email_normalized,
      statusLabel: "待验证",
      hint: "验证邮件已发送到该邮箱，输入邮件中的验证码完成绑定。",
    };
  }
  return {
    state: "verified",
    email: me.email_normalized,
    statusLabel: "已验证",
    hint: "解绑需要重新输入当前密码确认。",
  };
}

// --- client validation (mirrors backend bands via the T2 validators) ---------------

/** Two-new-password agreement; the band itself is `validatePassword`. */
export function validatePasswordConfirmation(
  password: string,
  confirmation: string,
): string | null {
  if (confirmation.length === 0) {
    return "请再次输入新密码";
  }
  if (password !== confirmation) {
    return "两次输入的密码不一致";
  }
  return null;
}
