"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { AuthField } from "@/features/auth/AuthField";
import { SubmitButton } from "@/features/auth/SubmitButton";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";

import { getOwnerProfile, saveOwnerProfile, type OwnerFields, type OwnerProfileDto } from "./ownerApi";
import { describeOwnerSaveError, EMPTY_OWNER_FIELDS, normalizeOwnerFields, ownerFields, OWNER_FIELDS, validateOwnerFields, type OwnerFieldErrors, type OwnerSaveError } from "./ownerForm";

export function OwnerProfileView() {
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  if (state.me.role !== "STUDENT" || state.me.status !== "ACTIVE") return <p className="alert alert-warning" role="alert">仅正常状态的学生账号可以管理自己的负责人资料。</p>;
  return <ProfileLoader key={`${state.me.id}:${getAuthEpoch()}`} />;
}

function ProfileLoader() {
  const { state, retry } = useSection(getOwnerProfile);
  if (state.status === "loading") return <div role="status" aria-label="正在读取负责人资料"><SectionSkeleton /></div>;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <ProfileEditor initial={state.data.profile} />;
}

function ProfileEditor({ initial }: { initial: OwnerProfileDto | null }) {
  const [record, setRecord] = useState(initial);
  const [fields, setFields] = useState<OwnerFields>(initial ? ownerFields(initial) : EMPTY_OWNER_FIELDS);
  const [errors, setErrors] = useState<OwnerFieldErrors>({});
  const [error, setError] = useState<OwnerSaveError | null>(null);
  const [remote, setRemote] = useState<{ profile: OwnerProfileDto | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const active = useRef(false);
  const epoch = useRef(getAuthEpoch());
  const form = useRef<HTMLFormElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const original = record ? ownerFields(record) : EMPTY_OWNER_FIELDS;
  const dirty = OWNER_FIELDS.some(({ key }) => fields[key] !== original[key]);

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function current() { return active.current && epoch.current === getAuthEpoch(); }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || error?.reload || !current()) return;
    setSaved(false);
    const nextErrors = validateOwnerFields(fields);
    setErrors(nextErrors);
    setError(null);
    const first = OWNER_FIELDS.find(({ key }) => nextErrors[key]);
    if (first) { form.current?.querySelector<HTMLElement>(`[name="owner-${first.key}"]`)?.focus(); return; }
    setBusy(true);
    try {
      const result = await saveOwnerProfile(normalizeOwnerFields(fields), record?.version ?? 0);
      if (!current()) return;
      setRecord(result); setFields(ownerFields(result)); setRemote(null); setSaved(true);
    } catch (caught) {
      if (current()) setError(describeOwnerSaveError(caught));
    } finally { if (current()) setBusy(false); }
  }

  async function readLatest() {
    if (busy || !current()) return;
    setBusy(true);
    try {
      const result = await getOwnerProfile();
      if (current()) setRemote(result);
    } catch (caught) {
      if (current()) setError({ ...describeOwnerSaveError(caught), reload: true });
    } finally { if (current()) setBusy(false); }
  }

  return (
    <section className="section" aria-label="负责人资料编辑器">
      <h2 className="section-title" ref={heading} tabIndex={-1}>本人负责人资料</h2>
      <form className="panel form ie-draft-form" method="post" onSubmit={submit} noValidate ref={form}>
        <p className="field-hint">四项均为必填，仅用于负责人资料准备。保存不会自动开通负责人资格；资格开通方式尚待学校确定。</p>
        {OWNER_FIELDS.map(({ key, label, limit }) => (
          <AuthField key={key} name={`owner-${key}`} label={label} error={errors[key]} hint={`必填，最多 ${limit} 个字符。`}
            inputProps={{ type: "text", required: true, autoComplete: "off", value: fields[key], disabled: busy, onChange: (event) => { setFields({ ...fields, [key]: event.target.value }); setSaved(false); } }} />
        ))}
        {error ? <div className="alert alert-error" role="alert">
          <p>{error.message}</p>
          {error.requestId ? <p className="req-id">请求 ID：{error.requestId}</p> : null}
          {error.reload ? <Button variant="secondary" disabled={busy} onClick={() => void readLatest()}>读取最新资料（保留当前输入）</Button> : null}
        </div> : null}
        <p className={saved ? "alert alert-success" : "field-hint"} role="status">{busy ? "正在保存或读取，请稍候…" : saved ? "资料已保存；负责人资格尚未由此开通。" : dirty ? "有未保存的修改。" : record ? "当前资料已保存。" : "尚未填写负责人资料。"}</p>
        <SubmitButton loading={busy} disabled={error?.reload === true}>保存负责人资料</SubmitButton>
      </form>
      {remote ? <section className="panel ie-draft-remote" aria-label="最新负责人资料">
        <h3 className="section-title">最新负责人资料</h3>
        <p className="field-hint">上方输入仍保留。载入下列资料会替换当前输入，请先复制需要保留的内容。</p>
        {remote.profile ? <dl>{OWNER_FIELDS.map(({ key, label }) => <div key={key}><dt className="field-label">{label}</dt><dd>{remote.profile?.[key]}</dd></div>)}</dl> : <p>目前还没有保存过资料。</p>}
        <Button variant="secondary" disabled={busy} onClick={() => {
          setRecord(remote.profile); setFields(remote.profile ? ownerFields(remote.profile) : EMPTY_OWNER_FIELDS); setRemote(null); setError(null); setErrors({}); setSaved(false); heading.current?.focus();
        }}>载入此资料（替换当前输入）</Button>
      </section> : null}
    </section>
  );
}
