import type { TalentFieldsDto, TalentState, TalentWorkMode, TalentContactChannel } from './talentApi';
import { codePointCount } from './formUtils';
import { invalidRecruitmentContactChannels, RECRUITMENT_CONTACT_INPUTS } from './contactFields';

export const TALENT_TEXT_FIELDS = [
  {key:'name',label:'展示姓名',limit:50,rows:1,required:true},
  {key:'faculty',label:'院系',limit:100,rows:1,required:true},
  {key:'grade',label:'年级',limit:30,rows:1,required:true},
  {key:'introduction',label:'自我介绍',limit:1000,rows:3,required:true},
  {key:'advantages',label:'我的优势',limit:2000,rows:3,required:true},
  {key:'skill_notes',label:'技能补充',limit:1000,rows:2,required:false},
  {key:'cooperation_direction',label:'期望合作方向',limit:500,rows:2,required:false},
  {key:'desired_roles',label:'希望承担的角色',limit:500,rows:2,required:false},
  {key:'experience',label:'实践经历',limit:3000,rows:3,required:false},
] as const;
type TextKey=typeof TALENT_TEXT_FIELDS[number]['key'];
export type TalentForm=Record<TextKey,string> & {
  tag_ids:string[]; weekly_hours:string; work_mode:TalentWorkMode; cooperation_state:TalentState|'';
  links:{title:string;url:string}[]; file_ids:string[]; contacts:{channel:TalentContactChannel;value:string}[];
};
export type TalentFormErrors=Record<string,string>;
export const TALENT_STATES:Record<TalentState,string>={LOOKING:'找项目',OPEN_TO_OPPORTUNITIES:'愿意了解机会',NOT_AVAILABLE:'暂不接洽'};
export const TALENT_WORK_MODES:Record<TalentWorkMode,string>={UNSPECIFIED:'未填写',ONLINE:'线上',OFFLINE:'线下',EITHER:'线上或线下均可'};

export function emptyTalentForm():TalentForm {
  return {name:'',faculty:'',grade:'',introduction:'',advantages:'',skill_notes:'',cooperation_direction:'',desired_roles:'',experience:'',tag_ids:[],weekly_hours:'',work_mode:'UNSPECIFIED',cooperation_state:'',links:[],file_ids:[],contacts:[]};
}
export function fieldsToTalentForm(fields:TalentFieldsDto):TalentForm {
  const form=emptyTalentForm();
  for (const {key} of TALENT_TEXT_FIELDS) form[key]=fields[key] ?? '';
  return {...form,tag_ids:[...(fields.tag_ids??[])],weekly_hours:fields.weekly_hours==null?'':String(fields.weekly_hours),work_mode:fields.work_mode??'UNSPECIFIED',cooperation_state:fields.cooperation_state??'',links:(fields.links??[]).map(link=>({...link})),file_ids:[...(fields.file_ids??[])],contacts:(fields.contacts??[]).map(contact=>({...contact}))};
}
export function talentFormToFields(form:TalentForm):TalentFieldsDto {
  const fields = {name:'',faculty:'',grade:'',introduction:'',advantages:'',skill_notes:'',cooperation_direction:'',desired_roles:'',experience:''};
  for (const {key} of TALENT_TEXT_FIELDS) fields[key]=form[key].trim();
  return {...fields,tag_ids:[...form.tag_ids],weekly_hours:form.weekly_hours.trim()===''?null:Number(form.weekly_hours),work_mode:form.work_mode,cooperation_state:form.cooperation_state||null,links:form.links.map(link=>({title:link.title.trim(),url:link.url.trim()})),file_ids:[...form.file_ids],contacts:form.contacts.map(contact=>({...contact,value:contact.value.trim()}))};
}
export function hasTalentChanges(form:TalentForm,fields:TalentFieldsDto):boolean {
  // Transport object ordering is not an edit; compare the same normalized shape.
  return JSON.stringify(talentFormToFields(form))!==JSON.stringify(talentFormToFields(fieldsToTalentForm(fields)));
}
export function validateTalentForm(form:TalentForm,mode:'save'|'publish'):TalentFormErrors {
  const errors:TalentFormErrors={};
  for (const {key,label,limit,required} of TALENT_TEXT_FIELDS) {
    if (mode==='publish' && required && !form[key].trim()) errors[key]=`请填写${label}。`;
    else if (codePointCount(form[key].trim())>limit) errors[key]=`最多 ${limit} 个字符。`;
  }
  if (form.tag_ids.length>10 || new Set(form.tag_ids).size!==form.tag_ids.length) errors.tag_ids='最多选择 10 个不重复的标准标签。';
  else if (mode==='publish' && !form.tag_ids.length) errors.tag_ids='请至少选择一个技能标签。';
  if (mode==='publish' && !form.cooperation_state) errors.cooperation_state='请选择当前合作状态。';
  if (form.weekly_hours.trim() && (!/^\d{1,3}$/.test(form.weekly_hours.trim()) || Number(form.weekly_hours)>168)) errors.weekly_hours='每周投入须为 0～168 的整数；留空表示未填写。';
  if (form.file_ids.length>5 || new Set(form.file_ids).size!==form.file_ids.length) errors.file_ids='最多选择 5 个不重复的公开文件。';
  if (form.links.length>5) errors.links='最多添加 5 个作品链接。';
  form.links.forEach((link,index)=>{
    if (!link.title.trim() || codePointCount(link.title.trim())>100) errors[`link-title-${index}`]='请填写作品名称，最多 100 个字符。';
    let safe=false; const url=link.url.trim();
    try { const parsed=new URL(url); safe=/^https?:\/\//i.test(url) && !!parsed.hostname && !/[\u0000-\u0020]/.test(url) && codePointCount(url)<=2048; } catch { /* Expected invalid input; no URL is submitted. */ }
    if (!safe) errors[`link-url-${index}`]='请输入完整的 http/https 作品链接，最多 2048 个字符。';
  });
  for (const channel of invalidRecruitmentContactChannels(form.contacts)) errors[`contact-${channel}`]=RECRUITMENT_CONTACT_INPUTS[channel].error;
  return errors;
}
