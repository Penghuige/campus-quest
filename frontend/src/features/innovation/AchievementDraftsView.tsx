"use client";

import { AchievementReviewPanel } from "./AchievementReviewPanel";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState, SectionError, SectionHeading, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { AuthField } from "@/features/auth/AuthField";
import { SubmitButton } from "@/features/auth/SubmitButton";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { getProjectDraft } from "./api";
import { createAchievementDraft, getAchievementDraft, listAchievementDrafts, updateAchievementDraft, type AchievementDraftDto, type AchievementFields } from "./achievementApi";
import { ACHIEVEMENT_FIELDS, EMPTY_ACHIEVEMENT, achievementFields, describeAchievementError, normalizeAchievementFields, validWorkLink, validateAchievementFields, type AchievementErrors, type AchievementSaveError } from "./achievementForm";

const PAGE_SIZE = 20;

export function AchievementDraftsView({ projectId }: { projectId: string }) {
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  if (state.me.role !== "STUDENT" || state.me.status !== "ACTIVE") return <p className="alert alert-warning" role="alert">仅正常状态的学生账号可以管理自己的成果草稿。</p>;
  return <ProjectScope key={`${state.me.id}:${getAuthEpoch()}:${projectId}`} projectId={projectId} />;
}

function ProjectScope({ projectId }: { projectId: string }) {
  const { state, retry } = useSection(() => getProjectDraft(projectId));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <><p className="field-hint">所属项目：{state.data.title}</p><Workspace projectId={projectId} /></>;
}

function Workspace({ projectId }: { projectId: string }) {
  const [selected, setSelected] = useState<string | null | undefined>();
  const [offset, setOffset] = useState(0);
  if (selected !== undefined) return selected === null
    ? <Editor projectId={projectId} onBack={() => setSelected(undefined)} />
    : <Loader key={selected} projectId={projectId} id={selected} onBack={() => setSelected(undefined)} />;
  return (
    <section className="section" aria-label="成果草稿列表">
      <SectionHeading title="已保存的成果草稿" action={<Button onClick={() => setSelected(null)}>新建成果草稿</Button>} />
      <DraftList key={offset} projectId={projectId} offset={offset} onPage={setOffset} onEdit={setSelected} />
    </section>
  );
}

function DraftList({ projectId, offset, onPage, onEdit }: { projectId: string; offset: number; onPage: (offset: number) => void; onEdit: (id: string) => void }) {
  const { state, retry } = useSection(() => listAchievementDrafts(projectId, { limit: PAGE_SIZE, offset }));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  const page = state.data;
  return <>
    {page.items.length ? <ul className="ie-draft-list">{page.items.map((draft) => (
      <li className="ie-draft-row" key={draft.id}>
        <div className="ie-draft-copy"><h3 className="section-title">{draft.title}</h3><p className="ie-draft-excerpt">{draft.description || "作品说明待补充"}</p><p className="field-hint">打开查看核实与公开状态</p></div>
        <Button variant="secondary" aria-label={`编辑成果：${draft.title}`} onClick={() => onEdit(draft.id)}>编辑</Button>
      </li>
    ))}</ul> : <EmptyState title="还没有成果草稿" hint="同一项目可以记录多份作品与阶段成果。" />}
    {page.total > PAGE_SIZE || offset > 0 ? <nav className="ie-draft-actions" aria-label="成果草稿分页">
      <Button variant="secondary" disabled={!offset} onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
      <span className="field-hint">第 {Math.floor(offset / PAGE_SIZE) + 1} 页 · 共 {page.total} 份</span>
      <Button variant="secondary" disabled={offset + PAGE_SIZE >= page.total} onClick={() => onPage(offset + PAGE_SIZE)}>下一页</Button>
    </nav> : null}
  </>;
}

function Loader({ projectId, id, onBack }: { projectId: string; id: string; onBack: () => void }) {
  const { state, retry } = useSection(() => getAchievementDraft(projectId, id));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <><SectionError error={state.error} onRetry={retry} /><Button onClick={onBack}>返回成果列表</Button></>;
  return <Editor projectId={projectId} initial={state.data} onBack={onBack} />;
}

