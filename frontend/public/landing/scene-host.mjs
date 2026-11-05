import {SceneSwitch} from './seal/scene-switch.mjs';

const root=document.documentElement;
const hero=document.getElementById('hero'),intro=document.getElementById('paperLayer');
const seal=document.getElementById('seal-experience'),spacer=document.getElementById('scroll-space');
const shade=document.getElementById('scene-shade'),status=document.getElementById('scene-status');
const motion=matchMedia('(prefers-reduced-motion: reduce)');
const positions={hero:0,intro:0,seal:0};
const names={hero:'智汇华师',intro:'图文介绍',seal:'聚树成印'};
let animation=null,loadedIntro=null,displayed='hero',sealModuleFailed=false;
// Scene positions are restored at the fade midpoint, never by native hash scrolling.
history.scrollRestoration='manual';
window.ScrollTrigger.clearScrollMemory('manual');

async function loadIntro(){
 if(!loadedIntro)loadedIntro=Promise.all([
  document.fonts.load('500 48px IntroSerif'),document.fonts.load('400 16px IntroSans')
 ]).catch(()=>{}).then(()=>import('./intro-animation.mjs'));
 return loadedIntro.then(module=>{if(displayed==='intro')return module.startIntroAnimation();});
}

function syncHero(){
 const visible=displayed==='hero'&&!document.hidden;
 window.dispatchEvent(new CustomEvent('campus:hero-visibility',{detail:{visible}}));
}
function show(scene,{restore=false}={}){
 const changed=displayed!==scene;
 if(changed)positions[displayed]=scrollY;
 displayed=scene;root.dataset.scene=scene;
 hero.hidden=scene!=='hero';intro.hidden=scene!=='intro';spacer.hidden=scene!=='seal';
 for(const [id,element] of [['hero',hero],['intro',intro],['seal',seal]]){
  element.inert=id!==scene;element.setAttribute('aria-hidden',String(id!==scene));
 }
 // Never expose the tree below a white scene, even while the renderer is loading.
 seal.style.visibility=scene==='seal'?'visible':'hidden';
 scrollTo({top:restore?positions[scene]:0,behavior:'instant'});
 // Existing hero, trail and carousel lifecycles keep their original implementations.
 dispatchEvent(new Event('resize'));syncHero();
 dispatchEvent(new CustomEvent('campus:scene-change',{detail:{scene,restore}}));
 status.textContent=names[scene];
}

const flow=new SceneSwitch({onChange:snapshot=>{
 root.dataset.scenePhase=snapshot.phase;
 for(const button of document.querySelectorAll('.scene-next'))button.disabled=snapshot.phase!=='idle';
 dispatchEvent(new CustomEvent('campus:scene-state',{detail:snapshot}));
}});

async function fade(to,{restore=false,interrupt=false,record=true}={}){
 const token=flow.request(to,{interrupt});
 if(interrupt){animation?.cancel();animation=null;}
 if(!token){if(flow.snapshot().phase==='idle')shade.style.opacity='0';return;}
 shade.style.background=to==='seal'?'#040a0a':'#f4f4f2';
 try{
  if(!motion.matches){
   animation=shade.animate([{opacity:Number(getComputedStyle(shade).opacity)},{opacity:1}],{duration:220,easing:'ease-in-out',fill:'forwards'});
   await animation.finished;
  }
  if(!flow.midpoint(token))return;
  shade.style.opacity='1';animation?.cancel();animation=null;
  show(to,{restore});
  if(record)history.pushState({scene:to},'',to==='hero'?location.pathname:'#'+to);
  if(to==='intro')await loadIntro();
  if(flow.snapshot().token!==token)return;
  if(!motion.matches){
   animation=shade.animate([{opacity:1},{opacity:0}],{duration:260,easing:'ease-in-out',fill:'forwards'});
   await animation.finished;
  }
  if(!flow.complete(token))return;
  shade.style.opacity='0';animation?.cancel();animation=null;
  if(to==='seal'&&sealModuleFailed)document.querySelector('#seal-module-error a')?.focus({preventScroll:true});
  if(to!=='seal')document.querySelector('#'+(to==='hero'?'hero':'paperLayer')+' .scene-next')?.focus({preventScroll:true});
 }catch(error){
  // Cancellation belongs to a newer history navigation. It must not finish this one.
  if(flow.snapshot().token===token){flow.cancel();shade.style.opacity='0';}
  if(error.name!=='AbortError')console.error(error);
 }
}

for(const button of document.querySelectorAll('.scene-next')){
 button.addEventListener('click',()=>fade(button.dataset.next));
 // Only stop input over the 48px button; keep the surrounding footer trail live.
 for(const event of ['pointermove','pointerdown','pointerup','click'])button.addEventListener(event,e=>e.stopPropagation());
}
addEventListener('popstate',()=>{
 const target=['#intro','#seal'].includes(location.hash)?location.hash.slice(1):'hero';
 void fade(target,{restore:true,interrupt:true,record:false});
});
document.addEventListener('visibilitychange',syncHero);
addEventListener('pageshow',()=>syncHero());
history.replaceState({scene:'hero'},'',location.pathname+location.search);
show('hero');

// A failed module/dependency cannot run render.mjs's own init().catch.
// Keep recovery outside that dependency graph and outside its inert UI.
const moduleFallback=document.getElementById('seal-module-error');
moduleFallback.hidden=false;
void import('./seal/render.mjs').then(()=>{moduleFallback.hidden=true;}).catch(error=>{
 sealModuleFailed=true;
 moduleFallback.querySelector('h2').textContent='校园动效未能载入';
 moduleFallback.querySelector('p').textContent='你仍可进入平台，或刷新后再试';
 if(displayed==='seal'&&flow.snapshot().phase==='idle')moduleFallback.querySelector('a').focus({preventScroll:true});
 console.error('Campus scene module unavailable',error);
});
