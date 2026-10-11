"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { getAuthEpoch } from "@/lib/accessToken";
import { describeSectionError } from "@/lib/errors";
import { getRecruitmentContacts, type PublicRecruitmentDto } from "./recruitmentApi";
import { CONTACT_LABELS } from "./recruitmentPresentation";

export function RecruitmentContacts({ item }: { item: PublicRecruitmentDto }) {
  const [contacts, setContacts] = useState<Awaited<ReturnType<typeof getRecruitmentContacts>> | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const active = useRef(false); const epoch = useRef(getAuthEpoch());
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  async function read() {
    if (busy) return;
    setContacts(null); setBusy(true); setError(null);
    try {
      const next = await getRecruitmentContacts(item.id);
      if (!active.current || epoch.current !== getAuthEpoch()) return;
      if (next.revision_number !== item.revision_number) setError("岗位已更新，请刷新岗位详情后再查看联系方式。");
      else setContacts(next);
    } catch (cause) { if (active.current && epoch.current === getAuthEpoch()) setError(describeSectionError(cause).message); }
    finally { if (active.current && epoch.current === getAuthEpoch()) setBusy(false); }
  }
  return <section className="section" aria-label="授权联系方式"><h2 className="section-title">联系负责人</h2><p className="field-hint">已授权渠道：{item.contact_channels.map((channel) => CONTACT_LABELS[channel]).join("、")}。仅在你主动查看时读取，访问会留存审计记录。</p><Button variant="secondary" disabled={busy} onClick={() => void read()}>{busy ? "正在读取…" : "查看联系方式"}</Button>{error ? <p className="alert alert-error" role="alert">{error}</p> : null}{contacts ? <dl className="ie-review-prose">{contacts.contacts.map(({ channel, value }) => <div key={channel}><dt className="field-label">{CONTACT_LABELS[channel]}</dt><dd>{value}</dd></div>)}</dl> : null}</section>;
}
