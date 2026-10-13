'use client';
import Link from 'next/link';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { SectionError, SectionSkeleton } from '@/components/ui/sectionStates';
import { useSection } from '@/components/ui/useSection';
import { useSession } from '@/features/auth/session';
import { getAuthEpoch } from '@/lib/accessToken';
import { getTalentProfile, getTalentTags, getTalentFiles, saveTalentProfile, publishTalentProfile, hideTalentProfile, revokeTalentContact, removeTalentFile, revokeTalentFile, type TalentProfileDto, type TalentTagDto, type TalentFileDto, type TalentContactChannel } from './talentApi';
import { emptyTalentForm, fieldsToTalentForm, talentFormToFields, validateTalentForm, hasTalentChanges, type TalentForm, type TalentFormErrors } from './talentForm';
import { getTalentRecovery, reconcileTalentRecovery, talentRecoveryNeedsComparison, putTalentRecovery, settleTalentRecovery, getTalentUploadRecovery, discardTalentUploadRecovery, type TalentRecovery, type TalentPending } from './talentRecovery';
import { talentError, talentDisplayStatus, talentActionMessage } from './talentPresentation';
import { TalentFields } from './TalentFields';
import { TalentFiles } from './TalentFiles';
import { TalentPreview, TalentPreviewContent, type TalentConfirmation } from './TalentPreview';
import { TalentTagSuggestion } from './TalentTagSuggestion';
import { CONTACT_LABELS } from './recruitmentPresentation';

