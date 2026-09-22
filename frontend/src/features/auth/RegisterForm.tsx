"use client";
/**
 * Student registration island (spec §5.1-5.4, §33.2; patterns §6).
 *
 * Flow: student number + nickname + password + phone OTP —
 *   1. `获取验证码` -> POST /auth/phone/challenges (cooldown countdown;
 *      an `OTP_RESEND_COOLDOWN` envelope restarts it from the server hint
 *      or the 60s default);
 *   2. submit -> POST /auth/phone/challenges/{id}/verify (the SMS code
 *      becomes the single-use `phone_token`, the challenge is spent);
 *   3. POST /auth/register with that token -> redirect to /login.
 *
 * Proof lifecycle (identity/otp.py + service.py ordering, mirrored here):
 * - a wrong code does NOT consume the challenge — retry keeps the OTP;
 * - a `VALIDATION_ERROR` from register runs BEFORE the token is consumed,
 *   so the form keeps the minted `phone_token` and a fixed field resubmits
 *   without a new SMS;
 * - every other register failure consumes the token server-side, so the
 *   form drops the whole OTP step and asks for a fresh code instead of
 *   silently resubmitting a dead proof.
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import {
  registerStudent,
  requestPhoneChallenge,
  verifyPhoneChallenge,
} from "./api";
import {
  AUTH_ERROR_TEXT,
  DEFAULT_RESEND_COOLDOWN_SECONDS,
  FIELD_FOR_CODE,
  NETWORK_ERROR_TEXT,
  cooldownSecondsFrom,
  describeAuthError,
  resendButtonLabel,
  withFieldErrorSummary,
  type AuthErrorView,
  type AuthFieldName,
} from "./errors";
import { useCooldown } from "./useCooldown";
import {
  NICKNAME_MAX_GRAPHEME_CLUSTERS,
  countGraphemes,
  validateNickname,
  validateOtpCode,
  validatePassword,
  validatePhone,
  validateStudentNumber,
} from "./validation";

type RegisterFields = Extract<
  AuthFieldName,
  "student_number" | "nickname" | "phone" | "code" | "password"
>;

const OTP_RESET_NOTE = "验证码已失效，请重新获取后再提交。";

/** The live OTP material: an open challenge, plus its minted proof if any. */
interface OtpState {
  challengeId: string;
  /** Single-use REGISTER proof; `null` until the code is verified. */
  phoneToken: string | null;
}

