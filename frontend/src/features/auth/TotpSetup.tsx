"use client";
/**
 * TOTP onboarding island — the second half of staff invitation acceptance
 * (spec §5.8 steps 3-5; patterns §6).
 *
 * State model (each transition is a backend verdict, never a client guess):
 *
 *   loading ──begin──▶ entry ──confirm(code)──▶ codes(shown) ──确认已保存──▶ done
 *      │                 │ │                          │
 *      ▼                 ▼ └─ 409 already-bound ─────▶ (message; go login)
 *   load-failed ─retry─▶ └─ 401 wrong code: stay, retry safe
 *
 * - the pending staff session's ACCESS TOKEN arrives by prop and lives in
 *   React state only — it is the sanctioned body-token exception
 *   (backend `PendingStaffSession`), confined server-side to these two
 *   endpoints; never stored, never logged;
 * - the SECRET + otpauth URI are displayed as COPYABLE TEXT, not a QR
 *   code — the documented choice, see `TOTP_QR_STRATEGY`
 *   (staffAuthView.ts);
 * - RECOVERY CODES pass through the one-time display state machine
 *   (`recoveryCodesIssued` -> `confirmRecoveryCodesSeen`): shown exactly
 *   once, dropped from state on confirm, never refetchable — the backend
 *   returns the plaintext list exactly once and only Argon2id hashes rest
 *   server-side;
 * - done points at /staff/login: the pending session is for setup only,
 *   staff login requires the fresh second factor.
 */
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { SectionSkeleton } from "@/components/ui/sectionStates";
import { isApiError } from "@/lib/errors";

import { AuthField } from "./AuthField";
import { CopyButton } from "./CopyButton";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { beginTotpSetup, confirmTotpSetup } from "./api";
import {
  NETWORK_ERROR_TEXT,
  type AuthErrorView,
} from "./errors";
import {
  confirmRecoveryCodesSeen,
  copyAllRecoveryCodesText,
  recoveryCodesIssued,
  totpProvisioningView,
  validateTotpCode,
  describeStaffError,
  type RecoveryCodesState,
  type TotpProvisioningView,
} from "./staffAuthView";

type TotpStep =
  | { kind: "loading" }
  | { kind: "load-failed"; error: unknown }
  | { kind: "entry"; provisioning: TotpProvisioningView }
  | { kind: "codes"; view: RecoveryCodesState };

export interface TotpSetupProps {
  /** The pending staff session's short-lived access token (body-only). */
  accessToken: string;
}

