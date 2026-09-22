"use client";
/**
 * Account settings island (spec §5.3-§5.6, §40; patterns §6): nickname
 * change, phone change (re-auth password + new-phone OTP, spec §5.4),
 * email bind/verify/unbind (§5.5), and password change (§5.6).
 *
 * Re-auth semantics per the backend (identity/profile_router.py): the
 * CURRENT password travels on the phone-change REQUEST, on email
 * UNBIND, and as `current_password` on rotation — never on the OTP
 * confirm or the email verify. A wrong re-auth password answers
 * AUTHENTICATION_REQUIRED; the settings forms render that next to the
 * password field with copy that fits this surface (not the login line).
 *
 * Contact values render here and ONLY here: MePublic carries them for
 * the authenticated owner's own settings (spec §40).
 *
 * Validation mirrors the backend bands by REUSING the T2 validators
 * (`validation.ts`); the backend stays the authority. Mutations
 * returning MePublic feed `useSession().refresh()` so the shell's
 * nickname/identity stays in step.
 */
import { useState, type FormEvent, type ReactNode } from "react";

import { isApiError } from "@/lib/errors";

import {
  confirmEmailVerification,
  confirmPhoneChange,
  changePassword,
  requestEmailVerification,
  requestPhoneChange,
  unbindEmail,
  updateNickname,
  type ChallengeView,
} from "@/features/auth/api";
import { AuthField } from "@/features/auth/AuthField";
import { FormErrorSummary } from "@/features/auth/FormErrorSummary";
import { SubmitButton } from "@/features/auth/SubmitButton";
import { useSession } from "@/features/auth/session";
import { useCooldown } from "@/features/auth/useCooldown";
import {
  describeAuthError,
  NETWORK_ERROR_TEXT,
  OTP_CHALLENGE_RESET_CODES,
  withFieldErrorSummary,
  type AuthErrorView,
  type AuthFieldName,
} from "@/features/auth/errors";
import {
  countGraphemes,
  NICKNAME_MAX_GRAPHEME_CLUSTERS,
  validateNickname,
  validateOtpCode,
  validatePassword,
  validatePhone,
} from "@/features/auth/validation";
import {
  emailPanelView,
  validatePasswordConfirmation,
} from "@/features/auth/accountView";

/** Re-auth failure copy for the settings forms (login copy doesn't fit). */
const REAUTH_ERROR_TEXT = "当前密码不正确，请重新输入";
const CURRENT_PASSWORD_HINT = "为了安全，敏感操作需要先验证当前密码。";

type FieldErrors = Partial<Record<AuthFieldName, string>>;

/** Field keys this surface renders (the account-settings subset). */
type SettingsField = Extract<
  AuthFieldName,
  | "nickname"
  | "password"
  | "new_phone"
  | "code"
  | "email"
  | "token"
  | "current_password"
  | "new_password"
>;

/**
 * Render one failed settings mutation. `reauthField` is where a wrong
 * current password lands on this particular form.
 */
