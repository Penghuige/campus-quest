"use client";
/**
 * SystemAdmin — the /admin/system page (plan Task 10 step 3/4): the
 * five typed system-settings keys (edit + per-key version display),
 * NotificationTemplate administration, the notification-delivery
 * failure query (provider-safe error metadata, spec §25.4), and the two
 * named state repairs (release-occupied-assignment /
 * force-fail-delivery).
 *
 * Settings contract notes (backend system/service.py):
 * - stored forms are canonical per key (JSON array for emoji, decimal
 *   text for the abandon limit, "true"/"false" for the network flag,
 *   comma-separated CIDRs); `null` value means "no row — the deployment
 *   seed decides" (G7) and the readers here parse accordingly;
 * - the transport carries the OPTIONAL `reason` on settings PUTs (T10's
 *   audit-completeness gap-fill): the confirm dialog collects it beside
 *   the explicit 旧值 → 新值 diff and it rides the write's audit row —
 *   blank-after-trim is omitted (None is legal server-side), so the
 *   dialog never blocks on it, and the wrapper never sends a
 *   whitespace-only reason (that would be the typed 422);
 * - the MANAGEMENT_NETWORK_* pair has the cross-key rule: enabling with
 *   an empty effective CIDR list (or emptying it while enabled) is the
 *   typed 422, rendered through `describeAdminMutationError`.
 *
 * Templates gained an admin listing in the contract (T10 gap-fill,
 * `GET /admin/notification-templates` — disabled rows included), but no
 * page consumes it yet: the create/update/enable/disable responses
 * remain this panel's only row source, so it still edits by id (from
 * the create response or the audit trail) until that wiring lands.
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
  createNotificationTemplate,
  disableNotificationTemplate,
  enableNotificationTemplate,
  forceFailDelivery,
  getCurrentAcademicTerm,
  listNotificationFailures,
  listSystemSettings,
  putAbandonDailyLimit,
  putCurrentAcademicTerm,
  putEmojiWhitelist,
  putManagementNetworkCidrs,
  putManagementNetworkEnabled,
  releaseOccupiedAssignment,
  updateNotificationTemplate,
  type AdminNotificationTemplateDto,
  type CurrentAcademicTermDto,
  type NotificationFailureDto,
  type SystemSettingItemDto,
  type TemplateChannelDto,
  type TemplateEventTypeDto,
} from "./adminApi";
import {
  adminReasonReady,
  describeAdminMutationError,
  parseAbandonLimitValue,
  parseCidrsSettingValue,
  parseEmojiSettingValue,
  parseNetworkEnabledValue,
  SETTING_KEY_ABANDON_DAILY_LIMIT,
  SETTING_KEY_CURRENT_ACADEMIC_TERM,
  SETTING_KEY_EMOJI_WHITELIST,
  SETTING_KEY_MANAGEMENT_NETWORK_CIDRS,
  SETTING_KEY_MANAGEMENT_NETWORK_ENABLED,
  SETTING_KEY_VIEWS,
  settingDiffText,
  settingValueText,
  settingVersionText,
  TEMPLATE_CHANNEL_OPTIONS,
  TEMPLATE_EVENT_TYPE_OPTIONS,
} from "./adminView";

export function SystemAdmin() {
  return (
    <>
      <SettingsSection />
      <TemplatesSection />
      <FailuresSection />
      <RepairsSection />
    </>
  );
}

// --- typed system settings -------------------------------------------------------------------

type SettingsPhase =
  | { kind: "loading" }
  | { kind: "ready"; items: SystemSettingItemDto[]; effectiveTerm: CurrentAcademicTermDto }
  | { kind: "error"; error: unknown };

function SettingsSection() {
  const [phase, setPhase] = useState<SettingsPhase>({ kind: "loading" });
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listSystemSettings(), getCurrentAcademicTerm()]).then(
      ([list, term]) => {
        if (!cancelled) {
          setPhase({ kind: "ready", items: list.items, effectiveTerm: term });
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          setPhase({ kind: "error", error: cause });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [reloadSeed]);

  /** After any applied write: refetch the whole (five-key) list — cheap and honest. */
  const refresh = useCallback(() => {
    setPhase({ kind: "loading" });
    setReloadSeed((seed) => seed + 1);
  }, []);

  return (
    <section className="section" aria-label="系统设置">
      <div className="section-head">
        <h2 className="section-title">系统设置</h2>
        <button type="button" className="btn btn-ghost" onClick={refresh}>
          刷新
        </button>
      </div>
      {phase.kind === "loading" ? (
        <SectionSkeleton lines={8} />
      ) : phase.kind === "error" ? (
        <SectionError error={phase.error} onRetry={refresh} retryLabel="重新加载" />
      ) : (
        <div className="workbench-columns">
          {SETTING_KEY_VIEWS.map((view) => (
            <SettingKeyCard
              key={view.key}
              view={view}
              item={
                phase.items.find((item) => item.key === view.key) ?? {
                  key: view.key,
                  value: null,
                  version: null,
                  updated_at: null,
                }
              }
              effectiveTerm={
                view.key === SETTING_KEY_CURRENT_ACADEMIC_TERM
                  ? phase.effectiveTerm.value
                  : null
              }
              onApplied={refresh}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function SettingKeyCard({
  view,
  item,
  effectiveTerm,
  onApplied,
}: {
  view: { key: string; title: string; hint: string };
  item: SystemSettingItemDto;
  /** Rendered only for the term key: the provider-resolved effective value. */
  effectiveTerm: string | null;
  onApplied: () => void;
}) {
  return (
    <div className="panel moderation-panel">
      <h4 className="section-title">{view.title}</h4>
      <p className="field-hint">{view.hint}</p>
      <div className="fact-rows">
        <div className="fact-row">
          <span className="fact-label">当前值</span>
          <span className="fact-value">{settingValueText(view.key, item.value)}</span>
        </div>
        {effectiveTerm !== null ? (
          <div className="fact-row">
            <span className="fact-label">生效学期</span>
            <span className="fact-value">{effectiveTerm}</span>
          </div>
        ) : null}
        <div className="fact-row">
          <span className="fact-label">版本</span>
          <span className="fact-value">
            {settingVersionText(item.version, item.updated_at, (iso) =>
              formatDeadlineDateTime(parseServerInstant(iso)),
            )}
          </span>
        </div>
      </div>
      <SettingKeyEditor
        key={`${item.key}:${item.value ?? "unset"}:${item.version ?? "0"}`}
        view={view}
        item={item}
        onApplied={onApplied}
      />
    </div>
  );
}

/** The per-key draft + explicit old->new confirm (the optional reason rides the audit row — see module note). */
function SettingKeyEditor({
  view,
  item,
  onApplied,
}: {
  view: { key: string; title: string; hint: string };
  item: SystemSettingItemDto;
  onApplied: () => void;
}) {
  const key = view.key;
  // Drafts seed from the SERVER value at mount; the parent remounts this
  // editor (key = key+value+version) whenever a refetch changes the
  // row, so no reset effect is needed — a failed write keeps the drafts
  // for adjusting and retrying.
  const [termDraft, setTermDraft] = useState(item.value ?? "");
  const [emojiDraft, setEmojiDraft] = useState(() =>
    parseEmojiSettingValue(item.value).join("\n"),
  );
  const [limitDraft, setLimitDraft] = useState(() => {
    const limit = parseAbandonLimitValue(item.value);
    return limit === null ? "" : String(limit);
  });
  const [enabledDraft, setEnabledDraft] = useState(
    parseNetworkEnabledValue(item.value) === true ? "true" : "false",
  );
  const [cidrsDraft, setCidrsDraft] = useState(() =>
    parseCidrsSettingValue(item.value).join("\n"),
  );
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  /** The typed new value + its preview text, or null when the draft is unusable. */
  function draft(): {
    apply: (reason: string | undefined) => Promise<unknown>;
    preview: string;
  } | null {
    switch (key) {
      case SETTING_KEY_CURRENT_ACADEMIC_TERM: {
        const value = termDraft.trim();
        if (value.length === 0 || value.length > 64) {
          return null;
        }
        return {
          apply: (reason) => putCurrentAcademicTerm(value, reason),
          preview: value,
        };
      }
      case SETTING_KEY_EMOJI_WHITELIST: {
        const value = emojiDraft
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line.length > 0);
        return {
          apply: (reason) => putEmojiWhitelist(value, reason),
          preview: value.length > 0 ? value.join(" ") : "（空列表：全部禁用）",
        };
      }
      case SETTING_KEY_ABANDON_DAILY_LIMIT: {
        if (!/^\d+$/.test(limitDraft.trim())) {
          return null;
        }
        const value = Number.parseInt(limitDraft.trim(), 10);
        return {
          apply: (reason) => putAbandonDailyLimit(value, reason),
          preview: String(value),
        };
      }
      case SETTING_KEY_MANAGEMENT_NETWORK_ENABLED: {
        const value = enabledDraft === "true";
        return {
          apply: (reason) => putManagementNetworkEnabled(value, reason),
          preview: value ? "开启" : "关闭",
        };
      }
      case SETTING_KEY_MANAGEMENT_NETWORK_CIDRS: {
        const value = cidrsDraft
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line.length > 0);
        return {
          apply: (reason) => putManagementNetworkCidrs(value, reason),
          preview: value.length > 0 ? value.join("，") : "（空列表）",
        };
      }
      default:
        return null;
    }
  }

  const next = draft();
  const errorView =
    error !== null ? describeAdminMutationError(error, "保存失败，请稍后重试") : null;

  return (
    <div className="field">
      {key === SETTING_KEY_CURRENT_ACADEMIC_TERM ? (
        <>
          <label className="field-label" htmlFor={`setting-${key}`}>新学期值</label>
          <input
            id={`setting-${key}`}
            className="input"
            value={termDraft}
            onChange={(event) => setTermDraft(event.target.value)}
            disabled={busy}
            placeholder="2026-2027-1"
          />
        </>
      ) : null}
      {key === SETTING_KEY_EMOJI_WHITELIST ? (
        <>
          <label className="field-label" htmlFor={`setting-${key}`}>表情列表（每行一个）</label>
          <textarea
            id={`setting-${key}`}
            className="input"
            rows={3}
            value={emojiDraft}
            onChange={(event) => setEmojiDraft(event.target.value)}
            disabled={busy}
          />
        </>
      ) : null}
      {key === SETTING_KEY_ABANDON_DAILY_LIMIT ? (
        <>
          <label className="field-label" htmlFor={`setting-${key}`}>每日放弃上限</label>
          <input
            id={`setting-${key}`}
            className="input"
            inputMode="numeric"
            value={limitDraft}
            onChange={(event) => setLimitDraft(event.target.value)}
            disabled={busy}
          />
        </>
      ) : null}
      {key === SETTING_KEY_MANAGEMENT_NETWORK_ENABLED ? (
        <>
          <label className="field-label" htmlFor={`setting-${key}`}>管理网络限制</label>
          <select
            id={`setting-${key}`}
            className="input"
            value={enabledDraft}
            onChange={(event) => setEnabledDraft(event.target.value)}
            disabled={busy}
          >
            <option value="false">关闭</option>
            <option value="true">开启</option>
          </select>
        </>
      ) : null}
      {key === SETTING_KEY_MANAGEMENT_NETWORK_CIDRS ? (
        <>
          <label className="field-label" htmlFor={`setting-${key}`}>CIDR 列表（每行一个）</label>
          <textarea
            id={`setting-${key}`}
            className="input"
            rows={3}
            value={cidrsDraft}
            onChange={(event) => setCidrsDraft(event.target.value)}
            disabled={busy}
            placeholder="10.0.0.0/8"
          />
        </>
      ) : null}
      <div className="dialog-actions">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={next === null || busy}
          onClick={() => setConfirming(true)}
        >
          保存修改
        </button>
      </div>
      {confirming && next !== null ? (
        <ConfirmSettingDialog
          title={`修改${view.title}`}
          diff={settingDiffText(
            key === SETTING_KEY_EMOJI_WHITELIST ||
              key === SETTING_KEY_MANAGEMENT_NETWORK_CIDRS ||
              key === SETTING_KEY_MANAGEMENT_NETWORK_ENABLED
              ? settingValueText(key, item.value)
              : item.value,
            next.preview,
          )}
          busy={busy}
          errorView={errorView}
          onConfirm={async (reason) => {
            setBusy(true);
            setError(null);
            try {
              await next.apply(reason);
              setConfirming(false);
              onApplied();
            } catch (cause) {
              setError(cause);
            } finally {
              setBusy(false);
            }
          }}
          onCancel={() => setConfirming(false)}
        />
      ) : null}
      {errorView !== null && !confirming ? (
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
    </div>
  );
}

function ConfirmSettingDialog({
  title,
  diff,
  busy,
  errorView,
  onConfirm,
  onCancel,
}: {
  title: string;
  diff: string;
  busy: boolean;
  errorView: { message: string; requestId: string | null } | null;
  onConfirm: (reason: string | undefined) => Promise<void>;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  /** Blank-after-trim is OMITTED (None is legal server-side), never sent. */
  const reasonToSend = reason.trim().length > 0 ? reason.trim() : undefined;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="setting-confirm-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <div className="dialog-body">
        <h3 id="setting-confirm-title" className="dialog-title">
          {title}
        </h3>
        <p className="report-target">
          确认后立即生效并记入审计日志：{diff}
          {title.includes("学期")
            ? "。切换学期后，新创建的兑换将快照新学期（已有兑换保持不变）。"
            : ""}
        </p>
        <div className="field">
          <label className="field-label" htmlFor="setting-confirm-reason">
            修改原因（可选，将记入审计日志）
          </label>
          <textarea
            id="setting-confirm-reason"
            className="input"
            rows={2}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={busy}
            placeholder="例如：新学期教务安排"
          />
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
            type="button"
            className="btn btn-primary"
            onClick={() => void onConfirm(reasonToSend)}
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认修改</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </dialog>
  );
}

// --- notification templates (no read endpoint; edit by id) -------------------------------------

function TemplatesSection() {
  const [rows, setRows] = useState<AdminNotificationTemplateDto[]>([]);
  const [createError, setCreateError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false);
  const [eventType, setEventType] = useState<TemplateEventTypeDto>(
    TEMPLATE_EVENT_TYPE_OPTIONS[0]!.value as TemplateEventTypeDto,
  );
  const [channel, setChannel] = useState<TemplateChannelDto>("SMS");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  async function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) {
      return;
    }
    if (!adminReasonReady(title) || !adminReasonReady(body)) {
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const row = await createNotificationTemplate({
        event_type: eventType,
        channel,
        title: title.trim(),
        template_body: body.trim(),
      });
      setRows((previous) => [row, ...previous.filter((item) => item.id !== row.id)]);
      setTitle("");
      setBody("");
    } catch (cause) {
      setCreateError(cause);
    } finally {
      setCreating(false);
    }
  }

  const upsert = useCallback((row: AdminNotificationTemplateDto) => {
    setRows((previous) => {
      const next = previous.map((item) => (item.id === row.id ? row : item));
      return previous.some((item) => item.id === row.id) ? next : [row, ...next];
    });
  }, []);

  const createErrorView =
    createError !== null
      ? describeAdminMutationError(createError, "创建失败，请稍后重试")
      : null;

  return (
    <section className="section" aria-label="通知模板管理">
      <div className="section-head">
        <h2 className="section-title">通知模板</h2>
      </div>
      <p className="field-hint">
        模板暂无查询接口：创建后在此处继续管理；历史模板的 ID 可在审计日志中检索，
        通过下方「按 ID 管理」编辑（title/body 全量替换，版本号自动 +1）。
        不安全语法或未知占位符会被服务端以 422 拒绝并原样提示。
      </p>
      <div className="workbench-columns">
        <form className="panel moderation-panel" onSubmit={onCreate} noValidate>
          <h4 className="section-title">创建模板</h4>
          <div className="field">
            <label className="field-label" htmlFor="template-event">事件类型</label>
            <select
              id="template-event"
              className="input"
              value={eventType}
              onChange={(event) => setEventType(event.target.value as TemplateEventTypeDto)}
              disabled={creating}
            >
              {TEMPLATE_EVENT_TYPE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="template-channel">渠道</label>
            <select
              id="template-channel"
              className="input"
              value={channel}
              onChange={(event) => setChannel(event.target.value as TemplateChannelDto)}
              disabled={creating}
            >
              {TEMPLATE_CHANNEL_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="template-title">标题</label>
            <input
              id="template-title"
              className="input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              disabled={creating}
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="template-body">正文</label>
            <textarea
              id="template-body"
              className="input"
              rows={3}
              value={body}
              onChange={(event) => setBody(event.target.value)}
              disabled={creating}
              required
            />
            <p className="field-hint">支持占位符（如 {"{nickname}"}）；不支持 HTML 等不安全语法。</p>
          </div>
          {createErrorView !== null ? (
            <div className="alert alert-error" role="alert">
              <p>
                <span className="alert-marker" aria-hidden="true">!</span>
                {createErrorView.message}
              </p>
              {createErrorView.requestId !== null ? (
                <p className="req-id">请求 ID：{createErrorView.requestId}</p>
              ) : null}
            </div>
          ) : null}
          <div className="dialog-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={creating || !adminReasonReady(title) || !adminReasonReady(body)}
              aria-busy={creating}
            >
              {creating ? <span className="spinner" aria-hidden="true" /> : null}
              <span>创建模板</span>
            </button>
          </div>
        </form>
        <TemplateByIdPanel onVerdict={upsert} />
      </div>
      {rows.length > 0 ? (
        <div className="table-scroll">
          <table className="staff-table" aria-label="本次会话管理的模板">
            <thead>
              <tr>
                <th scope="col">模板 ID</th>
                <th scope="col">事件 / 渠道</th>
                <th scope="col">标题</th>
                <th scope="col">版本</th>
                <th scope="col">状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.id}</td>
                  <td className="mono">
                    {row.event_type} / {row.channel}
                  </td>
                  <td>{row.title}</td>
                  <td className="meta-num">v{row.version}</td>
                  <td>
                    <span className={`badge badge-${row.enabled ? "success" : "muted"}`}>
                      {row.enabled ? "启用" : "停用"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function TemplateByIdPanel({
  onVerdict,
}: {
  onVerdict: (row: AdminNotificationTemplateDto) => void;
}) {
  const [templateId, setTemplateId] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<"update" | "enable" | "disable" | null>(null);

  async function run(kind: "update" | "enable" | "disable") {
    if (busy !== null) {
      return;
    }
    if (templateId.trim().length === 0) {
      setFieldError("请填写模板 ID");
      return;
    }
    if (kind === "update" && (!adminReasonReady(title) || !adminReasonReady(body))) {
      setFieldError("更新内容时标题与正文均必填");
      return;
    }
    setFieldError(null);
    setBusy(kind);
    setError(null);
    try {
      if (kind === "update") {
        onVerdict(
          await updateNotificationTemplate(templateId.trim(), {
            title: title.trim(),
            template_body: body.trim(),
          }),
        );
      } else if (kind === "enable") {
        onVerdict(await enableNotificationTemplate(templateId.trim()));
      } else {
        onVerdict(await disableNotificationTemplate(templateId.trim()));
      }
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  }

  const errorView =
    error !== null ? describeAdminMutationError(error, "操作失败，请稍后重试") : null;

  return (
    <div className="panel moderation-panel">
      <h4 className="section-title">按 ID 管理</h4>
      <div className="field">
        <label className="field-label" htmlFor="template-by-id">模板 ID</label>
        <input
          id="template-by-id"
          className="input mono"
          value={templateId}
          onChange={(event) => {
            setTemplateId(event.target.value);
            if (fieldError !== null) {
              setFieldError(null);
            }
          }}
          placeholder="00000000-0000-0000-0000-000000000000"
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="template-by-id-title">新标题（更新时必填）</label>
        <input
          id="template-by-id-title"
          className="input"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          disabled={busy !== null}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="template-by-id-body">新正文（更新时必填）</label>
        <textarea
          id="template-by-id-body"
          className="input"
          rows={3}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          disabled={busy !== null}
        />
      </div>
      {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
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
          type="button"
          className="btn btn-primary"
          onClick={() => void run("update")}
          disabled={busy !== null}
          aria-busy={busy === "update"}
        >
          {busy === "update" ? <span className="spinner" aria-hidden="true" /> : null}
          <span>更新内容</span>
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => void run("enable")}
          disabled={busy !== null}
        >
          启用
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => void run("disable")}
          disabled={busy !== null}
        >
          停用
        </button>
      </div>
    </div>
  );
}

// --- notification delivery failures (spec §25.4) ------------------------------------------------

const FAILURES_PAGE_LIMIT = 20;

function FailuresSection() {
  const [items, setItems] = useState<NotificationFailureDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listNotificationFailures({ limit: FAILURES_PAGE_LIMIT }).then(
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
      const page = await listNotificationFailures({
        limit: FAILURES_PAGE_LIMIT,
        offset: items.length,
      });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  return (
    <section className="section" aria-label="通知投递失败">
      <div className="section-head">
        <h2 className="section-title">通知投递失败</h2>
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
      <p className="field-hint">
        投递失败的通知及其服务端安全错误摘要（错误信息已由后端做安全处理，不含敏感内容）。
        卡在发送中的投递可通过下方「具名修复」强制失败。
      </p>
      {phase === "loading" ? (
        <SectionSkeleton lines={5} />
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
        <EmptyState title="没有失败的投递" hint="投递失败的通知会出现在这里供排查" />
      ) : (
        <>
          <div className="table-scroll">
            <table className="staff-table" aria-label="投递失败列表">
              <thead>
                <tr>
                  <th scope="col">事件</th>
                  <th scope="col">渠道</th>
                  <th scope="col">尝试次数</th>
                  <th scope="col">最后错误</th>
                  <th scope="col">计划时间</th>
                  <th scope="col">投递 ID</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.event_key}</td>
                    <td>{row.channel}</td>
                    <td className="meta-num">{row.attempts}</td>
                    <td className="mono">{row.last_error ?? "—"}</td>
                    <td>
                      <time className="notif-time" dateTime={row.scheduled_at}>
                        {formatDeadlineDateTime(parseServerInstant(row.scheduled_at))}
                      </time>
                    </td>
                    <td>
                      <span className="mono" title="投递 ID（用于强制失败修复）">
                        {row.id}
                      </span>
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
    </section>
  );
}

// --- named state repairs (plan step 4: reason-mandatory, explicit consequence copy) ---------------

function RepairsSection() {
  return (
    <section className="section" aria-label="具名状态修复">
      <div className="section-head">
        <h2 className="section-title">具名状态修复</h2>
      </div>
      <p className="field-hint">
        仅用于异常状态的人工修复；每次操作都会记入审计日志，原因必填。
        与当前状态冲突时服务端返回 409，请刷新数据后重试。
      </p>
      <div className="workbench-columns">
        <RepairForm kind="release-assignment" />
        <RepairForm kind="force-fail" />
      </div>
    </section>
  );
}

function RepairForm({ kind }: { kind: "release-assignment" | "force-fail" }) {
  const isRelease = kind === "release-assignment";
  const [targetId, setTargetId] = useState("");
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (targetId.trim().length === 0) {
      setFieldError(isRelease ? "请填写任务单元 ID" : "请填写投递 ID");
      return;
    }
    if (!adminReasonReady(reason)) {
      setFieldError("修复原因必填（将记入审计日志）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    setOutcome(null);
    try {
      if (isRelease) {
        const result = await releaseOccupiedAssignment(targetId.trim(), reason.trim());
        setOutcome(
          `已释放任务单元 ${result.id}（任务 ${result.task_id}），当前可用状态：${result.availability_status}。`,
        );
      } else {
        const result = await forceFailDelivery(targetId.trim(), reason.trim());
        setOutcome(
          `投递 ${result.id} 已标记失败（状态 ${result.status}，尝试 ${result.attempts} 次）。`,
        );
      }
      setTargetId("");
      setReason("");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null ? describeAdminMutationError(error, "修复失败，请稍后重试") : null;

  return (
    <form className="panel moderation-panel" onSubmit={onSubmit} noValidate>
      <h4 className="section-title">{isRelease ? "释放占用中的任务单元" : "强制失败卡住的投递"}</h4>
      <p className="field-hint">
        {isRelease
          ? "将悬空处于 OCCUPIED 状态的任务单元放回 AVAILABLE（不改动领取历史）。仅当占用已悬空时可用；正常占用会被服务端以 409 拒绝。"
          : "将卡在 SENDING 状态的通知投递标记为 FAILED（消息与历史不变）。非 SENDING 状态会被服务端以 409 拒绝。"}
      </p>
      <div className="field">
        <label className="field-label" htmlFor={`repair-id-${kind}`}>
          {isRelease ? "任务单元 ID" : "投递 ID"}
        </label>
        <input
          id={`repair-id-${kind}`}
          className="input mono"
          value={targetId}
          onChange={(event) => {
            setTargetId(event.target.value);
            if (fieldError !== null) {
              setFieldError(null);
            }
          }}
          aria-invalid={fieldError !== null}
          disabled={busy}
          placeholder="00000000-0000-0000-0000-000000000000"
          required
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`repair-reason-${kind}`}>修复原因</label>
        <textarea
          id={`repair-reason-${kind}`}
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
          disabled={busy}
          required
        />
        {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
      </div>
      {outcome !== null ? (
        <div className="alert alert-success" role="status">
          <p>{outcome}</p>
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
        <button type="submit" className="btn btn-danger" disabled={busy} aria-busy={busy}>
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          <span>{isRelease ? "确认释放占用" : "确认强制失败"}</span>
        </button>
      </div>
    </form>
  );
}