export function TalentProfileView() {
  const {state}=useSession();
  if(state.status!=='authenticated')return null;
  if(state.me.role!=='STUDENT'||state.me.status!=='ACTIVE')return <p className="alert alert-warning" role="alert">仅正常状态的学生账号可以维护本人的人才档案。</p>;
  return <TalentLoader key={`${state.me.id}:${getAuthEpoch()}`}/>;
}
function TalentLoader(){
  const {state,retry}=useSection(async()=>{const [own,tags]=await Promise.all([getTalentProfile(),getTalentTags()]);const files=own.profile?await getTalentFiles():{items:[]};return {record:own.profile,tags:tags.items,files:files.items};});
  if(state.status==='loading')return <div role="status" aria-label="正在读取人才档案"><SectionSkeleton/></div>;
  if(state.status==='error')return <SectionError error={state.error} onRetry={retry}/>;
  return <TalentEditor initial={state.data.record} tags={state.data.tags} initialFiles={state.data.files}/>;
}
function TalentEditor({initial,tags,initialFiles}:{initial:TalentProfileDto|null;tags:TalentTagDto[];initialFiles:TalentFileDto[]}){
  const [data,setData]=useState<TalentRecovery>(()=>reconcileTalentRecovery(initial));
  const [files,setFiles]=useState(initialFiles);const [errors,setErrors]=useState<TalentFormErrors>({});
  const [error,setError]=useState<ReturnType<typeof talentError>|null>(()=>talentRecoveryNeedsComparison(initial)?{message:'服务器草稿已改变；输入已恢复，请比较后明确选择如何继续。',requestId:null,unknown:false,refresh:true}:null);const [message,setMessage]=useState(()=>getTalentRecovery()?'站内返回已核对最新展示状态；未保存输入仍保留，请核对后保存。':'');const [busy,setBusy]=useState(false);
  const [remote,setRemote]=useState<TalentProfileDto|null>(()=>talentRecoveryNeedsComparison(initial)?initial:null);const [confirmation,setConfirmation]=useState<TalentConfirmation|null>(null);
  const active=useRef(false);const running=useRef(false);const latest=useRef(data);const resultRef=useRef<HTMLParagraphElement>(null);const formRef=useRef<HTMLFormElement>(null);
  const {record,form,pending}=data;const dirty=hasTalentChanges(form,record?.fields??talentFormToFields(emptyTalentForm()));
  useEffect(()=>{active.current=true;putTalentRecovery(latest.current);return()=>{active.current=false;};},[]);
  useEffect(()=>{if(!dirty&&!pending)return;const warn=(event:BeforeUnloadEvent)=>{event.preventDefault();};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[dirty,pending]);
  function current(){return active.current&&latest.current.epoch===getAuthEpoch();}
  function update(next:TalentRecovery){latest.current=next;putTalentRecovery(next);setData(next);}
  function change(next:TalentForm){update({...latest.current,form:next});setErrors({});}
  function validate(mode:'save'|'publish'){
    const next=validateTalentForm(latest.current.form,mode);
    if(mode==='publish'){
      if(form.tag_ids.some(id=>!tags.some(tag=>tag.id===id)))next.tag_ids='请移除停用标签并重新选择。';
      if(form.file_ids.some(id=>!files.some(file=>file.id===id&&file.state==='READY'&&!file.revoked_at&&!file.removed_at)))next.file_ids='所选文件尚未通过检查或已撤回，请取消选择或刷新状态。';
    }
    setErrors(next);
    const first=Object.keys(next)[0];if(first){setMessage('请修正提示的内容后继续。');formRef.current?.querySelector<HTMLElement>(`[name="talent-${first}"]`)?.focus();return false;}return true;
  }
  async function run(command:TalentPending){
    if(running.current||!current())return;
    running.current=true;setBusy(true);setError(null);update({...latest.current,pending:command});
    try{
      const result=command.kind==='save'?await saveTalentProfile(command.body):command.kind==='publish'?await publishTalentProfile(command.body):command.kind==='hide'?await hideTalentProfile(command.body):command.kind==='revoke-contact'?await revokeTalentContact(command.body):command.kind==='remove-file'?await removeTalentFile(command.fileId,command.body):await revokeTalentFile(command.fileId,command.body);
      const next=settleTalentRecovery(latest.current.epoch,command,result.current);
      if(!current()||!next)return;
      latest.current=next;setData(next);setRemote(null);
      setMessage(talentActionMessage(result));
      if(command.kind!=='save')setConfirmation(value=>value?{...value,completed:true}:null);
      if(command.kind==='remove-file'||command.kind==='revoke-file'){
        const upload=getTalentUploadRecovery();if(upload?.grant?.file.id===command.fileId)discardTalentUploadRecovery(upload.epoch,upload.body.request_id);
        const all=await getTalentFiles();if(current())setFiles(all.items);
      }
    }catch(cause){
      const view=talentError(cause);const stored=getTalentRecovery();
      if(!view.unknown&&stored?.pending?.body.request_id===command.body.request_id){const next={...stored,pending:null};putTalentRecovery(next);if(current()){latest.current=next;setData(next);}}
      if(current())setError(view);
    }finally{running.current=false;if(current())setBusy(false);}
  }
  const locked=busy||!!pending;
  function save(event:FormEvent){event.preventDefault();if(locked||error?.refresh||!validate('save'))return;void run({kind:'save',form,body:{request_id:crypto.randomUUID(),version:record?.version??0,fields:talentFormToFields(form)}});}
  function preview(kind:TalentConfirmation['kind'],channel?:TalentContactChannel,fileId?:string){
    if(!record||locked||error?.refresh)return;
    if(kind==='publish'){if(!validate('publish'))return;if(dirty){setMessage('请先保存当前输入，再预览公开；保存不会更新校内展示。');resultRef.current?.focus();return;}}
    setError(null);setConfirmation({kind,channel,fileId,completed:false});
  }
  function confirm(){
    if(pending){void run(pending);return;}if(!record||!confirmation||busy)return;
    const body={request_id:crypto.randomUUID(),version:record.version};const kind=confirmation.kind;
    if(kind==='revoke-contact')void run({kind,body:{...body,channel:confirmation.channel!},form});
    else if(kind==='remove-file'||kind==='revoke-file')void run({kind,body,fileId:confirmation.fileId!,form});
    else void run({kind,body,form});
  }
  async function readLatest(){if(running.current||pending||!current())return;running.current=true;setBusy(true);try{const own=await getTalentProfile();if(current()){setRemote(own.profile);if(!own.profile)setMessage('当前账号暂无档案；请返回后重新建档。');}}catch(cause){if(current())setError(talentError(cause));}finally{running.current=false;if(current())setBusy(false);}}
  return <section className="section" aria-label="本人人才档案编辑器">
    <h2 className="section-title">本人人才档案</h2><p className="field-hint">{talentDisplayStatus(record)}{record?.has_unpublished_changes?' · 有尚未公开的改动':''}</p>
    <p className="field-hint">任何正常状态的学生都可建档，无需负责人资格或已有项目。先保存草稿，再预览并确认公开；作品、经历和联系方式均可留空。</p>
    {dirty?<p className="field-hint">未保存输入仅暂留本标签页内存，站内返回可继续；刷新或退出账号会清除。</p>:null}
    <form className="panel form ie-draft-form" method="post" noValidate ref={formRef} onSubmit={save}>
      <TalentFields form={form} tags={tags} errors={errors} onChange={change}/>
      <TalentFiles files={files} selected={form.file_ids} publicIds={record?.public_file_ids??[]} available={!!record} locked={locked} error={errors.file_ids} onFiles={setFiles} onSelection={ids=>change({...latest.current.form,file_ids:ids})} onAction={(kind,id)=>preview(kind,undefined,id)}/>
      {error?<div className="alert alert-error" role="alert"><p>{error.message}</p>{pending?<Button variant="secondary" disabled={busy} onClick={()=>void run(pending)}>重试原请求</Button>:error.refresh?<Button variant="secondary" disabled={busy} onClick={()=>void readLatest()}>读取最新版本（保留输入）</Button>:null}</div>:pending&&!busy?<div className="alert alert-warning"><p>上次请求结果尚待确认；重试会保留后来输入，使用原请求核对结果。</p><Button variant="secondary" onClick={()=>void run(pending)}>重试原请求</Button></div>:null}
      <p className={Object.keys(errors).length?'field-error':'field-hint'} role="status" aria-label="人才档案操作结果" tabIndex={-1} ref={resultRef}>{busy?'正在处理，请稍候…':message||(dirty?'有未保存的输入。':record?'草稿已保存。':'可以开始填写，也可以先保存空草稿后上传文件。')}</p>
      <Button type="submit" disabled={locked||error?.refresh===true}>{busy?'正在处理…':'保存人才草稿'}</Button>
      <div className="ie-draft-actions">{record?.moderation_state==='NORMAL'?<><Button disabled={locked||error?.refresh===true} onClick={()=>preview('publish')}>{record.visibility==='VISIBLE'?'预览更新公开档案':'预览开启校内展示'}</Button>{record.visibility==='VISIBLE'?<Button variant="secondary" disabled={locked||error?.refresh===true} onClick={()=>preview('hide')}>关闭校内展示</Button>:null}</>:null}{record?.public_contact_channels.map(channel=><Button variant="secondary" key={channel} disabled={locked||error?.refresh===true} onClick={()=>preview('revoke-contact',channel)}>立即撤回{CONTACT_LABELS[channel]}授权</Button>)}{record?.visibility==='VISIBLE'?<Link href={`/innovation/talents/${record.id}`}>查看校内公开档案</Link>:null}</div>
      {record?.public_contact_channels.length?<p className="field-hint">取消上方勾选只改变草稿；要立即停止当前公开渠道的读取，请使用“立即撤回”操作。</p>:null}
    </form>
    {remote?<section className="panel" aria-label="最新人才版本"><h3 className="section-title">最新已保存版本</h3><p>{talentDisplayStatus(remote)} · {remote.fields.name||'未填写姓名'}</p><p className="field-hint">上方输入仍保留。以最新版本继续保存会用当前输入更新草稿；也可以明确载入服务器内容。</p><TalentPreviewContent form={fieldsToTalentForm(remote.fields)} tags={tags} files={files}/><div className="ie-draft-actions"><Button variant="secondary" onClick={()=>{update({...latest.current,record:remote,pending:null});setRemote(null);setError(null);setMessage('已以最新版本继续，当前输入仍保留，请核对后保存。');}}>以最新版本继续（保留输入）</Button><Button variant="secondary" onClick={()=>{update({...latest.current,record:remote,form:fieldsToTalentForm(remote.fields),pending:null});setRemote(null);setError(null);setErrors({});setMessage('已载入服务器内容。');}}>载入服务器内容（替换输入）</Button></div></section>:null}
    <TalentTagSuggestion/>
    {record&&confirmation?<TalentPreview confirmation={confirmation} record={record} form={form} tags={tags} files={files} busy={busy} unknown={!!pending&&!busy} error={error?.message} onClose={()=>setConfirmation(null)} onConfirm={confirm} resultFocus={()=>resultRef.current}/>:null}
  </section>;
}
