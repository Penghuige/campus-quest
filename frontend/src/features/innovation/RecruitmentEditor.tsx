"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { AuthField } from "@/features/auth/AuthField";
import { getAuthEpoch } from "@/lib/accessToken";
import { BUSINESS_TIME_CONFIG, serverNow } from "@/lib/time";
import { currentServerClockOffset } from "@/lib/serverClock";
import { createRecruitment, updateRecruitment, getRecruitment, publishRecruitment, closeRecruitment, reopenRecruitment, confirmActiveRecruitment, revokeRecruitmentContact, type RecruitmentDto, type ContactChannel, type RevokeCommand } from "./recruitmentApi";
import { initialRecruitmentForm, hasRecruitmentChanges, invalidRecruitmentContactChannels, normalizeRecruitmentForm, toRecruitmentForm, validateRecruitmentForm, RECRUITMENT_TEXT_FIELDS, RECRUITMENT_CONTACT_INPUTS, isRecruitmentIdeaField, type RecruitmentForm, type RecruitmentProjectDefaults, type RecruitmentFormErrors } from "./recruitmentForm";
import { recruitmentError, recruitmentReceiptText, recruitmentStatusText, CONTACT_LABELS, recruitmentDateTime } from "./recruitmentPresentation";
import { RecruitmentConfirm } from "./RecruitmentConfirm";
import { RecruitmentStageField } from "./RecruitmentStageField";
import { codePointCount } from "./draftForm";
import { getPendingRecruitment, rememberPendingRecruitment, clearPendingRecruitment, type RecruitmentAction as Action, type PendingRecruitmentCommand as Pending, type RecruitmentConfirmation as Confirmation } from "./recruitmentPending";
import { getRecruitmentRecovery, rememberRecruitmentRecovery, clearRecruitmentRecovery } from "./recruitmentRecovery";

