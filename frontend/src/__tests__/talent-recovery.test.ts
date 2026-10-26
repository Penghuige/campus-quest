import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { getAuthEpoch, recordLogin, recordLogout, setAccessToken } from '../lib/accessToken';
import { emptyTalentForm, fieldsToTalentForm, talentFormToFields } from '../features/innovation/talentForm';
import { getTalentRecovery, putTalentRecovery, discardTalentRecovery, settleTalentRecovery, getTalentUploadRecovery, putTalentUploadRecovery, discardTalentUploadRecovery, reconcileTalentRecovery, reconcileTalentUploadFiles, talentRecoveryNeedsComparison } from '../features/innovation/talentRecovery';
import type { TalentProfileDto, TalentFileDto, TalentUploadGrant } from '../features/innovation/talentApi';
afterEach(recordLogout);
const snapshot=()=>({epoch:getAuthEpoch(), record:null, form:{...emptyTalentForm(),introduction:'未保存输入',contacts:[{channel:'EMAIL' as const,value:'qa@example.invalid'}]},pending:null});

function record(version=1,visibility:'HIDDEN'|'VISIBLE'='HIDDEN'):TalentProfileDto{return {id:'profile',version,visibility,moderation_state:'NORMAL',public_version:version,public_contact_channels:[],public_file_ids:[],has_unpublished_changes:false,fields:talentFormToFields({...emptyTalentForm(),name:'模拟同学',introduction:`版本${version}`}),updated_at:'2026-10-13T00:00:00Z'};}
test('simultaneous private edits require an explicit comparison without dropping an uncertain command',()=>{
  const original=record();const remote=record(2);assert.equal(talentRecoveryNeedsComparison(remote),false);
  const form={...fieldsToTalentForm(original.fields),advantages:'本地编辑'};putTalentRecovery({epoch:getAuthEpoch(),record:original,form,pending:null});assert.equal(talentRecoveryNeedsComparison(remote),true);assert.equal(talentRecoveryNeedsComparison(null),false);
  assert.equal(talentRecoveryNeedsComparison({...original,version:2,visibility:'VISIBLE'}),false);
  const pending={kind:'save' as const,form,body:{request_id:'uncertain',version:1,fields:talentFormToFields(form)}};putTalentRecovery({epoch:getAuthEpoch(),record:original,form,pending});assert.equal(talentRecoveryNeedsComparison(remote),false);assert.equal(reconcileTalentRecovery(remote).pending,pending);
  putTalentRecovery({epoch:getAuthEpoch(),record:original,form:fieldsToTalentForm(original.fields),pending:null});assert.equal(talentRecoveryNeedsComparison(remote),false);
});
test('current server record controls visibility and clean recovered fields',()=>{
  assert.equal(reconcileTalentRecovery(null).record,null);const old=record();putTalentRecovery({epoch:getAuthEpoch(),record:old,form:fieldsToTalentForm(old.fields),pending:null});
  const opened=record(2,'VISIBLE');assert.equal(reconcileTalentRecovery(opened).record,opened);assert.equal(reconcileTalentRecovery(opened).form.introduction,'版本2');assert.equal(reconcileTalentRecovery(null).record,null);
});
test('fresh server state never erases unsaved input or rekeys an original pending command',()=>{
  const old=record();const form={...fieldsToTalentForm(old.fields),introduction:'未保存的新输入'};const body={request_id:'original-save',version:1,fields:talentFormToFields(form)};const pending={kind:'save' as const,body,form};
  putTalentRecovery({epoch:getAuthEpoch(),record:old,form,pending});const current=record(2,'VISIBLE');const result=reconcileTalentRecovery(current);assert.equal(result.record,current);assert.equal(result.form,form);assert.equal(result.pending,pending);assert.equal(result.pending?.body.version,1);
  putTalentRecovery({...snapshot(),pending:null});assert.equal(reconcileTalentRecovery(current).form.introduction,'未保存输入');recordLogin('new-account');assert.equal(reconcileTalentRecovery(current).form.introduction,'版本2');
});
test('authoritative file terminal states settle only the matching granted upload',()=>{
  const file:TalentFileDto={id:'file',name:'模拟文件.pdf',size:4,content_type:'application/pdf',state:'PENDING',failure_code:null,version:1,created_at:'2026-10-13T00:00:00Z',removed_at:null,revoked_at:null};
  const grant:TalentUploadGrant={file,upload_url:'https://example.invalid/private-put',expires_at:'2026-10-13T00:15:00Z',client_headers:{},pinned_content_length:4};
  const value={epoch:getAuthEpoch(),file:new File(['mock'],file.name),body:{request_id:'original-file',name:file.name,size:4,content_type:'application/pdf' as const},grant};
  assert.equal(reconcileTalentUploadFiles([],'original-file'),null);putTalentUploadRecovery({...value,grant:null});assert.equal(reconcileTalentUploadFiles([],'original-file'),null);assert.ok(getTalentUploadRecovery());
  putTalentUploadRecovery(value);assert.equal(reconcileTalentUploadFiles([],'other-request'),null);assert.ok(getTalentUploadRecovery());assert.equal(reconcileTalentUploadFiles([file],'original-file'),null);assert.ok(getTalentUploadRecovery());
  for(const terminal of [{...file,state:'READY' as const},{...file,state:'REJECTED' as const},{...file,removed_at:'2026-10-13T00:01:00Z'},{...file,revoked_at:'2026-10-13T00:01:00Z'}]){putTalentUploadRecovery(value);assert.equal(reconcileTalentUploadFiles([terminal],'original-file'),terminal.removed_at||terminal.revoked_at?'UNAVAILABLE':terminal.state);assert.equal(getTalentUploadRecovery(),undefined);}
  putTalentUploadRecovery(value);assert.equal(reconcileTalentUploadFiles([],'original-file'),'UNAVAILABLE');assert.equal(getTalentUploadRecovery(),undefined);
});
test('recovery is memory scoped to the current identity and survives token rotation', () => {
  const value=snapshot(); putTalentRecovery(value); setAccessToken('same-account');
  assert.equal(getTalentRecovery(),value); recordLogin('another-account');
  assert.equal(getTalentRecovery(),undefined); putTalentRecovery(value);
  assert.equal(getTalentRecovery(),undefined);
});
test('late clear cannot erase newer edits', () => {
  const old=snapshot(); putTalentRecovery(old); const newer={...old,form:{...old.form,introduction:'较新的输入'}};
  putTalentRecovery(newer); discardTalentRecovery(old.epoch,old.form);
  assert.equal(getTalentRecovery(),newer); discardTalentRecovery(newer.epoch,newer.form);
  assert.equal(getTalentRecovery(),undefined);
});
test('lost receipt retry retains original payload while late acknowledgement preserves newer input', () => {
  const old=snapshot(); const command={kind:'save' as const,body:{request_id:'00000000-0000-4000-8000-000000000011',version:0,fields:talentFormToFields(old.form)},form:old.form};
  const newer={...old,form:{...old.form,introduction:'保存途中写的新输入'},pending:command}; putTalentRecovery(newer);
  const record={id:'00000000-0000-4000-8000-000000000021',fields:command.body.fields,version:1,visibility:'HIDDEN' as const,moderation_state:'NORMAL' as const,public_version:0,public_contact_channels:[],public_file_ids:[],has_unpublished_changes:true,updated_at:'2026-10-12T00:00:00Z'};
  settleTalentRecovery(old.epoch,command,record);
  assert.equal(getTalentRecovery()?.form,newer.form); assert.equal(getTalentRecovery()?.record?.version,1); assert.equal(getTalentRecovery()?.pending,null);
  assert.equal(command.body.fields.introduction,'未保存输入');
  recordLogout(); putTalentRecovery(snapshot()); settleTalentRecovery(old.epoch,command,record);
  assert.equal(getTalentRecovery()?.record,null);
});

