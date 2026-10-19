"use client";
/**
 * The invitation lifecycle panel (V1.1 spec §4.2, Wave 2a): the
 * four-state listing under the create form — 撤销 kills a pending
 * invitation's token NOW; 重发 mints a fresh token (the old one dies)
 * and hands the admin the new link in the SAME exactly-once grammar
 * the create flow uses. ACCEPTED rows carry no verbs (their token is
 * spent history); the audit trail stays the server's story.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { CopyButton } from "@/features/auth/CopyButton";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { isApiError } from "@/lib/errors";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  listAdminInvitations,
  resendAdminInvitation,
  revokeAdminInvitation,
  type AdminInvitationDto,
} from "./adminPowersApi";
import { buildInvitationLink } from "./staffInvitationView";
import { apiBasePath } from "@/lib/apiBase";

const STATUS_VIEWS: Record<
  string,
  { label: string; tone: string }
> = {
  PENDING: { label: "待接受", tone: "badge-info" },
  ACCEPTED: { label: "已接受", tone: "badge-success" },
  EXPIRED: { label: "已过期", tone: "badge-muted" },
  REVOKED: { label: "已撤销", tone: "badge-danger" },
};

const ROLE_LABELS: Record<string, string> = {
  TEACHER: "教师",
  ADMIN: "管理员",
};

/** A resend's exactly-once handoff (the create flow's grammar). */
interface ResentLink {
  email: string;
  link: string;
}

type Phase = "loading" | "ready" | "error";

export function InvitationListPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [items, setItems] = useState<AdminInvitationDto[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [revoking, setRevoking] = useState<AdminInvitationDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [resent, setResent] = useState<ResentLink | null>(null);

  const load = useCallback(() => {
    setPhase("loading");
    setError(null);
  }, []);

  // The fetch effect keys on BOTH the phase and the workspace's
  // issuance nudge: a refreshKey bump is stale-work (the ref remembers
  // the key the last fetch served), so the reload fires without any
  // setState ever running synchronously inside an effect (the
  // react-compiler rule — the relay-effect shape trips it).
  const lastFetchedKey = useRef(0);
  useEffect(() => {
    const staleNudge = lastFetchedKey.current !== refreshKey;
    if (phase !== "loading" && !staleNudge) {
      return;
    }
    lastFetchedKey.current = refreshKey;
    let cancelled = false;
    void (async () => {
      try {
        const page = await listAdminInvitations();
        if (!cancelled) {
          setItems(page.items);
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
  }, [phase, refreshKey]);

  async function run(action: () => Promise<void>) {
    if (busy) {
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await action();
    } catch (cause) {
      setNote(isApiError(cause) ? cause.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  function onRevokeConfirm() {
    const target = revoking;
    if (target === null) {
      return;
    }
    void run(async () => {
      await revokeAdminInvitation(target.id);
      setRevoking(null);
      setNote(`已撤销 ${target.email} 的邀请（原链接立即失效）。`);
      load();
    });
  }

  function onResend(row: AdminInvitationDto) {
    void run(async () => {
      // The wire carries the fresh one-time TOKEN; the copyable URL
      // composes through the create surface's own builder.
      const { token } = await resendAdminInvitation(row.id);
      setResent({
        email: row.email,
        link: buildInvitationLink(window.location.origin, apiBasePath(), token),
      });
      load();
    });
  }

  return (
    <section className="section" aria-label="邀请记录">
      <div className="section-head">
        <h2 className="section-title">邀请记录</h2>
        <Button variant="ghost" onClick={load} disabled={phase === "loading"}>
          刷新
        </Button>
      </div>
      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={error} onRetry={load} retryLabel="重新加载邀请记录" />
      ) : items.length === 0 ? (
        <p className="field-hint">还没有邀请记录。</p>
      ) : (
        <div className="table-scroll">
          <table className="staff-table" aria-label="邀请列表">
            <thead>
              <tr>
                <th>受邀邮箱</th>
                <th>角色</th>
                <th>状态</th>
                <th>签发人</th>
                <th>签发时间</th>
                <th>失效时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const status = STATUS_VIEWS[row.status] ?? {
                  label: row.status,
                  tone: "badge-muted",
                };
                const canResend = row.status !== "ACCEPTED";
                return (
                  <tr key={row.id}>
                    <td className="staff-cell-title">{row.email}</td>
                    <td>{ROLE_LABELS[row.role] ?? row.role}</td>
                    <td>
                      <span className={`badge ${status.tone}`}>{status.label}</span>
                    </td>
                    <td>{row.created_by}</td>
                    <td>
                      <time className="notif-time" dateTime={row.created_at}>
                        {formatDeadlineDateTime(parseServerInstant(row.created_at))}
                      </time>
                    </td>
                    <td>
                      <time className="notif-time" dateTime={row.expires_at}>
                        {formatDeadlineDateTime(parseServerInstant(row.expires_at))}
                      </time>
                    </td>
                    <td>
                      <div className="row-actions">
                        {row.status === "PENDING" ? (
                          <Button
                            variant="secondary"
                            onClick={() => setRevoking(row)}
                            disabled={busy}
                          >
                            撤销
                          </Button>
                        ) : null}
                        {canResend ? (
                          <Button
                            variant="secondary"
                            onClick={() => onResend(row)}
                            disabled={busy}
                          >
                            重发
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {note !== null ? (
        <p className="field-hint" role="status">
          {note}
        </p>
      ) : null}
      {resent !== null ? (
        <div className="alert alert-warning" role="alert">
          <p>
            <strong>新邀请链接只显示这一次。</strong>
            （重发给 {resent.email}；旧链接已立即失效，新链接同样一次性使用。）
          </p>
          <p className="secret-line">
            <span className="secret-label">新邀请链接</span>
            <span className="mono" data-testid="resend-link">
              {resent.link}
            </span>
          </p>
          <p className="copy-row">
            <CopyButton value={resent.link} label="复制新邀请链接" />
            <Button variant="ghost" onClick={() => setResent(null)}>
              收起
            </Button>
          </p>
        </div>
      ) : null}
      <Dialog
        open={revoking !== null}
        onOpenChange={(open) => {
          if (!open) {
            setRevoking(null);
          }
        }}
      >
        <DialogContent>
          <DialogTitle>撤销这条邀请？</DialogTitle>
          <p className="field-hint">
            将撤销发给 {revoking?.email} 的邀请，其链接立即失效、不可再接受。此操作会写入审计日志。
          </p>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRevoking(null)} disabled={busy}>
              取消
            </Button>
            <Button variant="primary" onClick={onRevokeConfirm} disabled={busy}>
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              <span>确认撤销</span>
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
