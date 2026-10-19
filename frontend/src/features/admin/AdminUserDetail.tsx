"use client";
/**
 * The admin user-detail island (V1.1 spec §3, Wave 2a): the panorama
 * (account facts + wallet + the last ten submissions + the session and
 * remembered-device census) and the three power actions.
 *
 * Action discipline (every verb audited server-side):
 * - 代重置密码 answers a ONE-TIME link (the invitation/recovery-codes
 *   display grammar: rendered exactly once, copy affordance, explicit
 *   48h notice) — never for the admin's own account (hidden here, and
 *   the server refuses regardless);
 * - 踢线 revokes ALL sessions + remembered devices after an explicit
 *   confirm (the stolen-account emergency verb);
 * - 角色升降 walks TEACHER↔ADMIN with a confirm that names both sides
 *   — hidden for the admin's own account and for students (the server
 *   owns every guard: self, last-ACTIVE-ADMIN).
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { CopyButton } from "@/features/auth/CopyButton";
import { useSession } from "@/features/auth/session";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { isApiError } from "@/lib/errors";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { apiBasePath } from "@/lib/apiBase";

import {
  changeAdminUserRole,
  fetchAdminUserDetail,
  issueAdminPasswordReset,
  revokeAdminUserSessions,
  type AdminUserDetailDto,
} from "./adminPowersApi";
import { buildAdminResetLink } from "./staffInvitationView";

const ROLE_LABELS: Record<string, string> = {
  STUDENT: "学生",
  TEACHER: "教师",
  ADMIN: "管理员",
};

const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "正常",
  PENDING_APPROVAL: "待审核",
  SUSPENDED: "已停用",
  BANNED: "已封禁",
};

type Phase = "loading" | "ready" | "error";

/** The confirm-verdict strip under the actions (server-owned counts). */
function ActionNote({ text, tone }: { text: string; tone: "ok" | "err" }) {
  return (
    <p className={tone === "ok" ? "field-hint" : "field-error"} role="status">
      {text}
    </p>
  );
}

export function AdminUserDetail({ userId }: { userId: string }) {
  const { state } = useSession();
  const [phase, setPhase] = useState<Phase>("loading");
  const [detail, setDetail] = useState<AdminUserDetailDto | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [resetLink, setResetLink] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; tone: "ok" | "err" } | null>(null);

  const load = useCallback(() => {
    setPhase("loading");
    setError(null);
  }, []);

  useEffect(() => {
    if (phase !== "loading") {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const next = await fetchAdminUserDetail(userId);
        if (!cancelled) {
          setDetail(next);
          setPhase("ready");
        }
      } catch (cause) {
        if (!cancelled) {
          setError(cause);
          setPhase("error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [phase, userId]);

  async function run(action: () => Promise<void>) {
    if (busy) {
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await action();
    } catch (cause) {
      setNote({
        text: isApiError(cause) ? cause.message : "操作失败，请稍后重试",
        tone: "err",
      });
    } finally {
      setBusy(false);
    }
  }

  function onIssueReset() {
    void run(async () => {
      // The wire carries the one-time TOKEN; the copyable URL composes
      // client-side onto the public consume page (48h validity).
      const { reset_token } = await issueAdminPasswordReset(userId);
      setResetLink(
        buildAdminResetLink(window.location.origin, apiBasePath(), reset_token),
      );
    });
  }

  function onKick() {
    void run(async () => {
      const { revoked_sessions, revoked_devices } = await revokeAdminUserSessions(userId);
      setNote({
        text: `已撤销 ${revoked_sessions} 项会话与 ${revoked_devices} 项设备凭据。`,
        tone: "ok",
      });
      load();
    });
  }

  function onRoleChange(nextRole: "TEACHER" | "ADMIN") {
    void run(async () => {
      const user = await changeAdminUserRole(userId, nextRole);
      setNote({
        text: `角色已调整为${ROLE_LABELS[user.role] ?? user.role}。`,
        tone: "ok",
      });
      load();
    });
  }

  const isSelf = state.status === "authenticated" && state.me.id === userId;
  const role = detail?.user.role ?? "";
  const otherRole: "TEACHER" | "ADMIN" | null =
    role === "TEACHER" ? "ADMIN" : role === "ADMIN" ? "TEACHER" : null;

  return (
    <section className="section" aria-label="用户详情">
      <div className="section-head">
        <h2 className="section-title">全景</h2>
        <Button variant="ghost" onClick={load} disabled={phase === "loading"}>
          刷新
        </Button>
      </div>
      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={error} onRetry={load} retryLabel="重新加载详情" />
      ) : detail === null ? null : (
        <>
          <dl className="fact-rows">
            <div className="fact-row">
              <dt className="fact-label">账号</dt>
              <dd className="fact-value mono">{detail.user.username}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">昵称</dt>
              <dd className="fact-value">{detail.user.nickname}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">角色</dt>
              <dd className="fact-value">{ROLE_LABELS[detail.user.role] ?? detail.user.role}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">状态</dt>
              <dd className="fact-value">{STATUS_LABELS[detail.user.status] ?? detail.user.status}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">钱包余额</dt>
              <dd className="fact-value">{detail.wallet_balance}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">活跃会话</dt>
              <dd className="fact-value">{detail.active_session_count}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">记住的设备</dt>
              <dd className="fact-value">{detail.remembered_device_count}</dd>
            </div>
          </dl>

          <h3 className="section-title">最近提交</h3>
          {detail.recent_submissions.length === 0 ? (
            <p className="field-hint">暂无提交记录。</p>
          ) : (
            <div className="table-scroll">
              <table className="staff-table">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>状态</th>
                    <th>提交时间</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.recent_submissions.map((row) => (
                    <tr key={row.id}>
                      <td>{row.task_title}</td>
                      <td>{row.validation_status}</td>
                      <td>
                        <time className="notif-time" dateTime={row.submitted_at}>
                          {formatDeadlineDateTime(parseServerInstant(row.submitted_at))}
                        </time>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="section-title">操作</h3>
          <div className="row-actions">
            {isSelf ? null : (
              <>
                <Button
                  variant="secondary"
                  onClick={onIssueReset}
                  disabled={busy}
                >
                  代重置密码
                </Button>
                <Button variant="secondary" onClick={onKick} disabled={busy}>
                  踢出全部会话
                </Button>
              </>
            )}
            {otherRole !== null && !isSelf ? (
              <Button
                variant="secondary"
                onClick={() => onRoleChange(otherRole)}
                disabled={busy}
              >
                {otherRole === "ADMIN" ? "提升为管理员" : "降级为教师"}
              </Button>
            ) : null}
            <Link className="link" href="/admin/users">
              返回用户列表
            </Link>
          </div>
          {isSelf ? (
            <p className="field-hint">对自己的账号不可使用管理动作（自助通道在个人设置）。</p>
          ) : null}
          {note !== null ? <ActionNote text={note.text} tone={note.tone} /> : null}
          {resetLink !== null ? (
            <div className="alert alert-success" role="status">
              <p>一次性重置链接（48 小时内有效，仅本次显示，请立即转交本人）：</p>
              <p className="mono">{resetLink}</p>
              <p>
                <CopyButton value={resetLink} label="复制重置链接" />
              </p>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