test('original upload stays in memory until its own receipt or account change',()=>{
  const epoch=getAuthEpoch();const file=new File(['mock'],'模拟作品.pdf',{type:'application/pdf'});
  const value={epoch,file,body:{request_id:'original-upload',name:file.name,size:file.size,content_type:'application/pdf' as const},grant:null};
  putTalentUploadRecovery(value);setAccessToken('refreshed-same-account');assert.equal(getTalentUploadRecovery()?.file,file);
  discardTalentUploadRecovery(epoch,'another-request');assert.equal(getTalentUploadRecovery(),value);
  discardTalentUploadRecovery(epoch,'original-upload');assert.equal(getTalentUploadRecovery(),undefined);
  putTalentUploadRecovery(value);recordLogin('another-account');assert.equal(getTalentUploadRecovery(),undefined);
  putTalentUploadRecovery(value);assert.equal(getTalentUploadRecovery(),undefined);
});

test('revocation updates only the affected selection and preserves unrelated newer input',()=>{
  const old=snapshot();const form={...old.form,file_ids:['revoked-file','kept-file']};
  const body={request_id:'revoke-file',version:1};const pending={kind:'revoke-file' as const,fileId:'revoked-file',body,form};
  putTalentRecovery({...old,form:{...form,introduction:'较新输入'},pending});
  const record={id:'profile',fields:talentFormToFields(form),version:2,visibility:'VISIBLE' as const,moderation_state:'NORMAL' as const,public_version:2,public_contact_channels:[],public_file_ids:['kept-file'],has_unpublished_changes:true,updated_at:'2026-10-13T00:00:00Z'};
  settleTalentRecovery(old.epoch,pending,record);assert.equal(getTalentRecovery()?.form.introduction,'较新输入');assert.deepEqual(getTalentRecovery()?.form.file_ids,['kept-file']);
  const current=getTalentRecovery()!;const revoke={kind:'revoke-contact' as const,body:{request_id:'revoke-contact',version:2,channel:'EMAIL' as const},form:current.form};putTalentRecovery({...current,pending:revoke});settleTalentRecovery(old.epoch,revoke,{...record,version:3});assert.deepEqual(getTalentRecovery()?.form.contacts,[]);assert.equal(getTalentRecovery()?.form.introduction,'较新输入');
});
