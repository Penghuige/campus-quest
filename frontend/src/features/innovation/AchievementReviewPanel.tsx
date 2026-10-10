"use client";
import Link from "next/link";
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { Button } from "@/components/ui/button";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { getAuthEpoch } from "@/lib/accessToken";
import { isApiError } from "@/lib/errors";
import { getProjectDraft } from "./api";
import type { AchievementDraftDto } from "./achievementApi";
import { completeEvidence, getWorkflow, listEvidence, publishRevision, readEvidence, removeEvidence, saveProofBlob, submitRevision, uploadEvidence, withdrawReview, type RevisionCommand } from "./reviewApi";
import { evidenceFileError, evidenceStatusText, reviewError, reviewStatusText } from "./reviewPresentation";
import { ReviewConfirm } from "./ReviewConfirm";
import { getOwnerQualification } from "./qualificationApi";

type Action = "submit" | "publish" | "withdraw";
export function AchievementReviewPanel({ projectId, record, dirty, saving, onLocked }: { projectId: string; record: AchievementDraftDto; dirty: boolean; saving: boolean; onLocked: (locked: boolean) => void }) {
  const { state, retry } = useSection(getOwnerQualification);
  useEffect(() => { if (state.status !== "ready" || state.data.status !== "APPROVED") onLocked(state.status === "loading"); }, [state, onLocked]);
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  if (state.data.status !== "APPROVED") return <section className="section" aria-label="成果核实与证明"><h3 className="section-title">成果首次核实</h3><p className="field-hint">草稿可以继续保存。请先<Link href="/profile/owner-profile">申请并开通负责人资格</Link>，再上传证明并提交核实。</p><Button variant="secondary" onClick={retry}>重新读取负责人资格</Button></section>;
  return <QualifiedReviewPanel projectId={projectId} record={record} dirty={dirty} saving={saving} onLocked={onLocked} />;
}
function QualifiedReviewPanel({ projectId, record, dirty, saving, onLocked }: { projectId: string; record: AchievementDraftDto; dirty: boolean; saving: boolean; onLocked: (locked: boolean) => void }) {
  const focusRef = useRef<HTMLElement>(null);
  const retryFocusRef = useRef<HTMLButtonElement>(null);
  const { state, retry } = useSection(async () => {
    const [workflow, evidence] = await Promise.all([getWorkflow(projectId, record.id), listEvidence(projectId, record.id)]);
    return { workflow, evidence: evidence.items };
  });
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef<{ action: "submit" | "publish"; body: RevisionCommand } | null>(null);
  const upload = useRef<{ file: File; requestId: string } | null>(null);
  const [retryUpload, setRetryUpload] = useState(false);
  const active = useRef(false); const epoch = useRef(getAuthEpoch());
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const pendingReview = state.status === "ready" && state.data.workflow.first_review_state === "SUBMITTED";
  useEffect(() => { onLocked(busy || uncertain || state.status === "loading" || pendingReview); }, [busy, uncertain, state.status, pendingReview, onLocked]);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  async function run(action: () => Promise<void>) {
    if (busy || saving || !current()) return;
    setBusy(true); setError(""); setMessage("");
    try { await action(); }
    catch (cause) { if (current()) setError(reviewError(cause)); }
    finally { if (current()) { setBusy(false); retry(); } }
  }
  async function send(action: Action) {
    if (state.status !== "ready" && !pending.current) return;
    const wf = state.status === "ready" ? state.data.workflow : null;
    await run(async () => {
      try {
        if (action === "withdraw") {
          if (!wf?.review_case) return;
          await withdrawReview(projectId, record.id, { workflow_version: wf.version, case_id: wf.review_case.id, case_version: wf.review_case.version });
        } else {
          if (!pending.current) {
            if (!wf) return;
            const project = await getProjectDraft(projectId);
            if (!current()) return;
            pending.current = { action, body: { request_id: crypto.randomUUID(), workflow_version: wf.version, project_version: project.version, achievement_version: record.version, evidence_ids: [...selected] } };
          }
          const frozen = pending.current;
          await (frozen.action === "submit" ? submitRevision : publishRevision)(projectId, record.id, frozen.body);
        }
        if (current()) { setMessage(action === "withdraw" ? "已撤回，可以修改草稿后重新提交。" : action === "publish" ? "更新已发布；首次核实时间不变，本次更新未逐项复审。" : "已提交首次核实，等待运营处理。待审期间须先撤回才能修改。" ); pending.current = null; setUncertain(false); setConfirm(null); }
      } catch (cause) {
        if (current()) { if ((!isApiError(cause) || cause.status >= 500) && pending.current) setUncertain(true); else { pending.current = null; setUncertain(false); } setConfirm(null); }
        throw cause;
      }
    });
  }
  async function pick(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = "";
    if (!file) return;
    const validation = evidenceFileError(file); if (validation) { setError(validation); return; }
    upload.current = { file, requestId: crypto.randomUUID() }; await sendFile();
  }
  async function sendFile() {
    const frozen = upload.current; if (!frozen) return;
    await run(async () => {
      try {
        const checked = await uploadEvidence(projectId, record.id, frozen.file, frozen.requestId);
        if (current()) { setMessage(evidenceStatusText(checked.state)); setRetryUpload(false); upload.current = null; }
      } catch (cause) { if (current()) setRetryUpload(true); throw cause; }
    });
  }
  const disabled = busy || saving || uncertain;
  return <section className="section" aria-label="成果核实与证明" ref={focusRef} tabIndex={-1}>
    <h3 className="section-title">核实与公开状态</h3>
    {state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : <>
      <p className="alert">{reviewStatusText(state.data.workflow.first_review_state)} · {state.data.workflow.moderation_state === "TAKEN_DOWN" ? "已下架，更新不会恢复公开" : state.data.workflow.first_review_state === "APPROVED" ? "校内登录用户可浏览已发布版本" : "尚未公开"}</p>
      {state.data.workflow.review_case?.reason ? <p className="alert alert-warning">退回原因：{state.data.workflow.review_case.reason}</p> : null}
      {state.data.workflow.first_review_state === "APPROVED" ? <><Link className="section-link" href={`/innovation/achievements/${record.id}`}>查看校内公开版本</Link><p className="field-hint">保存草稿不会更新公开版本。发布更新无需再次核实，但会明确标记为更新内容未逐项复审。</p></> : <p className="field-hint">提交会冻结已保存的项目概况、成果内容、负责人资料与所选证明。请先补齐资料，核对后再提交。</p>}
      <h4 className="field-label">私密证明材料</h4>
      <p className="field-hint" id="evidence-upload-hint">PDF、PNG 或 JPEG；单份最多 10 MiB，每次提交选择 1～5 份。仅本人及领取该审核单的运营可下载。文件检查通过不代表人工核实通过。</p>
      {!pendingReview ? <div className="field"><label className="field-label" htmlFor="evidence-file">上传证明材料</label><input className="input" id="evidence-file" type="file" accept="application/pdf,image/png,image/jpeg" aria-describedby="evidence-upload-hint" disabled={disabled || retryUpload} onChange={(event) => void pick(event)} /></div> : <p className="field-hint">待审期间材料不可变更；如需修改，请先撤回。</p>}
      {retryUpload ? <><div className="ie-draft-actions"><Button variant="secondary" disabled={disabled} onClick={() => void sendFile()}>重试原文件上传与检查</Button><Button variant="ghost" disabled={disabled} onClick={() => { upload.current = null; setRetryUpload(false); setError(""); setMessage("已放弃本次本地重试，可以重新选择文件。这不会删除已有上传记录。"); }}>放弃本次上传重试</Button></div><p className="field-hint">重试沿用原文件。放弃重试后可以重新选择文件；不用的未引用材料请在下方列表中移除。</p></> : null}
      {state.data.evidence.length ? <ul className="ie-draft-list">{state.data.evidence.map((proof, index) => <li className="ie-draft-row" key={proof.id}>
        <div className="ie-draft-copy"><label className="field-label"><input type="checkbox" checked={selected.includes(proof.id)} disabled={disabled || pendingReview || proof.state !== "READY"} onChange={(event) => setSelected(event.target.checked ? [...selected, proof.id] : selected.filter((id) => id !== proof.id))} /> 选择证明 {index + 1}</label><p className="field-hint">{proof.content_type === "application/pdf" ? "PDF" : proof.content_type === "image/png" ? "PNG" : "JPEG"} · {Math.ceil(proof.size / 1024)} KiB · {evidenceStatusText(proof.state)}</p>{proof.failure_code ? <p className="field-error">未获准使用；可移除此意向后上传合规文件，或重试临时失败的检查。</p> : null}</div>
        <div className="ie-draft-actions">
          {proof.state === "READY" ? <Button variant="secondary" disabled={disabled} aria-label={`下载证明 ${index + 1}`} onClick={() => void run(async () => { const file = await readEvidence(projectId, record.id, proof.id); if (current()) saveProofBlob(file); })}>下载</Button> : null}
          {proof.state === "PENDING" || proof.state === "CHECKING" ? <Button variant="secondary" disabled={disabled || pendingReview} aria-label={`重试检查证明 ${index + 1}`} onClick={() => void run(async () => { const checked = await completeEvidence(projectId, record.id, proof.id); if (current()) setMessage(evidenceStatusText(checked.state)); })}>重试检查</Button> : null}
          <Button variant="ghost" disabled={disabled || pendingReview} aria-label={`移除证明 ${index + 1}`} onClick={() => void run(async () => { await removeEvidence(projectId, record.id, proof.id); if (current()) { setSelected((ids) => ids.filter((id) => id !== proof.id)); setMessage("未引用材料已移除。历史版本引用的材料不能移除。"); } })}>移除</Button>
        </div>
      </li>)}</ul> : <p className="field-hint">还没有证明材料。</p>}
      <p className="field-hint">已选择 {selected.length} 份检查通过的证明。已提交或公开版本引用的材料保留在历史记录中，不能通过移除改写历史。</p>
      {dirty ? <p className="alert alert-warning">请先保存上方成果修改，再提交或发布更新。</p> : null}
      <div className="ie-draft-actions">
        {pendingReview ? <Button variant="secondary" disabled={disabled} onClick={() => setConfirm("withdraw")}>撤回首次核实</Button> : <Button disabled={disabled || dirty || selected.length < 1 || selected.length > 5} onClick={() => setConfirm(state.data.workflow.first_review_state === "APPROVED" ? "publish" : "submit")}>{state.data.workflow.first_review_state === "APPROVED" ? "发布更新（免复审）" : "提交首次核实"}</Button>}
        <Button variant="secondary" disabled={busy || saving} onClick={retry}>重新读取核实与材料状态</Button>

      </div>
    </>}
    {error ? <p className="alert alert-error" role="alert">{error}</p> : null}
    {message ? <p className="alert alert-success" role="status">{message}</p> : null}
    {uncertain ? <Button ref={retryFocusRef} disabled={busy || saving} onClick={() => void send(pending.current!.action)}>确认上次提交结果（沿用原请求）</Button> : null}
    {busy ? <p className="field-hint" aria-live="polite">正在处理，请稍候…</p> : null}
    <ReviewConfirm fallbackFocus={() => retryFocusRef.current ?? focusRef.current} open={confirm !== null} busy={busy} title={confirm === "withdraw" ? "撤回首次核实" : confirm === "publish" ? "发布更新" : "提交首次核实"} description={confirm === "withdraw" ? "这次审核单将关闭。若运营已先作出决定，撤回会被拒绝，请重新读取结果。" : confirm === "publish" ? "校内用户将看到已保存的更新内容。本次更新不会逐项复审，也不会恢复已下架成果。" : "核实针对已保存内容与所选证明；提交后请先撤回再修改。通过核实后该版本向校内登录用户公开。"} onClose={() => setConfirm(null)} onConfirm={() => { if (confirm) void send(confirm); }} />
  </section>;
}
