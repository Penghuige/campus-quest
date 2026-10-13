import { apiRequest, type ApiRequestInit } from '@/lib/api';
import { getAuthEpoch } from '@/lib/accessToken';
import type { components } from '@/lib/api/schema';

type Schema = components['schemas'];
export type TalentFieldsDto = Schema['TalentFields'];
export type TalentProfileDto = Schema['TalentProfileResponse'];
export type TalentFileDto = Schema['TalentFileResponse'];
export type TalentTagDto = Schema['TalentTagResponse'];
export type TalentSaveCommand = Schema['TalentSaveCommand'];
export type TalentVersionCommand = Schema['TalentVersionCommand'];
export type TalentRevokeContactCommand = Schema['TalentRevokeContactCommand'];
export type TalentActionDto = Schema['TalentActionResult'];
export type TalentUploadIntent = Schema['TalentFileIntentCreate'];
export type TalentUploadGrant = Schema['TalentFileIntentResponse'];
export type PublicTalentDto = Schema['PublicTalentResponse'];
export type TalentSummaryDto = Schema['PublicTalentSummary'];
export type TalentState = Schema['PublicTalentContent']['cooperation_state'];
export type TalentWorkMode = Schema['PublicTalentContent']['work_mode'];
export type TalentContactChannel = Schema['ContactInput']['channel'];
const own = '/api/v1/ie/me/talent-profile';
const files = `${own}/files`;
const encoded = encodeURIComponent;

async function request<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await apiRequest<T>(path, { signal:AbortSignal.timeout(15_000), ...init, cache:'no-store' });
  if (epoch !== getAuthEpoch()) throw new DOMException('账号会话已改变','AbortError');
  return result;
}
export const getTalentProfile = () => request<Schema['TalentProfileReadResponse']>(own);
export const saveTalentProfile = (body:TalentSaveCommand) => request<TalentActionDto>(own,{method:'PUT',body});
export const publishTalentProfile = (body:TalentVersionCommand) => request<TalentActionDto>(`${own}/publish`,{method:'POST',body});
export const hideTalentProfile = (body:TalentVersionCommand) => request<TalentActionDto>(`${own}/hide`,{method:'POST',body});
export const revokeTalentContact = (body:TalentRevokeContactCommand) => request<TalentActionDto>(`${own}/revoke-contact`,{method:'POST',body});
export const getTalentTags = () => request<Schema['TalentTagListResponse']>('/api/v1/ie/talent-tags');
export const suggestTalentTag = (body:Schema['TalentSuggestionCommand']) => request<Schema['TalentSuggestionResponse']>('/api/v1/ie/me/talent-tag-suggestions',{method:'POST',body});
export const getTalentFiles = () => request<Schema['TalentFileListResponse']>(files);
export const createTalentFileIntent = (body:TalentUploadIntent) => request<TalentUploadGrant>(files,{method:'POST',body});
export const completeTalentFile = (id:string) => request<TalentFileDto>(`${files}/${encoded(id)}/complete`,{method:'POST',signal:AbortSignal.timeout(150_000)});
export const removeTalentFile = (id:string,body:TalentVersionCommand) => request<TalentActionDto>(`${files}/${encoded(id)}/remove`,{method:'POST',body});
export const revokeTalentFile = (id:string,body:TalentVersionCommand) => request<TalentActionDto>(`${files}/${encoded(id)}/revoke`,{method:'POST',body});
export const downloadOwnTalentFile = (id:string) => request<Blob>(`${files}/${encoded(id)}/content`,{responseFormat:'blob',signal:AbortSignal.timeout(150_000)});
export const getPublicTalents = (params:URLSearchParams) => request<Schema['PublicTalentListResponse']>(`/api/v1/ie/talents?${params}`);
export const getPublicTalent = (id:string) => request<PublicTalentDto>(`/api/v1/ie/talents/${encoded(id)}`);
export const getTalentContacts = (id:string,publicVersion:number) => request<Schema['TalentContactsResponse']>(`/api/v1/ie/talents/${encoded(id)}/contacts`,{method:'POST',body:{expected_public_version:publicVersion}});
export const downloadPublicTalentFile = (id:string,fileId:string,publicVersion:number) => request<Blob>(`/api/v1/ie/talents/${encoded(id)}/files/${encoded(fileId)}/content?expected_public_version=${publicVersion}`,{responseFormat:'blob',signal:AbortSignal.timeout(150_000)});

/** A 412 is only a possible existing object: completeTalentFile must confirm it. */
export async function putTalentFile(grant:TalentUploadGrant,file:File):Promise<void> {
  const epoch=getAuthEpoch();
  const response=await fetch(grant.upload_url,{method:'PUT',headers:grant.client_headers,body:file,credentials:'omit',signal:AbortSignal.timeout(120_000)});
  if (epoch !== getAuthEpoch()) throw new DOMException('账号会话已改变','AbortError');
  if (!response.ok && response.status !== 412) throw new Error('文件上传未完成，请使用原文件重试。');
}

export function saveTalentBlob(blob:Blob,name:string):void {
  const url=URL.createObjectURL(blob);
  const anchor=document.createElement('a'); anchor.href=url; anchor.download=name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  // The object URL is only a local copy of bytes already authorized by the API.
  setTimeout(()=>URL.revokeObjectURL(url),1_000);
}