export function TotpSetup({ accessToken }: TotpSetupProps) {
  const [step, setStep] = useState<TotpStep>({ kind: "loading" });
  const [seed, setSeed] = useState(0);
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState<string | undefined>(undefined);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [confirming, setConfirming] = useState(false);

  // begin: generate (or rotate) the unconfirmed secret. Retry-safe — the
  // backend happily rotates an unconfirmed credential, so the 重新加载
  // button just bumps `seed` (the click handler resets the step; this
  // effect only STARTS the fetch — the lint-driven state shape every
  // other data island in this codebase uses).
  //
  // SINGLE-FLIGHT per generation (the E5 flake watch, fixed): the
  // backend ROTATES the stored unconfirmed secret on every begin, and
  // the last begin to REACH THE SERVER owns what confirm verifies. Two
  // begins in flight (React StrictMode's dev double-effect; any
  // duplicate effect run) can reach the server in the reverse order to
  // their dispatch on a slow first hop, leaving the page displaying one
  // begin's secret while the server stores the other's — every confirm
  // code is then rejected with no recovery. The dispatch guard keys on
  // the effect's own dependency identity (refs survive StrictMode's
  // simulated remount), so only a genuine retry (`seed`) or a new
  // `accessToken` begins again; each response then applies only while
  // ITS generation is still current, which replaces the per-run
  // `cancelled` flag (that flag would discard the sole surviving
  // fetch's response: the run that started it was cleaned up by the
  // remount).
  const begunGeneration = useRef<string | null>(null);
  useEffect(() => {
    const generation = `${seed}#${accessToken}`;
    if (begunGeneration.current === generation) {
      return;
    }
    begunGeneration.current = generation;
    beginTotpSetup(accessToken).then(
      (setup) => {
        if (begunGeneration.current === generation) {
          setStep({ kind: "entry", provisioning: totpProvisioningView(setup) });
        }
      },
      (error: unknown) => {
        if (begunGeneration.current === generation) {
          setStep({ kind: "load-failed", error });
        }
      },
    );
  }, [accessToken, seed]);

  function retryBegin() {
    setStep({ kind: "loading" });
    setSummary(null);
    setSeed((value) => value + 1);
  }

  const onConfirm = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const error = validateTotpCode(code) ?? undefined;
      setCodeError(error);
      setSummary(null);
      if (error !== undefined) {
        return;
      }
      setConfirming(true);
      try {
        const confirmed = await confirmTotpSetup(accessToken, code.trim());
        setCode("");
        setStep({ kind: "codes", view: recoveryCodesIssued(confirmed.recovery_codes) });
      } catch (cause) {
        if (!isApiError(cause)) {
          setSummary({ summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null });
          return;
        }
        // A wrong code burns nothing server-side (the credential stays
        // unconfirmed) — the entry form stays armed for another attempt.
        setSummary(describeStaffError(cause, "totp-confirm"));
      } finally {
        setConfirming(false);
      }
    },
    [accessToken, code],
  );

  if (step.kind === "loading") {
    return <SectionSkeleton lines={3} />;
  }

  if (step.kind === "load-failed") {
    const view = isApiError(step.error)
      ? describeStaffError(step.error, "totp-session")
      : null;
    return (
      <div className="alert alert-error" role="alert">
        <p>{view !== null ? view.summary : NETWORK_ERROR_TEXT}</p>
        {view !== null && view.requestId !== null ? (
          <p className="req-id">请求 ID：{view.requestId}</p>
        ) : null}
        <p>
          <button type="button" className="btn btn-secondary" onClick={retryBegin}>
            重新加载
          </button>
        </p>
      </div>
    );
  }

  if (step.kind === "codes") {
    // The one-time window. `view.phase` IS the machine: once the user
    // confirms, codes are gone and only the done state renders.
    const codes = step.view.codes;
    if (codes === null) {
      return <TotpDone />;
    }
    return (
      <div className="totp-step">
        <div className="alert alert-warning" role="alert">
          <p>
            <strong>恢复代码只显示这一次。</strong>
            动态验证器丢失或无法使用时，每个恢复代码可代替一次动态验证码登录。请复制并保存在安全的地方；离开此页面后将无法再次查看。
          </p>
        </div>
        <ol className="recovery-codes" aria-label="恢复代码">
          {codes.map((value) => (
            <li key={value}>
              <span className="mono">{value}</span>
            </li>
          ))}
        </ol>
        <div className="copy-row">
          <CopyButton value={copyAllRecoveryCodesText(codes)} label="复制全部恢复代码" />
        </div>
        {/* Not inside a form: an explicit action button, not a submit. */}
        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={() =>
            setStep({ kind: "codes", view: confirmRecoveryCodesSeen(step.view) })
          }
        >
          我已妥善保存，完成绑定
        </button>
      </div>
    );
  }

  // entry: the fresh credential + the confirm form.
  const { provisioning } = step;
  return (
    <form className="totp-step form" onSubmit={onConfirm} noValidate>
      <FormErrorSummary view={summary} />
      <div className="field">
        <div className="field-head">
          <span className="field-label">第 1 步：在验证器应用中添加账号</span>
        </div>
        <p className="field-hint">
          本页不生成二维码：请打开验证器应用（如系统自带动态口令、Google
          Authenticator），选择“手动输入”，按下方信息填写；或复制绑定链接，在支持的应用中打开。
        </p>
        <div className="secret-box">
          <p className="secret-line">
            <span className="secret-label">密钥</span>
            <span className="mono" data-testid="totp-secret">
              {provisioning.secret}
            </span>
          </p>
          {provisioning.accountLabel !== null ? (
            <p className="secret-line">
              <span className="secret-label">名称</span>
              <span className="mono">{provisioning.accountLabel}</span>
            </p>
          ) : null}
          <p className="secret-line">
            <span className="secret-label">绑定链接</span>
            <span className="mono" data-testid="totp-uri">
              {provisioning.otpauthUri}
            </span>
          </p>
        </div>
        <div className="copy-row">
          <CopyButton value={provisioning.secret} label="复制密钥" />
          <CopyButton value={provisioning.otpauthUri} label="复制绑定链接" />
        </div>
      </div>
      <AuthField
        name="totp_code"
        label="第 2 步：输入验证器当前显示的动态验证码"
        hint="6 位数字，每 30 秒变化一次；请输入当前正在显示的那一组。若反复失败，请重新打开邀请链接，从第 1 步重新开始绑定。"
        error={codeError}
        inputProps={{
          value: code,
          onChange: (event) => {
            setCode(event.target.value);
            if (codeError !== undefined) {
              setCodeError(undefined);
            }
          },
          type: "text",
          inputMode: "numeric",
          autoComplete: "one-time-code",
          autoCapitalize: "none",
          autoCorrect: "off",
          placeholder: "6 位数字",
          maxLength: 6,
          disabled: confirming,
        }}
      />
      <SubmitButton loading={confirming}>确认绑定</SubmitButton>
    </form>
  );
}

/** Terminal state: 2FA is live; the next stop is the staff login. */
function TotpDone() {
  return (
    <div className="totp-step">
      <div className="alert alert-success" role="status">
        <p>动态口令绑定完成，员工账号已激活。</p>
      </div>
      <p className="auth-alt-action">
        下一步：
        <Link className="link" href="/staff/login">
          前往员工登录
        </Link>
      </p>
    </div>
  );
}
