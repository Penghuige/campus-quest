import AxeBuilder from '@axe-core/playwright';
import { API_URL, BASE_URL, expect, test, mintToken, loginThroughUi, parseSeededAccount, staffLogin } from './fixtures';
import { TALENT_OWN, editor, result, prepareTalent, fillTalent, saveTalent, publishTalent, uploadTalent, ownRecord, PNG, ownerHeaders } from './talent-helpers';

test.skip(process.env.CQ_E2E!=='1','CQ_E2E=1 with an owned real database');

// The second seeded student's user id rides CQ_E2E_AUTHOR_SECRETS' fifth
// field (browser_world exports nickname,username,phone,email,user_id) —
// the upstream branch leaned on an unset CQ_E2E_AUTHOR_STUDENT_ID.
const AUTHOR_STUDENT_ID=(process.env.CQ_E2E_AUTHOR_SECRETS ?? '').split(',')[4];

test('R4.1: public materials filter follows publication and withdrawal without certifying talent',async({page})=>{
  test.setTimeout(240_000);await prepareTalent(page);await fillTalent(page,'模拟材料同学');await saveTalent(page);await publishTalent(page);
  const selected=await uploadTalent(page,'模拟材料标志.png');await selected.check();await saveTalent(page);
  const record=(await ownRecord(page))!;
  const publicHeaders={Authorization:`Bearer ${mintToken(AUTHOR_STUDENT_ID!)}`};
  const privateList=await page.request.get(`${API_URL}/ie/talents?q=模拟材料同学&has_public_materials=true`,{headers:publicHeaders});
  expect(privateList.status()).toBe(200);expect((await privateList.json()).total).toBe(0);
  await page.goto(`${BASE_URL}/innovation/talents?q=模拟材料同学&has_public_materials=false`);
  const filters=page.getByRole('form',{name:'人才筛选'});
  await expect(filters.getByLabel('作品/证明材料筛选',{exact:true})).toHaveValue('false');
  await expect(page.getByRole('link',{name:'模拟材料同学',exact:true})).toBeVisible();
  await expect(page.getByRole('region',{name:'校内人才库'})).not.toContainText('含作品/证明材料');
  await filters.getByLabel('作品/证明材料筛选',{exact:true}).selectOption('true');
  await filters.getByRole('button',{name:'查找人才',exact:true}).click();await expect(page).toHaveURL(/has_public_materials=true/);
  await expect(page.getByRole('region',{name:'校内人才库'})).toContainText('暂无符合条件的人才');
  await page.goto(`${BASE_URL}/profile/talent-profile`);await expect(editor(page)).toBeVisible();
  await publishTalent(page);
  await page.goto(`${BASE_URL}/innovation/talents?q=模拟材料同学&has_public_materials=true`);
  await expect(page.getByRole('region',{name:'校内人才库'})).toContainText('含作品/证明材料');
  await expect(page.getByRole('region',{name:'校内人才库'})).toContainText('文件安全检查不代表内容真实性或能力已核实');
  await page.getByRole('link',{name:'模拟材料同学',exact:true}).click();
  await expect(page.getByRole('article')).toContainText('含作品/证明材料');
  await expect(page.getByRole('button',{name:'下载 模拟材料标志.png',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'返回人才列表',exact:true}).click();await expect(page).toHaveURL(/has_public_materials=true/);
  await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  const materialSelect=page.getByLabel('作品/证明材料筛选',{exact:true});await materialSelect.focus();await expect(materialSelect).toBeFocused();await page.keyboard.press('Tab');await expect(page.getByRole('checkbox').first()).toBeFocused();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  const latest=(await ownRecord(page))!;
  const revoked=await page.request.post(`${API_URL}${TALENT_OWN}/files/${record.fields.file_ids[0]}/revoke`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:latest.version}});
  expect(revoked.status()).toBe(200);await page.reload();await expect(page.getByRole('region',{name:'校内人才库'})).toContainText('暂无符合条件的人才');
  await page.getByLabel('作品/证明材料筛选',{exact:true}).selectOption('false');await page.getByRole('button',{name:'查找人才',exact:true}).click();await expect(page.getByRole('link',{name:'模拟材料同学',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'清除筛选',exact:true}).click();await expect(page).toHaveURL(`${BASE_URL}/innovation/talents`);await expect(page.getByLabel('作品/证明材料筛选',{exact:true})).toHaveValue('');
});