function toErrorView(
  error: unknown,
  reauthField: SettingsField,
): AuthErrorView {
  if (!isApiError(error)) {
    return { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
  }
  if (error.code === "AUTHENTICATION_REQUIRED") {
    return withFieldErrorSummary({
      summary: null,
      fieldErrors: { [reauthField]: REAUTH_ERROR_TEXT },
      requestId: null,
    });
  }
  return withFieldErrorSummary(describeAuthError(error));
}

/** Keep only the field texts this form renders. */
function pickFields(
  view: AuthErrorView,
  fields: readonly SettingsField[],
): FieldErrors {
  const picked: FieldErrors = {};
  for (const field of fields) {
    const text = view.fieldErrors[field];
    if (text !== undefined) {
      picked[field] = text;
    }
  }
  return picked;
}

function SettingsSection({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="section" aria-label={title}>
      <div className="section-head">
        <h2 className="section-title">{title}</h2>
      </div>
      {hint !== undefined ? <p className="field-hint">{hint}</p> : null}
      {children}
    </section>
  );
}

export function AccountSettings() {
  const { state, refresh } = useSession();
  if (state.status !== "authenticated") {
    return null; // The shell gates the whole group on the session.
  }
  const me = state.me;

  return (
    <>
      <NicknameForm me={me} onUpdated={refresh} />
      <PhoneChangeForm me={me} onUpdated={refresh} />
      <EmailPanel me={me} onUpdated={refresh} />
      <PasswordForm onUpdated={refresh} />
    </>
  );
}

// --- nickname -------------------------------------------------------------------------

function NicknameForm({
  me,
  onUpdated,
}: {
  me: { nickname: string };
  onUpdated: () => void;
}) {
  const [value, setValue] = useState(me.nickname);
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);

  const count = countGraphemes(value);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavedNote(null);
    const error = validateNickname(value);
    setFieldErrors(error === null ? {} : { nickname: error });
    setSummary(null);
    if (error !== null) {
      return;
    }
    setSubmitting(true);
    try {
      await updateNickname(value.trim());
      onUpdated();
      setSavedNote("昵称已更新。");
    } catch (cause) {
      setSummary(toErrorView(cause, "password"));
      setFieldErrors({});
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SettingsSection title="昵称">
      <form className="panel form" onSubmit={onSubmit} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="nickname"
          label="昵称"
          counter={`${count}/${NICKNAME_MAX_GRAPHEME_CLUSTERS}`}
          counterOver={count > NICKNAME_MAX_GRAPHEME_CLUSTERS}
          hint="展示给其他同学的名称；按显示字符计数，emoji 算 1 个。"
          error={fieldErrors.nickname}
          inputProps={{
            value,
            onChange: (event) => setValue(event.target.value),
            type: "text",
            autoComplete: "nickname",
            disabled: submitting,
          }}
        />
        {savedNote !== null ? (
          <p className="alert alert-success" role="status">
            {savedNote}
          </p>
        ) : null}
        <SubmitButton loading={submitting}>保存昵称</SubmitButton>
      </form>
    </SettingsSection>
  );
}

// --- phone change (re-auth + new-phone OTP, spec §5.4) --------------------------------

function PhoneChangeForm({
  me,
  onUpdated,
}: {
  me: { phone_e164: string | null };
  onUpdated: () => void;
}) {
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [password, setPassword] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<ChallengeView | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const { remaining, start } = useCooldown();

  function backToRequest(message: string) {
    setChallenge(null);
    setStep("request");
    setCode("");
    setNote(message);
  }

  /** Step 1: re-auth + challenge the NEW phone (also the resend path). */
  async function sendChallenge(validate: boolean) {
    setNote(null);
    if (validate) {
      const errors: FieldErrors = {
        password:
          password.length === 0
            ? "请输入当前密码"
            : (validatePassword(password) ?? undefined),
        new_phone: validatePhone(newPhone) ?? undefined,
      };
      setFieldErrors(errors);
      setSummary(null);
      if (Object.values(errors).some((message) => message !== undefined)) {
        return;
      }
    }
    setRequesting(true);
    try {
      const next = await requestPhoneChange(password, newPhone.trim());
      setChallenge(next);
      setStep("confirm");
      start(60);
      setNote("验证码已发送到新手机号，请查收短信。");
    } catch (cause) {
      if (isApiError(cause) && cause.code === "OTP_RESEND_COOLDOWN") {
        start(60);
        setNote("发送过于频繁，请等待倒计时结束后重试。");
        return;
      }
      const view = toErrorView(cause, "password");
      setSummary(view);
      setFieldErrors(pickFields(view, ["password", "new_phone"]));
    } finally {
      setRequesting(false);
    }
  }

  /** Step 2: the NEW phone's OTP completes the change. */
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
      await confirmPhoneChange(current.challenge_id, code);
      onUpdated();
      setChallenge(null);
      setStep("request");
      setPassword("");
      setNewPhone("");
      setCode("");
      setNote("手机号已更新。");
    } catch (cause) {
      // A dead challenge sends the user back to step 1; a wrong code
      // keeps it (backend OTP semantics, identity/otp.py).
      if (isApiError(cause) && OTP_CHALLENGE_RESET_CODES.has(cause.code)) {
        backToRequest("验证码已失效，请重新获取。");
        return;
      }
      const view = toErrorView(cause, "password");
      setSummary(view);
      setFieldErrors(pickFields(view, ["code", "new_phone", "password"]));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SettingsSection title="手机号" hint={CURRENT_PASSWORD_HINT}>
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
              name="phone-password"
              label="当前密码"
              error={fieldErrors.password}
              inputProps={{
                value: password,
                onChange: (event) => setPassword(event.target.value),
                type: "password",
                autoComplete: "current-password",
                disabled: requesting,
              }}
            />
            <AuthField
              name="new_phone"
              label="新手机号"
              hint="验证码将发送到新手机号；更换完成后以新手机号为准。"
              error={fieldErrors.new_phone}
              inputProps={{
                value: newPhone,
                onChange: (event) => setNewPhone(event.target.value),
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
              name="phone-code"
              label="短信验证码"
              hint="6 位数字，发送到新手机号。"
              error={fieldErrors.code}
              inputProps={{
                value: code,
                onChange: (event) => setCode(event.target.value),
                type: "text",
                inputMode: "numeric",
                autoComplete: "one-time-code",
                maxLength: 6,
                disabled: submitting,
              }}
            />
            <SubmitButton loading={submitting}>确认更换手机号</SubmitButton>
            <button
              type="button"
              className="btn btn-secondary btn-block"
              onClick={() => void sendChallenge(true)}
              disabled={remaining > 0 || requesting}
            >
              {remaining > 0 ? `重新发送（${remaining} 秒）` : "重新发送验证码"}
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-block"
              onClick={() => backToRequest("已取消，可重新填写。")}
              disabled={submitting}
            >
              取消更换
            </button>
          </form>
        )}
        {note !== null ? (
          <p className="field-hint" role="status">
            {note}
          </p>
        ) : null}
      </div>
    </SettingsSection>
  );
}

