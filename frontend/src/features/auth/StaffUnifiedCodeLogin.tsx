"use client";
/**
 * The UNIFIED staff code-login island (two-channel convergence,
 * 2026-10-20): ONE identifier field (邮箱或手机号) + the code + the
 * remember checkbox. The backend routes the code stream by identifier
 * shape — the employee picks no channel. Structure mirrors the family
 * (cooldown, the ADMIN pending leg, the #84 landing rule); the
 * TERMINAL remember-device ruling holds verbatim: the code leg a
 * TEACHER completes never sends remember_device — the checkbox rides
 * along in state and its meaning lives on the ADMIN totp leg only.
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
  requestUnifiedStaffCode,
  staffMobileCodeTotp,
  verifyUnifiedStaffCode,
} from "./api";
import { NETWORK_ERROR_TEXT } from "./errors";
import { isApiError } from "@/lib/errors";
import { resendButtonLabel, type AuthErrorView } from "./errors";
import { landAfterStaffLogin } from "./staffLoginFlow";
import {
  describeMobileStaffLoginError,
  validateEmailCode,
} from "./staffEmailLoginView";

const RESEND_COOLDOWN_SECONDS = 60;

export function StaffUnifiedCodeLogin() {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [identifier, setIdentifier] = useState("");
  const [code, setCode] = useState("");
  const [identifierError, setIdentifierError] = useState<string | undefined>(undefined);
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

  useEffect(() => {
    if (cooldown === 0) {
      return;
    }
    const timer = setTimeout(() => setCooldown(cooldown - 1), 1_000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  /** The identifier band: an email address OR a CN mobile. */
  function validateIdentifier(value: string): string | undefined {
    const trimmed = value.trim();
    if (/^1[3-9]\d{9}$/.test(trimmed)) {
      return undefined;
    }
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      return undefined;
    }
    return "请输入员工邮箱或手机号";
  }

  async function onSendCode() {
    if (sending || cooldown > 0) {
      return;
    }
    setSummary(null);
    setSentNote(false);
    const error = validateIdentifier(identifier) ?? undefined;
    setIdentifierError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      // 202 always — anti-enumeration: the response never tells which
      // identifiers exist.
      await requestUnifiedStaffCode(identifier.trim());
      setCooldown(RESEND_COOLDOWN_SECONDS);
      setSentNote(true);
    } catch (cause) {
      setSummary(toSummary(cause, "验证码发送失败，请稍后重试"));
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
      const outcome = await verifyUnifiedStaffCode(identifier.trim(), code.trim());
      if (outcome.kind === "totp_required") {
        setPendingToken(outcome.pendingToken);
        setTotpCode("");
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
      // The checkbox's meaning lives HERE (the ADMIN second leg) —
      // the code leg never sent remember_device (the 2026-10-15
      // ruling, option a).
      await staffMobileCodeTotp(pendingToken, totpCode.trim(), rememberDevice);
      landOnWorkspace();
    } catch (cause) {
      setSummary(toSummary(cause, "登录失败，请稍后重试"));
    } finally {
      setVerifying(false);
    }
  }

  function landOnWorkspace() {
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

  // --- the ADMIN second leg ---------------------------------------------------
  if (pendingToken !== null) {
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
          验证码已发送，请查收邮件或短信（5 分钟内有效）
        </p>
      ) : null}
      <form className="form" onSubmit={onVerify} noValidate>
        <FormErrorSummary view={summary} />
        <AuthField
          name="staff-unified-identifier"
          label="邮箱或手机号"
          hint="绑定员工的邮箱或手机号均可；验证码将发送到对应的邮箱或手机"
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
            placeholder: "员工邮箱或手机号",
            disabled: sending || verifying,
          }}
        />
        <div className="field">
          <label className="field-label" htmlFor="staff-unified-code">
            验证码
          </label>
          <div className="otp-row">
            <input
              id="staff-unified-code"
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
              aria-describedby={codeError !== undefined ? "staff-unified-code-error" : undefined}
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
            <p className="field-error" id="staff-unified-code-error">
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
          记住此设备 30 天（对需要动态口令的账号生效）
        </label>
        <SubmitButton loading={verifying}>登录</SubmitButton>
      </form>
    </>
  );
}