test('R4 review: returning reconciles current visibility and clean fields while preserving edits',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  await editor(page).getByLabel('我的优势',{exact:true}).fill('本窗口尚未保存的优势');
  await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);
  let row=(await ownRecord(page))!;
  const published=await page.request.post(`${API_URL}${TALENT_OWN}/publish`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version}});expect(published.status()).toBe(200);row=(await published.json()).current;
  await page.goBack();await expect(editor(page)).toContainText('校内展示中');await expect(editor(page).getByRole('button',{name:'关闭校内展示',exact:true})).toBeVisible();await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('本窗口尚未保存的优势');
  await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);
  const hidden=await page.request.post(`${API_URL}${TALENT_OWN}/hide`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version}});expect(hidden.status()).toBe(200);
  await page.goBack();await expect(editor(page)).toContainText('仅自己可见');await expect(editor(page).getByRole('button',{name:'关闭校内展示',exact:true})).toHaveCount(0);await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('本窗口尚未保存的优势');await saveTalent(page);
  await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);row=(await ownRecord(page))!;
  const remote=await page.request.put(`${API_URL}${TALENT_OWN}`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version,fields:{...row.fields,introduction:'另一窗口已保存的新介绍'}}});expect(remote.status()).toBe(200);
  await page.goBack();await expect(editor(page).getByLabel('自我介绍',{exact:true})).toHaveValue('另一窗口已保存的新介绍');
  await editor(page).getByLabel('我的优势',{exact:true}).fill('本窗口第二次未保存优势');await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);row=(await ownRecord(page))!;
  const changed=await page.request.put(`${API_URL}${TALENT_OWN}`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version,fields:{...row.fields,introduction:'需要明确比较的服务器介绍'}}});expect(changed.status()).toBe(200);
  await page.goBack();await expect(page.getByRole('region',{name:'最新人才版本'})).toContainText('需要明确比较的服务器介绍');await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('本窗口第二次未保存优势');await expect(editor(page).getByRole('button',{name:'保存人才草稿',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'以最新版本继续（保留输入）',exact:true}).click();await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('本窗口第二次未保存优势');await expect(editor(page).getByRole('button',{name:'保存人才草稿',exact:true})).toBeEnabled();
  await saveTalent(page);const selected=await uploadTalent(page,'模拟会被移除.png');await selected.check();await saveTalent(page);await editor(page).getByLabel('我的优势',{exact:true}).fill('清除失效选择时保留此输入');
  await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);row=(await ownRecord(page))!;const fileId=row.fields.file_ids[0];
  const removed=await page.request.post(`${API_URL}${TALENT_OWN}/files/${fileId}/remove`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version}});expect(removed.status()).toBe(200);
  await page.goBack();await editor(page).getByRole('button',{name:'清除已移除的文件选择',exact:true}).click();await page.getByRole('button',{name:'以最新版本继续（保留输入）',exact:true}).click();await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('清除失效选择时保留此输入');await saveTalent(page);
});

test('R4 review: removed upload grant is reconciled and does not lock file selection',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const complete=(url:URL)=>url.pathname.startsWith('/api/v1/ie/me/talent-profile/files/')&&url.pathname.endsWith('/complete');
  await page.route(complete,route=>route.fulfill({status:503,json:{error:{code:'HTTP_ERROR',message:'模拟检查服务暂不可用'}}}));
  await editor(page).getByLabel('选择作品或证明文件',{exact:true}).setInputFiles({name:'模拟移除前.png',mimeType:'image/png',buffer:PNG});await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toBeVisible();await page.unroute(complete);
  const listed=await page.request.get(`${API_URL}${TALENT_OWN}/files`,{headers:ownerHeaders()});expect(listed.status()).toBe(200);const file=(await listed.json()).items[0];const row=(await ownRecord(page))!;
  const removed=await page.request.post(`${API_URL}${TALENT_OWN}/files/${file.id}/remove`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:row.version}});expect(removed.status()).toBe(200);
  await editor(page).getByLabel('我的优势',{exact:true}).fill('移除发生后仍保留的新输入');await editor(page).getByRole('button',{name:'重试原文件上传',exact:true}).click();
  await editor(page).getByRole('button',{name:'刷新文件状态',exact:true}).click();await expect(editor(page).getByLabel('选择作品或证明文件',{exact:true})).toBeEnabled();await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toHaveCount(0);await expect(editor(page).getByLabel('我的优势',{exact:true})).toHaveValue('移除发生后仍保留的新输入');await uploadTalent(page,'模拟重新选择.png');
});

