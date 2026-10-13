'use client';
import type { TalentProfileDto, TalentFileDto, TalentTagDto, TalentContactChannel } from './talentApi';
import { TALENT_STATES, type TalentForm } from './talentForm';
import { RecruitmentConfirm } from './RecruitmentConfirm';
import { CONTACT_LABELS } from './recruitmentPresentation';
import { talentSize } from './talentPresentation';
import { TalentTags, TalentMaterialFlag, TalentReadingGroup, TalentReadingSections, TALENT_MATERIAL_NOTICE } from './TalentOverview';

export type TalentConfirmation={kind:'publish'|'hide'|'revoke-contact'|'remove-file'|'revoke-file';channel?:TalentContactChannel;fileId?:string;completed:boolean};
export function TalentPreviewContent({form,tags,files}:{form:TalentForm;tags:TalentTagDto[];files:TalentFileDto[]}){
  const selected=files.filter(file=>form.file_ids.includes(file.id)&&file.state==='READY'&&!file.removed_at&&!file.revoked_at);
  return <div className="ie-talent-preview">
    <div className="ie-talent-summary"><p className="section-title">{form.name||'未填写展示姓名'}</p><p>{form.faculty||'未填写院系'} · {form.grade||'未填写年级'}</p><p className="ie-talent-state">{form.cooperation_state?TALENT_STATES[form.cooperation_state]:'未填写合作状态'}</p><TalentTags tags={tags.filter(tag=>form.tag_ids.includes(tag.id))}/>{!form.tag_ids.length?<p className="field-hint">未匹配到可用标签</p>:null}<TalentMaterialFlag present={selected.length>0}/><p className="field-hint">{TALENT_MATERIAL_NOTICE} 确认公开后，选中的有效附件才会计入材料标志。</p></div>
    <TalentReadingSections content={form}/>
    <TalentReadingGroup title="作品与证明材料">{form.links.map((link,index)=><p className="ie-talent-prose" key={index}>作品链接：{link.title} · {link.url}</p>)}<p className="ie-talent-prose"><strong>公开文件：</strong>{form.file_ids.length?selected.map(file=>`${file.name}（${talentSize(file.size)}）`).join('、')||'所选文件当前不可用，请刷新状态并重新选择':'未选择文件'}</p></TalentReadingGroup>
    <TalentReadingGroup title="联系授权"><p><strong>授权联系：</strong>{form.contacts.length?'以下渠道由你主动填写并选择':'未授权联系方式；仍可以公开档案'}</p>{form.contacts.map(contact=><p className="ie-talent-prose" key={contact.channel}>{CONTACT_LABELS[contact.channel]}：{contact.value}</p>)}</TalentReadingGroup>
  </div>;
}
export function TalentPreview({confirmation,record,form,tags,files,busy,unknown,error,onClose,onConfirm,resultFocus}:{confirmation:TalentConfirmation;record:TalentProfileDto;form:TalentForm;tags:TalentTagDto[];files:TalentFileDto[];busy:boolean;unknown:boolean;error?:string;onClose:()=>void;onConfirm:()=>void;resultFocus:()=>HTMLElement|null}) {
  const {kind}=confirmation;
  const title=kind==='publish'?(record.visibility==='VISIBLE'?'预览更新人才档案':'预览开启人才档案'):kind==='hide'?'关闭校内展示':kind==='revoke-contact'?`撤回${CONTACT_LABELS[confirmation.channel!] }授权`:kind==='revoke-file'?'撤回公开文件':'移除未公开文件';
  const description=kind==='publish'?'确认后，下列内容才会成为校内可见版本。联系方式仅在查看者主动点击时提供。':kind==='hide'?'停止后续搜索、详情、联系与文件访问；私有草稿仍保留。已经获取的内容无法远程收回。':kind==='revoke-contact'?'立即停止该渠道的后续读取，同时取消草稿中的该授权；其他资料继续展示。':kind==='revoke-file'?'立即停止该文件的后续公开下载；本人仍可下载，但该文件不能再次公开，需重新上传并检查。':'移除后不可再上传到或读取这条记录；需要时请重新选择文件。';
  return <RecruitmentConfirm title={title} description={description} confirmLabel={unknown?'用原请求确认结果':kind==='publish'?'确认公开':kind==='hide'?'确认关闭':kind==='revoke-contact'?'确认撤回授权':kind==='revoke-file'?'确认撤回文件':'确认移除'} busy={busy} completed={confirmation.completed} onClose={onClose} onConfirm={onConfirm} resultFocus={resultFocus}>
    {kind==='publish'?<TalentPreviewContent form={form} tags={tags} files={files}/>:confirmation.fileId?<p>{files.find(file=>file.id===confirmation.fileId)?.name??'所选文件'}</p>:null}
    {error?<p className="alert alert-error" role="alert">{error}</p>:null}
  </RecruitmentConfirm>;
}
