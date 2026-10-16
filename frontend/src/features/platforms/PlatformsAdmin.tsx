"use client";
/**
 * PlatformsAdmin — the /admin/platforms page (platform-registry spec
 * 2026-10-16 §4): the registry's full listing (disabled rows included)
 * plus create / rename / reorder / enable-toggle. Lifecycle model (the
 * spec's simplification): NO delete — a referenced platform only ever
 * toggles; `code` is immutable after creation.
 *
 * Pattern: the RewardsAdmin catalogue family (list + create dialog +
 * per-row edit dialog, server verdicts replace rows in place, the
 * shared admin mutation-error envelope with the CONFLICT copy for a
 * duplicate code).
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

import {
  createAdminPlatform,
  listAdminPlatforms,
  updateAdminPlatform,
} from "@/features/platforms/platformApi";
import {
  describePlatformAdminError,
  validatePlatformCode,
  validatePlatformDisplayName,
  validatePlatformSortOrder,
  type AdminPlatformDto,
} from "@/features/platforms/platformView";
import { Button } from "@/components/ui/button";

export function PlatformsAdmin() {
  const [rows, setRows] = useState<AdminPlatformDto[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [reloadSeed, setReloadSeed] = useState(0);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminPlatformDto | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listAdminPlatforms().then(
      (list) => {
        if (!cancelled) {
          setRows(list);
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

  /** The lint-driven state shape: loading is set by the TRIGGER (the
   * initial mount state / the reload buttons), never inside the effect. */
  const reload = useCallback(() => {
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  const onVerdict = useCallback((verdict: AdminPlatformDto) => {
    setRows((previous) =>
      previous.map((row) => (row.code === verdict.code ? verdict : row)),
    );
  }, []);

  const onCreated = useCallback(() => {
    setCreating(false);
    reload();
  }, [reload]);

  const errorView =
    actionError !== null
      ? describePlatformAdminError(actionError, "操作失败，请稍后重试")
      : null;

  return (
    <section className="section" aria-label="平台管理">
      <div className="section-head">
        <h2 className="section-title">平台注册表</h2>
        <div className="row-actions">
          <Button variant="ghost" onClick={reload}>
            刷新
          </Button>
          <Button variant="primary" onClick={() => setCreating(true)}>
            新建平台
          </Button>
        </div>
      </div>
      <p className="field-hint">
        任务分配引用平台注册表。代码创建后不可改；已被任务引用的平台只能启停（停用=不可用于新任务，存量分配不受影响），不提供删除。
      </p>

      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={null} onRetry={reload} retryLabel="重新加载" />
      ) : rows.length === 0 ? (
        <EmptyState title="注册表为空" hint="通过「新建平台」添加第一个平台" />
      ) : (
        <div className="table-scroll">
          <table className="staff-table" aria-label="平台注册表">
            <thead>
              <tr>
                <th scope="col">代码</th>
                <th scope="col">显示名</th>
                <th scope="col">排序</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.code}>
                  <td className="mono">{row.code}</td>
                  <td>{row.display_name}</td>
                  <td className="mono">{row.sort_order}</td>
                  <td>
                    <span className={`badge badge-${row.enabled ? "success" : "muted"}`}>
                      {row.enabled ? "启用" : "停用"}
                    </span>
                  </td>
                  <td>
                    <div className="row-actions">
                      <Button variant="secondary" onClick={() => setEditing(row)}>
                        编辑
                      </Button>
                      <Button
                        variant="secondary"
                        disabled={actionError !== null}
                        onClick={() => void onToggle(row)}
                      >
                        {row.enabled ? "停用" : "启用"}
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

      {creating ? (
        <CreatePlatformDialog onDone={onCreated} onCancel={() => setCreating(false)} />
      ) : null}
      {editing !== null ? (
        <EditPlatformDialog
          row={editing}
          onDone={(verdict) => {
            setEditing(null);
            onVerdict(verdict);
          }}
          onCancel={() => setEditing(null)}
        />
      ) : null}
    </section>
  );

  async function onToggle(row: AdminPlatformDto) {
    setActionError(null);
    try {
      const verdict = await updateAdminPlatform(row.code, {
        enabled: !row.enabled,
      });
      onVerdict(verdict);
    } catch (cause) {
      setActionError(cause);
    }
  }
}

