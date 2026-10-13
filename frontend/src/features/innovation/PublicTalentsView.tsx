'use client';
import Link from 'next/link';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { AuthField } from '@/features/auth/AuthField';
import { EmptyState, SectionError, SectionSkeleton } from '@/components/ui/sectionStates';
import { useSection } from '@/components/ui/useSection';
import { useSession } from '@/features/auth/session';
import { getAuthEpoch } from '@/lib/accessToken';
import { describeSectionError, isApiError } from '@/lib/errors';
import { getPublicTalents, getPublicTalent, getTalentTags, downloadPublicTalentFile, saveTalentBlob, type PublicTalentDto, type TalentTagDto } from './talentApi';
import { parseTalentQuery, buildTalentQuery, talentListHref } from './talentQuery';
import { TALENT_STATES, TALENT_WORK_MODES, TALENT_TEXT_FIELDS } from './talentForm';
import { codePointCount } from './draftForm';
import { TalentContacts } from './TalentContacts';
import { talentSize } from './talentPresentation';

export function PublicTalentsView({id,query=''}:{id?:string;query?:string}){
  const {state}=useSession();if(state.status!=='authenticated'||state.me.status!=='ACTIVE')return null;
  const key=`${state.me.id}:${getAuthEpoch()}:${id??query}`;
  return id?<TalentDetail key={key} id={id} query={query}/>:<TalentDirectory key={key} query={query} student={state.me.role==='STUDENT'}/>;
}
function TalentDirectory({query,student}:{query:string;student:boolean}){
  const filters=parseTalentQuery(new URLSearchParams(query));
  const {state,retry}=useSection(async()=>{const [list,tags]=await Promise.all([getPublicTalents(buildTalentQuery(filters)),getTalentTags()]);return {list,tags:tags.items};});
  return <section className="section" aria-label="校内人才库">
    {student?<p><Link className="section-link" href="/profile/talent-profile">建立或管理我的人才档案</Link></p>:null}
    {state.status==='loading'?<div role="status" aria-label="正在查找人才"><SectionSkeleton/></div>:state.status==='error'?<SectionError error={state.error} onRetry={retry}/>:<>
      <TalentFilters query={query} tags={state.data.tags}/><p className="field-hint" role="status">找到 {state.data.list.total} 位同学，按最近公开时间排序。</p>
      {state.data.list.items.length?<ul className="ie-draft-list">{state.data.list.items.map(item=><li className="ie-draft-row" key={item.id}><div className="ie-draft-copy"><h2 className="section-title"><Link href={`/innovation/talents/${item.id}${buildTalentQuery(filters).size?'?'+buildTalentQuery(filters):''}`}>{item.name}</Link></h2><p>{item.faculty} · {item.grade} · {TALENT_STATES[item.cooperation_state]}</p><p className="field-hint">{item.tags.map(tag=>tag.name).join('、')}</p><p className="ie-draft-excerpt">{item.introduction}</p><p className="ie-draft-excerpt">{item.advantages}</p></div></li>)}</ul>:<EmptyState title="暂无符合条件的人才" hint="只有同学主动公开的档案会出现在这里，可以调整筛选或稍后再来。"/>}
      <nav className="ie-draft-actions" aria-label="人才分页">{filters.offset>0?<Link href={talentListHref(buildTalentQuery(filters,Math.max(0,filters.offset-20)).toString())}>上一页</Link>:null}{filters.offset+state.data.list.items.length<state.data.list.total?<Link href={talentListHref(buildTalentQuery(filters,filters.offset+20).toString())}>下一页</Link>:null}</nav>
    </>}
  </section>;
}
function TalentFilters({query,tags}:{query:string;tags:TalentTagDto[]}){
  const router=useRouter();const [filters,setFilters]=useState(()=>parseTalentQuery(new URLSearchParams(query)));const [error,setError]=useState('');const inputRef=useRef<HTMLInputElement>(null);
  function submit(event:FormEvent){event.preventDefault();if(codePointCount(filters.q.trim())>100){setError('搜索词最多 100 个字符。');inputRef.current?.focus();return;}setError('');router.push(talentListHref(buildTalentQuery(filters,0).toString()));}
  return <form className="panel form" onSubmit={submit} noValidate method="get" aria-label="人才筛选"><AuthField name="talent-search" label="搜索同学或技能" hint="可搜索展示姓名、院系、介绍、优势、经历和技能；最多 100 字。" error={error} inputProps={{value:filters.q,ref:inputRef,onChange:event=>setFilters({...filters,q:event.target.value})}}/>
    <fieldset className="ie-recruitment-fields"><legend className="field-label">技能筛选</legend><p className="field-hint">最多选择 5 个；同时具备所有所选标签的同学才会匹配。</p>{tags.map(tag=><label className="field-label" key={tag.id}><input type="checkbox" checked={filters.tag_ids.includes(tag.id)} disabled={!filters.tag_ids.includes(tag.id)&&filters.tag_ids.length>=5} onChange={event=>setFilters({...filters,tag_ids:event.target.checked?[...filters.tag_ids,tag.id]:filters.tag_ids.filter(id=>id!==tag.id)})}/> {tag.name}</label>)}</fieldset>
    <div className="field"><label className="field-label" htmlFor="talent-filter-state">合作状态筛选</label><select className="input" id="talent-filter-state" value={filters.cooperation_state} onChange={event=>setFilters({...filters,cooperation_state:event.target.value as typeof filters.cooperation_state})}><option value="">所有合作状态</option>{Object.entries(TALENT_STATES).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></div><div className="ie-draft-actions"><Button type="submit">查找人才</Button><Link href="/innovation/talents">清除筛选</Link></div>
  </form>;
}
function TalentDetail({id,query}:{id:string;query:string}){
  const {state,retry}=useSection(()=>getPublicTalent(id));
  const back=<Link className="section-link" href={talentListHref(query)}>返回人才列表</Link>;
  if(state.status==='loading')return <>{back}<h1 className="page-title">人才档案</h1><SectionSkeleton/></>;
  if(state.status==='error')return <>{back}<h1 className="page-title">人才档案</h1>{isApiError(state.error)&&state.error.status===404?<p className="alert alert-error" role="alert">档案不存在或已停止校内展示，请返回列表查看当前人才。</p>:<SectionError error={state.error} onRetry={retry}/>}</>;
  const item=state.data;const content=item.content;
  return <article className="section ie-talent-public" aria-label="校内人才详情">{back}<h1 className="page-title">{content.name}</h1><p className="page-subtitle">{content.faculty} · {content.grade} · {TALENT_STATES[content.cooperation_state]}</p><p className="field-hint">同学本人填写并确认公开，未经成果核实。演示阶段使用模拟资料。</p><p>{content.tags.map(tag=>tag.name).join('、')}</p>
    <dl className="ie-review-prose">{TALENT_TEXT_FIELDS.filter(({key})=>!['name','faculty','grade'].includes(key)&&!!content[key]).map(({key,label})=><div key={key}><dt className="field-label">{label}</dt><dd>{content[key]}</dd></div>)}<div><dt className="field-label">每周可投入</dt><dd>{content.weekly_hours==null?'未填写':`${content.weekly_hours} 小时`} · {TALENT_WORK_MODES[content.work_mode]}</dd></div></dl>
    <section className="section" aria-label="公开作品链接"><h2 className="section-title">作品链接</h2>{content.links.length?<ul>{content.links.map((link,index)=><li key={index}><a href={link.url} target="_blank" rel="noopener noreferrer">{link.title}（新窗口）</a></li>)}</ul>:<p className="field-hint">暂无作品链接。</p>}</section>
    <TalentPublicFiles key={`files:${item.public_version}`} item={item} onRefresh={retry}/><TalentContacts key={`contacts:${item.public_version}`} item={item} onRefresh={retry}/>
  </article>;
}
function TalentPublicFiles({item,onRefresh}:{item:PublicTalentDto;onRefresh:()=>void}){
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [changed,setChanged]=useState(false);const active=useRef(false);const epoch=useRef(getAuthEpoch());const running=useRef(false);
  useEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
  async function download(file:PublicTalentDto['files'][number]){if(running.current)return;running.current=true;setBusy(true);setError('');try{const blob=await downloadPublicTalentFile(item.id,file.id,item.public_version);if(active.current&&epoch.current===getAuthEpoch())saveTalentBlob(blob,file.name);}catch(cause){if(active.current&&epoch.current===getAuthEpoch()){setError(isApiError(cause)&&cause.status===404?'档案或文件已停止公开，无法下载。':describeSectionError(cause).message);setChanged(isApiError(cause)&&[404,409].includes(cause.status));}}finally{running.current=false;if(active.current&&epoch.current===getAuthEpoch())setBusy(false);}}
  return <section className="section" aria-label="公开作品文件"><h2 className="section-title">作品与证明文件</h2>{item.files.length?<ul className="ie-draft-list">{item.files.map(file=><li className="ie-draft-row" key={file.id}><div className="ie-draft-copy"><p>{file.name} · {talentSize(file.size)}</p><Button variant="secondary" disabled={busy||changed} onClick={()=>void download(file)}>下载 {file.name}</Button></div></li>)}</ul>:<p className="field-hint">暂无公开文件。</p>}<p className="field-hint" role="status">{busy?'正在核对权限并读取文件…':''}</p>{error?<p className="alert alert-error" role="alert">{error}</p>:null}{changed?<Button variant="secondary" onClick={onRefresh}>刷新档案</Button>:null}</section>;
}
