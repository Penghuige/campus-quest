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
import { requestMobileLoginCode, fastLoginRememberedDevice, verifyMobileLoginCode } from "./mobileLoginApi";
import {
  describeMobileLoginError,
  validateMobileCode,
  validateMobileLoginPhone,
} from "./mobileLoginView";
import { resendButtonLabel } from "./errors";
import { NETWORK_ERROR_TEXT, type AuthErrorView } from "./errors";
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
      await verifyMobileLoginCode(phone.trim(), code.trim(), rememberDevice);
      refreshSession();
      router.replace("/"); // the student home (the sole student landing)
      router.refresh();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
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
        {/* The honest-fallback fast path (the backend contract's no-code
         * verify): the cookie is HttpOnly so its presence is NOT
         * detectable — the button shows unconditionally, and a
         * cookie-less click simply falls back to this code form. */}
        <Button
          variant="ghost"
          block
          onClick={() => void onFastLogin()}
          disabled={verifying || sending}
        >
          快速登录（已记住的设备）
        </Button>
      </form>
    </>
  );

  async function onFastLogin() {
    if (verifying) {
      return;
    }
    setSummary(null);
    const error = validateMobileLoginPhone(phone) ?? undefined;
    setPhoneError(error);
    if (error !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      await fastLoginRememberedDevice(phone.trim());
      refreshSession();
      router.replace("/");
      router.refresh();
    } catch {
      // No trusted cookie (absent/expired) — the honest fallback is the
      // code form itself; a quiet hint, never an error state.
      setSentNote(false);
      setSummary({
        summary: "该设备尚未记住或已过期，请获取短信验证码登录",
        fieldErrors: {},
        requestId: null,
      });
    } finally {
      setVerifying(false);
    }
  }
}