/** Create: code + display_name + sort_order (code immutable from here). */
function CreatePlatformDialog({
  onDone,
  onCancel,
}: {
  onDone: (verdict: AdminPlatformDto) => void;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [sortOrder, setSortOrder] = useState("100");
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<"code" | "display_name" | "sort_order", string>>>({});
  const [summary, setSummary] = useState<{ message: string; requestId: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const errors = {
      code: validatePlatformCode(code),
      display_name: validatePlatformDisplayName(displayName),
      sort_order: validatePlatformSortOrder(sortOrder),
    };
    setFieldErrors(errors);
    setSummary(null);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }
    setBusy(true);
    try {
      const verdict = await createAdminPlatform({
        code: code.trim(),
        display_name: displayName.trim(),
        sort_order: Number(sortOrder.trim()),
      });
      onDone(verdict);
    } catch (cause) {
      const view = describePlatformAdminError(cause, "创建失败，请稍后重试");
      setSummary({ message: view.message, requestId: view.requestId });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogContent asChild aria-labelledby="platform-create-title"
        onPointerDownOutside={(event) => { event.preventDefault(); }}>
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="platform-create-title">新建平台</DialogTitle>
          <div className="field">
            <label className="field-label" htmlFor="platform-code">平台代码（创建后不可改）</label>
            <input
              id="platform-code"
              className="input mono"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="boss-zhipin"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              disabled={busy}
              aria-invalid={fieldErrors.code !== undefined || undefined}
            />
            {fieldErrors.code !== undefined ? (
              <p className="field-error">{fieldErrors.code}</p>
            ) : (
              <p className="field-hint">仅小写字母、数字与连字符；用于任务导入与 API。</p>
            )}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="platform-display-name">显示名</label>
            <input
              id="platform-display-name"
              className="input"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="BOSS直聘"
              disabled={busy}
              aria-invalid={fieldErrors.display_name !== undefined || undefined}
            />
            {fieldErrors.display_name !== undefined ? (
              <p className="field-error">{fieldErrors.display_name}</p>
            ) : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="platform-sort">排序值</label>
            <input
              id="platform-sort"
              className="input mono"
              value={sortOrder}
              onChange={(event) => setSortOrder(event.target.value)}
              inputMode="numeric"
              disabled={busy}
              aria-invalid={fieldErrors.sort_order !== undefined || undefined}
            />
            {fieldErrors.sort_order !== undefined ? (
              <p className="field-error">{fieldErrors.sort_order}</p>
            ) : (
              <p className="field-hint">学生端平台列表按此值升序排列。</p>
            )}
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
            <Button type="submit" variant="primary" disabled={busy} aria-busy={busy}>
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              <span>创建平台</span>
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

/** Edit: display_name + sort_order (+ enable via the row toggle). */
function EditPlatformDialog({
  row,
  onDone,
  onCancel,
}: {
  row: AdminPlatformDto;
  onDone: (verdict: AdminPlatformDto) => void;
  onCancel: () => void;
}) {
  const [displayName, setDisplayName] = useState(row.display_name);
  const [sortOrder, setSortOrder] = useState(String(row.sort_order));
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<"display_name" | "sort_order", string>>>({});
  const [summary, setSummary] = useState<{ message: string; requestId: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const errors = {
      display_name: validatePlatformDisplayName(displayName),
      sort_order: validatePlatformSortOrder(sortOrder),
    };
    setFieldErrors(errors);
    setSummary(null);
    if (Object.values(errors).some((message) => message !== undefined)) {
      return;
    }
    setBusy(true);
    try {
      const verdict = await updateAdminPlatform(row.code, {
        display_name: displayName.trim(),
        sort_order: Number(sortOrder.trim()),
      });
      onDone(verdict);
    } catch (cause) {
      const view = describePlatformAdminError(cause, "保存失败，请稍后重试");
      setSummary({ message: view.message, requestId: view.requestId });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !busy) onCancel(); }}>
      <DialogContent asChild aria-labelledby="platform-edit-title"
        onPointerDownOutside={(event) => { event.preventDefault(); }}>
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="platform-edit-title">编辑平台</DialogTitle>
          <p className="report-target">
            代码 <span className="mono">{row.code}</span> 创建后不可修改；此处仅改显示名与排序。
          </p>
          <div className="field">
            <label className="field-label" htmlFor="platform-edit-display-name">显示名</label>
            <input
              id="platform-edit-display-name"
              className="input"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              disabled={busy}
              aria-invalid={fieldErrors.display_name !== undefined || undefined}
            />
            {fieldErrors.display_name !== undefined ? (
              <p className="field-error">{fieldErrors.display_name}</p>
            ) : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="platform-edit-sort">排序值</label>
            <input
              id="platform-edit-sort"
              className="input mono"
              value={sortOrder}
              onChange={(event) => setSortOrder(event.target.value)}
              inputMode="numeric"
              disabled={busy}
              aria-invalid={fieldErrors.sort_order !== undefined || undefined}
            />
            {fieldErrors.sort_order !== undefined ? (
              <p className="field-error">{fieldErrors.sort_order}</p>
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
            <Button type="submit" variant="primary" disabled={busy} aria-busy={busy}>
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              <span>保存修改</span>
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
