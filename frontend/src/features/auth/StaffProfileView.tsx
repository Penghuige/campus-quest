"use client";
/**
 * Defect #17 (QA 2026-10-03): the teacher workspace's own profile
 * view — the account facts /me already carries (spec §40: the owner's
 * own account view), rendered as definition rows under the teacher
 * shell.
 *
 * Field scope: 昵称/账号/角色/账号状态 from the original DTO;
 * 注册日期 carried by MePublic.created_at since PR #46 — formatted
 * date-only in the business timezone, the growth view's honor-date
 * shape.
 *
 * V1.1 (plan Task 10 ruling): the nickname edit row. The rename nudge
 * points staff here, and PATCH /me/nickname accepts any active role —
 * until this row the endpoint had no staff UI, which would have made
 * the nudge a dead end. Reuses the shared validation + API modules the
 * student AccountSettings editor rides (no second nickname rule).
 */
import { useState, type FormEvent } from "react";

import { AuthField } from "./AuthField";
import { DevicePanel } from "./DevicePanel";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import {
  confirmPhoneBind,
  requestPhoneBind,
  updateNickname,
  type ChallengeView,
} from "./api";
import {
  describeAuthError,
  NETWORK_ERROR_TEXT,
  OTP_CHALLENGE_RESET_CODES,
  type AuthErrorView,
} from "./errors";
import { useSession } from "./session";
import { SettingsSection } from "./AccountSettings";
import { useCooldown } from "./useCooldown";
import {
  countGraphemes,
  NICKNAME_MAX_GRAPHEME_CLUSTERS,
  validateNickname,
  validateOtpCode,
  validatePhone,
} from "./validation";
import { isApiError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { BUSINESS_TIME_CONFIG } from "@/lib/time";

/** Account status text — the admin surfaces' established mapping. */
const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "正常",
  SUSPENDED: "已停用",
};