export function RegisterForm() {
  const router = useRouter();
  const [values, setValues] = useState({
    student_number: "",
    nickname: "",
    phone: "",
    code: "",
    password: "",
  });
  const [otp, setOtp] = useState<OtpState | null>(null);
  const [otpNote, setOtpNote] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<
    Partial<Record<RegisterFields, string>>
  >({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [requestingOtp, setRequestingOtp] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const { remaining, start, clear } = useCooldown();

  const nicknameCount = countGraphemes(values.nickname);

  function setField(name: RegisterFields, value: string) {
    setValues((previous) => ({ ...previous, [name]: value }));
    setFieldErrors((previous) => {
      if (previous[name] === undefined) {
        return previous;
      }
      const rest: Partial<Record<RegisterFields, string>> = { ...previous };
      delete rest[name];
      return rest;
    });
  }

  /** Drop the spent challenge/proof so the next submit demands a fresh code. */
  function resetOtp() {
    setOtp(null);
    clear();
    setValues((previous) => ({ ...previous, code: "" }));
    setOtpNote(OTP_RESET_NOTE);
  }

  function applyError(view: AuthErrorView) {
    setSummary(view);
    setFieldErrors(view.fieldErrors as Partial<Record<RegisterFields, string>>);
  }

  async function onRequestCode() {
    const phoneError = validatePhone(values.phone);
    if (phoneError !== null) {
      setFieldErrors({ phone: phoneError });
      return;
    }
    setFieldErrors({});
    setSummary(null);
    setRequestingOtp(true);
    try {
      const next = await requestPhoneChallenge(values.phone.trim());
      setOtp({ challengeId: next.challenge_id, phoneToken: null });
      setOtpNote("验证码已发送，请查收短信。");
      start(DEFAULT_RESEND_COOLDOWN_SECONDS);
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      const cooldown = cooldownSecondsFrom(error);
      if (cooldown > 0) {
        // Server cooldown envelope -> countdown on the resend button.
        start(cooldown);
        setOtpNote("发送过于频繁，请等待倒计时结束后重试。");
        return;
      }
      applyError(withFieldErrorSummary(describeAuthError(error)));
    } finally {
      setRequestingOtp(false);
    }
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Partial<Record<RegisterFields, string>> = {
      student_number: validateStudentNumber(values.student_number) ?? undefined,
      nickname: validateNickname(values.nickname) ?? undefined,
      phone: validatePhone(values.phone) ?? undefined,
      code: otp === null ? "请先获取验证码" : (validateOtpCode(values.code) ?? undefined),
      password: validatePassword(values.password) ?? undefined,
    };
    setFieldErrors(errors);
    setSummary(null);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }
    const currentOtp = otp;
    if (currentOtp === null) {
      return; // unreachable: the code error above already returned
    }

    setSubmitting(true);
    try {
      let phoneToken = currentOtp.phoneToken;
      if (phoneToken === null) {
        const verified = await verifyPhoneChallenge(
          currentOtp.challengeId,
          values.code,
        );
        phoneToken = verified.phone_token;
        // Keep the proof for a possible retry after a register-side
        // VALIDATION_ERROR (the backend has not consumed it yet).
        setOtp({ challengeId: currentOtp.challengeId, phoneToken });
      }
      await registerStudent({
        student_number: values.student_number,
        nickname: values.nickname,
        phone_token: phoneToken,
        password: values.password,
      });
      router.replace("/login?registered=1");
    } catch (error) {
      if (!isApiError(error)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }

      // A wrong code keeps the challenge; a band-level VALIDATION_ERROR keeps
      // an unconsumed proof. Everything else burned server-side OTP material.
      if (error.code !== "VALIDATION_ERROR" && error.code !== "OTP_CODE_INVALID") {
        resetOtp();
      }

      // Codes with a natural home render there (whitelist -> student number,
      // OTP lifecycle -> code); withFieldErrorSummary then guarantees the
      // failure is also announced, never field-text-only.
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
      applyError(withFieldErrorSummary({ ...base, fieldErrors: mergedFields }));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="student_number"
        label="学号"
        hint="只有在老师导入的白名单中的学号可以注册。"
        error={fieldErrors.student_number}
        inputProps={{
          value: values.student_number,
          onChange: (event) => setField("student_number", event.target.value),
          type: "text",
          inputMode: "numeric",
          autoComplete: "username",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "请输入学号",
          maxLength: 20,
          disabled: submitting,
        }}
      />
      <AuthField
        name="nickname"
        label="昵称"
        counter={`${nicknameCount}/${NICKNAME_MAX_GRAPHEME_CLUSTERS}`}
        counterOver={nicknameCount > NICKNAME_MAX_GRAPHEME_CLUSTERS}
        hint="展示给其他同学的名称；按显示字符计数，emoji 算 1 个。"
        error={fieldErrors.nickname}
        inputProps={{
          value: values.nickname,
          onChange: (event) => setField("nickname", event.target.value),
          type: "text",
          autoComplete: "nickname",
          placeholder: "请输入昵称",
          disabled: submitting,
        }}
      />
      <div className="field">
        <div className="field-head">
          <label className="field-label" htmlFor="phone">
            手机号
          </label>
        </div>
        <div className="otp-row">
          <input
            className="input"
            id="phone"
            name="phone"
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder="用于接收验证码"
            value={values.phone}
            onChange={(event) => setField("phone", event.target.value)}
            aria-invalid={fieldErrors.phone !== undefined ? true : undefined}
            aria-describedby={
              [
                fieldErrors.phone ? "phone-error" : null,
                otpNote ? "otp-note" : null,
              ]
                .filter(Boolean)
                .join(" ") || undefined
            }
            disabled={submitting}
          />
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onRequestCode}
            disabled={requestingOtp || remaining > 0 || submitting}
          >
            {resendButtonLabel(
              remaining,
              otp === null ? "获取验证码" : "重新发送",
              requestingOtp,
            )}
          </button>
        </div>
        {otpNote ? (
          <p className="field-hint" id="otp-note" role="status">
            {otpNote}
          </p>
        ) : null}
        {fieldErrors.phone ? (
          <p className="field-error" id="phone-error">
            {fieldErrors.phone}
          </p>
        ) : null}
      </div>
      <AuthField
        name="code"
        label="短信验证码"
        hint="验证码以短信发送到上述手机号。"
        error={fieldErrors.code}
        inputProps={{
          value: values.code,
          onChange: (event) => setField("code", event.target.value),
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
        label="密码"
        hint="10-128 个字符，无其他限制。"
        error={fieldErrors.password}
        inputProps={{
          value: values.password,
          onChange: (event) => setField("password", event.target.value),
          type: "password",
          autoComplete: "new-password",
          placeholder: "请设置密码",
          disabled: submitting,
        }}
      />
      <SubmitButton loading={submitting}>注册</SubmitButton>
    </form>
  );
}
