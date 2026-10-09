"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { OWNER_FIELDS } from "./ownerForm";
import { approveOwnerQualification, getOwnerQualificationApplication, listOwnerQualificationApplications, type OwnerQualificationDetailDto } from "./qualificationApi";
import { qualificationMutationError, type QualificationMutationError } from "./qualificationForm";

const PAGE_LIMIT = 20;

export function OwnerQualificationAdmin() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.role !== "ADMIN" || state.me.status !== "ACTIVE") return null;
  return <ApplicationDirectory key={`${state.me.id}:${getAuthEpoch()}`} />;
}

function ApplicationDirectory() {
  const [offset, setOffset] = useState(0);
  return <ApplicationPage key={offset} offset={offset} onPage={setOffset} />;
}

function ApplicationPage({ offset, onPage }: { offset: number; onPage: (offset: number) => void }) {
  const { state, retry } = useSection(() => listOwnerQualificationApplications({ limit: PAGE_LIMIT, offset }));
  const [selected, setSelected] = useState("");
  const [completed, setCompleted] = useState<string[]>([]);
  if (state.status === "loading") return <div role="status" aria-label="正在读取资格申请队列"><SectionSkeleton lines={6} /></div>;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <section className="section" aria-label="负责人资格申请队列">
    <div className="section-head"><h2 className="section-title">等待开通的资格申请</h2><Button variant="ghost" onClick={retry}>刷新申请队列</Button></div>
    <p className="field-hint">列表不包含姓名、学号、专业或年级。明确查看申请后才读取四项资料快照，并记录敏感读取审计。</p>
    {state.data.items.length === 0 ? <EmptyState title="暂无等待开通的资格申请" hint="学生保存负责人资料并申请后，会出现在这里。" /> : <>
      <div className="table-scroll">
        <table className="staff-table" aria-label="负责人资格申请列表">
          <thead><tr><th scope="col">申请账号</th><th scope="col">申请时间</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
          <tbody>{state.data.items.map((application) => <tr key={application.user_id}>
            <td className="staff-cell-title mono">{application.user_id}</td>
            <td><time dateTime={application.requested_at}>{formatDeadlineDateTime(parseServerInstant(application.requested_at))}</time></td>
            <td>{completed.includes(application.user_id) ? "已开通" : "等待开通"}</td>
            <td><Button variant="secondary" disabled={completed.includes(application.user_id)} onClick={() => setSelected(application.user_id)}>查看申请</Button></td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
    {state.data.items.length > 0 || offset > 0 ?
      <nav className="ie-draft-actions" aria-label="资格申请分页">
        <Button variant="secondary" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - PAGE_LIMIT))}>上一页申请</Button>
        <span>第 {offset / PAGE_LIMIT + 1} 页，共 {state.data.total} 条申请</span>
        <Button variant="secondary" disabled={offset + PAGE_LIMIT >= state.data.total} onClick={() => onPage(offset + PAGE_LIMIT)}>下一页申请</Button>
      </nav>
    : null}
    {selected && state.data.items.some((application) => application.user_id === selected) ? <ApplicationLoader key={selected} userId={selected} onApproved={() => setCompleted((previous) => previous.includes(selected) ? previous : [...previous, selected])} /> : null}
  </section>;
}

function ApplicationLoader({ userId, onApproved }: { userId: string; onApproved: () => void }) {
  const { state, retry } = useSection(() => getOwnerQualificationApplication(userId));
  if (state.status === "loading") return <div role="status" aria-label="正在读取资格申请详情"><SectionSkeleton /></div>;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <ApplicationDetail initial={state.data} onApproved={onApproved} />;
}

function ApplicationDetail({ initial, onApproved }: { initial: OwnerQualificationDetailDto; onApproved: () => void }) {
  const [record, setRecord] = useState(initial);
  const [error, setError] = useState<QualificationMutationError | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef(false);
  const epoch = useRef(getAuthEpoch());
  const approveButton = useRef<HTMLButtonElement>(null);
  const reloadButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  function current() { return active.current && epoch.current === getAuthEpoch(); }

  async function approve() {
    if (busy || error?.reload || record.status !== "PENDING" || !current()) return;
    setBusy(true); setError(null); setMessage("");
    try {
      const result = await approveOwnerQualification(record.user_id, record.version);
      if (current()) {
        setRecord({ ...record, ...result }); setConfirm(false); setMessage("负责人资格已开通。"); onApproved();
      }
    } catch (cause) {
      if (current()) { setError(qualificationMutationError(cause)); setConfirm(false); }
    } finally { if (current()) setBusy(false); }
  }

  async function reread() {
    if (busy || !current()) return;
    setBusy(true); setMessage("");
    try {
      const result = await getOwnerQualificationApplication(record.user_id);
      if (current()) { setRecord(result); setError(null); setMessage("已重新读取申请详情，请核对本次快照后再操作。"); if (result.status === "APPROVED") onApproved(); }
    } catch (cause) {
      if (current()) setError({ ...qualificationMutationError(cause), reload: true });
    } finally { if (current()) setBusy(false); }
  }

  return <section className="panel form ie-draft-form ie-draft-remote" aria-label="负责人资格申请详情">
    <h2 className="section-title">申请资料快照</h2>
    <p role="status" aria-label="申请开通状态">{record.status === "APPROVED" ? "负责人资格已开通" : "等待管理员开通"}</p>
    <p className="field-hint">这是学生明确提交的第 {record.profile_version} 版资料快照。开通负责人资格不会核实成果，也不会授予运营、教师或管理员权限。</p>
    <dl>{OWNER_FIELDS.map(({ key, label }) => <div key={key}><dt className="field-label">{label}</dt><dd>{record.profile[key]}</dd></div>)}</dl>
    {error ? <div className="alert alert-error" role="alert"><p>{error.message}</p>{error.requestId ? <p className="req-id">请求 ID：{error.requestId}</p> : null}</div> : null}
    {message ? <p role="status" aria-label="管理员资格操作结果" className="alert alert-success">{message}</p> : null}
    <div className="ie-draft-actions">
      <Button ref={approveButton} disabled={busy || error?.reload === true || record.status !== "PENDING"} onClick={() => setConfirm(true)}>开通负责人资格</Button>
      <Button ref={reloadButton} variant="secondary" disabled={busy} aria-busy={busy} onClick={() => void reread()}>重新读取申请详情</Button>
    </div>
    <Dialog open={confirm} onOpenChange={(next) => { if (!busy) setConfirm(next); }}>
      <DialogContent aria-labelledby="owner-qualification-confirm-title" aria-describedby={undefined} onCloseAutoFocus={(event) => {
        event.preventDefault();
        if (!current()) return;
        const action = approveButton.current;
        if (action && !action.disabled) action.focus();
        else reloadButton.current?.focus();
      }}>
        <DialogTitle id="owner-qualification-confirm-title">开通负责人资格</DialogTitle>
        <p className="field-hint">确认依据刚才查看的资料快照，为{record.profile.name}开通负责人资格？此操作将记入审计日志，成果仍需单独核实。</p>
        <DialogFooter>
          <Button disabled={busy} aria-busy={busy} onClick={() => void approve()}>{busy ? <span className="spinner" aria-hidden="true" /> : null}确认开通</Button>
          <Button variant="secondary" disabled={busy} onClick={() => setConfirm(false)}>取消</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>;
}
