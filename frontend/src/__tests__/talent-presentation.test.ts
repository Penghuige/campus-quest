import assert from 'node:assert/strict';
import { test } from 'node:test';
import { talentActionMessage, talentDisplayStatus, talentFileStatus, talentError, talentSize } from '../features/innovation/talentPresentation';
import { ApiError } from '../lib/errors';
import { emptyTalentForm, talentFormToFields } from '../features/innovation/talentForm';
import type { TalentActionDto, TalentFileDto } from '../features/innovation/talentApi';

function receipt(action:string,visibility:'HIDDEN'|'VISIBLE',replayed=false):TalentActionDto{return {action,replayed,applied_version:1,current:{id:'00000000-0000-4000-8000-000000000001',fields:talentFormToFields(emptyTalentForm()),version:3,visibility,moderation_state:'NORMAL',public_version:2,public_contact_channels:[],public_file_ids:[],has_unpublished_changes:false,updated_at:'2026-10-13T00:00:00Z'}};}
test('replayed publication does not claim a currently closed profile is public',()=>{
  const text=talentActionMessage(receipt('publish','HIDDEN',true));
  assert.match(text,/仅自己可见/);assert.doesNotMatch(text,/已公开/);
});
test('replayed closure reports the current reopened state instead of claiming denial',()=>{
  const text=talentActionMessage(receipt('hide','VISIBLE',true));
  assert.match(text,/校内展示中/);assert.doesNotMatch(text,/后续旧链接访问也已停止/);
});
test('normal save and publication still explain the completed operation',()=>{
  assert.match(talentActionMessage(receipt('save','HIDDEN')),/草稿已保存.*仅自己可见/);
  assert.match(talentActionMessage(receipt('publish','VISIBLE')),/已公开/);
});

test('governance removal is distinguishable from a hidden or unbuilt profile',()=>{
  assert.match(talentDisplayStatus(null),/尚未建档/);
  assert.match(talentDisplayStatus({...receipt('publish','VISIBLE').current,moderation_state:'TAKEN_DOWN'}),/停止校内展示.*运营/);
});
test('uncertain transport outcomes require original retry while conflicts require refresh',()=>{
  for(const cause of [new Error('network'),new ApiError({code:'INTERNAL_ERROR',message:'unsafe debug',status:500})]){assert.equal(talentError(cause).unknown,true);assert.doesNotMatch(talentError(cause).message,/unsafe debug/);}
  const conflict=talentError(new ApiError({code:'CONFLICT',message:'档案已变化',status:409}));assert.equal(conflict.unknown,false);assert.equal(conflict.refresh,true);
});
test('file feedback never equates waiting, scanner failure or rejection with readiness',()=>{
  const file:TalentFileDto={id:'file',name:'模拟作品.png',content_type:'image/png',size:1024,state:'PENDING',version:1,failure_code:null,removed_at:null,revoked_at:null,created_at:'2026-10-13T00:00:00Z'};
  for(const pending of [file,{...file,state:'CHECKING' as const},{...file,failure_code:'CHECK_UNAVAILABLE'},{...file,failure_code:'MISSING_OBJECT'},{...file,state:'REJECTED' as const}])assert.doesNotMatch(talentFileStatus(pending),/^检查通过$/);
  assert.equal(talentFileStatus({...file,state:'READY'}),'检查通过');assert.match(talentFileStatus({...file,state:'READY',revoked_at:'2026-10-13T00:00:00Z'}),/已撤回.*本人下载/);
  assert.equal(talentSize(1048576),'1.0 MiB');assert.equal(talentSize(1024),'1 KiB');
});
