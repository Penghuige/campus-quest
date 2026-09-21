"use client";
/**
 * Staff invitation acceptance island — spec §5.8 steps 1-2 (patterns §6).
 *
 * Two steps, one component:
 *
 *   password ──accept(token, password)──▶ totp (TotpSetup with the pending
 *                                         session's access token)
 *
 * - the invitation token arrives as a ROUTE PARAM (prop) — it is a
 *   single-use 256-bit secret, so it is never logged and never echoed
 *   back into the UI;
 * - password band (10-128) is client-mirrored BEFORE the request so an
 *   obvious mistake cannot burn the one-time token (the backend checks
 *   the band first for exactly this reason — staff_service);
 * - success answers `TokenPairResponse`: the body's short-lived access
 *   token is the sanctioned PendingStaffSession exception, held in React
 *   state ONLY for the TOTP step — the refresh token stays in its
 *   HttpOnly cookie untouched (the setup endpoints never need it);
 * - 401 on accept is UNIFORM for unknown/expired/used tokens (the branch
 *   reason never leaves the server); 409 USERNAME_ALREADY_EXISTS maps to
 *   the stable "email taken" copy.
 */
import { useState, type FormEvent } from "react";

import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { TotpSetup } from "./TotpSetup";
import { acceptStaffInvitation } from "./api";
import { NETWORK_ERROR_TEXT, withFieldErrorSummary, type AuthErrorView } from "./errors";
import {
  describeStaffError,
  validateInvitePassword,
  validateInvitePasswordRepeat,
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
  const [step, setStep] = useState<InviteStep>({ kind: "password" });
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<InviteFields, string>>>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Partial<Record<InviteFields, string>> = {
      password: validateInvitePassword(password) ?? undefined,
      repeat_password: validateInvitePasswordRepeat(password, repeat) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    if (errors.password !== undefined || errors.repeat_password !== undefined) {
      return;
    }

    setSubmitting(true);
    try {
      const tokens = await acceptStaffInvitation(token, password);
      // PendingStaffSession: body access token, confined to /staff/totp/*.
      setPassword("");
      setRepeat("");
      setStep({ kind: "totp", accessToken: tokens.access_token });
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
        label="设置密码"
        hint="10-128 个字符；密码设置后邀请链接即失效。"
        error={fieldErrors.password}
        inputProps={{
          value: password,
          onChange: (event) => setPassword(event.target.value),
          type: "password",
          autoComplete: "new-password",
          placeholder: "请设置密码",
          disabled: submitting,
        }}
      />
      <AuthField
        name="repeat_password"
        label="确认密码"
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
      <SubmitButton loading={submitting}>设置密码并继续</SubmitButton>
    </form>
  );
}
