import assert from 'node:assert/strict';
import { test } from 'node:test';
import { emptyTalentForm, fieldsToTalentForm, talentFormToFields, validateTalentForm, TALENT_TEXT_FIELDS, hasTalentChanges } from '../features/innovation/talentForm';
import { isKnownErrorCode } from '../lib/errors';

test('talent contact unavailability has a registered independent client error code', () => {
  assert.ok(isKnownErrorCode('TALENT_CONTACTS_UNAVAILABLE'));
});

const minimal = () => ({ ...emptyTalentForm(), name:'模拟新生', faculty:'模拟学院', grade:'2026级', introduction:'模拟资料：愿意尝试', advantages:'愿意从小任务开始', tag_ids:['00000000-0000-4000-8000-000000000001'], cooperation_state:'LOOKING' as const });

test('saved fields are clean regardless of JSON transport property order',()=>{
  const form=minimal();const values=talentFormToFields(form);
  const reordered=Object.fromEntries(Object.entries(values).reverse()) as typeof values;
  assert.equal(hasTalentChanges(form,reordered),false);
  assert.equal(hasTalentChanges({...form,introduction:'新的私有输入'},reordered),true);
});
test('a novice can publish without experience, files, links or contacts', () => {
  assert.deepEqual(validateTalentForm(minimal(),'publish'),{});
  assert.deepEqual(validateTalentForm(emptyTalentForm(),'save'),{});
  assert.ok(Object.keys(validateTalentForm(emptyTalentForm(),'publish')).length >= 7);
});
for (const {key,limit} of TALENT_TEXT_FIELDS) test(`${key} counts Unicode codepoints at its exact boundary`, () => {
  const form = {...minimal(),[key]:'🌱'.repeat(limit)};
  assert.equal(validateTalentForm(form,'save')[key],undefined);
  assert.ok(validateTalentForm({...form,[key]:form[key]+'🌱'},'save')[key]);
});
test('zero weekly hours is different from no stated availability', () => {
  assert.equal(talentFormToFields({...minimal(),weekly_hours:''}).weekly_hours,null);
  assert.equal(talentFormToFields({...minimal(),weekly_hours:'0'}).weekly_hours,0);
  assert.equal(fieldsToTalentForm(talentFormToFields({...minimal(),weekly_hours:'0'})).weekly_hours,'0');
  for (const value of ['-1','169','1.5','abc']) assert.ok(validateTalentForm({...minimal(),weekly_hours:value},'save').weekly_hours);
});
test('links are optional but selected entries require safe absolute HTTP links', () => {
  for (const url of ['javascript:alert(1)','https:example.invalid','http://','https://example.\ninvalid']) {
    const errors = validateTalentForm({...minimal(), links:[{title:'模拟作品',url}]},'publish');
    assert.ok(Object.keys(errors).some(key=>key.startsWith('link-')),url);
  }
  assert.deepEqual(validateTalentForm({...minimal(),links:[{title:'模拟作品',url:'https://example.invalid/demo'}]},'publish'),{});
  assert.ok(validateTalentForm({...minimal(),links:Array.from({length:6},()=>({title:'模拟作品',url:'https://example.invalid/demo'}))},'save').links);
});
test('standard tag and work selections cannot duplicate or exceed limits', () => {
  const form=minimal();
  assert.ok(validateTalentForm({...form,tag_ids:[...form.tag_ids,...form.tag_ids]},'save').tag_ids);
  assert.ok(validateTalentForm({...form,file_ids:Array.from({length:6},(_,i)=>String(i))},'save').file_ids);
});
test('only explicitly selected contact values enter the payload', () => {
  const form={...minimal(), contacts:[{channel:'EMAIL' as const,value:' qa@example.invalid '}]};
  assert.deepEqual(talentFormToFields(form).contacts,[{channel:'EMAIL',value:'qa@example.invalid'}]);
  assert.ok(validateTalentForm({...form,contacts:[{channel:'WECHAT',value:'x'}]},'publish')['contact-WECHAT']);
});
