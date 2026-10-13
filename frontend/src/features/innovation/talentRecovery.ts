import { getAuthEpoch, onAuthEpochChange } from '@/lib/accessToken';
import type { TalentProfileDto, TalentFileDto, TalentSaveCommand, TalentVersionCommand, TalentRevokeContactCommand, TalentUploadGrant, TalentUploadIntent } from './talentApi';
import { emptyTalentForm, fieldsToTalentForm, hasTalentChanges, talentFormToFields, type TalentForm } from './talentForm';

export type TalentPending =
  | {kind:'save';body:TalentSaveCommand;form:TalentForm}
  | {kind:'publish';body:TalentVersionCommand;form:TalentForm}
  | {kind:'hide';body:TalentVersionCommand;form:TalentForm}
  | {kind:'revoke-contact';body:TalentRevokeContactCommand;form:TalentForm}
  | {kind:'remove-file';body:TalentVersionCommand;fileId:string;form:TalentForm}
  | {kind:'revoke-file';body:TalentVersionCommand;fileId:string;form:TalentForm};
export type TalentRecovery={epoch:number;record:TalentProfileDto|null;form:TalentForm;pending:TalentPending|null};
export type TalentUploadRecovery={epoch:number;body:TalentUploadIntent;file:File;grant:TalentUploadGrant|null};
let recovery:TalentRecovery|undefined;
let upload:TalentUploadRecovery|undefined;
export function resetTalentRecovery():void { recovery=undefined; upload=undefined; }
onAuthEpochChange(resetTalentRecovery);
export function getTalentRecovery():TalentRecovery|undefined { return recovery?.epoch===getAuthEpoch()?recovery:undefined; }
/** Fresh server state owns privacy/version; only unsaved input and commands recover. */
export function reconcileTalentRecovery(initial:TalentProfileDto|null):TalentRecovery {
  const stored=getTalentRecovery();
  const fresh=initial?fieldsToTalentForm(initial.fields):emptyTalentForm();
  if(!stored)return {epoch:getAuthEpoch(),record:initial,form:fresh,pending:null};
  const dirty=hasTalentChanges(stored.form,stored.record?.fields??talentFormToFields(emptyTalentForm()));
  return {...stored,record:initial,form:dirty||stored.pending?stored.form:fresh};
}
export function talentRecoveryNeedsComparison(initial:TalentProfileDto|null):boolean {
  const stored=getTalentRecovery();
  if(!stored||stored.pending||!initial)return false;
  const base=stored.record?.fields??talentFormToFields(emptyTalentForm());
  return hasTalentChanges(stored.form,base)&&hasTalentChanges(fieldsToTalentForm(initial.fields),base);
}
export function putTalentRecovery(value:TalentRecovery):void { if (value.epoch===getAuthEpoch()) recovery=value; }
export function discardTalentRecovery(epoch:number,expectedForm?:TalentForm):void {
  if (recovery?.epoch===epoch && (!expectedForm || recovery.form===expectedForm)) recovery=undefined;
}
export function settleTalentRecovery(epoch:number,command:TalentPending,record:TalentProfileDto):TalentRecovery|undefined {
  const value=getTalentRecovery();
  if (epoch!==getAuthEpoch() || value?.epoch!==epoch || value.pending?.body.request_id!==command.body.request_id) return undefined;
  let form=value.form===command.form && command.kind==='save'?fieldsToTalentForm(record.fields):value.form;
  if (command.kind==='revoke-contact') form={...form,contacts:form.contacts.filter(contact=>contact.channel!==command.body.channel)};
  if (command.kind==='remove-file'||command.kind==='revoke-file') form={...form,file_ids:form.file_ids.filter(id=>id!==command.fileId)};
  recovery={...value,record,form,pending:null};
  return recovery;
}
export function getTalentUploadRecovery():TalentUploadRecovery|undefined { return upload?.epoch===getAuthEpoch()?upload:undefined; }
export function putTalentUploadRecovery(value:TalentUploadRecovery):void { if (value.epoch===getAuthEpoch()) upload=value; }
export function discardTalentUploadRecovery(epoch:number,requestId:string):void { if (upload?.epoch===epoch && upload.body.request_id===requestId) upload=undefined; }
/** Only an authoritative list for this original request can settle its File. */
export function reconcileTalentUploadFiles(files:TalentFileDto[],requestId:string|undefined):'READY'|'REJECTED'|'UNAVAILABLE'|null {
  const stored=getTalentUploadRecovery();
  if(!stored?.grant||stored.body.request_id!==requestId)return null;
  const file=files.find(item=>item.id===stored.grant!.file.id);
  const outcome=!file||file.removed_at||file.revoked_at?'UNAVAILABLE':file.state==='READY'||file.state==='REJECTED'?file.state:null;
  if(outcome)discardTalentUploadRecovery(stored.epoch,stored.body.request_id);
  return outcome;
}
