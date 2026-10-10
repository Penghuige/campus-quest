"use client";
import { useRef, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState, SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { isApiError } from "@/lib/errors";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { getInnovationCapabilities } from "./operationsApi";
import { claimReview, declareConflict, decideReview, getReviewDetail, listReviewQueue, readReviewEvidence, saveProofBlob, type DecisionCommand, type ReviewDetailDto, type ReviewItemDto } from "./reviewApi";
import { reviewError } from "./reviewPresentation";
import { ReviewConfirm } from "./ReviewConfirm";

export function AchievementReviewsView() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.role !== "STUDENT" || state.me.status !== "ACTIVE") return null;
  return <OperationsScope key={`${state.me.id}:${getAuthEpoch()}`} />;
}
function OperationsScope() {
  const { state, retry } = useSection(getInnovationCapabilities);
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  if (!state.data.operations_enabled) return <p className="alert alert-warning" role="alert">尚未获管理员指定的双创运营授权。</p>;
  return <Queue />;
}
function Queue() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  if (selected) return <Detail key={selected} id={selected} onBack={() => setSelected(null)} />;
  return <QueuePage key={offset} offset={offset} onPage={setOffset} onSelect={setSelected} />;
}
function QueuePage({ offset, onPage, onSelect }: { offset: number; onPage: (value: number) => void; onSelect: (id: string) => void }) {
  const focusRef = useRef<HTMLElement>(null);
  const { state, retry } = useSection(() => listReviewQueue(offset));
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [conflict, setConflict] = useState<ReviewItemDto | null>(null);
  const active = useRef(false); const epoch = useRef(getAuthEpoch());
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  async function act(item: ReviewItemDto, avoid: boolean) {
    if (busy || !current()) return; setBusy(true); setError("");
    try {
      if (avoid) await declareConflict(item.id, item.version); else await claimReview(item.id, item.version);
      if (current()) { if (!avoid) onSelect(item.id); setConflict(null); retry(); }
    } catch (cause) { if (current()) { setError(reviewError(cause)); retry(); } }
    finally { if (current()) setBusy(false); }
  }
  return <section className="section" aria-label="成果核实待办" ref={focusRef} tabIndex={-1}>
    <div className="section-head"><h2 className="section-title">待办目录</h2><Button variant="secondary" disabled={busy} onClick={retry}>重新读取待办</Button></div>
    <p className="field-hint">领取后才可查看负责人资料与证明。本人项目自动回避；有其他利益冲突请主动声明回避。</p>
    {state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : <>
      {state.data.items.length ? <ul className="ie-draft-list">{state.data.items.map((item) => <li className="ie-draft-row" key={item.id}>
        <div className="ie-draft-copy"><h3 className="section-title">{item.achievement_title}</h3><p>{item.project_title}</p><p className="field-hint">{item.claimed_by_me ? "已由你领取" : "待领取"} · <time dateTime={item.submitted_at}>{formatDeadlineDateTime(parseServerInstant(item.submitted_at))}</time></p></div>
        <div className="ie-draft-actions"><Button disabled={busy} onClick={() => item.claimed_by_me ? onSelect(item.id) : void act(item, false)}>{item.claimed_by_me ? "查看核实快照" : "领取并核实"}</Button><Button variant="secondary" disabled={busy} aria-label={`声明回避：${item.achievement_title}`} onClick={() => setConflict(item)}>声明利益冲突</Button></div>
      </li>)}</ul> : <EmptyState title="暂无可领取的成果" hint="已完成的审核单不再出现在待办中。" />}
      <nav className="ie-draft-actions" aria-label="核实待办分页"><Button variant="secondary" disabled={busy || !offset} onClick={() => onPage(Math.max(0, offset - 20))}>上一页</Button><span className="field-hint">共 {state.data.total} 份 · 第 {Math.floor(offset / 20) + 1} 页</span><Button variant="secondary" disabled={busy || offset + 20 >= state.data.total} onClick={() => onPage(offset + 20)}>下一页</Button></nav>
    </>}
    {error ? <p className="alert alert-error" role="alert">{error}</p> : null}
    <ReviewConfirm fallbackFocus={() => focusRef.current} open={Boolean(conflict)} busy={busy} title="声明利益冲突并回避" description="你将不能领取或继续审核该项目的成果；当前领取会释放，其他无冲突运营可继续处理。" onClose={() => setConflict(null)} onConfirm={() => { if (conflict) void act(conflict, true); }} />
  </section>;
}
function Detail({ id, onBack }: { id: string; onBack: () => void }) {
  const { state, retry } = useSection(() => getReviewDetail(id));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <><SectionError error={state.error} onRetry={retry} /><Button variant="secondary" onClick={onBack}>返回核实待办</Button></>;
  return <ReviewEditor id={id} detail={state.data} onReload={retry} onBack={onBack} />;
}
function ReviewEditor({ id, detail, onReload, onBack }: { id: string; detail: ReviewDetailDto; onReload: () => void; onBack: () => void }) {
  const focusRef = useRef<HTMLElement>(null);
  const retryFocusRef = useRef<HTMLButtonElement>(null);
  const [reason, setReason] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [confirm, setConfirm] = useState<"APPROVED" | "RETURNED" | null>(null);
  const [outcome, setOutcome] = useState(""); const [uncertain, setUncertain] = useState(false);
  const pending = useRef<DecisionCommand | null>(null);
  const active = useRef(false); const epoch = useRef(getAuthEpoch());
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  async function decision(kind: "APPROVED" | "RETURNED") {
    if (busy || !current()) return;
    if (kind === "RETURNED" && !reason.trim()) { setError("退回必须填写原因。"); setConfirm(null); return; }
    setBusy(true); setError("");
    pending.current ??= { request_id: crypto.randomUUID(), version: detail.case.version, revision_id: detail.case.revision_id, decision: kind, reason: reason.trim() };
    try {
      const result = await decideReview(id, pending.current);
      if (current()) { setOutcome(result.status === "APPROVED" ? "核实通过，该版本已可校内浏览。" : "已退回，负责人可查看原因并修改重交。"); pending.current = null; setUncertain(false); }
    } catch (cause) {
      if (current()) { setError(reviewError(cause)); if (!isApiError(cause) || cause.status >= 500) setUncertain(true); else { pending.current = null; setUncertain(false); } }
    } finally { if (current()) { setBusy(false); setConfirm(null); } }
  }
  async function download(evidence: string) {
    if (busy || !current()) return; setBusy(true); setError("");
    try { const file = await readReviewEvidence(id, evidence); if (current()) saveProofBlob(file); }
    catch (cause) { if (current()) setError(reviewError(cause)); }
    finally { if (current()) setBusy(false); }
  }
  if (outcome) return <section className="section" aria-label="核实处理结果" ref={focusRef} tabIndex={-1}><p className="alert alert-success" role="status">{outcome}</p><Button onClick={onBack}>返回核实待办</Button></section>;
  return <section className="section" aria-label="成果核实快照" ref={focusRef} tabIndex={-1}>
    <div className="section-head"><h2 className="section-title">{detail.achievement_content.title}</h2><Button variant="secondary" disabled={busy} onClick={onBack}>返回核实待办</Button></div>
    <p className="field-hint">此处为提交时的不可变版本。资料和证明仅用于本次核实，请勿向无权限人员传播。</p>
    <ContentFields title="已提交项目概况" content={detail.project_content} labels={{ title: "项目名称", summary: "项目简介", direction: "研究方向", stage: "项目阶段", team_status: "团队情况" }} />
    <ContentFields title="已提交成果内容" content={detail.achievement_content} labels={{ title: "成果名称", description: "作品与阶段成果说明", award_text: "立项或获奖说明", work_url: "作品链接" }} />
    <ContentFields title="核实用负责人资料" content={detail.owner_profile} labels={{ name: "姓名", student_no: "学号", major: "专业", grade: "年级" }} />
    <div className="panel"><h3 className="section-title">提交证明</h3><div className="ie-draft-actions">{detail.evidence.map((evidence, index) => <Button key={evidence} variant="secondary" disabled={busy || uncertain} onClick={() => void download(evidence)}>下载核实证明 {index + 1}</Button>)}</div></div>
    <div className="field"><label className="field-label" htmlFor="review-reason">核实备注／退回原因</label><textarea className="input ie-draft-textarea" id="review-reason" rows={4} maxLength={1000} value={reason} disabled={busy || uncertain} onChange={(event) => setReason(event.target.value)} aria-describedby="review-reason-hint" /><p id="review-reason-hint" className="field-hint">退回须说明具体修改要求，最多 1000 字；负责人将收到结果通知。</p></div>
    {error ? <p className="alert alert-error" role="alert">{error}</p> : null}
    <div className="ie-draft-actions"><Button disabled={busy || uncertain} onClick={() => setConfirm("APPROVED")}>通过首次核实</Button><Button variant="secondary" disabled={busy || uncertain} onClick={() => setConfirm("RETURNED")}>退回修改</Button><Button variant="ghost" disabled={busy || uncertain} onClick={onReload}>重新读取核实快照</Button>{uncertain ? <Button ref={retryFocusRef} disabled={busy} onClick={() => void decision(pending.current!.decision)}>确认上次决定结果（沿用原请求）</Button> : null}</div>
    <ReviewConfirm fallbackFocus={() => retryFocusRef.current ?? focusRef.current} open={Boolean(confirm)} busy={busy} title={confirm === "RETURNED" ? "退回成果修改" : "通过首次核实"} description={confirm === "RETURNED" ? "该版本不会公开；负责人将看到退回原因，并可以修改后重新提交。" : "确认材料与已提交内容一致。通过后仅公开项目概况与成果内容，私密证明及负责人资料不会进入公开版本。"} onClose={() => setConfirm(null)} onConfirm={() => { if (confirm) void decision(confirm); }} />
  </section>;
}
function ContentFields({ title, content, labels }: { title: string; content: Record<string, string>; labels: Record<string, string> }) {
  return <section className="panel ie-draft-remote" aria-label={title}><h3 className="section-title">{title}</h3><dl>{Object.entries(labels).map(([key, label]) => <div key={key}><dt className="field-label">{label}</dt><dd>{content[key] || "未填写"}</dd></div>)}</dl></section>;
}
