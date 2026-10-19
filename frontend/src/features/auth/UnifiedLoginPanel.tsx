"use client";
/**
 * The unified login island (unified-login spec §4): ONE page,
 * progressive disclosure — the layers appear only when their
 * condition holds:
 *
 *   0  the remembered one-click banner (no cookie → no element);
 *   1  手机号或邮箱 identifier + 获取验证码 + the password weak link;
 *   1a the password expansion — the identifier widens to the
 *      three-state band (手机号、邮箱或学号);
 *   2  the code inputs, after the first 获取验证码;
 *   2a the ADMIN TOTP leg (totp_required — the checkbox's meaning
 *      lives HERE; the code leg a TEACHER completes never sends a
 *      sole-factor exemption, the 2026-10-15 terminal ruling);
 *   2b the unregistered phone's inline register (register_token).
 *
 * Every wire call rides the frozen challenge family (§3); the landing
 * is role-collected through the readMe seam (the role is only
 * knowable from the session's own answer, never from the pre-login
 * state).
 */
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { useSession } from "@/features/auth/session";
import { COPY_GENERIC_FAIL, isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FastLoginHint } from "./FastLoginHint";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import {
  loginWithPassword,
  readMe,
  requestUnifiedChallenge,
  unifiedChallengeTotp,
  verifyUnifiedChallenge,
} from "./api";
import { registerViaMobileCode } from "./mobileLoginApi";
import {
  NETWORK_ERROR_TEXT,
  describeAuthError,
  resendButtonLabel,
  withFieldErrorSummary,
  type AuthErrorView,
} from "./errors";
import { landAfterStaffLogin } from "./staffLoginFlow";
import { useCooldown } from "./useCooldown";
import { validatePassword, validateStudentNumber } from "./validation";
import {
  describeChallengeError,
  validateChallengeCode,
  validateChallengeIdentifier,
  validatePasswordIdentifier,
} from "./unifiedLoginView";

const RESEND_COOLDOWN_SECONDS = 60;

export function UnifiedLoginPanel() {
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [registerToken, setRegisterToken] = useState<string | null>(null);

  // Layers 2a/2b replace the form wholesale (the family's pattern) —
  // the back links hand the identifier flow back untouched.
  if (pendingToken !== null) {
    return <TotpLeg pendingToken={pendingToken} onBack={() => setPendingToken(null)} />;
  }
  if (registerToken !== null) {
    return <InlineRegister token={registerToken} onReset={() => setRegisterToken(null)} />;
  }
  return (
    <>
      <FastLoginHint onTotpRequired={setPendingToken} />
      <IdentifierForm onTotpRequired={setPendingToken} onUnregistered={setRegisterToken} />
    </>
  );
}

/** The shared landing: readMe answers the role, the role answers the path. */
function useLanding() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  return async function land(): Promise<void> {
    await landAfterStaffLogin({
      login: async () => undefined,
      refreshSession,
      router,
      readMe,
    });
  };
}

