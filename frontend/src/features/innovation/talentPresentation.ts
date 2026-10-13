import { describeSectionError, isApiError } from '@/lib/errors';
import type { TalentActionDto, TalentFileDto, TalentProfileDto } from './talentApi';

export function talentError(cause:unknown) {
  const view=describeSectionError(cause);
  return {...view,unknown:!isApiError(cause)||cause.status>=500,refresh:isApiError(cause)&&[403,404,409].includes(cause.status)};
}
export function talentDisplayStatus(record:TalentProfileDto|null):string {
  if (!record) return '尚未建档';
  if (record.moderation_state==='TAKEN_DOWN') return '已停止校内展示（运营限制）';
  return record.visibility==='VISIBLE'?'校内展示中':'仅自己可见';
}
export function talentActionMessage(result:TalentActionDto):string {
  if(result.replayed)return `原请求已处理；当前${talentDisplayStatus(result.current)}，请核对当前授权渠道和公开文件。`;
  if(result.action==='save')return '人才草稿已保存，仅自己可见；公开内容需另外确认更新。';
  if(result.action==='publish')return result.current.visibility==='VISIBLE'&&result.current.moderation_state==='NORMAL'?'人才档案已公开。':`发布请求已确认；当前${talentDisplayStatus(result.current)}。`;
  if(result.action==='hide')return '已关闭校内展示，后续旧链接访问也已停止。';
  if(result.action==='revoke-contact')return '联系授权已撤回，其他资料继续展示。';
  if(result.action==='revoke-file')return '文件已撤回公开，后续下载已停止。';
  return '文件已移除。';
}
export function talentFileStatus(file:TalentFileDto):string {
  if (file.revoked_at) return '已撤回公开，检查通过的文件仍可本人下载';
  if (file.state==='READY') return '检查通过';
  if (file.state==='CHECKING') return '正在检查，稍后可刷新状态';
  if (file.failure_code==='CHECK_UNAVAILABLE') return '检查服务暂不可用，可重试检查';
  if (file.failure_code==='MISSING_OBJECT') return '尚未收到文件字节，请重试上传或移除后重选';
  if (file.state==='REJECTED') return '检查未通过，请移除后选择其他文件';
  return '等待上传或检查';
}
export function talentSize(size:number):string {
  return size>=1048576?`${(size/1048576).toFixed(1)} MiB`:`${Math.ceil(size/1024)} KiB`;
}
