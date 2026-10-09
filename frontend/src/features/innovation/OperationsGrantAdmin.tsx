"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { listAdminUsers, type AdminUserDto } from "@/features/admin/adminApi";
import { roleLabel, userStatusView } from "@/features/admin/adminView";
import { AuthField } from "@/features/auth/AuthField";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { getOperationsGrant, saveOperationsGrant, type OperationsGrantDto } from "./operationsApi";
import { operationsMutationError } from "./operationsForm";

export function OperationsGrantAdmin() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.role !== "ADMIN" || state.me.status !== "ACTIVE") return null;
  return <Directory key={`${state.me.id}:${getAuthEpoch()}`} />;
}

function Directory() {
  const [offset, setOffset] = useState(0);
  return <DirectoryPage key={offset} offset={offset} onPage={setOffset} />;
}

function DirectoryPage({ offset, onPage }: { offset: number; onPage: (offset: number) => void }) {
  const { state, retry } = useSection(() => listAdminUsers({ limit: 50, offset }));
  const [selected, setSelected] = useState("");
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  const user = state.data.items.find((item) => item.id === selected);
  return <section className="section" aria-label="双创运营授权管理">
    <div className="panel form ie-draft-form">
      <label className="field-label" htmlFor="operations-account">授权对象</label>
      <select id="operations-account" className="input" value={selected} onChange={(event) => setSelected(event.target.value)}>
        <option value="">请选择账号</option>
        {state.data.items.map((item) => <option key={item.id} value={item.id}>{item.username} · {item.nickname} · {roleLabel(item.role)} / {userStatusView(item.status).label}</option>)}
      </select>
      <p className="field-hint">仅正常状态的学生可被授予。所有账号均可查询已有授权，以便撤回已停用或身份改变的账号。</p>
      <nav className="ie-draft-actions" aria-label="授权账号分页">
        <Button variant="secondary" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}>上一页账号</Button>
        <span>第 {offset / 50 + 1} 页，共 {state.data.total} 个账号</span>
        <Button variant="secondary" disabled={offset + 50 >= state.data.total} onClick={() => onPage(offset + 50)}>下一页账号</Button>
      </nav>
    </div>
    {user ? <GrantLoader key={user.id} user={user} /> : <p className="field-hint">选择账号后读取其当前授权。</p>}
  </section>;
}

function GrantLoader({ user }: { user: AdminUserDto }) {
  const { state, retry } = useSection(() => getOperationsGrant(user.id));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <GrantEditor user={user} initial={state.data} />;
}

function GrantEditor({ user, initial }: { user: AdminUserDto; initial: OperationsGrantDto }) {
  const [record, setRecord] = useState(initial);
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string>();
  const [error, setError] = useState<ReturnType<typeof operationsMutationError> | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef(false);
  const epoch = useRef(getAuthEpoch());
  const reasonInput = useRef<HTMLInputElement>(null);
  const mutationButton = useRef<HTMLButtonElement>(null);
  const reloadButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  function current() { return active.current && epoch.current === getAuthEpoch(); }

  async function mutate() {
    if (busy || error?.reload || !current()) return;
    setBusy(true); setError(null); setMessage("");
    try {
      const result = await saveOperationsGrant(user.id, { enabled: !record.enabled, version: record.version, reason: reason.trim() });
      if (current()) { setRecord(result); setReason(""); setConfirm(false); setMessage(result.enabled ? "已授予双创运营身份。" : "已撤回双创运营身份。"); }
    } catch (caught) { if (current()) { setError(operationsMutationError(caught)); setConfirm(false); } }
    finally { if (current()) setBusy(false); }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || error?.reload) return;
    if (!reason.trim() || Array.from(reason.trim()).length > 500) { setFieldError("请填写操作原因，最多 500 个字符。"); reasonInput.current?.focus(); return; }
    setFieldError(undefined);
    if (record.enabled) setConfirm(true); else void mutate();
  }

  async function reload() {
    if (busy || !current()) return;
    setBusy(true); setMessage("");
    try { const result = await getOperationsGrant(user.id); if (current()) { setRecord(result); setError(null); setMessage("已重新读取授权状态，请核对后再操作。操作原因已保留。"); } }
    catch (caught) { if (current()) setError({ ...operationsMutationError(caught), reload: true }); }
    finally { if (current()) setBusy(false); }
  }

  const eligible = user.role === "STUDENT" && user.status === "ACTIVE";
  return <section className="panel form ie-draft-form" aria-label="当前账号运营授权">
    <h2 className="section-title">{user.nickname}的双创运营身份</h2>
    <p role="status">{record.enabled ? "当前已授权" : "当前未授权"}</p>
    <p className="field-hint">此授权不改变学生身份，不授予教师、管理员或积分调整权限。审核时仍须检查项目授权并回避本人参与项目。</p>
    <form method="post" onSubmit={submit} noValidate>
      <AuthField name="operations-reason" label="操作原因" error={fieldError} hint="必填，最多 500 个字符；会记入审计日志。" inputProps={{ ref: reasonInput, type: "text", value: reason, required: true, disabled: busy, onChange: (event) => { setReason(event.target.value); setFieldError(undefined); } }} />
      {error ? <div className="alert alert-error" role="alert"><p>{error.message}</p>{error.requestId ? <p className="req-id">请求 ID：{error.requestId}</p> : null}</div> : null}
      {!eligible && !record.enabled ? <p className="field-hint">该账号当前不符合授予条件。</p> : null}
      {message ? <p role="status" className="alert alert-success">{message}</p> : null}
      <div className="ie-draft-actions">
        <Button ref={mutationButton} type="submit" variant={record.enabled ? "danger" : "primary"} disabled={busy || error?.reload === true || (!eligible && !record.enabled)} aria-busy={busy}>{record.enabled ? "撤回运营身份" : "授予运营身份"}</Button>
        <Button ref={reloadButton} variant="secondary" disabled={busy} onClick={() => void reload()}>重新读取授权状态</Button>
      </div>
    </form>
    <Dialog open={confirm} onOpenChange={(next) => { if (!busy) setConfirm(next); }}>
      <DialogContent onCloseAutoFocus={(event) => {
        event.preventDefault();
        if (!current()) return;
        const action = mutationButton.current;
        if (action && !action.disabled) action.focus();
        else reloadButton.current?.focus();
      }}>
        <DialogTitle>撤回双创运营身份</DialogTitle>
        <DialogDescription>确认撤回{user.nickname}的双创运营身份？此操作会记录原因。</DialogDescription>
        <DialogFooter>
          <Button variant="danger" disabled={busy} onClick={() => void mutate()}>确认撤回</Button>
          <Button variant="secondary" disabled={busy} onClick={() => setConfirm(false)}>取消</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>;
}