function IdentifierForm({
  onTotpRequired,
  onUnregistered,
}: {
  onTotpRequired: (pendingToken: string) => void;
  onUnregistered: (registerToken: string) => void;
}) {
  const land = useLanding();
  const cooldown = useCooldown();
  const [mode, setMode] = useState<"code" | "password">("code");
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [rememberDevice, setRememberDevice] = useState(false);
  const [identifierError, setIdentifierError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [codeError, setCodeError] = useState<string | undefined>(undefined);
  const [sentNote, setSentNote] = useState(false);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);

  function switchMode() {
    // The bands differ per mode; re-validate on the next submit. The
    // typed values ride along — a toggle never loses input.
    setSummary(null);
    setIdentifierError(undefined);
    setMode(mode === "code" ? "password" : "code");
  }

  /** The send-note renders by the SUBMITTED shape (spec §3.1): the
   * client never learns which stream answered, only what it sent. */
  function sentNoteCopy(): string {
    return identifier.includes("@")
      ? "验证码已发送，请查收邮件（5 分钟内有效）。"
      : "验证码已发送，请查收短信（5 分钟内有效）。";
  }

  async function onSendCode() {
    if (sending || cooldown.remaining > 0) {
      return;
    }
    setSummary(null);
    setSentNote(false);
    const error = validateChallengeIdentifier(identifier) ?? undefined;
    setIdentifierError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      // 202 always — anti-enumeration: the response never tells which
      // identifiers exist.
      await requestUnifiedChallenge(identifier.trim());
      cooldown.start(RESEND_COOLDOWN_SECONDS);
      setCodeSent(true);
      setSentNote(true);
    } catch (cause) {
      setSummary(toChallengeSummary(cause));
    } finally {
      setSending(false);
    }
  }

  async function onVerifyCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    const errors = {
      identifier: validateChallengeIdentifier(identifier) ?? undefined,
      code: validateChallengeCode(code) ?? undefined,
    };
    setIdentifierError(errors.identifier);
    setCodeError(errors.code);
    if (errors.identifier !== undefined || errors.code !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      const outcome = await verifyUnifiedChallenge(
        identifier.trim(),
        code.trim(),
        rememberDevice,
      );
      if (outcome.kind === "totp_required") {
        onTotpRequired(outcome.pendingToken);
        return;
      }
      if (outcome.kind === "unregistered") {
        // The register leg owns its own login — nothing was adopted.
        onUnregistered(outcome.registerToken);
        return;
      }
      await land();
    } catch (cause) {
      setSummary(toChallengeSummary(cause));
    } finally {
      setVerifying(false);
    }
  }

  async function onPasswordLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    const errors = {
      identifier: validatePasswordIdentifier(identifier) ?? undefined,
      password: validatePassword(password) ?? undefined,
    };
    setIdentifierError(errors.identifier);
    setPasswordError(errors.password);
    if (errors.identifier !== undefined || errors.password !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      // The student number is a legal password identifier; an ADMIN
      // password answers totp_required — the second leg rides 2a.
      const outcome = await loginWithPassword(
        identifier.trim(),
        password,
        rememberDevice,
      );
      if (outcome.kind === "totp_required") {
        onTotpRequired(outcome.pendingToken);
        return;
      }
      await land();
    } catch (cause) {
      if (!isApiError(cause)) {
        setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
        return;
      }
      const view = withFieldErrorSummary(describeAuthError(cause));
      setSummary(view);
      setPasswordError(view.fieldErrors.password);
    } finally {
      setVerifying(false);
    }
  }

  function toChallengeSummary(cause: unknown): AuthErrorView {
    const view = describeChallengeError(cause, COPY_GENERIC_FAIL);
    return { summary: view.message, fieldErrors: {}, requestId: view.requestId };
  }

  const inPasswordMode = mode === "password";

  return (
    <>
      {sentNote && !inPasswordMode ? (
        <p className="alert alert-success" role="status">
          {sentNoteCopy()}
        </p>
      ) : null}
      {inPasswordMode ? (
        <form className="form" onSubmit={onPasswordLogin} noValidate>
          <FormErrorSummary view={summary} />
          <AuthField
            name="unified-login-identifier"
            label="手机号、邮箱或学号"
            error={identifierError}
            inputProps={{
              value: identifier,
              onChange: (event) => {
                setIdentifier(event.target.value);
                if (identifierError !== undefined) {
                  setIdentifierError(undefined);
                }
              },
              type: "text",
              autoComplete: "username",
              autoCapitalize: "none",
              autoCorrect: "off",
              placeholder: "请输入手机号、邮箱或学号",
              disabled: verifying,
            }}
          />
          <AuthField
            name="unified-login-password"
            label="密码"
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
              autoComplete: "current-password",
              placeholder: "请输入密码",
              disabled: verifying,
            }}
          />
          <label className="field-hint">
            <input
              type="checkbox"
              checked={rememberDevice}
              onChange={(event) => setRememberDevice(event.target.checked)}
              disabled={verifying}
            />{" "}
            记住此设备 30 天（对需要动态口令的账号生效）
          </label>
          <SubmitButton loading={verifying}>登录</SubmitButton>
        </form>
      ) : (
        <form className="form" onSubmit={onVerifyCode} noValidate>
          <FormErrorSummary view={summary} />
          <AuthField
            name="unified-login-identifier"
            label="手机号或邮箱"
            hint="将向该手机号或邮箱发送验证码"
            error={identifierError}
            inputProps={{
              value: identifier,
              onChange: (event) => {
                setIdentifier(event.target.value);
                if (identifierError !== undefined) {
                  setIdentifierError(undefined);
                }
              },
              type: "text",
              autoComplete: "username",
              autoCapitalize: "none",
              autoCorrect: "off",
              placeholder: "请输入手机号或邮箱",
              disabled: sending || verifying,
            }}
          />
          {codeSent ? (
            <>
              <div className="field">
                <label className="field-label" htmlFor="unified-login-code">
                  验证码
                </label>
                <div className="otp-row">
                  <input
                    id="unified-login-code"
                    className="input"
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    autoCapitalize="none"
                    autoCorrect="off"
                    placeholder="6 位数字"
                    maxLength={6}
                    value={code}
                    onChange={(event) => {
                      setCode(event.target.value);
                      if (codeError !== undefined) {
                        setCodeError(undefined);
                      }
                    }}
                    aria-invalid={codeError !== undefined || undefined}
                    aria-describedby={
                      codeError !== undefined ? "unified-login-code-error" : undefined
                    }
                    disabled={verifying}
                  />
                  <Button
                    variant="secondary"
                    onClick={() => void onSendCode()}
                    disabled={sending || cooldown.remaining > 0 || verifying}
                    aria-busy={sending}
                  >
                    {sending ? <span className="spinner" aria-hidden="true" /> : null}
                    <span>{resendButtonLabel(cooldown.remaining, "重新发送", false)}</span>
                  </Button>
                </div>
                {codeError !== undefined ? (
                  <p className="field-error" id="unified-login-code-error">
                    {codeError}
                  </p>
                ) : null}
              </div>
              <label className="field-hint">
                <input
                  type="checkbox"
                  checked={rememberDevice}
                  onChange={(event) => setRememberDevice(event.target.checked)}
                  disabled={verifying}
                />{" "}
                记住此设备 30 天
              </label>
              <SubmitButton loading={verifying}>登录</SubmitButton>
            </>
          ) : (
            <Button
              onClick={() => void onSendCode()}
              disabled={sending || cooldown.remaining > 0}
              aria-busy={sending}
              block
            >
              {sending ? <span className="spinner" aria-hidden="true" /> : null}
              <span>获取验证码</span>
            </Button>
          )}
        </form>
      )}
      <p className="auth-alt-action">
        <button
          type="button"
          className="link"
          onClick={switchMode}
          aria-expanded={inPasswordMode}
        >
          {inPasswordMode ? "使用验证码登录" : "使用密码登录"}
        </button>
      </p>
    </>
  );
}

