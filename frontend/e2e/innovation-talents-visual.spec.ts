import AxeBuilder from '@axe-core/playwright';
import { BASE_URL, expect, test, ensureStudentLogin } from './fixtures';
import { editor, prepareTalent, fillTalent, saveTalent, publishTalent } from './talent-helpers';

test.skip(process.env.CQ_E2E!=='1'||process.env.CQ_VISUAL!=='1'||process.env.CQ_E2E_FIXED_LABELS!=='1','separate Linux pinned-font R4 visual world');
async function stable(page:import('@playwright/test').Page){
  await page.addStyleTag({content:':root { --font-sans: "Noto Sans CJK SC", sans-serif; } *, *::before, *::after { animation: none !important; transition: none !important; }'});
  await page.evaluate(()=>document.fonts.ready);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
}
test('R4 visual: owner, mobile preview and public talent',async({page})=>{
  test.setTimeout(150_000);await prepareTalent(page);await fillTalent(page,'模拟人才同学');await editor(page).getByLabel('授权微信',{exact:true}).check();await editor(page).getByLabel('微信',{exact:true}).fill('demo_talent_visual');await saveTalent(page);
  await page.setViewportSize({width:1440,height:900});await stable(page);await expect(editor(page)).toHaveScreenshot('talent-owner-desktop.png',{maxDiffPixelRatio:0.01});
  await page.setViewportSize({width:390,height:844});const opener=editor(page).getByRole('button',{name:'预览开启校内展示',exact:true});await opener.click();await expect(page.getByRole('dialog')).toBeVisible();await stable(page);await expect(page.getByRole('dialog')).toHaveScreenshot('talent-preview-mobile.png',{maxDiffPixelRatio:0.01});await page.keyboard.press('Escape');await expect(opener).toBeFocused();await publishTalent(page);
  await editor(page).getByRole('link',{name:'查看校内公开档案',exact:true}).click();await expect(page.getByRole('article',{name:'校内人才详情'})).toBeVisible();await stable(page);await expect(page).toHaveScreenshot('talent-detail-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});await expect(page.getByRole('main')).not.toContainText('demo_talent_visual');
  await page.getByRole('button',{name:'查看授权联系方式',exact:true}).click();await expect(page.getByRole('region',{name:'人才授权联系'})).toContainText('demo_talent_visual');await stable(page);await expect(page.getByRole('region',{name:'人才授权联系'})).toHaveScreenshot('talent-contacts-mobile.png',{maxDiffPixelRatio:0.01});
  await page.setViewportSize({width:1440,height:900});await page.goto(`${BASE_URL}/innovation/talents`);await expect(page.getByRole('link',{name:'模拟人才同学',exact:true})).toBeVisible();await stable(page);await expect(page).toHaveScreenshot('talent-list-desktop.png',{fullPage:true,maxDiffPixelRatio:0.01});
});
test('R4 visual: empty, unavailable, loading and error',async({page})=>{
  await ensureStudentLogin(page);await page.setViewportSize({width:390,height:844});await page.goto(`${BASE_URL}/innovation/talents?q=不存在的模拟同学`);await expect(page.getByRole('region',{name:'校内人才库'})).toContainText('暂无符合条件的人才');await stable(page);await expect(page).toHaveScreenshot('talent-empty-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});
  await page.goto(`${BASE_URL}/innovation/talents/00000000-0000-4000-8000-000000000000`);await expect(page.getByRole('main').getByRole('alert')).toContainText('不存在');await stable(page);await expect(page).toHaveScreenshot('talent-unavailable-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});
  // Match the pathname: browsers may omit an empty query separator.
  const path=(url:URL)=>url.pathname==='/api/v1/ie/talents';
  let release!:()=>void;let intercepted!:()=>void;const held=new Promise<void>(resolve=>{release=resolve;});const requested=new Promise<void>(resolve=>{intercepted=resolve;});await page.route(path,async route=>{intercepted();await held;await route.continue();});
  try{await page.goto(`${BASE_URL}/innovation/talents`);await requested;await expect(page.getByRole('status',{name:'正在查找人才'})).toBeVisible();await stable(page);await expect(page).toHaveScreenshot('talent-loading-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});}finally{release();await page.unrouteAll({behavior:'wait'});}
  await expect(page.getByRole('form',{name:'人才筛选'})).toBeVisible();
  await page.route(path,route=>route.fulfill({status:503,json:{error:{code:'HTTP_ERROR',message:'模拟服务暂不可用，请稍后重试'}}}));
  const unavailable=page.waitForResponse(response=>path(new URL(response.url()))&&response.status()===503);
  await page.goto(`${BASE_URL}/innovation/talents?q=模拟错误场景`);await unavailable;
  await expect(page.getByRole('main').getByRole('alert')).toContainText('服务暂时不可用，请稍后重试');await stable(page);await expect(page).toHaveScreenshot('talent-error-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});await page.unroute(path);await page.getByRole('main').getByRole('button',{name:'重试',exact:true}).click();await expect(page.getByRole('form',{name:'人才筛选'})).toBeVisible();
});

test('R4 visual: long content and zero availability remain usable on mobile',async({page})=>{
  test.setTimeout(150_000);await prepareTalent(page);await fillTalent(page,'A'.repeat(50));await page.setViewportSize({width:390,height:844});
  await editor(page).getByLabel('自我介绍',{exact:true}).fill('模拟长资料'.repeat(100));await editor(page).getByLabel('我的优势',{exact:true}).fill('Skill'.repeat(400));await editor(page).getByLabel('每周可投入小时',{exact:true}).fill('0');await editor(page).getByLabel('当前合作状态',{exact:true}).selectOption('NOT_AVAILABLE');
  await editor(page).getByRole('button',{name:'添加作品链接',exact:true}).click();await editor(page).getByLabel('作品 1 名称',{exact:true}).fill('W'.repeat(100));await editor(page).getByLabel('作品 1 链接',{exact:true}).fill('https://example.invalid/'+'a'.repeat(1800));await saveTalent(page);await stable(page);await expect(editor(page)).toHaveScreenshot('talent-long-form-mobile.png',{maxDiffPixelRatio:0.01});
  await publishTalent(page);await editor(page).getByRole('link',{name:'查看校内公开档案',exact:true}).click();await expect(page.getByRole('article',{name:'校内人才详情'})).toContainText('暂不接洽');await expect(page.getByRole('article')).toContainText('0 小时');await stable(page);await expect(page).toHaveScreenshot('talent-long-detail-mobile.png',{fullPage:true,maxDiffPixelRatio:0.01});
});
