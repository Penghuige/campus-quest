import type { TalentFileDto, TalentTagDto } from './talentApi';
import { TALENT_STATES, type TalentForm } from './talentForm';
import { CONTACT_LABELS } from './recruitmentPresentation';
import { talentSize } from './talentPresentation';
import { TalentTags, TalentMaterialFlag, TalentReadingGroup, TalentReadingSections, TALENT_MATERIAL_NOTICE } from './TalentOverview';

export function TalentPreviewContent({form,tags,files}:{form:TalentForm;tags:TalentTagDto[];files:TalentFileDto[]}){
  const selected=files.filter(file=>form.file_ids.includes(file.id)&&file.state==='READY'&&!file.removed_at&&!file.revoked_at);
  const selectedTags=tags.filter(tag=>form.tag_ids.includes(tag.id));
  return <div className="ie-talent-preview">
    <div className="ie-talent-summary"><p className="section-title">{form.name||'未填写展示姓名'}</p><p>{form.faculty||'未填写院系'} · {form.grade||'未填写年级'}</p><p className="ie-talent-state">{form.cooperation_state?TALENT_STATES[form.cooperation_state]:'未填写合作状态'}</p><TalentTags tags={selectedTags}/>{!selectedTags.length?<p className="field-hint">未匹配到可用标签</p>:null}<TalentMaterialFlag present={selected.length>0}/><p className="field-hint">{TALENT_MATERIAL_NOTICE} 确认公开后，选中的有效附件才会计入材料标志。</p></div>
    <TalentReadingSections content={form}/>
    <TalentReadingGroup title="作品与证明材料">{form.links.map((link,index)=><p className="ie-talent-prose" key={index}>作品链接：{link.title} · {link.url}</p>)}<p className="ie-talent-prose"><strong>公开文件：</strong>{form.file_ids.length?selected.map(file=>`${file.name}（${talentSize(file.size)}）`).join('、')||'所选文件当前不可用，请刷新状态并重新选择':'未选择文件'}</p>{selected.length>0&&selected.length<form.file_ids.length?<p className="field-error">部分所选文件当前不可用，请刷新状态并重新选择；上方仅列出可用文件。</p>:null}</TalentReadingGroup>
    <TalentReadingGroup title="联系授权"><p><strong>授权联系：</strong>{form.contacts.length?'以下渠道由你主动填写并选择':'未授权联系方式；仍可以公开档案'}</p>{form.contacts.map(contact=><p className="ie-talent-prose" key={contact.channel}>{CONTACT_LABELS[contact.channel]}：{contact.value}</p>)}</TalentReadingGroup>
  </div>;
}