/** Layer 2a: the ADMIN second leg (spec §4). */
function TotpLeg({
  pendingToken,
  onBack,
}: {
  pendingToken: string;
  onBack: () => void;
}) {
  const land = useLanding();
  const [totpCode, setTotpCode] = useState("");
  const [totpError, setTotpError] = useState<string | undefined>(undefined);
  const [rememberDevice, setRememberDevice] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);

  async function onTotp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    const error = validateChallengeCode(totpCode.trim()) ?? undefined;
    setTotpError(error);
    if (error !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      // The checkbox's meaning lives HERE (the ADMIN second leg) —
      // this is the only leg remember_device may ever exempt.
      await unifiedChallengeTotp(pendingToken, totpCode.trim(), rememberDevice);
      await land();
    } catch (cause) {
      const view = describeChallengeError(cause, "登录失败，请稍后重试");
      setSummary({ summary: view.message, fieldErrors: {}, requestId: view.requestId });
    } finally {
      setVerifying(false);
    }
  }

  return (
    <form className="form" onSubmit={onTotp} noValidate>
      <FormErrorSummary view={summary} />
      <p className="field-hint" data-testid="unified-login-totp-leg">
        该账号需要动态口令验证：请输入验证器应用当前显示的 6 位数字。
      </p>
      <AuthField
        name="unified-login-totp-code"
        label="动态验证码"
        error={totpError}
        inputProps={{
          value: totpCode,
          onChange: (event) => {
            setTotpCode(event.target.value);
            if (totpError !== undefined) {
              setTotpError(undefined);
            }
          },
          type: "text",
          inputMode: "numeric",
          autoComplete: "one-time-code",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "6 位数字",
          maxLength: 6,
          disabled: verifying,
        }}
      />
      <label className="field-hint">
        <input
          type="checkbox"
          checked={rememberDevice}
          onChange={(event) => setRememberDevice(event.target.checked)}
          disabled={verifying}
        />{" "}
        记住此设备 30 天（下次登录可免动态口令）
      </label>
      <SubmitButton loading={verifying}>完成登录</SubmitButton>
      <p className="auth-alt-action">
        <Button variant="ghost" onClick={onBack} disabled={verifying}>
          返回登录
        </Button>
      </p>
    </form>
  );
}

