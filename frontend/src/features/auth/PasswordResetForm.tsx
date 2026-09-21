"use client";
/**
 * Password recovery island (spec §5.6; patterns §6).
 *
 * Two steps: username -> POST /auth/password/forgot (returns a challenge for
 * EVERY identifier class — the backend's anti-enumeration decoy), then
 * code + new password -> POST /auth/password/reset (204) -> back to /login.
 *
 * Anti-enumeration is a UI obligation here, not just a backend one
 * (task brief step 3): the copy after requesting a code is IDENTICAL no
 * matter what the server decided internally, and no state ever reveals
 * whether the username exists.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import {
  confirmPasswordReset,
  requestPasswordReset,
  type ChallengeView,
} from "./api";
import {
  AUTH_ERROR_TEXT,
  DEFAULT_RESEND_COOLDOWN_SECONDS,
  FIELD_FOR_CODE,
  NETWORK_ERROR_TEXT,
  OTP_CHALLENGE_RESET_CODES,
  cooldownSecondsFrom,
  describeAuthError,
  resendButtonLabel,
  withFieldErrorSummary,
  type AuthErrorView,
  type AuthFieldName,
} from "./errors";
import { useCooldown } from "./useCooldown";
import { validateOtpCode, validatePassword, validateUsername } from "./validation";

type ResetFields = Extract<AuthFieldName, "username" | "code" | "password">;

/** Uniform, enumeration-free copy shown once a code has been "sent". */
const CODE_SENT_NOTE =
  "如果该用户名对应的账号存在且已绑定手机号，验证码已发送至其绑定手机。";

export function PasswordResetForm() {
  const router = useRouter();
  const [step, setStep] = useState<"identify" | "confirm">("identify");
  const [username, setUsername] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [challenge, setChallenge] = useState<ChallengeView | null>(null);
  const [fieldErrors, setFieldErrors] = useState<
    Partial<Record<ResetFields, string>>
  >({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const { remaining, start } = useCooldown();

  function clearFieldError(name: ResetFields) {
    setFieldErrors((previous) => {
      if (previous[name] === undefined) {
        return previous;
      }
      const rest: Partial<Record<ResetFields, string>> = { ...previous };
      delete rest[name];
      return rest;
    });
  }

  async function requestCode(trimmedUsername: string) {
    setFieldErrors({});
    setSummary(null);
    setRequesting(true);
    try {
      const next = await requestPasswordReset(trimmedUsername);
      setChallenge(next);
      setStep("confirm");
      start(DEFAULT_RESEND_COOLDOWN_SECONDS);
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      const cooldown = cooldownSecondsFrom(error);
      if (cooldown > 0) {
        start(cooldown);
        return;
      }
      const view = withFieldErrorSummary(describeAuthError(error));
      setSummary(view);
      setFieldErrors(view.fieldErrors as Partial<Record<ResetFields, string>>);
    } finally {
      setRequesting(false);
    }
  }

  async function onRequestCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = username.trim();
    const usernameError = validateUsername(trimmed);
    if (usernameError !== null) {
      setFieldErrors({ username: usernameError });
      return;
    }
    await requestCode(trimmed);
  }

  async function onConfirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (challenge === null) {
      setStep("identify");
      return;
    }
    const errors: Partial<Record<ResetFields, string>> = {
      code: validateOtpCode(code) ?? undefined,
      password: validatePassword(newPassword) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    if (errors.code !== undefined || errors.password !== undefined) {
      return;
    }

    setSubmitting(true);
    try {
      await confirmPasswordReset({
        challenge_id: challenge.challenge_id,
        code,
        new_password: newPassword,
      });
      router.replace("/login?reset=1");
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }

      // Dead challenge (expired/consumed/invalid/attempts/proof mismatch):
      // confirmation is impossible until a new code is requested.
      if (OTP_CHALLENGE_RESET_CODES.has(error.code)) {
        setChallenge(null);
        setStep("identify");
        setCode("");
      }

      const base = describeAuthError(error);
      const placement = FIELD_FOR_CODE[error.code];
      const placedText =
        placement !== undefined ? AUTH_ERROR_TEXT[error.code] : undefined;
      const mergedFields =
        placement !== undefined &&
        placedText !== undefined &&
        base.fieldErrors[placement] === undefined
          ? { ...base.fieldErrors, [placement]: placedText }
          : base.fieldErrors;
      const view = withFieldErrorSummary({ ...base, fieldErrors: mergedFields });
      setSummary(view);
      setFieldErrors(view.fieldErrors as Partial<Record<ResetFields, string>>);
    } finally {
      setSubmitting(false);
    }
  }

  if (step === "identify") {
    return (
      <form className="form" onSubmit={onRequestCode} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="username"
          label="学号"
          hint="我们将向该账号绑定的手机号发送验证码。"
          error={fieldErrors.username}
          inputProps={{
            value: username,
            onChange: (event) => {
              setUsername(event.target.value);
              clearFieldError("username");
            },
            type: "text",
            inputMode: "numeric",
            autoComplete: "username",
            autoCapitalize: "none",
            autoCorrect: "off",
            placeholder: "请输入学号",
            maxLength: 20,
            disabled: requesting,
          }}
        />
        <SubmitButton loading={requesting}>获取验证码</SubmitButton>
        <p className="auth-alt-action">
          想起密码了？
          <Link className="link" href="/login">
            返回登录
          </Link>
        </p>
      </form>
    );
  }

  return (
    <form className="form" onSubmit={onConfirm} noValidate>
      <FormErrorSummary view={summary} />
      <p className="field-hint" role="status">
        {CODE_SENT_NOTE}
      </p>
      <AuthField
        name="code"
        label="短信验证码"
        error={fieldErrors.code}
        inputProps={{
          value: code,
          onChange: (event) => {
            setCode(event.target.value);
            clearFieldError("code");
          },
          type: "text",
          inputMode: "numeric",
          autoComplete: "one-time-code",
          placeholder: "6 位数字",
          maxLength: 6,
          disabled: submitting,
        }}
      />
      <AuthField
        name="password"
        label="新密码"
        hint="10-128 个字符；重置成功后，其他已登录设备会被退出。"
        error={fieldErrors.password}
        inputProps={{
          value: newPassword,
          onChange: (event) => {
            setNewPassword(event.target.value);
            clearFieldError("password");
          },
          type: "password",
          autoComplete: "new-password",
          placeholder: "请设置新密码",
          disabled: submitting,
        }}
      />
      <SubmitButton loading={submitting}>重置密码</SubmitButton>
      <button
        type="button"
        className="btn btn-secondary btn-block"
        onClick={() => void requestCode(username.trim())}
        disabled={requesting || remaining > 0 || submitting}
      >
        {resendButtonLabel(remaining, "重新发送验证码", requesting)}
      </button>
      <p className="auth-alt-action">
        想起密码了？
        <Link className="link" href="/login">
          返回登录
        </Link>
      </p>
    </form>
  );
}
