"use client";
/**
 * Staff mobile-code login island (login-iteration item ①, 2026-10-17)
 * — the THIRD channel tab: the staff account's BOUND phone → 6-digit
 * SMS code → logged in. A channel-for-channel mirror of the email-code
 * island (StaffEmailCodeLogin):
 *
 *   phone ──发送验证码──▶ code ──登录──▶ TEACHER/remembered-ADMIN: done
 *     │                    │
 *     │ 429 = 60s cooldown │ ADMIN without device: {status:"totp_required"}
 *     ▼                    ▼
 *   cooldown copy        totp leg ──动态口令──▶ done
 *
 * - the TERMINAL remember-device ruling (2026-10-15) applies verbatim:
 *   the code leg a TEACHER can complete never sends remember_device;
 *   the 记住此设备 checkbox lives ONLY on the ADMIN totp leg (also the
 *   anti-enumeration stance — the checkbox appears only after the
 *   response reveals an ADMIN);
 * - the totp leg's pending token is the family's body-token exception
 *   (one request, React state only);
 * - MOBILE_CODE_INVALID is uniform for wrong/expired/exhausted codes;
 *   the lockout/cooldown copy is the family's verbatim text.
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { useSession } from "@/features/auth/session";

import { AuthField } from "./AuthField";
import { Button } from "@/components/ui/button";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import {
  readMe,
  requestStaffMobileCode,
  staffMobileCodeTotp,
  verifyStaffMobileCode,
} from "./api";
import { NETWORK_ERROR_TEXT, resendButtonLabel, type AuthErrorView } from "./errors";
import { landAfterStaffLogin } from "./staffLoginFlow";
import {
  describeMobileStaffLoginError,
  validateEmailCode,
} from "./staffEmailLoginView";
import { validateMobileLoginPhone } from "./mobileLoginView";
import { isApiError } from "@/lib/errors";

const RESEND_COOLDOWN_SECONDS = 60;

export function StaffMobileCodeLogin() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [phoneError, setPhoneError] = useState<string | undefined>(undefined);
  const [codeError, setCodeError] = useState<string | undefined>(undefined);
  const [totpCode, setTotpCode] = useState("");
  const [totpError, setTotpError] = useState<string | undefined>(undefined);
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [rememberDevice, setRememberDevice] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [sentNote, setSentNote] = useState(false);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);

  // Text-only countdown (the family's reduced-motion-safe pattern).
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
    const error = validateMobileLoginPhone(phone) ?? undefined;
    setPhoneError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      // 202 always — anti-enumeration (unknown/student-bound phones
      // answer identically; the copy never branches on existence).
      await requestStaffMobileCode(phone.trim());
      setCooldown(RESEND_COOLDOWN_SECONDS);
      setSentNote(true);
    } catch (cause) {
      const view = describeMobileStaffLoginError(cause, "验证码发送失败，请稍后重试");
      setSummary({ summary: view.message, fieldErrors: {}, requestId: view.requestId });
    } finally {
      setSending(false);
    }
  }

  async function onVerify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying) {
      return;
    }
    setSummary(null);
    const error = validateEmailCode(code.trim()) ?? undefined;
    setCodeError(error);
    if (error !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      const outcome = await verifyStaffMobileCode(phone.trim(), code.trim());
      if (outcome.kind === "totp_required") {
        // The ADMIN second leg: the pending token stays in state only.
        setPendingToken(outcome.pendingToken);
        setTotpCode("");
        setRememberDevice(false);
        return;
      }
      landOnWorkspace();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
    } finally {
      setVerifying(false);
    }
  }

  async function onTotp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (verifying || pendingToken === null) {
      return;
    }
    setSummary(null);
    const error = validateEmailCode(totpCode.trim()) ?? undefined;
    setTotpError(error);
    if (error !== undefined) {
      return;
    }
    setVerifying(true);
    try {
      await staffMobileCodeTotp(pendingToken, totpCode.trim(), rememberDevice);
      landOnWorkspace();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
    } finally {
      setVerifying(false);
    }
  }

  function landOnWorkspace() {
    // Route by the POST-LOGIN /me answer (the #84 rule, the family's).
    void landAfterStaffLogin({
      login: async () => undefined,
      refreshSession,
      router,
      readMe,
    });
  }

  function toSummary(cause: unknown, fallback: string): AuthErrorView {
    if (!isApiError(cause)) {
      return { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
    }
    const view = describeMobileStaffLoginError(cause, fallback);
    return { summary: view.message, fieldErrors: {}, requestId: view.requestId };
  }

  // --- the ADMIN second leg --------------------------------------------------
  if (pendingToken !== null) {
    return (
      <form className="form" onSubmit={onTotp} noValidate>
        <FormErrorSummary view={summary} />
        <p className="field-hint" data-testid="mobile-login-totp-leg">
          该账号需要动态口令验证：请输入验证器应用当前显示的 6 位数字。
        </p>
        <AuthField
          name="mobile-login-totp-code"
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
          <Button
            variant="ghost"
            onClick={() => {
              setPendingToken(null);
              setSummary(null);
            }}
            disabled={verifying}
          >
            返回手机验证码登录
          </Button>
        </p>
      </form>
    );
  }

  // --- the default two-step code flow ----------------------------------------
  return (
    <>
      {sentNote ? (
        <p className="alert alert-success" role="status">
          验证码已发送，请查收短信（5 分钟内有效）。
        </p>
      ) : null}
      <form className="form" onSubmit={onVerify} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="staff-mobile-login-phone"
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
            placeholder: "绑定的手机号",
            disabled: sending || verifying,
          }}
        />
        <div className="field">
          <label className="field-label" htmlFor="staff-mobile-login-code">
            短信验证码
          </label>
          <div className="otp-row">
            <input
              id="staff-mobile-login-code"
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
              aria-describedby={codeError !== undefined ? "staff-mobile-login-code-error" : undefined}
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
            <p className="field-error" id="staff-mobile-login-code-error">
              {codeError}
            </p>
          ) : null}
        </div>
        <SubmitButton loading={verifying}>登录</SubmitButton>
      </form>
    </>
  );
}