/** Layer 2b: the unregistered phone's inline register (spec §4; the
 * register_token + /auth/mobile-code/register semantics, unchanged). */
function InlineRegister({
  token,
  onReset,
}: {
  token: string;
  onReset: () => void;
}) {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [studentNumber, setStudentNumber] = useState("");
  const [password, setPassword] = useState("");
  const [studentNumberError, setStudentNumberError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);

  async function onRegister(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    const errors = {
      student_number: validateStudentNumber(studentNumber) ?? undefined,
      password:
        password.length === 0 ? undefined : (validatePassword(password) ?? undefined),
    };
    setStudentNumberError(errors.student_number);
    setPasswordError(errors.password);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }
    setVerifying(true);
    try {
      await registerViaMobileCode(
        token,
        studentNumber.trim(),
        password.length === 0 ? null : password,
      );
      refreshSession();
      router.replace("/"); // registered AND logged in — straight home
      router.refresh();
    } catch (cause) {
      if (isApiError(cause) && cause.code === "VALIDATION_ERROR") {
        // The register-page discipline: a band-level validation
        // refusal keeps the (still-valid) token; anything else
        // consumes it — back to the identifier step for a fresh code.
        setSummary(toSummary(cause, "注册失败，请稍后重试"));
      } else {
        onReset();
        setSummary(toSummary(cause, "注册失败，请重新获取验证码"));
      }
    } finally {
      setVerifying(false);
    }
  }

  function toSummary(cause: unknown, fallback: string): AuthErrorView {
    if (!isApiError(cause)) {
      return { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
    }
    return { summary: cause.message || fallback, fieldErrors: {}, requestId: cause.requestId };
  }

  return (
    <form className="form" onSubmit={onRegister} noValidate>
      <FormErrorSummary view={summary} />
      <p className="field-hint">
        该手机号尚未注册。填写学号即可完成注册并直接登录（账号需管理员审核，审核前可浏览）。
      </p>
      <AuthField
        name="inline-register-student-number"
        label="学号"
        error={studentNumberError}
        inputProps={{
          value: studentNumber,
          onChange: (event) => {
            setStudentNumber(event.target.value);
            if (studentNumberError !== undefined) {
              setStudentNumberError(undefined);
            }
          },
          type: "text",
          inputMode: "numeric",
          autoComplete: "username",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "请输入学号",
          maxLength: 20,
          disabled: verifying,
        }}
      />
      <AuthField
        name="inline-register-password"
        label="密码（可选）"
        hint="10-128 个字符。留空则仅用手机验证码登录，之后可在设置中自行设置密码。"
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
          placeholder: "请设置密码",
          disabled: verifying,
        }}
      />
      <SubmitButton loading={verifying}>完成注册并登录</SubmitButton>
    </form>
  );
}