test('R4 review: existing uploaded bytes can be checked without replaying an expired PUT',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const complete=(url:URL)=>url.pathname.startsWith('/api/v1/ie/me/talent-profile/files/')&&url.pathname.endsWith('/complete');await page.route(complete,route=>route.fulfill({status:503,json:{error:{code:'HTTP_ERROR',message:'模拟丢失检查回执'}}}));
  await editor(page).getByLabel('选择作品或证明文件',{exact:true}).setInputFiles({name:'模拟已上传.png',mimeType:'image/png',buffer:PNG});await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toBeVisible();await page.unroute(complete);
  let oldPuts=0;await page.route('https://127.0.0.1:9002/**',route=>{if(route.request().method()==='PUT'){oldPuts++;return route.fulfill({status:403,body:'ExpiredToken'});}return route.continue();});
  await editor(page).getByRole('button',{name:'重试原文件上传',exact:true}).click();await expect(page.getByRole('status',{name:'人才文件操作结果'})).toContainText('检查通过');await expect(editor(page).getByLabel('选择作品或证明文件',{exact:true})).toBeEnabled();expect(oldPuts).toBe(0);
});

test('R4: a rejected upload intent allows selecting a corrected file',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const intent=(url:URL)=>url.pathname==='/api/v1/ie/me/talent-profile/files';
  await page.route(intent,route=>route.request().method()==='POST'?route.fulfill({status:422,json:{error:{code:'VALIDATION_ERROR',message:'请选择名称不超过 200 字的文件'}}}):route.continue());
  const chooser=editor(page).getByLabel('选择作品或证明文件',{exact:true});
  await chooser.setInputFiles({name:'模拟更正前.png',mimeType:'image/png',buffer:PNG});
  await expect(editor(page).getByRole('alert')).toContainText('请选择名称不超过 200 字的文件');
  await expect(chooser).toBeEnabled();
  await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toHaveCount(0);
  await page.unroute(intent);await uploadTalent(page,'模拟更正后.png');
});

test('R4: new_student_without_evidence_can_enable',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  await editor(page).getByRole('button',{name:'预览开启校内展示',exact:true}).click();
  await expect(page.getByRole('dialog')).toContainText('未授权联系方式；仍可以公开档案');
  await page.keyboard.press('Escape');await expect(editor(page).getByRole('button',{name:'预览开启校内展示',exact:true})).toBeFocused();
  await publishTalent(page);await expect(result(page)).toBeFocused();
  await editor(page).getByRole('link',{name:'查看校内公开档案',exact:true}).click();
  await expect(page.getByRole('article',{name:'校内人才详情'})).toContainText('暂无公开文件');
  await expect(page.getByRole('region',{name:'人才授权联系'})).toContainText('暂未授权联系方式');
  await expect(page.getByRole('button',{name:'查看授权联系方式',exact:true})).toHaveCount(0);
});

