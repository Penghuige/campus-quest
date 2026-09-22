"use client";
/**
 * RewardsAdmin — the reward-catalogue page (spec §16; plan Task 10
 * step 3/4): catalogue management (create / partial edit of
 * cost/stock/term-limit/time-window / disable) plus the REWARD_REVIEW
 * grant lifecycle (W3's scoped delegation, reason mandatory both ways).
 *
 * Frozen-contract read surface (see adminApi's module note): the
 * catalogue LISTING is the student `GET /rewards` — ENABLED items only.
 * A just-disabled row is therefore held from its admin verdict and keeps
 * rendering (with 已下架 state) until the next reload; on reload it is
 * gone from the listing. There is no admin catalogue view endpoint.
 *
 * Edit discipline (the backend's presence semantics): absent field =
 * unchanged, explicit null clears a nullable bound. The edit dialog
 * manages ONLY the fields the student listing carries — prefilling
 * fulfillment_instructions / requires_manual_review is impossible, so
 * they stay ABSENT on edit (never blindly clobbered); they are
 * settable at create.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { toDatetimeLocalValue } from "./teacherView";

import {
  createRewardItem,
  disableRewardItem,
  grantRewardReview,
  listRewardCatalogue,
  revokeRewardReview,
  updateRewardItem,
  type AdminRewardItemDto,
  type RewardCatalogueDto,
} from "./adminApi";
import {
  adminReasonReady,
  describeAdminMutationError,
  EMPTY_REWARD_FORM,
  rewardFormFromCatalogue,
  rewardFormToCreateBody,
  rewardFormToUpdateBody,
  rewardWindowText,
  type RewardEditSource,
  type RewardFormErrors,
  type RewardFormValues,
} from "./adminView";

export function RewardsAdmin() {
  return (
    <>
      <RewardCatalogue />
      <ReviewGrants />
    </>
  );
}

// --- catalogue -----------------------------------------------------------------------------

type Row =
  | { source: "catalogue"; row: RewardCatalogueDto }
  | { source: "verdict"; row: AdminRewardItemDto };

function rowId(row: Row): string {
  return row.row.id;
}

function RewardCatalogue() {
  const [rows, setRows] = useState<Row[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<RewardEditSource | null>(null);
  const [disabling, setDisabling] = useState<{ id: string; name: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    listRewardCatalogue().then(
      (page) => {
        if (!cancelled) {
          setRows(page.items.map((row) => ({ source: "catalogue", row }) as Row));
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

  /** Replace a row with its admin VERDICT (patterns §3 — server state at boundaries). */
  const onVerdict = useCallback((verdict: AdminRewardItemDto) => {
    setRows((previous) =>
      previous.map((entry) => (rowId(entry) === verdict.id ? { source: "verdict", row: verdict } : entry)),
    );
  }, []);

  const onCreated = useCallback((verdict: AdminRewardItemDto) => {
    setCreating(false);
    setRows((previous) => [...previous, { source: "verdict", row: verdict }]);
  }, []);

  return (
    <section className="section" aria-label="奖励目录">
      <div className="section-head">
        <h2 className="section-title">奖励目录</h2>
        <div className="row-actions">
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
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setCreating(true)}
          >
            新建奖励
          </button>
        </div>
      </div>
      <p className="field-hint">
        目录读取使用学生端列表（仅显示上架项目）；下架后的项目不再出现在列表中，直至重新上架。
        积分/库存/限购/时间窗的调整只影响之后的兑换请求，已创建的兑换保持原快照。
      </p>
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
      ) : rows.length === 0 ? (
        <EmptyState title="目录为空" hint="点击「新建奖励」创建第一个兑换奖励" />
      ) : (
        <div className="table-scroll">
          <table className="staff-table" aria-label="奖励目录列表">
            <thead>
              <tr>
                <th scope="col">名称</th>
                <th scope="col">积分</th>
                <th scope="col">库存</th>
                <th scope="col">学期限购</th>
                <th scope="col">可用时间窗</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((entry) => (
                <CatalogueRow
                  key={rowId(entry)}
                  entry={entry}
                  windowText={rewardWindowText(
                    entry.row,
                    (iso) => formatDeadlineDateTime(parseServerInstant(iso)),
                  )}
                  onEdit={() => setEditing(entry.row)}
                  onDisable={() =>
                    setDisabling({ id: entry.row.id, name: entry.row.name })
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating ? (
        <RewardFormDialog
          title="新建奖励"
          submitLabel="创建奖励"
          initial={EMPTY_REWARD_FORM}
          showCreateFields={true}
          onDone={onCreated}
          onCancel={() => setCreating(false)}
        />
      ) : null}
      {editing !== null ? (
        <RewardFormDialog
          title={`编辑奖励：${editing.name}`}
          submitLabel="保存修改"
          initial={rewardFormFromCatalogue(editing, toDatetimeLocalValue)}
          showCreateFields={false}
          row={editing}
          onDone={onVerdict}
          onCancel={() => setEditing(null)}
        />
      ) : null}      {disabling !== null ? (
        <DisableRewardDialog
          target={disabling}
          onDone={onVerdict}
          onCancel={() => setDisabling(null)}
        />
      ) : null}
    </section>
  );
}

function CatalogueRow({
  entry,
  windowText,
  onEdit,
  onDisable,
}: {
  entry: Row;
  windowText: string;
  onEdit: () => void;
  onDisable: () => void;
}) {
  const row = entry.row;
  // The student listing carries enabled items only; the verdict row
  // carries the server's own enabled flag (kept visible after 下架).
  const enabled = entry.source === "verdict" ? entry.row.enabled : true;
  const stock = row.stock === null ? "不限" : String(row.stock);
  const limit = row.per_user_term_limit === null ? "不限" : String(row.per_user_term_limit);
  return (
    <tr>
      <td className="staff-cell-title">{row.name}</td>
      <td className="meta-num">{row.point_cost}</td>
      <td className="meta-num">{stock}</td>
      <td className="meta-num">{limit}</td>
      <td>{windowText}</td>
      <td>
        <span className={`badge badge-${enabled ? "success" : "muted"}`}>
          {enabled ? "上架" : "已下架"}
        </span>
      </td>
      <td>
        <div className="row-actions">
          <button type="button" className="btn btn-secondary" onClick={onEdit}>
            编辑
          </button>
          {enabled ? (
            <button type="button" className="btn btn-danger" onClick={onDisable}>
              下架
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

/** Shared create/edit dialog (field set differs; reason mandatory both ways). */
function RewardFormDialog({
  title,
  submitLabel,
  initial,
  showCreateFields,
  row,
  onDone,
  onCancel,
}: {
  title: string;
  submitLabel: string;
  initial: RewardFormValues;
  /** Create manages description-bounds + instructions + review flag; edit does not. */
  showCreateFields: boolean;
  row?: RewardEditSource;
  onDone: (verdict: AdminRewardItemDto) => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [values, setValues] = useState<RewardFormValues>(initial);
  const [errors, setErrors] = useState<RewardFormErrors>({});
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  function set<K extends keyof RewardFormValues>(key: K, value: RewardFormValues[K]) {
    setValues((previous) => ({ ...previous, [key]: value }));
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (!adminReasonReady(values.reason)) {
      setReasonError("操作原因必填（将记入审计日志）");
      return;
    }
    setReasonError(null);
    if (row === undefined) {
      const result = rewardFormToCreateBody(values);
      setErrors(result.errors);
      if (!result.ok || result.body === undefined) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        onDone(await createRewardItem(result.body));
      } catch (cause) {
        setError(cause);
      } finally {
        setBusy(false);
      }
      return;
    }
    const result = rewardFormToUpdateBody(values, row);
    setErrors(result.errors);
    if (!result.ok || result.body === undefined) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onDone(await updateRewardItem(row.id, result.body));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null
      ? describeAdminMutationError(error, "保存失败，请稍后重试")
      : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog dialog-wide"
      aria-labelledby="reward-form-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="reward-form-title" className="dialog-title">
          {title}
        </h3>
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="reward-name">名称</label>
            <input
              id="reward-name"
              className="input"
              value={values.name}
              onChange={(event) => set("name", event.target.value)}
              aria-invalid={errors.name !== undefined}
              disabled={busy}
              required
            />
            {errors.name !== undefined ? <p className="field-error">{errors.name}</p> : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="reward-cost">兑换积分</label>
            <input
              id="reward-cost"
              className="input"
              inputMode="numeric"
              value={values.pointCost}
              onChange={(event) => set("pointCost", event.target.value)}
              aria-invalid={errors.pointCost !== undefined}
              disabled={busy}
              required
            />
            {errors.pointCost !== undefined ? (
              <p className="field-error">{errors.pointCost}</p>
            ) : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="reward-stock">库存（留空 = 不限）</label>
            <input
              id="reward-stock"
              className="input"
              inputMode="numeric"
              value={values.stock}
              onChange={(event) => set("stock", event.target.value)}
              aria-invalid={errors.stock !== undefined}
              disabled={busy}
            />
            {errors.stock !== undefined ? <p className="field-error">{errors.stock}</p> : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="reward-term-limit">
              学期限购（留空 = 不限）
            </label>
            <input
              id="reward-term-limit"
              className="input"
              inputMode="numeric"
              value={values.perUserTermLimit}
              onChange={(event) => set("perUserTermLimit", event.target.value)}
              aria-invalid={errors.perUserTermLimit !== undefined}
              disabled={busy}
            />
            {errors.perUserTermLimit !== undefined ? (
              <p className="field-error">{errors.perUserTermLimit}</p>
            ) : null}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="reward-from">可用起（留空 = 不限）</label>
            <input
              id="reward-from"
              className="input"
              type="datetime-local"
              value={values.availableFromLocal}
              onChange={(event) => set("availableFromLocal", event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="reward-until">可用止（留空 = 不限）</label>
            <input
              id="reward-until"
              className="input"
              type="datetime-local"
              value={values.availableUntilLocal}
              onChange={(event) => set("availableUntilLocal", event.target.value)}
              disabled={busy}
            />
          </div>
        </div>
        {errors.window !== undefined ? <p className="field-error">{errors.window}</p> : null}
        {showCreateFields ? (
          <>
            <div className="field">
              <label className="field-label" htmlFor="reward-description">描述（选填）</label>
              <textarea
                id="reward-description"
                className="input"
                rows={2}
                value={values.description}
                onChange={(event) => set("description", event.target.value)}
                disabled={busy}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="reward-instructions">
                发放说明（选填，学生兑换后可见）
              </label>
              <textarea
                id="reward-instructions"
                className="input"
                rows={2}
                value={values.fulfillmentInstructions}
                onChange={(event) => set("fulfillmentInstructions", event.target.value)}
                disabled={busy}
              />
            </div>
            <div className="field">
              <label className="field-hint">
                <input
                  type="checkbox"
                  checked={values.requiresManualReview}
                  onChange={(event) => set("requiresManualReview", event.target.checked)}
                  disabled={busy}
                />{" "}
                需要人工审核兑换
              </label>
            </div>
          </>
        ) : (
          <p className="field-hint">
            编辑仅管理目录读取到的字段（名称/积分/库存/限购/时间窗/描述）；发放说明与审核标记在创建时设置，编辑不会改动。
          </p>
        )}
        <div className="field">
          <label className="field-label" htmlFor="reward-reason">操作原因</label>
          <textarea
            id="reward-reason"
            className="input"
            rows={2}
            value={values.reason}
            onChange={(event) => {
              set("reason", event.target.value);
              if (reasonError !== null) {
                setReasonError(null);
              }
            }}
            aria-invalid={reasonError !== null}
            disabled={busy}
            required
          />
          {reasonError !== null ? <p className="field-error">{reasonError}</p> : null}
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
          <button type="submit" className="btn btn-primary" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>{submitLabel}</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

/** 下架 — the dedicated, reason-requiring transition (idempotent replay server-side). */
function DisableRewardDialog({
  target,
  onDone,
  onCancel,
}: {
  target: { id: string; name: string };
  onDone: (verdict: AdminRewardItemDto) => void;
  onCancel: () => void;
}) {
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
      setFieldError("下架原因必填（将记入审计日志）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      onDone(await disableRewardItem(target.id, reason.trim()));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null
      ? describeAdminMutationError(error, "下架失败，请稍后重试")
      : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="reward-disable-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="reward-disable-title" className="dialog-title">
          下架奖励
        </h3>
        <p className="report-target">
          下架后「{target.name}」不再对学生可见、不能发起新兑换；已创建的兑换保持原快照并继续处理。
          下架原因必填并记入审计日志。
        </p>
        <div className="field">
          <label className="field-label" htmlFor="reward-disable-reason">下架原因</label>
          <textarea
            id="reward-disable-reason"
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
          </div>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="btn btn-danger" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认下架</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

// --- review grants (W3 scoped delegation) --------------------------------------------------

function ReviewGrants() {
  return (
    <section className="section" aria-label="审阅授权管理">
      <div className="section-head">
        <h2 className="section-title">兑换审阅授权</h2>
      </div>
      <p className="field-hint">
        将全局兑换审阅权限授予教师（被授权教师可处理兑换审核队列）；撤销立即生效。
        教师的用户 ID 可在「用户与账户」页查询；授权与撤销的记录见审计日志。
      </p>
      <div className="workbench-columns">
        <GrantForm mode="grant" />
        <GrantForm mode="revoke" />
      </div>
    </section>
  );
}

function GrantForm({ mode }: { mode: "grant" | "revoke" }) {
  const [teacherId, setTeacherId] = useState("");
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (teacherId.trim().length === 0) {
      setFieldError("请填写教师的用户 ID");
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
      if (mode === "grant") {
        await grantRewardReview(teacherId.trim(), reason.trim());
      } else {
        await revokeRewardReview(teacherId.trim(), reason.trim());
      }
      setDone(true);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null
      ? describeAdminMutationError(
          error,
          mode === "grant" ? "授权失败，请稍后重试" : "撤销失败，请稍后重试",
        )
      : null;

  return (
    <form className="panel moderation-panel" onSubmit={onSubmit} noValidate>
      <h4 className="section-title">{mode === "grant" ? "授予审阅权限" : "撤销审阅权限"}</h4>
      <div className="field">
        <label className="field-label" htmlFor={`grant-teacher-${mode}`}>
          教师用户 ID
        </label>
        <input
          id={`grant-teacher-${mode}`}
          className="input mono"
          value={teacherId}
          onChange={(event) => {
            setTeacherId(event.target.value);
            if (fieldError !== null) {
              setFieldError(null);
            }
          }}
          aria-invalid={fieldError !== null}
          disabled={busy || done}
          placeholder="00000000-0000-0000-0000-000000000000"
          required
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`grant-reason-${mode}`}>
          操作原因
        </label>
        <textarea
          id={`grant-reason-${mode}`}
          className="input"
          rows={2}
          value={reason}
          onChange={(event) => {
            setReason(event.target.value);
            if (fieldError !== null) {
              setFieldError(null);
            }
          }}
          aria-invalid={fieldError !== null}
          disabled={busy || done}
          required
        />
        {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
      </div>
      {done ? (
        <div className="alert alert-success" role="status">
          <p>
            {mode === "grant" ? "已授予审阅权限。" : "已撤销审阅权限。"}
            （授权状态无查询接口；可在审计日志中核对操作记录）
          </p>
        </div>
      ) : null}
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
          className={`btn ${mode === "grant" ? "btn-primary" : "btn-danger"}`}
          disabled={busy || done}
          aria-busy={busy}
        >
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          <span>{mode === "grant" ? "确认授予" : "确认撤销"}</span>
        </button>
        {done ? (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setTeacherId("");
              setReason("");
              setDone(false);
            }}
          >
            再操作一次
          </button>
        ) : null}
      </div>
    </form>
  );
}
