import { API_URL, BASE_URL, ensureStudentLogin, expect, mintToken } from './fixtures';
import type { Page } from '@playwright/test';
import type { TalentProfileDto } from '../src/features/innovation/talentApi';

export const TALENT_OWN='/ie/me/talent-profile';
export const PNG=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA7sAAAAASUVORK5CYII=','base64');
export const editor=(page:Page)=>page.getByRole('region',{name:'本人人才档案编辑器'});
export const result=(page:Page)=>page.getByRole('status',{name:'人才档案操作结果'});
export function ownerHeaders(){return {Authorization:`Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}`};}
export async function ownRecord(page:Page){const response=await page.request.get(`${API_URL}${TALENT_OWN}`,{headers:ownerHeaders()});expect(response.status()).toBe(200);return (await response.json()).profile as TalentProfileDto|null;}
export async function prepareTalent(page:Page){
  await ensureStudentLogin(page);let record=await ownRecord(page);
  if(record){
    if(record.visibility==='VISIBLE'){const hide=await page.request.post(`${API_URL}${TALENT_OWN}/hide`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:record.version}});expect(hide.status()).toBe(200);record=(await hide.json()).current;}
    // A hidden snapshot deliberately retains past grants. Reset THIS test
    // account's consent through real commands before the next scenario.
    for(const channel of record!.public_contact_channels){const revoke=await page.request.post(`${API_URL}${TALENT_OWN}/revoke-contact`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:record!.version,channel}});expect(revoke.status()).toBe(200);record=(await revoke.json()).current;}
    const files=await page.request.get(`${API_URL}${TALENT_OWN}/files`,{headers:ownerHeaders()});expect(files.status()).toBe(200);
    for(const file of (await files.json()).items){const removed=await page.request.post(`${API_URL}${TALENT_OWN}/files/${file.id}/remove`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:record!.version}});expect(removed.status()).toBe(200);record=(await removed.json()).current;}
    const reset=await page.request.put(`${API_URL}${TALENT_OWN}`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:record!.version,fields:{}}});expect(reset.status()).toBe(200);
  }
  await page.goto(`${BASE_URL}/profile/talent-profile`);await expect(editor(page)).toBeVisible();
  return editor(page);
}
export async function fillTalent(page:Page,name='模拟新同学'){
  const form=editor(page);
  for(const [label,value] of [['展示姓名',name],['院系','模拟信息管理系'],['年级','2026级'],['自我介绍','模拟资料：愿意参与学习实验与产品实践。'],['我的优势','愿意倾听、整理资料，也愿意学习新技能。']])await form.getByLabel(label,{exact:true}).fill(value);
  await form.getByLabel('PPT 制作',{exact:true}).check();await form.getByLabel('当前合作状态',{exact:true}).selectOption('LOOKING');return form;
}
export async function saveTalent(page:Page){await editor(page).getByRole('button',{name:'保存人才草稿',exact:true}).click();await expect(result(page)).toContainText('已保存');}
export async function publishTalent(page:Page){await editor(page).getByRole('button',{name:/^预览(开启校内展示|更新公开档案)$/}).click();await expect(page.getByRole('dialog')).toBeVisible();await page.getByRole('dialog').getByRole('button',{name:'确认公开',exact:true}).click();await expect(result(page)).toContainText('已公开');await expect(page.getByRole('dialog')).toHaveCount(0);}
export async function uploadTalent(page:Page,name='模拟作品.png'){await editor(page).getByLabel('选择作品或证明文件',{exact:true}).setInputFiles({name,mimeType:'image/png',buffer:PNG});await expect(page.getByRole('status',{name:'人才文件操作结果'})).toContainText('文件检查通过',{timeout:150_000});return editor(page).getByRole('checkbox',{name:`公开 ${name}`,exact:true});}
