import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { getAuthEpoch, recordLogin, recordLogout, setAccessToken } from '../lib/accessToken';
import { emptyTalentForm, talentFormToFields } from '../features/innovation/talentForm';
import { getTalentRecovery, putTalentRecovery, discardTalentRecovery, settleTalentRecovery, getTalentUploadRecovery, putTalentUploadRecovery, discardTalentUploadRecovery } from '../features/innovation/talentRecovery';
afterEach(recordLogout);
const snapshot=()=>({epoch:getAuthEpoch(), record:null, form:{...emptyTalentForm(),introduction:'未保存输入',contacts:[{channel:'EMAIL' as const,value:'qa@example.invalid'}]},pending:null});
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
