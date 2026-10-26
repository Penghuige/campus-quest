import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { TalentPreviewContent } from '../features/innovation/TalentPreviewContent';
import { emptyTalentForm } from '../features/innovation/talentForm';
import type { TalentFileDto } from '../features/innovation/talentApi';

const ready:TalentFileDto={id:'ready',name:'模拟有效文件.pdf',content_type:'application/pdf',size:1024,state:'READY',version:1,failure_code:null,removed_at:null,revoked_at:null,created_at:'2026-10-13T00:00:00Z'};
test('private preview discloses every unavailable selected attachment alongside valid material',()=>{
  for(const unavailable of [undefined,{...ready,id:'other',name:'模拟待检查.pdf',state:'PENDING' as const},{...ready,id:'other',name:'模拟撤回.pdf',revoked_at:'2026-10-13T01:00:00Z'},{...ready,id:'other',name:'模拟移除.pdf',removed_at:'2026-10-13T01:00:00Z'}]){
    const form={...emptyTalentForm(),file_ids:['ready','other']};
    const html=renderToStaticMarkup(createElement(TalentPreviewContent,{form,tags:[],files:unavailable?[ready,unavailable]:[ready]}));
    assert.match(html,/模拟有效文件.pdf/);
    assert.match(html,/部分所选文件当前不可用/);
    assert.doesNotMatch(html,/模拟待检查.pdf|模拟撤回.pdf|模拟移除.pdf/);
    assert.match(html,/文件安全检查不代表内容真实性或能力已核实/);
  }
});
test('private preview distinguishes empty, valid and entirely unavailable file selections',()=>{
  const render=(ids:string[])=>renderToStaticMarkup(createElement(TalentPreviewContent,{form:{...emptyTalentForm(),file_ids:ids},tags:[],files:[ready]}));
  assert.match(render([]),/未选择文件/);assert.doesNotMatch(render([]),/含作品\/证明材料/);
  assert.match(render(['ready']),/含作品\/证明材料/);assert.doesNotMatch(render(['ready']),/文件当前不可用/);
  assert.match(render(['missing']),/所选文件当前不可用/);assert.doesNotMatch(render(['missing']),/含作品\/证明材料/);
});
test('private comparison explains nonempty selections with no currently available tag',()=>{
  const html=renderToStaticMarkup(createElement(TalentPreviewContent,{form:{...emptyTalentForm(),tag_ids:['disabled']},tags:[],files:[]}));
  assert.match(html,/未匹配到可用标签/);
});

test('private preview keeps complete self description, zero hours and consent without embedding external content',()=>{
  const form={...emptyTalentForm(),name:'模拟同学',faculty:'模拟院系',grade:'2026级',introduction:'<script>private</script>',advantages:'整理资料',skill_notes:'PPT实践',cooperation_direction:'学习实验',desired_roles:'资料整理',experience:'模拟经历',weekly_hours:'0',cooperation_state:'LOOKING' as const,tag_ids:['tag'],links:[{title:'模拟作品',url:'https://example.invalid/work'}],contacts:[{channel:'WECHAT' as const,value:'demo_private'}],file_ids:['ready']};
  const html=renderToStaticMarkup(createElement(TalentPreviewContent,{form,tags:[{id:'tag',name:'PPT 制作'}],files:[ready]}));
  for(const value of ['模拟同学','模拟院系','2026级','整理资料','学习实验','资料整理','模拟经历','0 小时','PPT 制作','模拟作品','demo_private']) assert.ok(html.includes(value),value);
  assert.match(html,/&lt;script&gt;private&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>|<iframe|<a /);
});
