"use client";
/**
 * ApprovalsAdmin — the /admin/approvals page (auth-extension-II spec
 * §5): the PENDING_APPROVAL student queue with single approve/reject
 * and batch approve. The listing rides the admin users list's status
 * filter (no dedicated endpoint, the backend contract's ruling);
 * server verdicts replace rows in place, a reviewed student leaves the
 * queue (the filter no longer matches), so every action's honest
 * terminal is row removal.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  approveStudent,
  listPendingStudents,
  rejectStudent,
} from "@/features/auth/mobileLoginApi";
import type { PendingStudentDto } from "@/features/auth/mobileLoginApi";
import {
  describeApprovalError,
  validateRejectReason,
} from "./approvalsView";
import { Button } from "@/components/ui/button";
import { COPY_GENERIC_FAIL } from "@/lib/errors";

interface PendingRow {
  id: string;
  username: string;
  nickname: string;
  /** The backend's project-uniform mask, rendered verbatim. */
  phoneMasked: string | null;
  createdAt: string;
}

function toRow(user: PendingStudentDto): PendingRow {
  return {
    id: user.id,
    username: user.username,
    nickname: user.nickname,
    phoneMasked: user.phone_masked ?? null,
    createdAt: user.created_at,
  };
}

export function ApprovalsAdmin() {
  const [rows, setRows] = useState<PendingRow[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [reloadSeed, setReloadSeed] = useState(0);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [rejecting, setRejecting] = useState<PendingRow | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listPendingStudents().then(
      (page) => {
        if (!cancelled) {
          setRows(page.items.map(toRow));
          setPhase("ready");
        }
      },
      () => {
        if (!cancelled) {
          setPhase("error");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [reloadSeed]);

  const reload = useCallback(() => {
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  /** A reviewed student leaves the queue — the filter stops matching. */
  const dropRow = useCallback((id: string) => {
    setRows((previous) => previous.filter((row) => row.id !== id));
  }, []);

  async function onApprove(row: PendingRow) {
    if (busyIds.has(row.id)) {
      return;
    }
    setBusyIds((prev) => new Set(prev).add(row.id));
    setActionError(null);
    try {
      await approveStudent(row.id);
      dropRow(row.id);
    } catch (cause) {
      setActionError(cause);
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(row.id);
        return next;
      });
    }
  }

  async function onBatchApprove() {
    if (batchBusy || rows.length === 0) {
      return;
    }
    setBatchBusy(true);
    setActionError(null);
    const approved: string[] = [];
    try {
      for (const row of rows) {
        await approveStudent(row.id);
        approved.push(row.id);
      }
    } catch (cause) {
      setActionError(cause);
    } finally {
      setBatchBusy(false);
      setRows((previous) => previous.filter((row) => !approved.includes(row.id)));
      if (approved.length > 0 && actionError === null) {
        // Everything approved — a silent full clear.
      }
    }
  }

  const errorView =
    actionError !== null
      ? describeApprovalError(actionError, COPY_GENERIC_FAIL)
      : null;

  return (
    <section className="section" aria-label="注册审批">
      <div className="section-head">
        <h2 className="section-title">待审核学生</h2>
        <div className="row-actions">
          <Button variant="ghost" onClick={reload}>
            刷新
          </Button>
          <Button
            variant="primary"
            onClick={() => void onBatchApprove()}
            disabled={batchBusy || rows.length === 0}
            aria-busy={batchBusy}
          >
            {batchBusy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>全部通过（{rows.length}）</span>
          </Button>
        </div>
      </div>
      <p className="field-hint">
        待审核学生可登录浏览但不能领取、提交、兑换或评论。通过后立即解锁全部功能；拒绝将封禁该账号（可附理由）。
      </p>

      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={null} onRetry={reload} retryLabel="重新加载" />
      ) : rows.length === 0 ? (
        <EmptyState title="没有待审核的学生" hint="有新注册时从这里审核" />
      ) : (
        <div className="table-scroll">
          <table className="staff-table" aria-label="待审核学生列表">
            <thead>
              <tr>
                <th scope="col">学号</th>
                <th scope="col">昵称</th>
                <th scope="col">手机号</th>
                <th scope="col">注册时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.username}</td>
                  <td>{row.nickname}</td>
                  <td className="mono">{row.phoneMasked ?? "—"}</td>
                  <td>
                    <time dateTime={row.createdAt}>
                      {formatDeadlineDateTime(parseServerInstant(row.createdAt))}
                    </time>
                  </td>
                  <td>
                    <div className="row-actions">
                      <Button
                        variant="primary"
                        onClick={() => void onApprove(row)}
                        disabled={busyIds.has(row.id) || batchBusy}
                        aria-busy={busyIds.has(row.id)}
                      >
                        通过
                      </Button>
                      <Button
                        variant="secondary"
                        onClick={() => setRejecting(row)}
                        disabled={busyIds.has(row.id) || batchBusy}
                      >
                        拒绝
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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

      {rejecting !== null ? (
        <RejectDialog
          row={rejecting}
          onDone={() => {
            dropRow(rejecting.id);
            setRejecting(null);
          }}
          onCancel={() => setRejecting(null)}
        />
      ) : null}
    </section>
  );
}

function RejectDialog({
  row,
  onDone,
  onCancel,
}: {
  row: PendingRow;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | undefined>(undefined);
  const [summary, setSummary] = useState<{ message: string; requestId: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const error = validateRejectReason(reason) ?? undefined;
    setFieldError(error);
    setSummary(null);
    if (error !== undefined) {
      return;
    }
    setBusy(true);
    try {
      await rejectStudent(row.id, reason.trim().length > 0 ? reason.trim() : null);
      onDone();
    } catch (cause) {
      const view = describeApprovalError(cause, COPY_GENERIC_FAIL);
      setSummary({ message: view.message, requestId: view.requestId });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogContent asChild aria-labelledby="reject-title"
        onPointerDownOutside={(event) => { event.preventDefault(); }}>
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="reject-title">拒绝注册</DialogTitle>
          <p className="report-target">
            学号 <span className="mono">{row.username}</span>{" "}
            将被封禁（不可登录）。该学生可重新联系管理员申诉。
          </p>
          <div className="field">
            <label className="field-label" htmlFor="reject-reason">
              拒绝理由（选填，随审计记录）
            </label>
            <textarea
              id="reject-reason"
              className="input"
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={busy}
            />
            {fieldError !== undefined ? (
              <p className="field-error">{fieldError}</p>
            ) : null}
          </div>
          {summary !== null ? (
            <div className="alert alert-error" role="alert">
              <p>
                <span className="alert-marker" aria-hidden="true">!</span>
                {summary.message}
              </p>
              {summary.requestId !== null ? (
                <p className="req-id">请求 ID：{summary.requestId}</p>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button type="submit" variant="danger" disabled={busy} aria-busy={busy}>
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              <span>确认拒绝</span>
            </Button>
            <Button variant="secondary" onClick={onCancel} disabled={busy}>
              取消
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
