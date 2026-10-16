"use client";
/**
 * Staff invitation acceptance island — spec §5.8 steps 1-2 (patterns
 * §6); V1.1 §2.3 re-shapes the branch per role (plan Tasks 7/9).
 *
 *   password (OPTIONAL since V1.1) ──accept(token, password?)──▶ ┌ teacher
 *     │                                totp_required: false      │  full login,
 *     │                                                           │  straight to the
 *     ├─ 401 uniform: unknown/expired/used token                  │  workspace
 *     │                                totp_required: true       └ admin: TotpSetup
 *     └─ client-invalid band (supplied values only):              (unchanged two-step
 *        field error, NO request — the token never burns           with the pending
 *                                                                   session)
 *
 * - the invitation token arrives as a ROUTE PARAM (prop) — it is a
 *   single-use 256-bit secret, so it is never logged and never echoed
 *   back into the UI;
 * - V1.1: a TEACHER may leave the password EMPTY — the account runs
 *   email-code-only (no password_hash). ADMIN invitations still
 *   require one (the backend band-checks a SUPPLIED password before
 *   touching the one-time token, so a wrong-shape value never burns
 *   it; an empty password on an ADMIN invite is refused server-side
 *   after lookup — the field error routes back here);
 * - the branch discriminator is the response's `totp_required`
 *   (reviewer ruling 2026-10-15); a body WITHOUT the field (the
 *   pre-V1.1 shape) defaults to the admin leg — the safe default
 *   until the backend's T7 lands;
 * - success answers full tokens (teacher) or the pending session's
 *   body access token (admin — the sanctioned exception, React state
 *   only, confined to the TOTP setup endpoints).
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { isApiError } from "@/lib/errors";
import { useSession } from "./session";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { TotpSetup } from "./TotpSetup";
import { acceptStaffInvitation, readMe } from "./api";
import { landAfterStaffLogin } from "./staffLoginFlow";
import { NETWORK_ERROR_TEXT, withFieldErrorSummary, type AuthErrorView } from "./errors";
import {
  describeStaffError,
  validateOptionalInvitePassword,
  validateOptionalInvitePasswordRepeat,
} from "./staffAuthView";

type InviteFields = "password" | "repeat_password";

type InviteStep =
  | { kind: "password" }
  | { kind: "totp"; accessToken: string };

export interface StaffInviteAcceptProps {
  /** The single-use invitation token from the email link (route param). */
  token: string;
}

export function StaffInviteAccept({ token }: StaffInviteAcceptProps) {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [step, setStep] = useState<InviteStep>({ kind: "password" });
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<InviteFields, string>>>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Partial<Record<InviteFields, string>> = {
      password: validateOptionalInvitePassword(password) ?? undefined,
      repeat_password: validateOptionalInvitePasswordRepeat(password, repeat) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    if (errors.password !== undefined || errors.repeat_password !== undefined) {
      return;
    }

    setSubmitting(true);
    try {
      const outcome = await acceptStaffInvitation(token, password.length === 0 ? null : password);
      setPassword("");
      setRepeat("");
      if (outcome.kind === "teacher") {
        // Full login adopted by the wrapper — land on the role's OWN
        // cockpit via the post-login /me answer (the #84 rule; a
        // teacher invitation today, but the landing never assumes it).
        // The rename nudge greets digit-shaped nicknames there.
        await landAfterStaffLogin({
          login: async () => undefined,
          refreshSession,
          router,
          readMe,
        });
        return;
      }
      // ADMIN: PendingStaffSession, confined to /staff/totp/*.
      setStep({ kind: "totp", accessToken: outcome.pendingToken });
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      const view = withFieldErrorSummary(describeStaffError(error, "invite-accept"));
      setSummary(view);
      setFieldErrors(view.fieldErrors as Partial<Record<InviteFields, string>>);
    } finally {
      setSubmitting(false);
    }
  }

  if (step.kind === "totp") {
    return (
      <div className="invite-flow">
        <p className="field-hint" role="status">
          密码已设置。请完成动态口令（TOTP）绑定，激活员工账号。
        </p>
        <TotpSetup accessToken={step.accessToken} />
      </div>
    );
  }

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="password"
        label="设置密码（可选）"
        hint="10-128 个字符。教师可留空（之后仅用邮箱验证码登录）；管理员必须设置。密码设置后邀请链接即失效。"
        error={fieldErrors.password}
        inputProps={{
          value: password,
          onChange: (event) => setPassword(event.target.value),
          type: "password",
          autoComplete: "new-password",
          placeholder: "请设置密码（教师可留空）",
          disabled: submitting,
        }}
      />
      <AuthField
        name="repeat_password"
        label="确认密码"
        hint="再次输入上面的密码；若留空请保持两项都为空。"
        error={fieldErrors.repeat_password}
        inputProps={{
          value: repeat,
          onChange: (event) => setRepeat(event.target.value),
          type: "password",
          autoComplete: "new-password",
          placeholder: "请再次输入密码",
          disabled: submitting,
        }}
      />
      <SubmitButton loading={submitting}>激活账号</SubmitButton>
    </form>
  );
}
