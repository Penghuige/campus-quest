"use client";
/**
 * Student login island (spec §5.6; patterns §2/§6).
 *
 * Client convenience validation mirrors the backend bands; the submit flow is
 * input -> validate -> POST /api/v1/auth/login -> branch on the §29 envelope
 * code -> on success refresh the session cache and redirect home. The
 * refresh token never touches JS: the backend delivers it in the HttpOnly
 * cookie and `apiRequest` sends credentials on the follow-up /me fetch.
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useSession } from "@/features/auth/session";
import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { loginStudent } from "./api";
import {
  NETWORK_ERROR_TEXT,
  describeAuthError,
  withFieldErrorSummary,
  type AuthErrorView,
  type AuthFieldName,
} from "./errors";
import { validatePassword, validateUsername } from "./validation";

type LoginFields = Extract<AuthFieldName, "username" | "password">;

export function LoginForm() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<LoginFields, string>>>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = username.trim(); // backend strips outer whitespace at login (§5.2)
    const errors: Partial<Record<LoginFields, string>> = {
      username: validateUsername(trimmed) ?? undefined,
      password: validatePassword(password) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    if (errors.username !== undefined || errors.password !== undefined) {
      return;
    }

    setLoading(true);
    try {
      await loginStudent(trimmed, password);
      refreshSession(); // invalidate the cached anonymous /me result
      router.replace("/");
      router.refresh(); // re-render server components with the new session
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      // Field-only views still get a summary line so the failure is
      // announced (patterns §18), not just painted next to the inputs.
      const view = withFieldErrorSummary(describeAuthError(error));
      setSummary(view);
      setFieldErrors(view.fieldErrors as Partial<Record<LoginFields, string>>);
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="username"
        label="学号"
        error={fieldErrors.username}
        inputProps={{
          value: username,
          onChange: (event) => setUsername(event.target.value),
          type: "text",
          inputMode: "numeric",
          autoComplete: "username",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "请输入学号",
          maxLength: 20,
          disabled: loading,
        }}
      />
      <AuthField
        name="password"
        label="密码"
        error={fieldErrors.password}
        inputProps={{
          value: password,
          onChange: (event) => setPassword(event.target.value),
          type: "password",
          autoComplete: "current-password",
          placeholder: "请输入密码",
          disabled: loading,
        }}
      />
      <SubmitButton loading={loading}>登录</SubmitButton>
    </form>
  );
}