export function RecruitmentEditor({ projectId, initial, projectDefaults, onBack }: { projectId: string; initial?: RecruitmentDto; projectDefaults?: RecruitmentProjectDefaults; onBack: () => void }) {
  const [resumed] = useState(() => getPendingRecruitment(projectId));
  const [recovered] = useState(() => getRecruitmentRecovery(projectId));
  const [record, setRecord] = useState(resumed ? resumed.record : recovered?.record ?? initial ?? null);
  const [newDefaults] = useState(() => recovered?.newDefaults ?? initialRecruitmentForm(projectDefaults));
  const [form, setForm] = useState(() => resumed?.form ?? recovered?.form ?? (initial ? toRecruitmentForm(initial.fields) : newDefaults));
  const [errors, setErrors] = useState<RecruitmentFormErrors>({});
  const [ideaCorrections, setIdeaCorrections] = useState<("goal" | "initial_plan")[]>([]);
  const [error, setError] = useState<ReturnType<typeof recruitmentError> | null>(resumed ? recruitmentError(null) : null);
  const [remote, setRemote] = useState<RecruitmentDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(recovered && !resumed ? "已恢复未保存输入，仍未保存到服务器。" : "");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(resumed?.confirmation ?? null);
  const pending = useRef<Pending | null>(resumed?.command ?? null);
  const active = useRef(false); const epoch = useRef(getAuthEpoch());
  const resultRef = useRef<HTMLParagraphElement>(null); const formRef = useRef<HTMLFormElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const original = record ? toRecruitmentForm(record.fields) : newDefaults;
  const dirty = hasRecruitmentChanges(form, original);
  const invalidContacts = invalidRecruitmentContactChannels(form.contacts);
  const locked = busy || error?.unknown === true;
  useEffect(() => { active.current = true; headingRef.current?.focus(); return () => { active.current = false; }; }, []);
  useEffect(() => {
    const key = Object.keys(errors)[0];
    if (!key || !active.current || epoch.current !== getAuthEpoch()) return;
    const invalid = invalidRecruitmentContactChannels(form.contacts);
    const selector = key === "contacts" ? invalid.length ? `[name="contact-${invalid[0]}"]` : '[name="contacts"]' : key === "stage" && form.idea_stage !== false ? '[name="idea_stage"]' : `[name="${key}"]`;
    const field = formRef.current?.querySelector<HTMLElement>(selector);
    field?.focus(); field?.scrollIntoView({ block: "center" });
  }, [errors, form.contacts, form.idea_stage]);
  useEffect(() => {
    if (epoch.current !== getAuthEpoch() || pending.current) return;
    if (hasRecruitmentChanges(form, record ? toRecruitmentForm(record.fields) : newDefaults)) rememberRecruitmentRecovery(projectId, { epoch: epoch.current, record, form, newDefaults });
    else clearRecruitmentRecovery(projectId, epoch.current);
  }, [form, record, newDefaults, projectId]);
  useEffect(() => {
    if (!dirty && !error?.unknown) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, error?.unknown]);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  function changeForm(next: RecruitmentForm) {
    if (!current() || locked) return;
    if (next.idea_stage !== form.idea_stage) setIdeaCorrections([]);
    if (hasRecruitmentChanges(next, original)) rememberRecruitmentRecovery(projectId, { epoch: epoch.current, record, form: next, newDefaults });
    else clearRecruitmentRecovery(projectId, epoch.current);
    setForm(next); setMessage(""); setErrors({});
  }
  function back() {
    if (!locked && (!dirty || window.confirm("有未保存内容，返回将丢弃这些输入，确定返回吗？"))) {
      clearRecruitmentRecovery(projectId, epoch.current); onBack();
    }
  }
  function validate(publication: boolean) {
    const next = validateRecruitmentForm(form, publication, serverNow(currentServerClockOffset())); setErrors(next);
    if (form.idea_stage !== true) setIdeaCorrections((["goal", "initial_plan"] as const).filter((key) => !!next[key]));
    const key = Object.keys(next)[0];
    if (key) {
      setMessage(`请修正表单中标出的 ${Object.keys(next).length} 项问题。`); return false;
    }
    return true;
  }
  async function run(command: Pending) {
    if (busy || !current()) return;
    pending.current = command; setBusy(true); setError(null);
    rememberPendingRecruitment(projectId, { epoch: epoch.current, command, record, form, confirmation });
    try {
      let next: RecruitmentDto;
      if (command.kind === "save") {
        next = command.id ? await updateRecruitment(projectId, command.id, { ...command.body, version: command.body.version! }) : await createRecruitment(projectId, command.body);
        clearPendingRecruitment(projectId, epoch.current, command.body.request_id);
        clearRecruitmentRecovery(projectId, epoch.current, form);
        if (!current()) return;
        setMessage("招募草稿已保存，仅自己可见；公开版本保持原内容。");
      } else {
        const operation = { publish: publishRecruitment, close: closeRecruitment, reopen: reopenRecruitment, confirm: confirmActiveRecruitment, revoke: revokeRecruitmentContact }[command.kind];
        const receipt = command.kind === "revoke" ? await revokeRecruitmentContact(projectId, command.id, command.body as RevokeCommand) : await operation(projectId, command.id, command.body as RevokeCommand);
        clearPendingRecruitment(projectId, epoch.current, command.body.request_id);
        if (command.kind === "publish" || command.kind === "reopen") clearRecruitmentRecovery(projectId, epoch.current, form);
        if (!current()) return;
        next = receipt.current; setMessage(recruitmentReceiptText(receipt));
        setConfirmation((value) => value ? { ...value, completed: true } : null);
      }
      setRecord(next);
      if (command.kind === "revoke") setForm((value) => ({ ...value, contacts: value.contacts.filter((contact) => contact.channel !== (command.body as RevokeCommand).channel) }));
      else if (command.kind === "save" || command.kind === "publish" || command.kind === "reopen") { setForm(toRecruitmentForm(next.fields)); setIdeaCorrections([]); }
      setRemote(null); pending.current = null;
    } catch (cause) {
      const view = recruitmentError(cause);
      if (!view.unknown) clearPendingRecruitment(projectId, epoch.current, command.body.request_id);
      if (current()) { setError(view); if (!view.unknown) pending.current = null; }
    } finally { if (current()) setBusy(false); }
  }
  function save(event: FormEvent) {
    event.preventDefault(); if (locked || error?.reload || !validate(false)) return;
    void run({ kind: "save", id: record?.id ?? null, body: { request_id: crypto.randomUUID(), ...(record ? { version: record.version } : {}), fields: normalizeRecruitmentForm(form) } });
  }
  function preview(kind: Action, channel?: ContactChannel) {
    if (!record || locked || error?.reload) return;
    if (kind === "publish" || kind === "reopen") {
      if (!validate(true)) return;
      if (dirty) { setMessage("请先保存当前草稿，再预览发布。保存不会更新公开内容。"); resultRef.current?.focus(); return; }
    }
    setConfirmation({ kind, channel, completed: false });
  }
  function confirm() {
    if (!record || !confirmation || locked) return;
    const { kind, channel } = confirmation;
    void run({ kind, id: record.id, body: { version: record.version, request_id: crypto.randomUUID(), ...(kind === "revoke" ? { channel: channel!, close_if_last: record.public_contact_channels.length === 1 } : {}) } });
  }
  async function readLatest() {
    if (!record || busy || !current()) return;
    setBusy(true);
    try { const next = await getRecruitment(projectId, record.id); if (current()) setRemote(next); }
    catch (cause) { if (current()) setError({ ...recruitmentError(cause), reload: true, unknown: false }); }
    finally { if (current()) setBusy(false); }
  }
  const lastContact = confirmation?.kind === "revoke" && record?.public_contact_channels.length === 1;
  const confirmLabel = confirmation?.kind === "publish" ? "确认发布" : confirmation?.kind === "reopen" ? "确认重新开放" : confirmation?.kind === "close" ? "确认关闭" : confirmation?.kind === "confirm" ? "确认继续招募" : lastContact ? "撤销并关闭招募" : "确认撤销授权";
  const title = confirmation?.kind === "publish" ? "发布招募" : confirmation?.kind === "reopen" ? "重新开放招募" : confirmation?.kind === "close" ? "关闭招募" : confirmation?.kind === "confirm" ? "确认继续招募" : lastContact ? "撤销最后一项联系授权" : "撤销联系授权";
  const description = confirmation?.kind === "publish" || confirmation?.kind === "reopen" ? "招募独立免审。确认后以下内容和所选联系渠道向校内用户开放；联系值仅在对方主动查看后提供。请确认你有权公开这些联系方式。" : confirmation?.kind === "close" ? "确认后岗位立即停止校内展示。已通过成果继续保留。" : confirmation?.kind === "confirm" ? "确认仍在招募。下一次提醒将在本次确认 30 天后；不回复提醒也不会自动关闭。" : lastContact ? "撤销后将同时关闭招募，校内用户不能再读取此联系值。重新授权须重新选择渠道并预览发布。" : "撤销后校内用户不能再读取此联系值。重新授权须重新选择渠道并预览发布。";
  return <section className="section" aria-label="招募岗位编辑器">
    <div className="section-head"><h2 ref={headingRef} tabIndex={-1} className="section-title">{record ? "编辑招募岗位" : "新建招募岗位"}</h2><Button variant="ghost" disabled={locked} onClick={back}>返回招募列表</Button></div>
    <p className="field-hint">草稿仅自己可见。发布招募须具备负责人资格及当前项目权限；无需先有通过审核的成果。</p>
    {record ? <p className="field-hint">当前{recruitmentStatusText(record.status, record.moderation_state)}{record.has_unpublished_changes ? " · 有未发布改动" : ""}{record.status === "OPEN" && record.moderation_state === "NORMAL" && !record.deadline_at && record.next_remind_at ? ` · 下次提醒 ${recruitmentDateTime(record.next_remind_at)}` : ""}</p> : null}
    <p className="field-hint">填写内容 → 保存草稿（仅自己可见） → 预览并确认发布。</p>
    {dirty ? <p className="field-hint">未保存输入暂留在本标签页，站内离开后返回此项目可继续；刷新或退出账号会清除。保存后才写入草稿。</p> : null}
    <form className="panel form ie-draft-form" method="post" onSubmit={save} noValidate ref={formRef}>
      <fieldset disabled={locked} className="ie-recruitment-fields"><legend className="section-title">项目与岗位</legend>
        {RECRUITMENT_TEXT_FIELDS.filter(({ key }) => !isRecruitmentIdeaField(key) || form.idea_stage === true || ideaCorrections.some((field) => field === key)).map(({ key, label, rows, limit }) => key === "stage" ? <RecruitmentStageField key={key} form={form} errors={errors} onChange={changeForm} /> : <div className="field" key={key}><div className="field-head"><label className="field-label" htmlFor={`recruit-${key}`}>{label}</label><span className="field-counter" data-over={codePointCount(form[key].trim()) > limit ? "true" : undefined}>{codePointCount(form[key].trim())}/{limit}</span></div><textarea className="input ie-draft-textarea" id={`recruit-${key}`} name={key} rows={rows} value={form[key]} aria-invalid={errors[key] ? true : undefined} aria-describedby={`recruit-${key}-hint${errors[key] ? ` recruit-${key}-error` : ""}`} onChange={(event) => changeForm({ ...form, [key]: event.target.value })} /><p className="field-hint" id={`recruit-${key}-hint`}>{key === "introduction" ? "仅描述目标、方向和工作计划，请勿填写未经核实的成果或奖项。发布时必填。" : key === "goal" || key === "initial_plan" ? form.idea_stage === true ? "创意起步项目发布时必填。" : "此前输入需要修正；当前阶段不要求填写此项，保存后将收起。" : "发布时必填。"} 最多 {limit} 个字符。</p>{errors[key] ? <p className="field-error" id={`recruit-${key}-error`}>{errors[key]}</p> : null}</div>)}
        <AuthField name="headcount" label="招募人数" error={errors.headcount} hint="发布时填写 1～999 的整数。" inputProps={{ type: "text", inputMode: "numeric", value: form.headcount, onChange: (event) => changeForm({ ...form, headcount: event.target.value }) }} />
        {([ ["beginner_friendly", "零基础友好"] ] as const).map(([key, label]) => <div className="field" key={key}><label className="field-label" htmlFor={`recruit-${key}`}>{label}</label><select className="input" id={`recruit-${key}`} name={key} value={String(form[key])} aria-invalid={errors[key] ? true : undefined} aria-describedby={errors[key] ? `recruit-${key}-error` : undefined} onChange={(event) => changeForm({ ...form, [key]: event.target.value === "" ? "" : event.target.value === "true" })}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>{errors[key] ? <p className="field-error" id={`recruit-${key}-error`}>{errors[key]}</p> : null}</div>)}
        <AuthField name="deadline_local" label="截止时间" hint={`按 ${BUSINESS_TIME_CONFIG.timeZone}（北京时间）填写；留空表示无截止日期，每 30 天提醒确认。`} error={errors.deadline_local} inputProps={{ type: "datetime-local", value: form.deadline_local, onChange: (event) => changeForm({ ...form, deadline_local: event.target.value }) }} />
      </fieldset>
      <fieldset disabled={locked} className="ie-recruitment-fields" id="recruit-contacts" tabIndex={-1} aria-describedby={errors.contacts && !invalidContacts.length ? "recruit-contacts-error" : undefined}><legend className="section-title">联系授权</legend><p className="field-hint">至少选择一个渠道；不会自动使用账号绑定资料。发布预览会再次确认授权。</p>
        {(Object.keys(CONTACT_LABELS) as ContactChannel[]).map((channel) => { const contact = form.contacts.find((item) => item.channel === channel); return <div className="field" key={channel}><label className="field-label"><input type="checkbox" name={channel === "WECHAT" ? "contacts" : undefined} checked={!!contact} aria-describedby={errors.contacts && !form.contacts.length ? "recruit-contacts-error" : undefined} onChange={(event) => changeForm({ ...form, contacts: event.target.checked ? [...form.contacts, { channel, value: "" }] : form.contacts.filter((item) => item.channel !== channel) })} /> 授权{CONTACT_LABELS[channel]}</label>{contact ? <AuthField name={`contact-${channel}`} label={CONTACT_LABELS[channel]} hint={RECRUITMENT_CONTACT_INPUTS[channel].hint} error={errors.contacts && invalidContacts.includes(channel) ? RECRUITMENT_CONTACT_INPUTS[channel].error : undefined} inputProps={{ type: RECRUITMENT_CONTACT_INPUTS[channel].type, inputMode: RECRUITMENT_CONTACT_INPUTS[channel].inputMode, autoComplete: "off", value: contact.value, onChange: (event) => changeForm({ ...form, contacts: form.contacts.map((item) => item.channel === channel ? { ...item, value: event.target.value } : item) }) }} /> : null}</div>; })}
        {errors.contacts && !invalidContacts.length ? <p className="field-error" role="alert" id="recruit-contacts-error">{errors.contacts}</p> : null}
      </fieldset>
      {error ? <div className="alert alert-error" role="alert"><p>{error.message}</p>{error.unknown ? <Button variant="secondary" disabled={busy} onClick={() => { if (pending.current) void run(pending.current); }}>重试原请求</Button> : error.reload && record ? <Button variant="secondary" disabled={busy} onClick={() => void readLatest()}>读取最新版本（保留当前输入）</Button> : null}</div> : null}
      <p role="status" aria-label="招募操作结果" tabIndex={-1} ref={resultRef} className={Object.keys(errors).length ? "field-error" : "field-hint"}>{busy ? "正在处理，请稍候…" : message || (dirty ? "有未保存的修改。请先保存，再预览发布。" : "可以准备招募内容。")}</p>
      <Button type="submit" disabled={locked || error?.reload === true}>{busy ? "正在处理…" : "保存招募草稿"}</Button>
      <div className="ie-draft-actions">{record && record.moderation_state === "NORMAL" ? <>
        {record.status === "DRAFT" || record.status === "OPEN" ? <Button type="button" disabled={locked || error?.reload === true} onClick={() => preview("publish")}>{record.status === "OPEN" ? "发布招募更新" : "预览发布招募"}</Button> : <Button type="button" disabled={locked || error?.reload === true} onClick={() => preview("reopen")}>重新开放招募</Button>}
        {record.status === "OPEN" ? <><Button type="button" variant="secondary" disabled={locked || error?.reload === true} onClick={() => preview("close")}>关闭招募</Button>{!record.deadline_at ? <Button type="button" variant="secondary" disabled={locked || error?.reload === true} onClick={() => preview("confirm")}>确认继续招募</Button> : null}</> : null}
        {record.public_contact_channels.map((channel) => <Button type="button" key={channel} variant="secondary" disabled={locked || error?.reload === true} onClick={() => preview("revoke", channel)} aria-label={`撤销联系授权：${CONTACT_LABELS[channel]}`}>撤销联系授权：{CONTACT_LABELS[channel]}</Button>)}
      </> : null}{record?.status === "OPEN" ? locked ? <span className="field-hint">请求结果确认后可查看校内岗位</span> : <Link href={`/innovation/recruitments/${record.id}`}>查看校内岗位</Link> : null}</div>
    </form>
    {remote ? <section className="panel ie-draft-remote" aria-label="最新招募版本"><h3 className="section-title">最新已保存版本</h3><p>{recruitmentStatusText(remote.status, remote.moderation_state)} · {remote.fields.role_name || "未命名岗位"}</p><p className="field-hint">上方输入仍保留；载入前请复制需要保留的内容。</p><pre className="ie-review-prose">{RECRUITMENT_TEXT_FIELDS.map(({ key, label }) => `${label}：${remote.fields[key] || "未填写"}`).join("\n")}</pre><Button variant="secondary" disabled={busy} onClick={() => { setRecord(remote); setForm(toRecruitmentForm(remote.fields)); setRemote(null); setError(null); setErrors({}); setMessage("已载入最新版本。"); }}>载入最新版本</Button></section> : null}
    {confirmation ? <RecruitmentConfirm title={title} description={description} confirmLabel={confirmLabel} busy={locked} completed={confirmation.completed} onClose={() => setConfirmation(null)} onConfirm={confirm} resultFocus={() => resultRef.current}>
      {confirmation.kind === "publish" || confirmation.kind === "reopen" ? <div className="ie-review-prose"><h3 className="section-title">{form.role_name}</h3><p>{form.project_title} · {form.direction} · {form.idea_stage && form.stage !== "创意起步" ? `创意起步（${form.stage}）` : form.stage}</p>{RECRUITMENT_TEXT_FIELDS.filter(({ key }) => !["project_title", "direction", "stage", "role_name"].includes(key) && (!isRecruitmentIdeaField(key) || form.idea_stage === true)).map(({ key, label }) => <p key={key}><strong>{label}：</strong>{form[key] || "未填写"}</p>)}<p>人数：{form.headcount} · 零基础友好：{form.beginner_friendly ? "是" : "否"}</p><p>截止：{form.deadline_local || "无日期，每 30 天提醒"}</p>{form.contacts.map((contact) => <p key={contact.channel}>授权{CONTACT_LABELS[contact.channel]}：{contact.value}</p>)}</div> : null}
      {error ? <div role="alert" className="alert alert-error"><p>{error.message}</p>{error.unknown ? <Button variant="secondary" disabled={busy} onClick={() => { if (pending.current) void run(pending.current); }}>重试原请求</Button> : <Button variant="secondary" disabled={busy} onClick={() => setConfirmation(null)}>返回编辑器</Button>}</div> : null}
    </RecruitmentConfirm> : null}
  </section>;
}