test('R4: selected_ready_file_and_contact_are_visible_only_after_confirmation',async({page,browser})=>{
  test.setTimeout(240_000);await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const selected=await uploadTalent(page);await expect(selected).not.toBeChecked();await selected.check();
  await editor(page).getByLabel('授权微信',{exact:true}).check();await editor(page).getByLabel('微信',{exact:true}).fill('demo_r4_talent');await saveTalent(page);
  const record=(await ownRecord(page))!;const headers={Authorization:`Bearer ${mintToken(AUTHOR_STUDENT_ID!)}`};
  expect((await page.request.get(`${API_URL}/ie/talents/${record.id}`,{headers})).status()).toBe(404);
  await publishTalent(page);
  const context=await browser.newContext({ignoreHTTPSErrors:true});const viewer=await context.newPage();
  try{
    await loginThroughUi(viewer,parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT,'CQ_E2E_AUTHOR_STUDENT'));
    let contacts=0;let downloads=0;viewer.on('request',r=>{const path=new URL(r.url()).pathname;if(path.endsWith('/contacts'))contacts++;if(path.endsWith('/content'))downloads++;});
    await viewer.goto(`${BASE_URL}/innovation/talents?q=模拟新同学&tag_ids=00000000-0000-4000-8000-000000000003`);
    await viewer.getByRole('link',{name:'模拟新同学',exact:true}).click();
    await expect(viewer.getByRole('article',{name:'校内人才详情'})).toBeVisible();expect(contacts).toBe(0);expect(downloads).toBe(0);
    await expect(viewer.getByRole('main')).not.toContainText('demo_r4_talent');
    await viewer.getByRole('button',{name:'查看授权联系方式',exact:true}).click();
    await expect(viewer.getByRole('region',{name:'人才授权联系'})).toContainText('demo_r4_talent');
    await expect(viewer.getByRole('status',{name:'人才联系读取结果'})).not.toContainText('demo_r4_talent');
    const download=viewer.waitForEvent('download');await viewer.getByRole('button',{name:'下载 模拟作品.png',exact:true}).click();
    const file=await download;expect(file.suggestedFilename()).toBe('模拟作品.png');const stream=await file.createReadStream();const chunks:Buffer[]=[];for await(const chunk of stream!)chunks.push(Buffer.from(chunk));expect(Buffer.concat(chunks)).toEqual(PNG);
    await viewer.getByRole('link',{name:'返回人才列表',exact:true}).click();await expect(viewer).toHaveURL(/q=.*&tag_ids=/);
    await editor(page).getByLabel('自我介绍',{exact:true}).fill('PRIVATE_R4_DRAFT');await saveTalent(page);await viewer.goto(`${BASE_URL}/innovation/talents/${record.id}`);await expect(viewer.getByRole('main')).not.toContainText('PRIVATE_R4_DRAFT');
    await editor(page).getByRole('button',{name:'立即撤回微信授权',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'确认撤回授权',exact:true}).click();await expect(result(page)).toContainText('授权已撤回');
    await viewer.reload();await expect(viewer.getByRole('region',{name:'人才授权联系'})).toContainText('暂未授权');await expect(viewer.getByRole('article')).not.toContainText('PRIVATE_R4_DRAFT');
  }finally{await context.close();}
});

test('R4: close_denies_old_profile_contact_and_file_links',async({page})=>{
  test.setTimeout(240_000);await prepareTalent(page);await fillTalent(page);await saveTalent(page);await(await uploadTalent(page)).check();await editor(page).getByLabel('授权微信',{exact:true}).check();await editor(page).getByLabel('微信',{exact:true}).fill('demo_r4_close');await saveTalent(page);await publishTalent(page);
  const old=(await ownRecord(page))!;const headers={Authorization:`Bearer ${mintToken(AUTHOR_STUDENT_ID!)}`};
  const viewer=await page.context().newPage();try{
    await viewer.goto(`${BASE_URL}/innovation/talents/${old.id}`);await expect(viewer.getByRole('article')).toBeVisible();
    await editor(page).getByRole('button',{name:'关闭校内展示',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'确认关闭',exact:true}).click();await expect(result(page)).toContainText('已关闭');
    await viewer.getByRole('button',{name:'查看授权联系方式',exact:true}).click();await expect(viewer.getByRole('main').getByRole('alert')).toContainText('停止展示');
    expect((await page.request.get(`${API_URL}/ie/talents/${old.id}`,{headers})).status()).toBe(404);
    expect((await page.request.post(`${API_URL}/ie/talents/${old.id}/contacts`,{headers,data:{expected_public_version:old.public_version}})).status()).toBe(404);
    expect((await page.request.get(`${API_URL}/ie/talents/${old.id}/files/${old.public_file_ids[0]}/content?expected_public_version=${old.public_version}`,{headers})).status()).toBe(404);
    expect((await page.request.get(`${API_URL}${TALENT_OWN}/files/${old.public_file_ids[0]}/content`,{headers:ownerHeaders()})).status()).toBe(200);
  }finally{await viewer.close();}
});

