'use client';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { getAuthEpoch } from '@/lib/accessToken';
import { describeSectionError, isApiError } from '@/lib/errors';
import { getTalentContacts, type PublicTalentDto } from './talentApi';
import { CONTACT_LABELS } from './contactFields';

export function TalentContacts({item,onRefresh}:{item:PublicTalentDto;onRefresh:()=>void}) {
  const [contacts,setContacts]=useState<Awaited<ReturnType<typeof getTalentContacts>>|null>(null);
  const [busy,setBusy]=useState(false); const [error,setError]=useState<string|null>(null); const [changed,setChanged]=useState(false);
  const active=useRef(false); const epoch=useRef(getAuthEpoch());
  useEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
  async function read() {
    if (busy) return;
    setBusy(true);setError(null);setContacts(null);setChanged(false);
    try {
      const result=await getTalentContacts(item.id,item.public_version);
      if (!active.current||epoch.current!==getAuthEpoch()) return;
      if (result.public_version!==item.public_version) {setError('资料已更新，请刷新后重新确认联系。');setChanged(true);}
      else setContacts(result);
    } catch(cause) {
      if (active.current&&epoch.current===getAuthEpoch()) {
        setError(isApiError(cause)&&cause.status===404?'资料已停止展示，无法继续读取联系方式。':describeSectionError(cause).message);
        setChanged(isApiError(cause)&&[404,409].includes(cause.status));
      }
    } finally {if(active.current&&epoch.current===getAuthEpoch())setBusy(false);}
  }
  return <section className="section" aria-label="人才授权联系"><h2 className="section-title">联系这位同学</h2>
    {!item.contact_channels.length?<p className="field-hint">这位同学暂未授权联系方式。</p>:<><p className="field-hint">已授权：{item.contact_channels.map(channel=>CONTACT_LABELS[channel]).join('、')}。点击后读取，访问会留存审计记录。</p><Button variant="secondary" disabled={busy||changed} onClick={()=>void read()}>{busy?'正在读取…':'查看授权联系方式'}</Button></>}
    <p role="status" aria-label="人才联系读取结果" className="field-hint">{busy?'正在读取…':contacts?'联系方式已读取，请查看下方渠道。':''}</p>
    {error?<p role="alert" className="alert alert-error">{error}</p>:null}{changed?<Button variant="secondary" onClick={onRefresh}>刷新档案</Button>:null}
    {contacts?<dl className="ie-review-prose">{contacts.contacts.map(({channel,value})=><div key={channel}><dt className="field-label">{CONTACT_LABELS[channel]}</dt><dd>{value}</dd></div>)}</dl>:null}
  </section>;
}
