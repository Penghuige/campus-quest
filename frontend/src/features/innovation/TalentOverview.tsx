import type { ReactNode } from 'react';
import type { PublicTalentDto, TalentTagDto } from './talentApi';
import { TALENT_TEXT_FIELDS, TALENT_WORK_MODES } from './talentForm';

export const TALENT_MATERIAL_NOTICE='材料由本人提供，文件安全检查不代表内容真实性或能力已核实。';

export function TalentTags({tags}:{tags:TalentTagDto[]}) {
  return <ul className="ie-talent-tags" aria-label="技能标签">{tags.map(tag=><li key={tag.id}>{tag.name}</li>)}</ul>;
}

export function TalentMaterialFlag({present,describedBy}:{present:boolean;describedBy?:string}) {
  return present?<span className="ie-talent-material" aria-describedby={describedBy}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9m-6 11 7-7"/></svg>含作品/证明材料</span>:null;
}

type ReadingContent=Pick<PublicTalentDto['content'],'introduction'|'advantages'|'skill_notes'|'cooperation_direction'|'desired_roles'|'experience'|'work_mode'> & {weekly_hours:string|number|null};
export function TalentReadingGroup({title,children}:{title:string;children:ReactNode}) {
  return <section className="ie-talent-reading" aria-label={title}><h2 className="section-title">{title}</h2>{children}</section>;
}

export function TalentReadingSections({content}:{content:ReadingContent}) {
  const fields=(keys:ReadonlyArray<keyof ReadingContent>)=><dl className="ie-talent-prose">{TALENT_TEXT_FIELDS.filter(({key})=>keys.includes(key as keyof ReadingContent)).map(({key,label})=>{
    const value=content[key as keyof ReadingContent];
    return value?<div key={key}><dt className="field-label">{label}</dt><dd>{value}</dd></div>:null;
  })}</dl>;
  return <>
    <TalentReadingGroup title="个人与优势">{fields(['introduction','advantages','skill_notes'])}</TalentReadingGroup>
    <TalentReadingGroup title="合作意向">{fields(['cooperation_direction','desired_roles'])}<dl className="ie-talent-prose"><div><dt className="field-label">每周可投入</dt><dd>{content.weekly_hours==null||content.weekly_hours===''?'未填写':`${content.weekly_hours} 小时`} · {TALENT_WORK_MODES[content.work_mode]}</dd></div></dl></TalentReadingGroup>
    <TalentReadingGroup title="实践经历">{content.experience?fields(['experience']):<p className="field-hint">暂未填写经历；可以从小任务开始合作。</p>}</TalentReadingGroup>
  </>;
}
