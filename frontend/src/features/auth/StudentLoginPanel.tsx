"use client";
/**
 * The student login page's channel panel (auth-extension-II spec §4):
 * the MOBILE-CODE channel is the default (phone + SMS code + the 30-day
 * remember-device checkbox — a remembered phone skips the SMS cost on
 * the next login); the student-number + password form stays one visible
 * toggle away for accounts that set a password.
 *
 * The toggle mirrors StaffLoginPanel's shape (aria-pressed buttons, no
 * tablist promise). The password channel's 401 on a passwordless
 * account renders the mobile-guidance copy with a one-click switch
 * (describeMobileLoginError's suggestMobileTab).
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { useSession } from "@/features/auth/session";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { LoginForm } from "./LoginForm";
import {
  fastLoginCookieOnly,
  fetchRememberedHint,
  registerViaMobileCode,
  requestMobileLoginCode,
  verifyMobileLoginCode,
} from "./mobileLoginApi";
import {
  describeMobileLoginError,
  validateMobileCode,
  validateMobileLoginPhone,
} from "./mobileLoginView";
import { resendButtonLabel } from "./errors";
import { NETWORK_ERROR_TEXT, type AuthErrorView } from "./errors";
import { validatePassword, validateStudentNumber } from "./validation";
import { isApiError } from "@/lib/errors";

const RESEND_COOLDOWN_SECONDS = 60;

type Channel = "mobile" | "password";

const CHANNEL_LABELS: Record<Channel, string> = {
  mobile: "手机验证码登录",
  password: "密码登录",
};

export function StudentLoginPanel() {
  const [channel, setChannel] = useState<Channel>("mobile");

  return (
    <>
      <FastLoginHint />
      <div className="login-channel-toggle" role="group" aria-label="登录方式">
        {(["mobile", "password"] as const).map((option) => (
          <Button
            key={option}
            variant={channel === option ? "secondary" : "ghost"}
            aria-pressed={channel === option}
            onClick={() => setChannel(option)}
          >
            {CHANNEL_LABELS[option]}
          </Button>
        ))}
      </div>
      {channel === "mobile" ? (
        <MobileLoginForm onSwitchToPassword={() => setChannel("password")} />
      ) : (
        <LoginForm />
      )}
    </>
  );
}

function MobileLoginForm({
  onSwitchToPassword,
}: {
  onSwitchToPassword: () => void;
}) {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [rememberDevice, setRememberDevice] = useState(false);
  const [phoneError, setPhoneError] = useState<string | undefined>(undefined);
  const [codeError, setCodeError] = useState<string | undefined>(undefined);
  const [cooldown, setCooldown] = useState(0);
  const [sentNote, setSentNote] = useState(false);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [suggestMobile, setSuggestMobile] = useState(false);
  // The send-code-as-entry third step (2026-10-19): an unregistered
  // phone's verify hands the register token; the 学号+optional-password
  // form below completes the account AND the login — no register-page
  // detour anywhere in the copy.
  const [registerToken, setRegisterToken] = useState<string | null>(null);
  const [studentNumber, setStudentNumber] = useState("");
  const [registerPassword, setRegisterPassword] = useState("");
  const [studentNumberError, setStudentNumberError] = useState<string | undefined>(undefined);
  const [registerPasswordError, setRegisterPasswordError] = useState<string | undefined>(undefined);

  // The shared text-only countdown family (reduced-motion safe).
  useEffect(() => {
    if (cooldown === 0) {
      return;
    }
    const timer = setTimeout(() => setCooldown(cooldown - 1), 1_000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  async function onSendCode() {
    if (sending || cooldown > 0) {
      return;
    }
    setSummary(null);
    setSentNote(false);
    setSuggestMobile(false);
    const error = validateMobileLoginPhone(phone) ?? undefined;
    setPhoneError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      await requestMobileLoginCode(phone.trim());
      setCooldown(RESEND_COOLDOWN_SECONDS);
      setSentNote(true);
    } catch (cause) {
      setSummary(toSummary(cause, "验证码发送失败，请稍后重试"));
    } finally {
      setSending(false);
    }
  }

  async function onLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    setSuggestMobile(false);
    const errors = {
      phone: validateMobileLoginPhone(phone) ?? undefined,
      code: validateMobileCode(code) ?? undefined,
    };
    setPhoneError(errors.phone);
    setCodeError(errors.code);
    if (errors.phone !== undefined || errors.code !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      const outcome = await verifyMobileLoginCode(phone.trim(), code.trim(), rememberDevice);
      if (outcome.kind === "unregistered") {
        // The send-code-as-entry third step: hand the inline register
        // form the token (nothing adopted yet — the register leg owns
        // the login).
        setRegisterToken(outcome.registerToken);
        setStudentNumber("");
        setRegisterPassword("");
        return;
      }
      refreshSession();
      router.replace("/"); // the student home (the sole student landing)
      router.refresh();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
    } finally {
      setVerifying(false);
    }
  }

  /** The third step: 学号 (+ optional password) completes the account. */
  async function onInlineRegister(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = registerToken;
    if (verifying || token === null) {
      return;
    }
    setSummary(null);
    const errors = {
      student_number: validateStudentNumber(studentNumber) ?? undefined,
      password:
        registerPassword.length === 0
          ? undefined
          : (validatePassword(registerPassword) ?? undefined),
    };
    setStudentNumberError(errors.student_number);
    setRegisterPasswordError(errors.password);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }
    setVerifying(true);
    try {
      await registerViaMobileCode(
        token,
        studentNumber.trim(),
        registerPassword.length === 0 ? null : registerPassword,
      );
      refreshSession();
      router.replace("/"); // registered AND logged in — straight home
      router.refresh();
    } catch (cause) {
      if (isApiError(cause) && cause.code === "VALIDATION_ERROR") {
        // The register-page discipline: a band-level validation refusal
        // keeps the (still-valid) token; anything else consumes it —
        // back to the code step for a fresh SMS.
        setSummary(toSummary(cause, "注册失败，请稍后重试"));
      } else {
        setRegisterToken(null);
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
    const view = describeMobileLoginError(cause, fallback);
    setSuggestMobile(view.suggestMobileTab);
    return { summary: view.message, fieldErrors: {}, requestId: view.requestId };
  }

  return (
    <>
      {registerToken !== null ? (
        <form className="form" onSubmit={onInlineRegister} noValidate>
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
            error={registerPasswordError}
            inputProps={{
              value: registerPassword,
              onChange: (event) => {
                setRegisterPassword(event.target.value);
                if (registerPasswordError !== undefined) {
                  setRegisterPasswordError(undefined);
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
      ) : (
        <>
          {sentNote ? (
            <p className="alert alert-success" role="status">
              验证码已发送，请查收短信（5 分钟内有效）。
            </p>
          ) : null}
      <form className="form" onSubmit={onLogin} noValidate>
        <FormErrorSummary view={summary} />
        {suggestMobile ? (
          <p className="field-hint">
            <Button variant="ghost" onClick={onSwitchToPassword}>
              切换到密码登录
            </Button>
          </p>
        ) : null}
        <AuthField
          name="mobile-login-phone"
          label="手机号"
          error={phoneError}
          inputProps={{
            value: phone,
            onChange: (event) => {
              setPhone(event.target.value);
              if (phoneError !== undefined) {
                setPhoneError(undefined);
              }
            },
            type: "tel",
            inputMode: "tel",
            autoComplete: "tel",
            autoCapitalize: "none",
            autoCorrect: "off",
            placeholder: "注册时使用的手机号",
            disabled: sending || verifying,
          }}
        />
        <div className="field">
          <label className="field-label" htmlFor="mobile-login-code">
            短信验证码
          </label>
          <div className="otp-row">
            <input
              id="mobile-login-code"
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
              aria-describedby={codeError !== undefined ? "mobile-login-code-error" : undefined}
              disabled={verifying}
            />
            <Button
              variant="secondary"
              onClick={() => void onSendCode()}
              disabled={sending || cooldown > 0 || verifying}
              aria-busy={sending}
            >
              {sending ? <span className="spinner" aria-hidden="true" /> : null}
              <span>{resendButtonLabel(cooldown, "重新发送", false)}</span>
            </Button>
          </div>
          {codeError !== undefined ? (
            <p className="field-error" id="mobile-login-code-error">
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
          记住此设备 30 天（下次登录免发短信）
        </label>
        <SubmitButton loading={verifying}>登录</SubmitButton>
      </form>
        </>
      )}
    </>
  );
}

/**
 * The login page's CONDITIONAL fast-login banner (owner feedback ③,
 * 2026-10-17): one anonymous probe on mount decides everything — a
 * remembered STUDENT device answers its masked phone and the banner
 * offers the one-click continuation; every other answer renders
 * NOTHING (无 cookie 时登录页零多余元素 — the page is byte-identical to
 * the pre-banner login). A click rides the cookie-only verify leg; a
 * refusal (the probe's answer went stale mid-session) swaps the offer
 * for the honest code-form guidance, never an error state.
 */
function FastLoginHint() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [mask, setMask] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const hint = await fetchRememberedHint();
      if (!cancelled && hint !== null) {
        setMask(hint);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (mask === null) {
    return null; // the zero-extra-elements login page
  }

  if (stale) {
    return (
      <p className="field-hint login-fast-stale" role="status">
        该设备尚未记住或已过期，请使用下方短信验证码登录。
      </p>
    );
  }

  async function onContinue() {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      await fastLoginCookieOnly();
      refreshSession();
      router.replace("/"); // the student home (the sole student landing)
      router.refresh();
    } catch {
      setStale(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-fast-hint">
      <p>
        继续以 <span className="mono">{mask}</span> 登录
      </p>
      <Button
        variant="secondary"
        onClick={() => void onContinue()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy ? <span className="spinner" aria-hidden="true" /> : null}
        <span>一键继续</span>
      </Button>
    </div>
  );
}
