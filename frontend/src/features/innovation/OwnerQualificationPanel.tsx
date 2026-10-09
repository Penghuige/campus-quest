"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { getAuthEpoch } from "@/lib/accessToken";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { getOwnerProfile, type OwnerProfileDto, type OwnerProfileReadDto } from "./ownerApi";
import { applyOwnerQualification, getOwnerQualification, type OwnerQualificationDto } from "./qualificationApi";
import { ownerQualificationView, qualificationMutationError, type QualificationMutationError } from "./qualificationForm";

interface OwnerQualificationPanelProps { profile: OwnerProfileDto | null; dirty: boolean; onLatestProfile: (result: OwnerProfileReadDto) => void }

export function OwnerQualificationPanel({ profile, dirty, onLatestProfile }: OwnerQualificationPanelProps) {
  const { state, retry } = useSection(getOwnerQualification);
  return <section className="section" aria-label="负责人资格">
    <h2 className="section-title">负责人资格</h2>
    {state.status === "loading" ? <div role="status" aria-label="正在读取负责人资格"><SectionSkeleton /></div>
      : state.status === "error" ? <SectionError error={state.error} onRetry={retry} />
        : <QualificationEditor initial={state.data} profile={profile} dirty={dirty} onLatestProfile={onLatestProfile} />}
  </section>;
}

function QualificationEditor({ initial, profile, dirty, onLatestProfile }: OwnerQualificationPanelProps & { initial: OwnerQualificationDto }) {
  const [record, setRecord] = useState(initial);
  const [error, setError] = useState<QualificationMutationError | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [latestProfileVersion, setLatestProfileVersion] = useState<number | null>();
  const active = useRef(false);
  const epoch = useRef(getAuthEpoch());
  const reloadButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => { if (error?.reload && !busy) reloadButton.current?.focus(); }, [error?.reload, busy]);
  function current() { return active.current && epoch.current === getAuthEpoch(); }
  const view = ownerQualificationView(record, profile?.version ?? null, dirty);
  const profileMismatch = record.status !== "APPROVED" && latestProfileVersion !== undefined && (latestProfileVersion === null ? profile !== null : profile === null || profile.version < latestProfileVersion);

  async function apply() {
    if (busy || error?.reload || profileMismatch || !view.canApply || !profile || !current()) return;
    setBusy(true); setError(null); setMessage("");
    try {
      const result = await applyOwnerQualification(record.version, profile.version);
      if (current()) {
        setRecord(result);
        setMessage("资格申请已提交，等待管理员人工开通。");
      }
    } catch (cause) {
      if (current()) setError(qualificationMutationError(cause));
    } finally { if (current()) setBusy(false); }
  }

  async function reread() {
    if (busy || !current()) return;
    setBusy(true); setMessage("");
    try {
      const [result, latest] = await Promise.all([getOwnerQualification(), getOwnerProfile()]);
      if (current()) {
        setRecord(result); setError(null); setLatestProfileVersion(latest.profile?.version ?? null);
        if (result.status !== "APPROVED" && (latest.profile?.version ?? null) !== (profile?.version ?? null)) onLatestProfile(latest);
        setMessage("已重新读取资格状态，请核对后再操作。");
      }
    } catch (cause) {
      if (current()) setError({ ...qualificationMutationError(cause), reload: true });
    } finally { if (current()) setBusy(false); }
  }

  return <div className="panel form ie-draft-form">
    <p role="status" aria-label="负责人资格状态">{view.label}</p>
    <p className="field-hint">{view.hint}</p>
    {record.profile_version !== null ? <p className="field-hint">本次申请使用第 {record.profile_version} 版已保存资料。</p> : null}
    {record.requested_at ? <p className="field-hint">申请时间：<time dateTime={record.requested_at}>{formatDeadlineDateTime(parseServerInstant(record.requested_at))}</time></p> : null}
    {record.approved_at ? <p className="field-hint">开通时间：<time dateTime={record.approved_at}>{formatDeadlineDateTime(parseServerInstant(record.approved_at))}</time></p> : null}
    <p className="field-hint">私有项目和成果草稿仍可保存；负责人资格与成果核实是不同流程。</p>
    {error ? <div className="alert alert-error" role="alert"><p>{error.message}</p>{error.requestId ? <p className="req-id">请求 ID：{error.requestId}</p> : null}</div> : null}
    {profileMismatch ? <p className="alert alert-warning" role="alert">本人资料已在其他页面更新。当前输入已保留，请在最新负责人资料区核对并明确载入后再申请。</p> : null}
    {message ? <p role="status" aria-label="资格操作结果" className="alert alert-success">{message}</p> : null}
    <div className="ie-draft-actions">
      {view.actionLabel ? <Button disabled={busy || !view.canApply || profileMismatch || error?.reload === true} aria-busy={busy} onClick={() => void apply()}>{busy ? <span className="spinner" aria-hidden="true" /> : null}{view.actionLabel}</Button> : null}
      <Button ref={reloadButton} variant="secondary" disabled={busy} onClick={() => void reread()}>重新读取资格状态</Button>
    </div>
  </div>;
}
