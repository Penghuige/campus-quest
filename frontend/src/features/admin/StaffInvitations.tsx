"use client";
/**
 * StaffInvitations — the /admin/invitations island (spec §5.8 step 1;
 * the §8 Admin surface): issue one one-shot staff invitation and hand
 * the admin the ONLY copy of the link.
 *
 * State model (every transition is a backend verdict, never a client
 * guess):
 *
 *   form ──issue(email, role)──▶ issued(one-time window) ──再发一个──▶ form
 *     │                                │
 *     └─ client-invalid email:         └─ the token is dropped from
 *        field error, NO request          state on reset — the row
 *        (the 20/hour bucket stays       stores only its hash, no
 *        untouched)                       refetch exists
 *
 * - the link joins the deployment mount prefix through apiBase —
 *   never a hardcoded origin or "/campus" (the 207 production mount);
 * - the ONE-TIME display contract mirrors the recovery-codes window
 *   (TotpSetup): rendered exactly once, copy affordance beside it,
 *   explicit notices (48h validity; the invitee sets their own
 *   password and binds TOTP themselves);
 * - error copy branches on `error.code` via describeInvitationError
 *   (§29) — the 409 branch rides the accept-side code today and lands
 *   on create with the backend pre-check (reviewer ruling 2026-10-15).
 */
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { CopyButton } from "@/features/auth/CopyButton";
import { apiBasePath } from "@/lib/apiBase";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import { createStaffInvitation } from "./adminApi";
import { COPY_GENERIC_FAIL } from "@/lib/errors";
import {
  buildInvitationLink,
  describeInvitationError,
  INVITATION_ROLE_OPTIONS,
  INVITATION_TTL_HOURS,
  validateInvitationEmail,
  type StaffInvitationRole,
} from "./staffInvitationView";

/** The one-time window: everything the admin must save NOW. */
interface IssuedInvitation {
  email: string;
  role: StaffInvitationRole;
  link: string;
  expiresAt: string;
}

export function StaffInvitations() {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<StaffInvitationRole>("TEACHER");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [issued, setIssued] = useState<IssuedInvitation | null>(null);

  async function onIssue(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setError(null);
    const invalid = validateInvitationEmail(email);
    setEmailError(invalid);
    if (invalid !== undefined) {
      // Never POST a client-rejected value — the rate-limit bucket
      // counts requests, not successes.
      return;
    }
    setSubmitting(true);
    try {
      const result = await createStaffInvitation({ email: email.trim(), role });
      setIssued({
        email: email.trim(),
        role,
        link: buildInvitationLink(
          window.location.origin,
          apiBasePath(),
          result.token,
        ),
        expiresAt: result.expires_at,
      });
      setEmail("");
      setEmailError(undefined);
    } catch (cause) {
      setError(cause);
    } finally {
      setSubmitting(false);
    }
  }

  function onReset() {
    // The issued link leaves state here for good — the token has no
    // second read; only the server-side hash remains.
    setIssued(null);
    setError(null);
  }

  if (issued !== null) {
    return (
      <section className="section" aria-label="发送员工邀请">
        <div className="section-head">
          <h2 className="section-title">邀请已创建</h2>
        </div>
        <div className="alert alert-warning" role="alert">
          <p>
            <strong>邀请链接只显示这一次。</strong>
            请现在复制并发送给受邀人（{issued.email}，角色：
            {issued.role === "ADMIN" ? "管理员" : "教师"}）；链接在{" "}
            {INVITATION_TTL_HOURS} 小时内有效，受邀人将通过链接自行设置密码并绑定动态口令（TOTP）后启用账号。
            离开此页面后将无法再次查看该链接。
          </p>
        </div>
        <div className="secret-box">
          <p className="secret-line">
            <span className="secret-label">邀请链接</span>
            <span className="mono" data-testid="invitation-link">
              {issued.link}
            </span>
          </p>
          <p className="secret-line">
            <span className="secret-label">失效时间</span>
            <time dateTime={issued.expiresAt}>
              {formatDeadlineDateTime(parseServerInstant(issued.expiresAt))}
            </time>
          </p>
          <div className="copy-row">
            <CopyButton value={issued.link} label="复制邀请链接" />
          </div>
        </div>
        <div className="dialog-actions">
          <Button variant="secondary" onClick={onReset}>
            再发一个邀请
          </Button>
        </div>
      </section>
    );
  }

  const errorView =
    error !== null
      ? describeInvitationError(error, COPY_GENERIC_FAIL)
      : null;

  return (
    <section className="section" aria-label="发送员工邀请">
      <div className="section-head">
        <h2 className="section-title">新建邀请</h2>
      </div>
      <p className="field-hint">
        受邀人通过链接自行设置密码并绑定动态口令（TOTP）；每个链接{" "}
        {INVITATION_TTL_HOURS} 小时内有效、仅可使用一次，且创建后只显示这一次。
      </p>
      <form className="panel" onSubmit={onIssue} noValidate>
        <div className="field">
          <label className="field-label" htmlFor="invitation-email">
            受邀人邮箱
          </label>
          <input
            id="invitation-email"
            className="input"
            type="email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
              if (emailError !== undefined) {
                setEmailError(undefined);
              }
            }}
            disabled={submitting}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            placeholder="teacher@example.edu"
            aria-invalid={emailError !== undefined || undefined}
            aria-describedby={emailError !== undefined ? "invitation-email-error" : undefined}
          />
          {emailError !== undefined ? (
            <p className="field-error" id="invitation-email-error">
              {emailError}
            </p>
          ) : null}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="invitation-role">
            角色
          </label>
          <select
            id="invitation-role"
            className="input"
            value={role}
            onChange={(event) =>
              setRole(event.target.value as StaffInvitationRole)
            }
            disabled={submitting}
          >
            {INVITATION_ROLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <p className="field-hint">
            教师发布并管理任务；管理员拥有与您相同的后台权限。学生注册走白名单流程，不通过邀请。
          </p>
        </div>
        <div className="dialog-actions">
          <Button
            type="submit"
            variant="primary"
            disabled={submitting}
            aria-busy={submitting}
          >
            {submitting ? <span className="spinner" aria-hidden="true" /> : null}
            <span>创建邀请</span>
          </Button>
        </div>
      </form>
      {errorView !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {errorView.message}
          </p>
          {errorView.requestId !== null ? (
            <p className="req-id">请求 ID：{errorView.requestId}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
