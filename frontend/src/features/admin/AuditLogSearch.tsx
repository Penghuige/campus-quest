"use client";
/**
 * AuditLogSearch — the read-only audit检索 page (spec §30; plan Task
 * 10 step 3): action / actor / target equality filters over the
 * newest-first offset page. No mutations exist on this surface, and the
 * anonymous-identity reveal deliberately does NOT live here — it stays
 * in the community-governance context's explicit dialog.
 *
 * Rows are admin-domain data (actor ids, reasons, §30 snapshots); the
 * details/before/after snapshots render collapsed (they can be large)
 * as pretty-printed JSON, verbatim — write-side redaction is the
 * backend's invariant, the reader adds and removes nothing.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import { listAuditLogs, type AuditLogDto } from "./adminApi";
import { roleLabel } from "./adminView";

const PAGE_LIMIT = 20;

export function AuditLogSearch() {
  const [filters, setFilters] = useState({ action: "", actor: "", targetType: "" });
  const [items, setItems] = useState<AuditLogDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [applied, setApplied] = useState(filters);

  useEffect(() => {
    let cancelled = false;
    listAuditLogs({
      limit: PAGE_LIMIT,
      action: applied.action,
      actorUserId: applied.actor,
      targetType: applied.targetType,
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
  }, [applied]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listAuditLogs({
        limit: PAGE_LIMIT,
        offset: items.length,
        action: applied.action,
        actorUserId: applied.actor,
        targetType: applied.targetType,
      });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [applied, items.length, loadingMore]);

  function onSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPhase("loading");
    setApplied({ ...filters });
  }

  return (
    <section className="section" aria-label="审计日志检索">
      <div className="section-head">
        <h2 className="section-title">审计日志</h2>
      </div>
      <p className="field-hint">
        只读检索，按操作名 / 操作者 / 目标类型精确过滤（均需完整匹配）。
      </p>
      <form className="panel" onSubmit={onSearch}>
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="audit-filter-action">
              操作名（如 USER_SUSPENDED）
            </label>
            <input
              id="audit-filter-action"
              className="input mono"
              value={filters.action}
              onChange={(event) =>
                setFilters((previous) => ({ ...previous, action: event.target.value }))
              }
              placeholder="USER_SUSPENDED"
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-filter-actor">
              操作者用户 ID
            </label>
            <input
              id="audit-filter-actor"
              className="input mono"
              value={filters.actor}
              onChange={(event) =>
                setFilters((previous) => ({ ...previous, actor: event.target.value }))
              }
              placeholder="00000000-0000-0000-0000-000000000000"
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-filter-target">
              目标类型（如 system_setting）
            </label>
            <input
              id="audit-filter-target"
              className="input mono"
              value={filters.targetType}
              onChange={(event) =>
                setFilters((previous) => ({ ...previous, targetType: event.target.value }))
              }
              placeholder="system_setting"
            />
          </div>
        </div>
        <div className="dialog-actions">
          <button type="submit" className="btn btn-primary" disabled={phase === "loading"}>
            检索
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setFilters({ action: "", actor: "", targetType: "" });
              setPhase("loading");
              setApplied({ action: "", actor: "", targetType: "" });
            }}
          >
            清空条件
          </button>
        </div>
      </form>

      {phase === "loading" ? (
        <SectionSkeleton lines={6} />
      ) : phase === "error" ? (
        <SectionError
          error={error}
          onRetry={() => {
            setPhase("loading");
            setApplied({ ...filters });
          }}
          retryLabel="重新加载"
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="没有匹配的审计记录"
          hint="调整过滤条件后重新检索；操作名与目标类型需完整匹配"
        />
      ) : (
        <>
          <div className="table-scroll">
            <table className="staff-table" aria-label="审计日志列表">
              <thead>
                <tr>
                  <th scope="col">时间</th>
                  <th scope="col">操作</th>
                  <th scope="col">操作者</th>
                  <th scope="col">目标</th>
                  <th scope="col">原因</th>
                  <th scope="col">快照</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <AuditRow key={row.id} row={row} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="list-footer">
            共 {total} 条{hasMorePages(items.length, total) ? "" : "（已全部加载）"}
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
    </section>
  );
}

function AuditRow({ row }: { row: AuditLogDto }) {
  const hasSnapshot =
    (row.details !== null && Object.keys(row.details).length > 0) ||
    row.before_snapshot !== null ||
    row.after_snapshot !== null;
  return (
    <tr>
      <td>
        <time className="notif-time" dateTime={row.created_at}>
          {formatDeadlineDateTime(parseServerInstant(row.created_at))}
        </time>
      </td>
      <td className="mono">{row.action}</td>
      <td>
        <span className="mono" title={row.actor_user_id}>
          {roleLabel(row.actor_role)} {row.actor_user_id.slice(0, 8)}…
        </span>
      </td>
      <td>
        <span className="mono">
          {row.target_type} {row.target_id.slice(0, 8)}
          {row.target_id.length > 8 ? "…" : ""}
        </span>
      </td>
      <td>{row.reason ?? "—"}</td>
      <td>
        {hasSnapshot ? (
          <details>
            <summary>查看</summary>
            <div className="report-preview">
              {row.details !== null ? (
                <pre className="report-sample">
                  {JSON.stringify(row.details, null, 2)}
                </pre>
              ) : null}
              {row.before_snapshot !== null ? (
                <>
                  <p className="field-hint">变更前</p>
                  <pre className="report-sample">
                    {JSON.stringify(row.before_snapshot, null, 2)}
                  </pre>
                </>
              ) : null}
              {row.after_snapshot !== null ? (
                <>
                  <p className="field-hint">变更后</p>
                  <pre className="report-sample">
                    {JSON.stringify(row.after_snapshot, null, 2)}
                  </pre>
                </>
              ) : null}
            </div>
          </details>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}