function Editor({ projectId, initial, onBack }: { projectId: string; initial?: AchievementDraftDto; onBack: () => void }) {
  const [record, setRecord] = useState(initial ?? null);
  const [fields, setFields] = useState<AchievementFields>(initial ? achievementFields(initial) : EMPTY_ACHIEVEMENT);
  const [errors, setErrors] = useState<AchievementErrors>({});
  const [error, setError] = useState<AchievementSaveError | null>(null);
  const [remote, setRemote] = useState<AchievementDraftDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [reviewLocked, setReviewLocked] = useState(Boolean(initial));
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState(false);
  const pendingCreate = useRef<{ requestId: string; fields: AchievementFields } | null>(null);
  const active = useRef(false);
  const epoch = useRef(getAuthEpoch());
  const heading = useRef<HTMLHeadingElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const dirty = ACHIEVEMENT_FIELDS.some(({ key }) => fields[key] !== (record ?? EMPTY_ACHIEVEMENT)[key]);
  const frozen = busy || reviewLocked || error?.retryCreate === true;
  useEffect(() => { active.current = true; heading.current?.focus(); return () => { active.current = false; }; }, []);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  function back() { if (!dirty || window.confirm("当前输入尚未保存，确定返回成果列表吗？")) onBack(); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || reviewLocked || error?.reload || !current()) return;
    const validation = validateAchievementFields(fields);
    setErrors(validation); setSaved(false);
    const first = ACHIEVEMENT_FIELDS.find(({ key }) => validation[key]);
    if (first) { form.current?.querySelector<HTMLElement>(`[name="${first.key}"]`)?.focus(); return; }
    setBusy(true); setError(null);
    try {
      const values = normalizeAchievementFields(fields);
      pendingCreate.current ??= { requestId: crypto.randomUUID(), fields: values };
      const result = record ? await updateAchievementDraft(projectId, record.id, values, record.version)
        : await createAchievementDraft(projectId, pendingCreate.current.fields, pendingCreate.current.requestId);
      if (!current()) return;
      setRecord(result); setFields(achievementFields(result)); setSaved(true); setRemote(null); pendingCreate.current = null;
    } catch (cause) {
      if (current()) {
        const next = describeAchievementError(cause, Boolean(record));
        setError(next);
        if (!next.retryCreate) pendingCreate.current = null;
      }
    } finally { if (current()) setBusy(false); }
  }
  async function readLatest() {
    if (!record || busy || !current()) return;
    setBusy(true);
    try { const latest = await getAchievementDraft(projectId, record.id); if (current()) setRemote(latest); }
    catch (cause) { if (current()) setError({ ...describeAchievementError(cause, true), reload: true }); }
    finally { if (current()) setBusy(false); }
  }
  return <section className="section" aria-label="成果草稿编辑器">
    <div className="section-head"><h2 className="section-title" tabIndex={-1} ref={heading}>{record ? "编辑成果草稿" : "新建成果草稿"}</h2><Button variant="ghost" disabled={busy} onClick={back}>返回成果列表</Button></div>
    <form className="panel form ie-draft-form" method="post" onSubmit={submit} noValidate ref={form}>
      <p className="field-hint">此处保存个人草稿。首次核实或更新复审通过后，校内用户才能看到对应公开版本。</p>
      {ACHIEVEMENT_FIELDS.map((field) => {
        const count = Array.from(fields[field.key].trim()).length;
        const change = (value: string) => { setFields({ ...fields, [field.key]: value }); setSaved(false); };
        if (field.rows === 1) return <AuthField key={field.key} name={field.key} label={field.label} hint={field.hint} counter={`${count}/${field.limit}`} counterOver={count > field.limit} error={errors[field.key]}
          inputProps={{ type: field.key === "work_url" ? "url" : "text", value: fields[field.key], disabled: frozen, required: field.key === "title", onChange: (event) => change(event.target.value) }} />;
        return <div className="field" key={field.key}>
          <div className="field-head"><label className="field-label" htmlFor={field.key}>{field.label}</label><span className="field-counter" data-over={count > field.limit ? "true" : undefined}>{count}/{field.limit}</span></div>
          <textarea className="input ie-draft-textarea" id={field.key} name={field.key} rows={field.rows} value={fields[field.key]} disabled={frozen} onChange={(event) => change(event.target.value)} aria-invalid={errors[field.key] ? true : undefined} aria-describedby={`${field.key}-hint${errors[field.key] ? ` ${field.key}-error` : ""}`} />
          <p className="field-hint" id={`${field.key}-hint`}>{field.hint} 最多 {field.limit} 个字符。</p>{errors[field.key] ? <p className="field-error" id={`${field.key}-error`}>{errors[field.key]}</p> : null}
        </div>;
      })}
      {error ? <div className="alert alert-error" role="alert"><p>{error.message}</p>{error.requestId ? <p className="req-id">请求 ID：{error.requestId}</p> : null}{error.reload && record ? <Button variant="secondary" disabled={busy} onClick={() => void readLatest()}>读取最新版本（保留当前输入）</Button> : null}</div> : null}
      <p className={saved ? "alert alert-success" : "field-hint"} role="status">{busy ? "正在保存或读取，请稍候…" : saved ? "成果草稿已保存，仅自己可见。" : dirty ? "有未保存的修改。" : record ? "当前内容已保存。" : "尚未保存。"}</p>
      <SubmitButton loading={busy} disabled={reviewLocked || error?.reload === true}>{error?.retryCreate ? "重试这次新建（不会重复创建）" : "保存成果草稿"}</SubmitButton>
      <Button variant="secondary" onClick={() => setPreview(!preview)}>{preview ? "收起私有预览" : "查看私有预览"}</Button>
    </form>
    {record ? <AchievementReviewPanel key={record.id} projectId={projectId} record={record} dirty={dirty} saving={busy} onLocked={setReviewLocked} /> : null}
    {remote ? <section className="panel ie-draft-remote" aria-label="最新已保存成果">
      <h3 className="section-title">最新已保存成果</h3><p className="field-hint">上方输入仍保留。请先复制你要保留的内容，再决定是否载入下列版本。</p>
      <dl>{ACHIEVEMENT_FIELDS.map(({ key, label }) => <div key={key}><dt className="field-label">{label}</dt><dd>{remote[key] || "未填写"}</dd></div>)}</dl>
      <Button variant="secondary" onClick={() => { setRecord(remote); setFields(achievementFields(remote)); setRemote(null); setError(null); setErrors({}); setSaved(false); heading.current?.focus(); }}>载入此版本（替换当前输入）</Button>
    </section> : null}
    {preview ? <section className="panel ie-draft-remote" aria-label="私有成果预览">
      <h3 className="section-title">{fields.title || "未命名成果"}</h3><p className="field-hint">{dirty ? "未保存输入预览" : "已保存草稿预览"} · 此预览仅自己可见，不等同于公开版本</p>
      <dl><div><dt className="field-label">作品与阶段成果说明</dt><dd>{fields.description || "待补充"}</dd></div><div><dt className="field-label">立项或获奖说明</dt><dd>{fields.award_text || "未填写"}</dd></div></dl>
      {validWorkLink(fields.work_url.trim()) ? <a href={fields.work_url.trim()} target="_blank" rel="noopener noreferrer">打开作品链接</a> : <p className="field-hint">尚无有效作品链接</p>}
    </section> : null}
  </section>;
}