/** Date-only, year-inclusive, business timezone (formatHonorDate's shape). */
function formatRegistrationDate(iso: string): string {
  return new Intl.DateTimeFormat(BUSINESS_TIME_CONFIG.locale, {
    timeZone: BUSINESS_TIME_CONFIG.timeZone,
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(Date.parse(iso)));
}

export function StaffProfileView() {
  const { state, refresh } = useSession();
  if (state.status !== "authenticated") {
    return null; // The shell gates the whole workspace on the session.
  }
  const me = state.me;

  return (
    <section className="section" aria-label="个人信息">
      <div className="section-head">
        <h2 className="section-title">个人信息</h2>
      </div>
      <p className="field-hint">当前登录的员工账号信息。</p>
      <dl className="task-facts">
        <div className="fact-row">
          <dt className="fact-label">昵称</dt>
          <dd className="fact-value">{me.nickname}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">账号</dt>
          <dd className="fact-value">{me.username}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">角色</dt>
          <dd className="fact-value">{me.role === "ADMIN" ? "管理员" : "教师"}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">账号状态</dt>
          <dd className="fact-value">
            {STATUS_LABELS[me.status] ?? me.status}
          </dd>
        </div>
        {/* PR #46's contract addition: the registration instant,
            display-only. */}
        <div className="fact-row">
          <dt className="fact-label">注册日期</dt>
          <dd className="fact-value" suppressHydrationWarning>
            {formatRegistrationDate(me.created_at)}
          </dd>
        </div>
        {/* Login-iteration item ①: the staff account's bound phone —
            the fact row; binding/rebinding rides the section below. */}
        <div className="fact-row">
          <dt className="fact-label">手机号</dt>
          <dd className="fact-value">{me.phone_e164 ?? "未绑定"}</dd>
        </div>
      </dl>
      <NicknameEditRow me={me} onUpdated={refresh} />
      <PhoneBindSection me={me} onUpdated={refresh} />
      <DevicePanel />
    </section>
  );
}

/** The one editable staff fact (V1.1 Task 10): nickname, the same
 * shared rules the student editor rides. */
function NicknameEditRow({
  me,
  onUpdated,
}: {
  me: { nickname: string };
  onUpdated: () => void;
}) {
  const [value, setValue] = useState(me.nickname);
  const [fieldError, setFieldError] = useState<string | undefined>(undefined);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [savedNote, setSavedNote] = useState(false);

  const count = countGraphemes(value);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setSavedNote(false);
    const error = validateNickname(value) ?? undefined;
    setFieldError(error);
    setSummary(null);
    if (error !== undefined) {
      return;
    }
    setSubmitting(true);
    try {
      await updateNickname(value.trim());
      onUpdated();
      setSavedNote(true);
    } catch (cause) {
      setSummary(
        isApiError(cause)
          ? describeAuthError(cause)
          : { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null },
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="panel form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="staff-nickname"
        label="修改昵称"
        counter={`${count}/${NICKNAME_MAX_GRAPHEME_CLUSTERS}`}
        counterOver={count > NICKNAME_MAX_GRAPHEME_CLUSTERS}
        hint="展示给同事和学生的名称；按显示字符计数，表情算 1 个"
        error={fieldError}
        inputProps={{
          value,
          onChange: (event) => {
            setValue(event.target.value);
            if (fieldError !== undefined) {
              setFieldError(undefined);
            }
          },
          type: "text",
          autoComplete: "nickname",
          disabled: submitting,
        }}
      />
      {savedNote ? (
        <p className="alert alert-success" role="status">
          昵称已更新。
        </p>
      ) : null}
      <SubmitButton loading={submitting}>保存昵称</SubmitButton>
    </form>
  );
}

/**
 * The staff phone BINDING flow (login-iteration item ①): the two-step
 * /me/phone/bind family — challenge the phone, confirm with its SMS
 * code; first bind and rebind walk the SAME steps. No password
 * re-auth (the staff session itself is the authority — the email
 * binding's precedent; students keep the password-gated change
 * family, their phone is the register-time identity). A phone owned
 * by any other account answers 409 PHONE_ALREADY_BOUND — the shared
 * copy family lands on the phone field.
 */
function PhoneBindSection({
  me,
  onUpdated,
}: {
  me: { phone_e164: string | null };
  onUpdated: () => void;
}) {
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<ChallengeView | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{ phone?: string; code?: string }>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const { remaining, start } = useCooldown();

  /** Step 1: challenge the phone (also the resend path). */
  async function sendChallenge(validate: boolean) {
    setNote(null);
    if (validate) {
      const error = validatePhone(phone) ?? undefined;
      setFieldErrors({ phone: error });
      setSummary(null);
      if (error !== undefined) {
        return;
      }
    }
    setRequesting(true);
    try {
      const next = await requestPhoneBind(phone.trim());
      setChallenge(next);
      setStep("confirm");
      start(60);
      setNote("验证码已发送到该手机号，请查收短信");
    } catch (cause) {
      if (isApiError(cause) && cause.code === "OTP_RESEND_COOLDOWN") {
        start(60);
        setNote("发送过于频繁，请等待倒计时结束后重试");
        return;
      }
      const view = isApiError(cause)
        ? describeAuthError(cause)
        : { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
      setSummary(view);
      setFieldErrors({
        phone: (view.fieldErrors as Record<string, string | undefined>).phone,
      });
    } finally {
      setRequesting(false);
    }
  }

  /** Step 2: the SMS code completes the binding. */
  async function onConfirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const current = challenge;
    if (current === null) {
      return;
    }
    const error = validateOtpCode(code);
    setFieldErrors(error === null ? {} : { code: error });
    setSummary(null);
    if (error !== null) {
      return;
    }
    setSubmitting(true);
    try {
      await confirmPhoneBind(current.challenge_id, code);
      onUpdated();
      setChallenge(null);
      setStep("request");
      setPhone("");
      setCode("");
      setNote(me.phone_e164 === null ? "手机号已绑定。" : "手机号已更换。");
    } catch (cause) {
      // A dead challenge sends the user back to step 1; a wrong code
      // keeps it (the shared OTP semantics, identity/otp.py).
      if (isApiError(cause) && OTP_CHALLENGE_RESET_CODES.has(cause.code)) {
        setChallenge(null);
        setStep("request");
        setCode("");
        setNote("验证码已失效，请重新获取。");
        return;
      }
      const view = isApiError(cause)
        ? describeAuthError(cause)
        : { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
      setSummary(view);
      setFieldErrors({
        code: (view.fieldErrors as Record<string, string | undefined>).code,
        phone: (view.fieldErrors as Record<string, string | undefined>).phone,
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SettingsSection
      title="手机号"
      hint="绑定后可使用手机验证码登录员工账号；无需验证密码，短信验证码即证明持有。"
    >
      <div className="panel form">
        <p className="field-hint">当前手机号：{me.phone_e164 ?? "未绑定"}</p>
        {step === "request" ? (
          <form
            className="form"
            onSubmit={(event) => {
              event.preventDefault();
              void sendChallenge(true);
            }}
            noValidate
          >
            <FormErrorSummary view={summary} />
            <AuthField
              name="staff-bind-phone"
              label={me.phone_e164 === null ? "绑定手机号" : "新手机号"}
              hint="验证码将发送到该手机号；一个手机号只能绑定一个账号。"
              error={fieldErrors.phone}
              inputProps={{
                value: phone,
                onChange: (event) => setPhone(event.target.value),
                type: "tel",
                inputMode: "tel",
                autoComplete: "tel",
                disabled: requesting,
              }}
            />
            <SubmitButton loading={requesting}>发送验证码</SubmitButton>
          </form>
        ) : (
          <form className="form" onSubmit={onConfirm} noValidate>
            <FormErrorSummary view={summary} />
            <AuthField
              name="staff-bind-code"
              label="短信验证码"
              hint="验证码已发送到上方手机号（5 分钟内有效）。"
              error={fieldErrors.code}
              inputProps={{
                value: code,
                onChange: (event) => setCode(event.target.value),
                type: "text",
                inputMode: "numeric",
                autoComplete: "one-time-code",
                placeholder: "6 位数字",
                maxLength: 6,
                disabled: submitting,
              }}
            />
            <SubmitButton loading={submitting}>
              {me.phone_e164 === null ? "绑定手机号" : "更换手机号"}
            </SubmitButton>
            <p className="field-hint">
              <Button
                variant="secondary"
                onClick={() => void sendChallenge(false)}
                disabled={remaining > 0 || requesting}
              >
                {remaining > 0 ? `重新发送（${remaining} 秒）` : "重新发送验证码"}
              </Button>
            </p>
          </form>
        )}
        {note !== null ? (
          <p className="alert alert-success" role="status">
            {note}
          </p>
        ) : null}
      </div>
    </SettingsSection>
  );
}
