"use client";
/**
 * Staff email-code login island (V1.1 §2.1/§2.2; plan Task 8) — the
 * DEFAULT login channel: email → 6-digit mailed code → logged in.
 *
 * State machine (every branch is a backend verdict, never a guess):
 *
 *   email ──发送验证码──▶ code ──登录──▶ TEACHER/remembered-ADMIN: done
 *     │                    │
 *     │ 429 = 60s cooldown │ ADMIN without device: {status:"totp_required"}
 *     ▼                    ▼
 *   cooldown copy        totp leg (验证码步重发可用) ──动态口令──▶ done
 *
 * - the resend button counts down 60s as TEXT ONLY — nothing animates,
 *   so the reduced-motion contract holds by construction;
 * - the "记住此设备 30 天" checkbox lives ONLY on the totp leg — a
 *   TERMINAL design decision (reviewer ruling 2026-10-15): a remembered
 *   device exempts an admin's TOTP but keeps the emailed code as a
 *   factor; a teacher remembered device would exempt the ONLY factor
 *   (pure single-factor cookie), which spec §2.2 never grants. The
 *   code step therefore never sends remember_device, and the checkbox
 *   is invisible before the response reveals an ADMIN (also the
 *   anti-enumeration stance);
 * - the totp leg's pending token is the sanctioned body-token
 *   exception: one Authorization header per call, React state only —
 *   the invitation flow's pending-session discipline verbatim;
 * - EMAIL_CODE_INVALID is uniform for wrong/expired/exhausted codes;
 *   ACCOUNT_LOCKED rounds retry_after_seconds up to whole minutes.
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { useSession } from "@/features/auth/session";
import { TEACHER_LANDING_PATH } from "@/features/auth/workspace";
import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { emailLoginTotp, requestEmailLoginCode, verifyEmailLoginCode } from "./api";
import { NETWORK_ERROR_TEXT, resendButtonLabel, type AuthErrorView } from "./errors";
import { validateStaffEmail } from "./staffAuthView";
import {
  describeEmailLoginError,
  validateEmailCode,
} from "./staffEmailLoginView";
import { Button } from "@/components/ui/button";

const RESEND_COOLDOWN_SECONDS = 60;

export function StaffEmailCodeLogin() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
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

  // Text-only countdown: one self-rescheduling timeout per second — no
  // interval to leak, nothing animating (reduced-motion safe).
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
    const error = validateStaffEmail(email.trim()) ?? undefined;
    setEmailError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      // 202 always — anti-enumeration: unknown/student/inactive emails
      // answer identically, so the copy never branches on existence.
      await requestEmailLoginCode(email.trim());
      setCooldown(RESEND_COOLDOWN_SECONDS);
      setSentNote(true);
    } catch (cause) {
      const view = describeEmailLoginError(cause, "验证码发送失败，请稍后重试");
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
      const outcome = await verifyEmailLoginCode(email.trim(), code.trim());
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
      await emailLoginTotp(pendingToken, totpCode.trim(), rememberDevice);
      landOnWorkspace();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
    } finally {
      setVerifying(false);
    }
  }

  function landOnWorkspace() {
    refreshSession();
    router.replace(TEACHER_LANDING_PATH); // Teacher landing: the review queue
    router.refresh();
  }

  function toSummary(cause: unknown, fallback: string): AuthErrorView {
    if (!isApiError(cause)) {
      return { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null };
    }
    const view = describeEmailLoginError(cause, fallback);
    return { summary: view.message, fieldErrors: {}, requestId: view.requestId };
  }

  // --- the ADMIN second leg --------------------------------------------------
  if (pendingToken !== null) {
    return (
      <form className="form" onSubmit={onTotp} noValidate>
        <FormErrorSummary view={summary} />
        <p className="field-hint" data-testid="email-login-totp-leg">
          该账号需要动态口令验证：请输入验证器应用当前显示的 6 位数字。
        </p>
        <AuthField
          name="email-login-totp-code"
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
            返回验证码登录
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
          验证码已发送，请到邮箱查收（5 分钟内有效；若未收到请检查垃圾箱）。
        </p>
      ) : null}
      <form className="form" onSubmit={onVerify} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="email-login-email"
          label="邮箱"
          error={emailError}
          inputProps={{
            value: email,
            onChange: (event) => {
              setEmail(event.target.value);
              if (emailError !== undefined) {
                setEmailError(undefined);
              }
            },
            type: "email",
            inputMode: "email",
            autoComplete: "email",
            autoCapitalize: "none",
            autoCorrect: "off",
            placeholder: "员工邮箱",
            disabled: sending || verifying,
          }}
        />
        <div className="field">
          <label className="field-label" htmlFor="email-login-code">
            邮箱验证码
          </label>
          <div className="otp-row">
            <input
              id="email-login-code"
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
              aria-describedby={codeError !== undefined ? "email-login-code-error" : undefined}
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
            <p className="field-error" id="email-login-code-error">
              {codeError}
            </p>
          ) : null}
        </div>
        <SubmitButton loading={verifying}>登录</SubmitButton>
      </form>
    </>
  );
}
