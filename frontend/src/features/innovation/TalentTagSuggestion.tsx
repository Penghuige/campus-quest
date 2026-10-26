'use client';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { AuthField } from '@/features/auth/AuthField';
import { Button } from '@/components/ui/button';
import { getAuthEpoch } from '@/lib/accessToken';
import { codePointCount } from './formUtils';
import { suggestTalentTag } from './talentApi';
import { talentError } from './talentPresentation';

export function TalentTagSuggestion(){
  const [name,setName]=useState('');const [reason,setReason]=useState('');const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');
  const active=useRef(false);const epoch=useRef(getAuthEpoch());const [pending,setPending]=useState<Parameters<typeof suggestTalentTag>[0]|null>(null);const running=useRef(false);
  useEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
  async function submit(event:FormEvent){event.preventDefault();if(running.current||!active.current||epoch.current!==getAuthEpoch())return;
    if(!pending&&(!name.trim()||codePointCount(name.trim())>50||codePointCount(reason.trim())>500)){setMessage('请填写 1～50 字的建议标签；理由最多 500 字。');return;}
    const body=pending??{request_id:crypto.randomUUID(),name:name.trim(),reason:reason.trim()};setPending(body);running.current=true;setBusy(true);
    try{await suggestTalentTag(body);if(active.current&&epoch.current===getAuthEpoch()){setPending(null);setMessage('标签建议已提交，等待运营处理；当前标准标签不会自动增加。');}}
    catch(cause){if(active.current&&epoch.current===getAuthEpoch()){const view=talentError(cause);if(!view.unknown)setPending(null);setMessage(`${view.message}${view.unknown?' 再次提交将重试原建议。':''}`);}}
    finally{running.current=false;if(active.current&&epoch.current===getAuthEpoch())setBusy(false);}
  }
  return <details className="section"><summary className="section-title">没有合适的技能标签？</summary><form className="panel form" noValidate method="post" onSubmit={submit}><p className="field-hint">建议由运营处理；不会直接创建标签或替你勾选。</p><AuthField name="talent-suggestion-name" label="建议标签" inputProps={{value:name,onChange:event=>setName(event.target.value)}}/><AuthField name="talent-suggestion-reason" label="建议理由（选填）" inputProps={{value:reason,onChange:event=>setReason(event.target.value)}}/><Button type="submit" disabled={busy}>{busy?'正在提交…':pending?'重试原标签建议':'提交标签建议'}</Button><p className="field-hint" role="status">{message}</p></form></details>;
}
