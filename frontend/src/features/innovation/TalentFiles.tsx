'use client';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { getAuthEpoch } from '@/lib/accessToken';
import { describeSectionError } from '@/lib/errors';
import { completeTalentFile, createTalentFileIntent, downloadOwnTalentFile, putTalentFile, saveTalentBlob, getTalentFiles, type TalentFileDto } from './talentApi';
import { getTalentUploadRecovery, putTalentUploadRecovery, discardTalentUploadRecovery, type TalentUploadRecovery } from './talentRecovery';
import { talentError, talentFileStatus, talentSize } from './talentPresentation';

export function TalentFiles({files,selected,publicIds,available,locked,error,onFiles,onSelection,onAction}:{files:TalentFileDto[];selected:string[];publicIds:string[];available:boolean;locked:boolean;error?:string;onFiles:(files:TalentFileDto[])=>void;onSelection:(ids:string[])=>void;onAction:(kind:'remove-file'|'revoke-file',id:string)=>void}) {
  const [busy,setBusy]=useState(false); const [message,setMessage]=useState(''); const [failure,setFailure]=useState<string|null>(null);
  const [resumable,setResumable]=useState(()=>!!getTalentUploadRecovery());
  const active=useRef(false); const epoch=useRef(getAuthEpoch()); const running=useRef(false);
  useEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
  function current(){return active.current&&epoch.current===getAuthEpoch();}
  const inventory=files.filter(file=>!file.removed_at&&!file.revoked_at).length;
  const canResume=resumable&&!!getTalentUploadRecovery();
  async function refresh(){if(running.current||!current())return;running.current=true;setBusy(true);try{const next=await getTalentFiles();if(current()){onFiles(next.items);setFailure(null);}}catch(cause){if(current())setFailure(describeSectionError(cause).message);}finally{running.current=false;if(current())setBusy(false);}}
  async function upload(value:TalentUploadRecovery) {
    if(running.current||!current())return;
    running.current=true;setBusy(true);setFailure(null);putTalentUploadRecovery(value);
    try {
      let grant=value.grant;
      if(!grant){setMessage('正在申请上传…');grant=await createTalentFileIntent(value.body);if(!current())return;value={...value,grant};putTalentUploadRecovery(value);}
      setMessage('正在上传文件…');await putTalentFile(grant,value.file);if(!current())return;
      setMessage('正在检查文件，请稍候…');const checked=await completeTalentFile(grant.file.id);if(!current())return;
      const next=await getTalentFiles();if(!current())return;onFiles(next.items);
      if(checked.state==='READY'||checked.state==='REJECTED'){discardTalentUploadRecovery(value.epoch,value.body.request_id);setResumable(false);}
      else setResumable(true);
      setMessage(checked.state==='READY'?'文件检查通过；仍仅自己可见。请勾选后再预览公开。':talentFileStatus(checked));
    } catch(cause){if(current()){
      const view=talentError(cause);setFailure(view.message);
      if(!value.grant&&!view.unknown){
        discardTalentUploadRecovery(value.epoch,value.body.request_id);setResumable(false);setMessage('上传申请未获接受，请修正问题后重新选择文件。');
      }else{setResumable(true);setMessage('上传或检查结果待确认，请使用原请求重试，或刷新服务端记录。');}
    }}
    finally{running.current=false;if(current())setBusy(false);}
  }
  function choose(file:File|undefined) {
    if(!file||!current())return;
    const mime=file.type||(/\.pdf$/i.test(file.name)?'application/pdf':/\.png$/i.test(file.name)?'image/png':/\.jpe?g$/i.test(file.name)?'image/jpeg':'');
    if(!['application/pdf','image/png','image/jpeg'].includes(mime)||file.size===0||file.size>10485760){setFailure('请选择非空 PDF、PNG 或 JPEG 文件，每个不超过 10 MiB。');return;}
    void upload({epoch:epoch.current,file,grant:null,body:{request_id:crypto.randomUUID(),name:file.name,content_type:mime as 'application/pdf'|'image/png'|'image/jpeg',size:file.size}});
  }
  async function check(file:TalentFileDto){if(running.current||!current())return;running.current=true;setBusy(true);setFailure(null);try{const next=await completeTalentFile(file.id);if(!current())return;const all=await getTalentFiles();if(current()){onFiles(all.items);setMessage(talentFileStatus(next));if(next.state==='READY'||next.state==='REJECTED'){const pending=getTalentUploadRecovery();if(pending?.grant?.file.id===file.id){discardTalentUploadRecovery(pending.epoch,pending.body.request_id);setResumable(false);}}}}catch(cause){if(current())setFailure(describeSectionError(cause).message);}finally{running.current=false;if(current())setBusy(false);}}
  async function download(file:TalentFileDto){if(running.current||!current())return;running.current=true;setBusy(true);setFailure(null);try{const blob=await downloadOwnTalentFile(file.id);if(current())saveTalentBlob(blob,file.name);}catch(cause){if(current())setFailure(describeSectionError(cause).message);}finally{running.current=false;if(current())setBusy(false);}}
  return <fieldset className="ie-recruitment-fields" name="talent-file_ids" tabIndex={-1} aria-invalid={!!error} aria-describedby="talent-files-hint">
    <legend className="section-title">作品与证明文件（选填）</legend><p id="talent-files-hint" className="field-hint">PDF、PNG、JPEG，每个不超过 10 MiB；最多 10 条有效上传记录，公开最多 5 个。可先上传 5 个新文件，更新公开版后再移除旧文件。</p>
    {!available?<p className="field-hint">请先保存档案，再上传文件；没有作品也可以公开。</p>:<><label className="field-label" htmlFor="talent-upload">选择作品或证明文件</label><input className="input" id="talent-upload" type="file" accept="application/pdf,image/png,image/jpeg" disabled={busy||locked||inventory>=10||canResume} onChange={event=>{choose(event.currentTarget.files?.[0]);event.currentTarget.value='';}}/><p className="field-hint">有效上传记录 {inventory}/10。检查通过不代表已公开，需主动勾选并确认。</p><div className="ie-draft-actions"><Button variant="secondary" disabled={busy||locked} onClick={()=>void refresh()}>刷新文件状态</Button>{canResume?<Button variant="secondary" disabled={busy||locked} onClick={()=>{const pending=getTalentUploadRecovery();if(pending)void upload(pending);}}>重试原文件上传</Button>:null}</div></>}
    {files.length?<ul className="ie-draft-list">{files.map(file=>{const checked=selected.includes(file.id);const ready=file.state==='READY'&&!file.revoked_at;const published=publicIds.includes(file.id);return <li className="ie-draft-row" key={file.id}><div className="ie-draft-copy"><label className="field-label"><input type="checkbox" aria-label={`公开 ${file.name}`} checked={checked} disabled={locked||(!ready&&!checked)||(!checked&&selected.length>=5)} onChange={event=>onSelection(event.target.checked?[...selected,file.id]:selected.filter(id=>id!==file.id))}/> {file.name}</label><p className="field-hint">{talentSize(file.size)} · {talentFileStatus(file)}{published?' · 当前公开':''}</p><div className="ie-draft-actions">{file.state==='READY'?<Button variant="secondary" disabled={busy||locked} onClick={()=>void download(file)}>本人下载 {file.name}</Button>:null}{!file.revoked_at&&file.state!=='REJECTED'&&file.state!=='READY'?<Button variant="secondary" disabled={busy||locked} onClick={()=>void check(file)}>检查或重试 {file.name}</Button>:null}<Button variant={published?'danger':'ghost'} disabled={busy||locked} onClick={()=>onAction(published?'revoke-file':'remove-file',file.id)}>{published?'撤回公开文件':'移除'} {file.name}</Button></div></div></li>;})}</ul>:<p className="field-hint">暂无文件，可以留空。</p>}
    {error?<p className="field-error" role="alert">{error}</p>:null}{failure?<p className="alert alert-error" role="alert">{failure}</p>:null}<p className="field-hint" role="status" aria-label="人才文件操作结果">{busy?message||'正在处理文件…':message}</p>
  </fieldset>;
}