test('R4: draft_recovery_does_not_cross_accounts',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await editor(page).getByLabel('自我介绍',{exact:true}).fill('R4_ACCOUNT_PRIVATE_SENTINEL');
  await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);await page.goBack();await expect(editor(page).getByLabel('自我介绍',{exact:true})).toHaveValue('R4_ACCOUNT_PRIVATE_SENTINEL');
  await page.locator('a[href="/innovation"]:visible').first().click();await page.goto(`${BASE_URL}/logout`);await page.getByRole('button',{name:'确认退出登录',exact:true}).click();await expect(page).toHaveURL(/\/login/);
  await loginThroughUi(page,parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT,'CQ_E2E_AUTHOR_STUDENT'));await page.goto(`${BASE_URL}/profile/talent-profile`);await expect(editor(page)).toBeVisible();await expect(editor(page).getByLabel('自我介绍',{exact:true})).not.toHaveValue('R4_ACCOUNT_PRIVATE_SENTINEL');
});

test('R4: late_save_preserves_newer_edit',async({page})=>{
  await prepareTalent(page);await fillTalent(page);let release!:()=>void;const held=new Promise<void>(resolve=>{release=resolve;});let acknowledged=false;const bodies:string[]=[];
  const path='**/api/v1/ie/me/talent-profile';await page.route(path,async route=>{if(route.request().method()!=='PUT')return route.continue();bodies.push(route.request().postData()!);const original=bodies.length===1;const response=await route.fetch();expect(response.status()).toBe(200);if(original){acknowledged=true;await held;}await route.fulfill({response});});
  try{
    await editor(page).getByRole('button',{name:'保存人才草稿',exact:true}).click();await expect.poll(()=>acknowledged).toBe(true);
    await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);await page.goBack();
    await editor(page).getByLabel('自我介绍',{exact:true}).fill('迟到回执不能覆盖的新输入');await editor(page).getByRole('button',{name:'重试原请求',exact:true}).click();await expect(result(page)).toContainText('原请求已处理');expect(bodies).toHaveLength(2);expect(bodies[1]).toBe(bodies[0]);
    release();await page.locator('a[href="/innovation"]:visible').first().click();await expect(page).toHaveURL(/\/innovation$/);await page.goBack();await expect(editor(page).getByLabel('自我介绍',{exact:true})).toHaveValue('迟到回执不能覆盖的新输入');
  }finally{release();await page.unroute(path);}
});

test('R4: keyboard_error_preview_return_focus',async({page})=>{
  await prepareTalent(page);await saveTalent(page);await editor(page).getByRole('button',{name:'预览开启校内展示',exact:true}).press('Enter');await expect(editor(page).getByLabel('展示姓名',{exact:true})).toBeFocused();await expect(editor(page).getByLabel('展示姓名',{exact:true})).toHaveAttribute('aria-invalid','true');
  await fillTalent(page);await saveTalent(page);const opener=editor(page).getByRole('button',{name:'预览开启校内展示',exact:true});await opener.press('Enter');await expect(page.getByRole('dialog')).toBeVisible();await expect(page.getByRole('dialog')).toContainText('模拟新同学');expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);await page.keyboard.press('Escape');await expect(opener).toBeFocused();expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
});

test('R4: conflicting_save_preserves_input_and_explicitly_rebases',async({page})=>{
  await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const record=(await ownRecord(page))!;
  await editor(page).getByLabel('自我介绍',{exact:true}).fill('窗口里的新介绍');
  const concurrent=await page.request.put(`${API_URL}${TALENT_OWN}`,{headers:ownerHeaders(),data:{request_id:crypto.randomUUID(),version:record.version,fields:{...record.fields,advantages:'另一窗口保存的优势'}}});expect(concurrent.status()).toBe(200);
  await editor(page).getByRole('button',{name:'保存人才草稿',exact:true}).click();
  await expect(editor(page).getByRole('alert')).toContainText('档案已变化');
  await expect(editor(page).getByLabel('自我介绍',{exact:true})).toHaveValue('窗口里的新介绍');
  await editor(page).getByRole('button',{name:'读取最新版本（保留输入）',exact:true}).click();
  const latest=page.getByRole('region',{name:'最新人才版本'});await expect(latest).toContainText('另一窗口保存的优势');
  await latest.getByRole('button',{name:'以最新版本继续（保留输入）',exact:true}).click();
  await expect(editor(page).getByLabel('自我介绍',{exact:true})).toHaveValue('窗口里的新介绍');await saveTalent(page);
  expect((await ownRecord(page))!.fields.introduction).toBe('窗口里的新介绍');
});

