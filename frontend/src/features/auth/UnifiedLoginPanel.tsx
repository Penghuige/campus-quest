"use client";
/**
 * The unified login island (unified-login spec §4): ONE page,
 * progressive disclosure — the layers appear only when their
 * condition holds:
 *
 *   0  the remembered one-click banner (no cookie → no element);
 *   1  the 账号 identifier (the band rides the PLACEHOLDER: 手机号
 *      或邮箱 on the code form, 手机号、邮箱、学号 once the password
 *      form expands) + 获取验证码 + the password weak link;
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
import { useEffect, useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { useSession } from "@/features/auth/session";
import { COPY_GENERIC_FAIL, isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { FastLoginHint } from "./FastLoginHint";
import { FormErrorSummary } from "./FormErrorSummary";
import { RecentLoginCardButton } from "./RecentLoginCard";
import { SubmitButton } from "./SubmitButton";
import {
  fastLoginCookieOnly,
  fetchRememberedHint,
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
import {
  listRecentLogins,
  maskIdentifier,
  recordRecentLogin,
  removeRecentLogin,
  type RecentLoginEntry,
} from "./recentLogins";
import { takeSwitchPick } from "./switchAccount";

const RESEND_COOLDOWN_SECONDS = 60;

export function UnifiedLoginPanel() {
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [registerToken, setRegisterToken] = useState<string | null>(null);
  // The remember checkbox's VALUE is panel-level: the user may check it
  // on the password form and complete the login on the totp leg — the
  // leg is where its MEANING lives (the terminal ruling), but the
  // checked state must survive the leg transition (#113's carry, kept).
  const [rememberDevice, setRememberDevice] = useState(false);
  // The identifier rides panel level too (batch 2): the recent-logins
  // card's click PREFILLS it, and every login-completion records it.
  const [identifier, setIdentifier] = useState("");
  // The form mode rides panel level (batch 3): a card click forces the
  // CODE form back on before its auto-send (the card's flow is always
  // the code leg — the password form would demand a second factor).
  const [mode, setMode] = useState<"code" | "password">("code");
  // The auto-send nudge: a card click that owes a code bumps the seq;
  // the form's effect (async, post-await setState only) fires the
  // request. seq-keyed so repeated picks of the SAME card re-fire.
  const [autoSend, setAutoSend] = useState<{ seq: number; identifier: string } | null>(null);
  const autoSendSeq = useRef(0);
  // The ledger reads localStorage — a BROWSER-only source: the first
  // render (server included) starts empty and the mount fills it on a
  // deferred tick. A lazy initializer would read localStorage during
  // render — the server has none, so the client's first HTML disagrees
  // (a hydration mismatch, caught in the terminal round); a synchronous
  // setState in the effect body trips the react-compiler rule instead.
  const [recent, setRecent] = useState<RecentLoginEntry[]>([]);
  useEffect(() => {
    const timer = setTimeout(() => {
      const rows = listRecentLogins();
      setRecent(rows);
      // The 切换账号 menu's handoff (popover batch, owner feedback
      // 2026-10-23): the popover stored its pick right before logging
      // out; once the rows are in, drive the picked card through the
      // SAME smart branch a click takes (remembered fast-landing /
      // auto-send / the password hand-back). The read deletes — the
      // pick fires once, and an unknown identifier (jar cleared,
      // another device) degrades to the plain page.
      const pick = takeSwitchPick();
      if (pick !== null) {
        const entry = rows.find((row) => row.identifier === pick);
        if (entry !== undefined) {
          void onPickCard(entry);
        }
      }
    }, 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-once by design; onPickCard's own awaits re-read what they need.
  }, []);
  const refreshRecent = () => setRecent(listRecentLogins());
  // The remembered-device probe is PANEL-level (batch 3): ONE anonymous
  // read feeds both the banner and the cards' one-click branch (a
  // card whose mask matches the probe's IS the remembered device).
  // probeSettledRef separates "still in flight" from "answered null" —
  // a card clicked before the mount probe returns must AWAIT a settled
  // answer, or every fresh-page click races into the auto-send branch
  // (the terminal-round failure: /login just mounted, click, code sent).
  const [probeMask, setProbeMask] = useState<string | null>(null);
  const probeSettledRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // Async read; the setState lands after the await (never the
      // synchronous-in-effect shape the compiler rule rejects).
      const hint = await fetchRememberedHint();
      if (!cancelled) {
        probeSettledRef.current = true;
        setProbeMask(hint);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const land = useLanding();

  /** Record a completed login (the jar upserts, caps, and never throws). */
  function recordLogin(role: string, nickname = "", explicitIdentifier?: string) {
    const trimmed = (explicitIdentifier ?? identifier).trim();
    if (trimmed.length === 0) {
      return;
    }
    recordRecentLogin({
      identifier: trimmed,
      masked: maskIdentifier(trimmed),
      role: role === "ADMIN" || role === "TEACHER" ? role : "STUDENT",
      nickname,
      lastUsedAt: Date.now(),
    });
    refreshRecent();
  }

  /**
   * The card click's smart branch (batch 3, owner feedback): a card
   * whose mask MATCHES the probe's is the remembered device — the
   * click rides the cookie-only verify (authenticated lands by role;
   * totp_required hands the ADMIN leg up). Any other card (no
   * credential, or a probe that went stale) prefills and AUTO-SENDS
   * its code — the owner's second-click complaint.
   */
  async function onPickCard(entry: RecentLoginEntry) {
    setIdentifier(entry.identifier);
    // Only a CODE-STREAM identifier (phone/email) can auto-send — a
    // student number (or anything the code band refuses) has no code
    // stream (学号 is the password channel's identifier): the click
    // hands the password form back instead of firing into a 422.
    if (validateChallengeIdentifier(entry.identifier) !== undefined) {
      setMode("password");
      return;
    }
    setMode("code");
    // The probe must be SETTLED before the branch judges: a click on a
    // freshly mounted page could otherwise race the in-flight read and
    // mistake "not answered yet" for "no remembered device".
    let mask = probeMask;
    if (!probeSettledRef.current) {
      mask = await fetchRememberedHint();
      probeSettledRef.current = true;
      setProbeMask(mask);
    }
    if (mask !== null && entry.masked === mask) {
      try {
        const outcome = await fastLoginCookieOnly();
        if (outcome.kind === "totp_required") {
          setPendingToken(outcome.pendingToken);
          return;
        }
        if (outcome.kind === "authenticated") {
          recordLogin(outcome.role, "", entry.identifier);
          await land();
          return;
        }
      } catch {
        // The cookie raced out between probe and click — fall through
        // to the code leg (the banner's same honest degradation).
      }
    }
    autoSendSeq.current += 1;
    setAutoSend({ seq: autoSendSeq.current, identifier: entry.identifier });
  }

  // Layers 2a/2b replace the form wholesale (the family's pattern) —
  // the back links hand the identifier flow back untouched.
  if (pendingToken !== null) {
    return (
      <TotpLeg
        pendingToken={pendingToken}
        rememberDevice={rememberDevice}
        setRememberDevice={setRememberDevice}
        recordLogin={recordLogin}
        onBack={() => setPendingToken(null)}
      />
    );
  }
  if (registerToken !== null) {
    return (
      <InlineRegister
        token={registerToken}
        recordLogin={recordLogin}
        onReset={() => setRegisterToken(null)}
      />
    );
  }
  return (
    <>
      <FastLoginHint mask={probeMask} onTotpRequired={setPendingToken} />
      <RecentLogins
        entries={recent}
        onPick={(entry) => {
          void onPickCard(entry);
        }}
        onRemove={(value) => {
          removeRecentLogin(value);
          refreshRecent();
        }}
      />
      <IdentifierForm
        identifier={identifier}
        setIdentifier={setIdentifier}
        mode={mode}
        setMode={setMode}
        autoSend={autoSend}
        rememberDevice={rememberDevice}
        setRememberDevice={setRememberDevice}
        onTotpRequired={setPendingToken}
        onUnregistered={setRegisterToken}
        onAuthenticated={recordLogin}
      />
    </>
  );
}