// --- email bind / verify / unbind (spec §5.5) ------------------------------------------

function validateEmail(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "请输入邮箱地址";
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
    return "请输入正确的邮箱地址";
  }
  return null;
}

function EmailPanel({
  me,
  onUpdated,
}: {
  me: { email_normalized: string | null; email_verified_at: string | null };
  onUpdated: () => void;
}) {
  const view = emailPanelView(me);
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [unbinding, setUnbinding] = useState(false);
  const [unbindOpen, setUnbindOpen] = useState(false);
  const [unbindPassword, setUnbindPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function onSend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNote(null);
    const error = validateEmail(email);
    setFieldErrors(error === null ? {} : { email: error });
    setSummary(null);
    if (error !== null) {
      return;
    }
    setSending(true);
    try {
      await requestEmailVerification(email.trim());
      setNote("验证邮件已发送，请到邮箱查收验证码。");
    } catch (cause) {
      const errorView = toErrorView(cause, "email");
      setSummary(errorView);
      setFieldErrors(pickFields(errorView, ["email"]));
    } finally {
      setSending(false);
    }
  }

  async function onVerify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNote(null);
    const error =
      token.trim().length === 0 ? "请输入邮件中的验证码" : null;
    setFieldErrors(error === null ? {} : { token: error });
    setSummary(null);
    if (error !== null) {
      return;
    }
    setVerifying(true);
    try {
      await confirmEmailVerification(token.trim());
      onUpdated();
      setEmail("");
      setToken("");
      setNote("邮箱已绑定并验证。");
    } catch (cause) {
      const errorView = toErrorView(cause, "token");
      setSummary(errorView);
      setFieldErrors(pickFields(errorView, ["token", "email"]));
    } finally {
      setVerifying(false);
    }
  }

  async function onUnbind(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNote(null);
    const error =
      unbindPassword.length === 0
        ? "请输入当前密码"
        : (validatePassword(unbindPassword) ?? null);
    setFieldErrors(error === null ? {} : { current_password: error });
    setSummary(null);
    if (error !== null) {
      return;
    }
    setUnbinding(true);
    try {
      await unbindEmail(unbindPassword);
      onUpdated();
      setUnbindOpen(false);
      setUnbindPassword("");
      setNote("邮箱已解绑。");
    } catch (cause) {
      const errorView = toErrorView(cause, "current_password");
      setSummary(errorView);
      setFieldErrors(pickFields(errorView, ["current_password"]));
    } finally {
      setUnbinding(false);
    }
  }

  return (
    <SettingsSection title="邮箱" hint={view.hint}>
      <div className="panel form">
        <p className="field-hint">
          当前邮箱：{view.email ?? "未绑定"}{" "}
          <span
            className={
              view.state === "verified"
                ? "badge badge-success"
                : view.state === "unverified"
                  ? "badge badge-warning"
                  : "badge"
            }
          >
            {view.statusLabel}
          </span>
        </p>

        {view.state !== "verified" ? (
          <>
            <form className="form" onSubmit={onSend} noValidate>
              <FormErrorSummary view={summary} />
              <AuthField
                name="email"
                label={view.state === "unverified" ? "重新绑定邮箱" : "绑定邮箱"}
                error={fieldErrors.email}
                inputProps={{
                  value: email,
                  onChange: (event) => setEmail(event.target.value),
                  type: "email",
                  inputMode: "email",
                  autoComplete: "email",
                  disabled: sending,
                }}
              />
              <SubmitButton loading={sending} variant="secondary">
                发送验证邮件
              </SubmitButton>
            </form>
            <form className="form" onSubmit={onVerify} noValidate>
              <FormErrorSummary view={summary} />
              <AuthField
                name="token"
                label="邮箱验证码"
                hint="邮件中的验证码；未收到可重新发送验证邮件。"
                error={fieldErrors.token}
                inputProps={{
                  value: token,
                  onChange: (event) => setToken(event.target.value),
                  type: "text",
                  autoComplete: "one-time-code",
                  disabled: verifying,
                }}
              />
              <SubmitButton loading={verifying}>验证并绑定</SubmitButton>
            </form>
          </>
        ) : unbindOpen ? (
          <form className="form" onSubmit={onUnbind} noValidate>
            <FormErrorSummary view={summary} />
            <AuthField
              name="current_password"
              label="当前密码"
              error={fieldErrors.current_password}
              inputProps={{
                value: unbindPassword,
                onChange: (event) => setUnbindPassword(event.target.value),
                type: "password",
                autoComplete: "current-password",
                disabled: unbinding,
              }}
            />
            <SubmitButton loading={unbinding}>确认解绑邮箱</SubmitButton>
            <button
              type="button"
              className="btn btn-secondary btn-block"
              onClick={() => setUnbindOpen(false)}
              disabled={unbinding}
            >
              取消
            </button>
          </form>
        ) : (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setUnbindOpen(true)}
          >
            解绑邮箱
          </button>
        )}
        {note !== null ? (
          <p className="field-hint" role="status">
            {note}
          </p>
        ) : null}
      </div>
    </SettingsSection>
  );
}