test('R4: active_staff_can_browse_but_not_edit_student_talent',async({page})=>{
  await staffLogin(page,process.env.CQ_E2E_STAFF!,process.env.CQ_E2E_STAFF_TOTP_SECRET!);
  await page.goto(`${BASE_URL}/innovation/talents`);await expect(page.getByRole('form',{name:'人才筛选'})).toBeVisible();
  await expect(page.getByRole('link',{name:'建立或管理我的人才档案',exact:true})).toHaveCount(0);
  // Main's role-blind (student) group routes a staff session away from
  // the profile pages to the staff workspace: the editor being absent
  // (count 0 above) plus the redirect off /profile/talent-profile IS
  // the not-editable guarantee here — the upstream's generic 学生|教师
  // wording assumed the fork's pre-redirect shell.
  await page.goto(`${BASE_URL}/profile/talent-profile`);await expect(page.getByRole('region',{name:'本人人才档案编辑器'})).toHaveCount(0);
  await expect(page).not.toHaveURL(/talent-profile/);
});

test('R4: scan_retry_and_replace_five_files',async({page})=>{
  test.setTimeout(360_000);await prepareTalent(page);await fillTalent(page);await saveTalent(page);
  const complete='**/api/v1/ie/me/talent-profile/files/*/complete';let blocked=true;await page.route(complete,async route=>{if(blocked)await route.fulfill({status:503,json:{error:{code:'HTTP_ERROR',message:'模拟检查服务暂不可用'}}});else await route.continue();});
  await editor(page).getByLabel('选择作品或证明文件',{exact:true}).setInputFiles({name:'旧作品1.png',mimeType:'image/png',buffer:PNG});await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toBeVisible();
  await editor(page).getByRole('button',{name:'刷新文件状态',exact:true}).click();await editor(page).getByRole('button',{name:'移除 旧作品1.png',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();await expect(editor(page).getByRole('button',{name:'重试原文件上传',exact:true})).toBeVisible();
  blocked=false;await editor(page).getByRole('button',{name:'重试原文件上传',exact:true}).click();await expect(page.getByRole('status',{name:'人才文件操作结果'})).toContainText('文件检查通过',{timeout:150_000});await editor(page).getByRole('checkbox',{name:'公开 旧作品1.png',exact:true}).check();await page.unroute(complete);
  for(let i=2;i<=5;i++)await(await uploadTalent(page,`旧作品${i}.png`)).check();await saveTalent(page);await publishTalent(page);
  for(let i=1;i<=5;i++)await uploadTalent(page,`新作品${i}.png`);
  await expect(editor(page).getByLabel('选择作品或证明文件',{exact:true})).toBeDisabled();
  for(let i=1;i<=5;i++)await editor(page).getByRole('checkbox',{name:`公开 旧作品${i}.png`,exact:true}).uncheck();
  for(let i=1;i<=5;i++)await editor(page).getByRole('checkbox',{name:`公开 新作品${i}.png`,exact:true}).check();await saveTalent(page);await publishTalent(page);
  for(let i=1;i<=5;i++){await editor(page).getByRole('button',{name:`移除 旧作品${i}.png`,exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'确认移除',exact:true}).click();await expect(editor(page).getByRole('button',{name:`移除 旧作品${i}.png`,exact:true})).toHaveCount(0);}
  await expect(editor(page).getByLabel('选择作品或证明文件',{exact:true})).toBeEnabled();
  await editor(page).getByRole('link',{name:'查看校内公开档案',exact:true}).click();await expect(page.getByRole('region',{name:'公开作品文件'}).getByRole('button')).toHaveCount(5);await expect(page.getByRole('region',{name:'公开作品文件'})).not.toContainText('旧作品');
});