/** Batch 2 (owner brief 2026-10-20): the recent-logins quick-switch
 * cards. ZERO entries → ZERO elements (the layer-0 discipline). The
 * card FACE is the shared `RecentLoginCardButton` (the shells'
 * 切换账号 menu renders the same face); the remove verb stays a
 * login-page affordance. */
function RecentLogins({
  entries,
  onPick,
  onRemove,
}: {
  entries: RecentLoginEntry[];
  onPick: (entry: RecentLoginEntry) => void;
  onRemove: (identifier: string) => void;
}) {
  if (entries.length === 0) {
    return null;
  }
  return (
    <div className="login-recent" aria-label="最近登录">
      {entries.map((entry) => (
        <div className="login-recent-card" key={entry.identifier}>
          <RecentLoginCardButton entry={entry} onPick={onPick} />
          <Button
            variant="ghost"
            className="login-recent-remove"
            onClick={() => onRemove(entry.identifier)}
            aria-label={`从本机移除 ${entry.masked}`}
          >
            移除
          </Button>
        </div>
      ))}
    </div>
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
  identifier,
  setIdentifier,
  mode,
  setMode,
  autoSend,
  rememberDevice,
  setRememberDevice,
  onTotpRequired,
  onUnregistered,
  onAuthenticated,
}: {
  identifier: string;
  setIdentifier: (value: string) => void;
  mode: "code" | "password";
  setMode: (value: "code" | "password") => void;
  autoSend: { seq: number; identifier: string } | null;
  rememberDevice: boolean;
  setRememberDevice: (value: boolean) => void;
  onTotpRequired: (pendingToken: string) => void;
  onUnregistered: (registerToken: string) => void;
  onAuthenticated: (role: string, nickname?: string) => void;
}) {
  const land = useLanding();
  const cooldown = useCooldown();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [identifierError, setIdentifierError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [codeError, setCodeError] = useState<string | undefined>(undefined);
  const [sentNote, setSentNote] = useState(false);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const codeInputRef = useRef<HTMLInputElement>(null);

  // The card click's AUTO-SEND (batch 3): a seq bump fires the request
  // for the picked identifier — the async body's setStates land after
  // awaits (never the synchronous-in-effect shape the rule rejects).
  const lastAutoSendSeq = useRef(0);
  useEffect(() => {
    if (autoSend === null || autoSend.seq === lastAutoSendSeq.current) {
      return;
    }
    lastAutoSendSeq.current = autoSend.seq;
    void onSendCode(autoSend.identifier);
    // onSendCode reads only refs/state through its parameters here —
    // the dependency is the nudge itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSend]);

  // The code field takes focus the moment it renders (auto-send's
  // "cursor lands where the work is" half — the owner's ask).
  useEffect(() => {
    if (codeSent) {
      codeInputRef.current?.focus();
    }
  }, [codeSent]);

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

  async function onSendCode(nextIdentifier?: string) {
    if (sending || cooldown.remaining > 0) {
      return;
    }
    setSummary(null);
    setSentNote(false);
    const target = (nextIdentifier ?? identifier).trim();
    const error = validateChallengeIdentifier(target) ?? undefined;
    setIdentifierError(error);
    if (error !== undefined) {
      return;
    }
    setSending(true);
    try {
      // 202 always — anti-enumeration: the response never tells which
      // identifiers exist.
      await requestUnifiedChallenge(target);
      cooldown.start(RESEND_COOLDOWN_SECONDS);
      setCodeSent(true);
      setSentNote(true);
    } catch (cause) {
      // A 429 means the backend's 60s resend bucket is ALREADY running
      // (a prior send this mailbox) — mirror it on the button like a
      // successful send would (useCooldown's display philosophy: show
      // WHY it is disabled and when it returns).
      if (isApiError(cause) && cause.code === "RATE_LIMITED") {
        cooldown.start(RESEND_COOLDOWN_SECONDS);
        setCodeSent(true);
      }
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
      onAuthenticated(outcome.kind === "authenticated" ? outcome.role : "");
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
      onAuthenticated(outcome.kind === "authenticated" ? outcome.role : "");
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
            label="账号"
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
              placeholder: "请输入手机号、邮箱、学号",
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
            label="账号"
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
                    ref={codeInputRef}
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
                30 天内免登录
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
  rememberDevice,
  setRememberDevice,
  recordLogin,
  onBack,
}: {
  pendingToken: string;
  rememberDevice: boolean;
  setRememberDevice: (value: boolean) => void;
  recordLogin: (role: string, nickname?: string) => void;
  onBack: () => void;
}) {
  const land = useLanding();
  const [totpCode, setTotpCode] = useState("");
  const [totpError, setTotpError] = useState<string | undefined>(undefined);
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
      const outcome = await unifiedChallengeTotp(pendingToken, totpCode.trim(), rememberDevice);
      if (outcome.kind === "authenticated") {
        recordLogin(outcome.role);
      }
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
  recordLogin,
  onReset,
}: {
  token: string;
  recordLogin: (role: string, nickname?: string) => void;
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
      recordLogin("STUDENT", studentNumber.trim());
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