// --- password change (spec §5.6) --------------------------------------------------------

function PasswordForm({ onUpdated }: { onUpdated: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNote(null);
    const currentError = current.length === 0 ? "请输入当前密码" : null;
    const newError = validatePassword(next);
    const matchError = validatePasswordConfirmation(next, confirmation);
    setFieldErrors({
      ...(currentError !== null ? { current_password: currentError } : {}),
      ...(newError !== null ? { new_password: newError } : {}),
    });
    setConfirmError(matchError);
    setSummary(null);
    if (currentError !== null || newError !== null || matchError !== null) {
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(current, next);
      onUpdated();
      setCurrent("");
      setNext("");
      setConfirmation("");
      setNote("密码已更新；其他设备上的登录已全部退出，当前登录保持。");
    } catch (cause) {
      const errorView = toErrorView(cause, "current_password");
      setSummary(errorView);
      setFieldErrors(pickFields(errorView, ["current_password", "new_password"]));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SettingsSection title="密码" hint="修改密码需要验证当前密码。">
      <form className="panel form" onSubmit={onSubmit} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="current_password"
          label="当前密码"
          error={fieldErrors.current_password}
          inputProps={{
            value: current,
            onChange: (event) => setCurrent(event.target.value),
            type: "password",
            autoComplete: "current-password",
            disabled: submitting,
          }}
        />
        <AuthField
          name="new_password"
          label="新密码"
          hint="10-128 个字符，无其他限制。"
          error={fieldErrors.new_password}
          inputProps={{
            value: next,
            onChange: (event) => setNext(event.target.value),
            type: "password",
            autoComplete: "new-password",
            disabled: submitting,
          }}
        />
        <AuthField
          name="new_password_confirm"
          label="确认新密码"
          error={confirmError}
          inputProps={{
            value: confirmation,
            onChange: (event) => setConfirmation(event.target.value),
            type: "password",
            autoComplete: "new-password",
            disabled: submitting,
          }}
        />
        {note !== null ? (
          <p className="alert alert-success" role="status">
            {note}
          </p>
        ) : null}
        <SubmitButton loading={submitting}>更新密码</SubmitButton>
      </form>
    </SettingsSection>
  );
}
