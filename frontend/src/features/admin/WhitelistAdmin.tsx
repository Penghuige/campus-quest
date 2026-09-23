"use client";
/**
 * WhitelistAdmin — the registration-whitelist page (spec §5.1; plan
 * Task 10 step 3): the preview/confirm import flow and the per-entry
 * enable/disable toggle.
 *
 * Import contract (backend `whitelist_admin.py`):
 * - preview is pure classification over the pasted text (one student
 *   number per line) — the confirm payload must carry the previewed
 *   importable set AND its digest back verbatim;
 * - confirm is all-or-nothing: a digest mismatch or any DB collision
 *   answers the typed 409 `CONFLICT` (the collision list rides
 *   `details.student_numbers` and renders in full);
 * - after any failure the preview is stale — back to a clean slate,
 *   re-preview is the only safe retry (the assignment-import stance).
 *
 * The toggle's reason is transport-OPTIONAL (it rides the per-entry
 * audit rows when present); disable still asks for one.
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
  confirmWhitelistImport,
  listWhitelist,
  previewWhitelistImport,
  toggleWhitelistEntry,
  type WhitelistEntryDto,
  type WhitelistPreviewDto,
} from "./adminApi";
import {
  adminReasonReady,
  canConfirmWhitelistImport,
  conflictStudentNumbers,
  describeAdminMutationError,
  whitelistCodeView,
  whitelistCountsText,
} from "./adminView";

const PAGE_LIMIT = 20;

export function WhitelistAdmin() {
  return (
    <>
      <WhitelistImport />
      <WhitelistEntries />
    </>
  );
}

// --- import (preview -> confirm) ---------------------------------------------------------

type ImportPhase =
  | { kind: "idle" }
  | { kind: "previewing" }
  | { kind: "preview"; preview: WhitelistPreviewDto }
  | { kind: "confirming"; preview: WhitelistPreviewDto }
  | { kind: "done"; created: number; enable: boolean };

function WhitelistImport() {
  const [content, setContent] = useState("");
  const [phase, setPhase] = useState<ImportPhase>({ kind: "idle" });
  const [enable, setEnable] = useState(true);
  const [error, setError] = useState<unknown>(null);

  function reset() {
    setContent("");
    setPhase({ kind: "idle" });
    setError(null);
  }

  async function onPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase.kind === "previewing") {
      return;
    }
    setError(null);
    setPhase({ kind: "previewing" });
    try {
      const preview = await previewWhitelistImport(content);
      setPhase({ kind: "preview", preview });
    } catch (cause) {
      setError(cause);
      setPhase({ kind: "idle" });
    }
  }

  async function onConfirm() {
    if (phase.kind !== "preview") {
      return;
    }
    setError(null);
    setPhase({ kind: "confirming", preview: phase.preview });
    try {
      const result = await confirmWhitelistImport({
        confirm_token: phase.preview.confirm_token,
        enable,
        student_numbers: [...phase.preview.importable],
      });
      setPhase({ kind: "done", created: result.created, enable: result.enable });
    } catch (cause) {
      // The digest binds this preview to its row set: any refusal means
      // the world moved — back to idle, re-preview is the only retry.
      setError(cause);
      setPhase({ kind: "idle" });
    }
  }

  const errorView =
    error !== null
      ? describeAdminMutationError(error, "导入失败，请稍后重试")
      : null;
  const collisions = error !== null ? conflictStudentNumbers(error) : [];

  return (
    <section className="section" aria-label="白名单批量导入">
      <div className="section-head">
        <h2 className="section-title">批量导入</h2>
        {phase.kind === "preview" || phase.kind === "confirming" ? (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={reset}
            disabled={phase.kind === "confirming"}
          >
            重新编辑
          </button>
        ) : null}
      </div>
      <p className="field-hint">
        每行一个学号（6-20 位 ASCII 数字，不接受全角形式）。导入前先预览逐行校验结果，确认后一次性写入；
        若与已有白名单冲突则整体不写入。
      </p>

      {phase.kind === "idle" || phase.kind === "previewing" ? (
        <form className="panel" onSubmit={onPreview}>
          <div className="field">
            <label className="field-label" htmlFor="whitelist-import-content">
              导入内容
            </label>
            <textarea
              id="whitelist-import-content"
              className="input"
              rows={6}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              disabled={phase.kind === "previewing"}
              placeholder={"20260001\n20260002"}
            />
          </div>
          <div className="dialog-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={content.trim().length === 0 || phase.kind === "previewing"}
              aria-busy={phase.kind === "previewing"}
            >
              {phase.kind === "previewing" ? (
                <span className="spinner" aria-hidden="true" />
              ) : null}
              <span>预览导入</span>
            </button>
          </div>
        </form>
      ) : null}

      {phase.kind === "preview" || phase.kind === "confirming" ? (
        <div className="import-preview">
          <p className="import-counts" aria-live="polite">
            {whitelistCountsText(phase.preview.counts)}。请核对逐行结果，确认后仅导入「可导入」的行。
          </p>
          <div className="table-scroll">
            <table className="staff-table" aria-label="导入预览逐行结果">
              <thead>
                <tr>
                  <th scope="col">行号</th>
                  <th scope="col">学号</th>
                  <th scope="col">判定</th>
                </tr>
              </thead>
              <tbody>
                {phase.preview.decisions.map((decision) => {
                  const code = whitelistCodeView(decision.code);
                  return (
                    <tr key={`${decision.row_number}-${decision.student_number ?? "x"}`}>
                      <td>第 {decision.row_number} 行</td>
                      <td className="mono">{decision.student_number ?? "—"}</td>
                      <td>
                        <span className={`badge badge-${code.tone}`}>{code.label}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="dialog-actions import-confirm-row">
            <label className="field-hint">
              <input
                type="checkbox"
                checked={enable}
                onChange={(event) => setEnable(event.target.checked)}
                disabled={phase.kind === "confirming"}
              />{" "}
              导入后立即启用（可导入注册）
            </label>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void onConfirm()}
              disabled={
                !canConfirmWhitelistImport(phase.preview) || phase.kind === "confirming"
              }
              aria-busy={phase.kind === "confirming"}
            >
              {phase.kind === "confirming" ? (
                <span className="spinner" aria-hidden="true" />
              ) : null}
              <span>确认导入 {phase.preview.importable.length} 行</span>
            </button>
          </div>
        </div>
      ) : null}

      {phase.kind === "done" ? (
        <div className="alert alert-success" role="status">
          <p>
            已成功导入 {phase.created} 个学号（{phase.enable ? "启用" : "停用"}状态）。
          </p>
          <p>
            <button type="button" className="btn btn-secondary" onClick={reset}>
              继续导入
            </button>
          </p>
        </div>
      ) : null}

      {errorView !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {errorView.message}
          </p>
          {collisions.length > 0 ? (
            <p>
              冲突学号：{collisions.join("、")}（本次导入未写入任何行，请移除后重新预览）
            </p>
          ) : null}
          {errorView.requestId !== null ? (
            <p className="req-id">请求 ID：{errorView.requestId}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

// --- entries (paginated + toggle) ---------------------------------------------------------

function WhitelistEntries() {
  const [items, setItems] = useState<WhitelistEntryDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [busyNumber, setBusyNumber] = useState<string | null>(null);
  const [disabling, setDisabling] = useState<WhitelistEntryDto | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listWhitelist({ limit: PAGE_LIMIT }).then(
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
  }, [reloadSeed]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listWhitelist({ limit: PAGE_LIMIT, offset: items.length });
      setItems((previous) =>
        mergeOffsetPage(previous, page.items, (row) => row.student_number),
      );
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  /** Window refresh at the loaded width (patterns §3: refetch at boundaries). */
  const refreshWindow = useCallback(async () => {
    try {
      const page = await listWhitelist({
        limit: Math.max(items.length, PAGE_LIMIT),
      });
      setItems(page.items);
      setTotal(page.total);
    } catch (cause) {
      setPhase("error");
      setError(cause);
    }
  }, [items.length]);

  const onToggled = useCallback(() => {
    setDisabling(null);
    setBusyNumber(null);
    void refreshWindow();
  }, [refreshWindow]);

  async function enableEntry(entry: WhitelistEntryDto) {
    if (busyNumber !== null) {
      return;
    }
    setBusyNumber(entry.student_number);
    setActionError(null);
    try {
      await toggleWhitelistEntry(entry.student_number, { enabled: true });
      await refreshWindow();
    } catch (cause) {
      setActionError(cause);
    } finally {
      setBusyNumber(null);
    }
  }

  const toggleErrorView =
    actionError !== null
      ? describeAdminMutationError(actionError, "操作失败，请稍后重试")
      : null;

  return (
    <section className="section" aria-label="白名单条目">
      <div className="section-head">
        <h2 className="section-title">白名单条目</h2>
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
        <EmptyState
          title="白名单为空"
          hint="通过上方批量导入添加允许注册的学号"
        />
      ) : (
        <>
          <div className="table-scroll">
            <table className="staff-table" aria-label="白名单列表">
              <thead>
                <tr>
                  <th scope="col">学号</th>
                  <th scope="col">状态</th>
                  <th scope="col">加入时间</th>
                  <th scope="col">停用时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry) => (
                  <tr key={entry.id}>
                    <td className="mono">{entry.student_number}</td>
                    <td>
                      <span
                        className={`badge badge-${entry.enabled ? "success" : "muted"}`}
                      >
                        {entry.enabled ? "启用" : "停用"}
                      </span>
                    </td>
                    <td>
                      <time className="notif-time" dateTime={entry.created_at}>
                        {formatDeadlineDateTime(parseServerInstant(entry.created_at))}
                      </time>
                    </td>
                    <td>
                      {entry.disabled_at !== null ? (
                        <time className="notif-time" dateTime={entry.disabled_at}>
                          {formatDeadlineDateTime(parseServerInstant(entry.disabled_at))}
                        </time>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      <div className="row-actions">
                        {entry.enabled ? (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => setDisabling(entry)}
                            disabled={busyNumber !== null}
                          >
                            停用
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => void enableEntry(entry)}
                            disabled={busyNumber !== null}
                            aria-busy={busyNumber === entry.student_number}
                          >
                            启用
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
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
      {toggleErrorView !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {toggleErrorView.message}
          </p>
        </div>
      ) : null}
      {disabling !== null ? (
        <WhitelistDisableDialog entry={disabling} onDone={onToggled} onCancel={() => setDisabling(null)} />
      ) : null}
    </section>
  );
}

/** Disable asks for an optional-but-encouraged reason (it rides the audit row). */
function WhitelistDisableDialog({
  entry,
  onDone,
  onCancel,
}: {
  entry: WhitelistEntryDto;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);

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
    setBusy(true);
    setError(null);
    try {
      await toggleWhitelistEntry(entry.student_number, {
        enabled: false,
        reason: adminReasonReady(reason) ? reason.trim() : null,
      });
      onDone();
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
      aria-labelledby="whitelist-disable-title"
      className="dialog"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="whitelist-disable-title" className="dialog-title">
          停用白名单条目
        </h3>
        <p className="report-target">
          停用后学号 <span className="mono">{entry.student_number}</span>{" "}
          将不能用于注册（已注册账号不受影响）。可随时重新启用。
        </p>
        <div className="field">
          <label className="field-label" htmlFor="whitelist-disable-reason">
            停用原因（选填，记入审计日志）
          </label>
          <textarea
            id="whitelist-disable-reason"
            className="input"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={busy}
          />
        </div>
        {errorView !== null ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {errorView.message}
            </p>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="btn btn-danger" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认停用</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}
