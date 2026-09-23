"use client";
/**
 * AdminUserAccounts — the account directory page (spec §5.7; plan Task
 * 10 step 3/4): role/status filters, offset pagination, and the three
 * audited status verbs (suspend / ban / reactivate), each behind a
 * reason-mandatory confirm dialog.
 *
 * G11 note: the directory DTO is governance-facts-only by construction
 * (username/nickname/role/status/created_at — no phone/email); the id
 * column renders so grants and audit filters have something to copy.
 * Every decision updates the row from the SERVER verdict (patterns §3);
 * a 409 CONFLICT (state flowed on elsewhere) renders through
 * `describeAdminMutationError` and suggests a refresh.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  banAdminUser,
  listAdminUsers,
  reactivateAdminUser,
  suspendAdminUser,
  type AccountStatusDto,
  type AdminUserDto,
  type RoleDto,
  type UserStatusDto,
} from "./adminApi";
import {
  accountActionView,
  accountActions,
  adminReasonReady,
  describeAdminMutationError,
  roleLabel,
  userStatusView,
  type AccountActionKind,
} from "./adminView";

const PAGE_LIMIT = 20;

const ROLE_FILTERS: readonly { value: RoleDto | ""; label: string }[] = [
  { value: "", label: "全部角色" },
  { value: "STUDENT", label: "学生" },
  { value: "TEACHER", label: "教师" },
  { value: "ADMIN", label: "管理员" },
];

const STATUS_FILTERS: readonly { value: UserStatusDto | ""; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "PENDING_PHONE", label: "待验证" },
  { value: "ACTIVE", label: "正常" },
  { value: "SUSPENDED", label: "已停用" },
  { value: "BANNED", label: "已封禁" },
];

type Filters = { role: RoleDto | ""; status: UserStatusDto | "" };

export function AdminUserAccounts() {
  const [filters, setFilters] = useState<Filters>({ role: "", status: "" });
  const [items, setItems] = useState<AdminUserDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [acting, setActing] = useState<{ kind: AccountActionKind; user: AdminUserDto } | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    listAdminUsers({
      limit: PAGE_LIMIT,
      role: filters.role === "" ? undefined : filters.role,
      status: filters.status === "" ? undefined : filters.status,
    }).then(
      (page) => {
        if (!cancelled) {
          setItems(page.items);
          setTotal(page.total);
          setPhase("ready");
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          setError(cause);
          setPhase("error");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [filters, reloadSeed]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listAdminUsers({
        limit: PAGE_LIMIT,
        offset: items.length,
        role: filters.role === "" ? undefined : filters.role,
        status: filters.status === "" ? undefined : filters.status,
      });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [filters, items.length, loadingMore]);

  /** Apply a decision: replace the row from the SERVER verdict (patterns §3). */
  const onStatusChanged = useCallback((verdict: AccountStatusDto) => {
    setActing(null);
    setItems((previous) =>
      previous.map((row) =>
        row.id === verdict.id ? { ...row, role: verdict.role, status: verdict.status } : row,
      ),
    );
  }, []);

  return (
    <section className="section" aria-label="账号目录">
      <div className="section-head">
        <h2 className="section-title">账号目录</h2>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => {
            setPhase("loading");
            setReloadSeed((seed) => seed + 1);
          }}
        >
          刷新
        </button>
      </div>

      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="user-filter-role">
              角色筛选
            </label>
            <select
              id="user-filter-role"
              className="input"
              value={filters.role}
              onChange={(event) => {
                const value = event.target.value as RoleDto | "";
                setFilters((previous) => ({ ...previous, role: value }));
                setPhase("loading");
              }}
            >
              {ROLE_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="user-filter-status">
              状态筛选
            </label>
            <select
              id="user-filter-status"
              className="input"
              value={filters.status}
              onChange={(event) => {
                const value = event.target.value as UserStatusDto | "";
                setFilters((previous) => ({ ...previous, status: value }));
                setPhase("loading");
              }}
            >
              {STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </form>

      {phase === "loading" ? (
        <SectionSkeleton lines={6} />
      ) : phase === "error" ? (
        <SectionError
          error={error}
          onRetry={() => {
            setPhase("loading");
            setReloadSeed((seed) => seed + 1);
          }}
          retryLabel="重新加载"
        />
      ) : items.length === 0 ? (
        <EmptyState title="没有符合条件的账号" hint="调整筛选条件后重试" />
      ) : (
        <>
          <div className="table-scroll">
            <table className="staff-table" aria-label="账号列表">
              <thead>
                <tr>
                  <th scope="col">学号 / 用户名</th>
                  <th scope="col">昵称</th>
                  <th scope="col">角色</th>
                  <th scope="col">状态</th>
                  <th scope="col">注册时间</th>
                  <th scope="col">用户 ID</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((user) => (
                  <UserRow key={user.id} user={user} onAct={(kind) => setActing({ kind, user })} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="list-footer">
            共 {total} 个账号{hasMorePages(items.length, total) ? "" : "（已全部加载）"}
          </p>
          {hasMorePages(items.length, total) ? (
            <div className="load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? <span className="spinner" aria-hidden="true" /> : null}
                <span>加载更多（{items.length}/{total}）</span>
              </button>
              {moreError !== null ? (
                <SectionError error={moreError} onRetry={() => void loadMore()} />
              ) : null}
            </div>
          ) : null}
        </>
      )}

      {acting !== null ? (
        <AccountStatusDialog
          kind={acting.kind}
          user={acting.user}
          onDone={onStatusChanged}
          onCancel={() => setActing(null)}
        />
      ) : null}
    </section>
  );
}

function UserRow({
  user,
  onAct,
}: {
  user: AdminUserDto;
  onAct: (kind: AccountActionKind) => void;
}) {
  const status = userStatusView(user.status);
  return (
    <tr>
      <td className="staff-cell-title mono">{user.username}</td>
      <td>{user.nickname}</td>
      <td>{roleLabel(user.role)}</td>
      <td>
        <span className={`badge badge-${status.tone}`}>{status.label}</span>
      </td>
      <td>
        <time className="notif-time" dateTime={user.created_at}>
          {formatDeadlineDateTime(parseServerInstant(user.created_at))}
        </time>
      </td>
      <td>
        <span className="mono" title="用户 ID（可用于授权与审计检索）">
          {user.id}
        </span>
      </td>
      <td>
        <div className="row-actions">
          {accountActions(user.status).map((action) => (
            <button
              key={action.kind}
              type="button"
              className={`btn ${action.buttonClass}`}
              onClick={() => onAct(action.kind)}
            >
              {action.label}
            </button>
          ))}
        </div>
      </td>
    </tr>
  );
}

/** The step-4 reason-mandatory confirm for one status verb (audited server-side). */
function AccountStatusDialog({
  kind,
  user,
  onDone,
  onCancel,
}: {
  kind: AccountActionKind;
  user: AdminUserDto;
  onDone: (verdict: AccountStatusDto) => void;
  onCancel: () => void;
}) {
  const action = accountActionView(kind);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (!adminReasonReady(reason)) {
      setFieldError("操作原因必填（将记入审计日志）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const verdict =
        kind === "suspend"
          ? await suspendAdminUser(user.id, reason.trim())
          : kind === "ban"
            ? await banAdminUser(user.id, reason.trim())
            : await reactivateAdminUser(user.id, reason.trim());
      onDone(verdict);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null
      ? describeAdminMutationError(error, "操作失败，请稍后重试")
      : null;

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="account-status-title"
      className="dialog"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="account-status-title" className="dialog-title">
          {action.title}
        </h3>
        <p className="report-target">{action.body(user)}</p>
        <div className="field">
          <label className="field-label" htmlFor="account-status-reason">
            操作原因
          </label>
          <textarea
            id="account-status-reason"
            className="input"
            rows={3}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              if (fieldError !== null) {
                setFieldError(null);
              }
            }}
            aria-invalid={fieldError !== null}
            disabled={busy}
            required
          />
          {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
        </div>
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
        <div className="dialog-actions">
          <button
            type="submit"
            className={`btn ${action.buttonClass === "btn-danger" ? "btn-danger" : "btn-primary"}`}
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>{action.confirmLabel}</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}
