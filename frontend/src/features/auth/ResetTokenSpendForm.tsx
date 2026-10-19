"use client";
/**
 * The admin-issued reset token's CONSUME island (Wave 2a §3.2): the
 * one-time urlsafe32 token arrives in the link's ?token= (editable —
 * the admin may hand over the bare token); the public spend sets the
 * new password and revokes every live session server-side. Success
 * routes to /login?reset=1 (the login page's existing reset note);
 * the uniform invalid (unknown/expired/replayed token) renders the
 * server's copy and keeps the form.
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { spendAdminResetToken } from "./api";
import { NETWORK_ERROR_TEXT, type AuthErrorView } from "./errors";
import { validatePassword } from "./validation";

export function ResetTokenSpendForm({ initialToken }: { initialToken: string }) {
  const router = useRouter();
  const [token, setToken] = useState(initialToken);
  const [password, setPassword] = useState("");
  const [tokenError, setTokenError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) {
      return;
    }
    const trimmed = token.trim();
    const errors = {
      token: trimmed.length === 0 ? "请输入重置口令" : undefined,
      password: validatePassword(password) ?? undefined,
    };
    setTokenError(errors.token);
    setPasswordError(errors.password);
    setSummary(null);
    if (errors.token !== undefined || errors.password !== undefined) {
      return;
    }
    setLoading(true);
    try {
      await spendAdminResetToken(trimmed, password);
      router.replace("/login?reset=1");
      router.refresh();
    } catch (cause) {
      if (!isApiError(cause)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      setSummary({ summary: cause.message, fieldErrors: {}, requestId: cause.requestId });
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="reset-token"
        label="重置口令"
        hint="管理员交付的一次性口令（48 小时内有效，仅可使用一次）"
        error={tokenError}
        inputProps={{
          value: token,
          onChange: (event) => {
            setToken(event.target.value);
            if (tokenError !== undefined) {
              setTokenError(undefined);
            }
          },
          type: "text",
          autoComplete: "one-time-code",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "请输入重置口令",
          disabled: loading,
        }}
      />
      <AuthField
        name="reset-token-password"
        label="新密码"
        error={passwordError}
        inputProps={{
          value: password,
          onChange: (event) => {
            setPassword(event.target.value);
            if (passwordError !== undefined) {
              setPasswordError(undefined);
            }
          },
          type: "password",
          autoComplete: "new-password",
          placeholder: "请设置新密码",
          disabled: loading,
        }}
      />
      <SubmitButton loading={loading}>重置密码</SubmitButton>
    </form>
  );
}
