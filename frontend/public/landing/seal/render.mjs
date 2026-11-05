import {navigateToLogin} from '../navigation.mjs';
import {mountOrbitUI} from './orbit-ui.mjs';
import {resizedJourneyY} from './orbit-presentation.mjs';
import * as THREE from './vendor/three/build/three.module.js';
import {createStageScene} from './stage-scene.mjs';
import {LandingJourneyDriver as JourneyDriver} from './landing-driver.mjs';
import {fitStageLayout} from './framing.mjs';
import {sampleTree,treeLayout} from './tree-environment.mjs';
import {scrollBounds,journeyStages} from './scroll-presentation.mjs';
import {finalPresentation} from './presentation.mjs';
import {WaterField} from './runtime/interactive-ripple-2026-11-01/water-field.mjs';
import {TerminalWater} from './terminal-water.mjs';
import {JourneyWaves} from './journey-waves.mjs';
const $=s=>document.querySelector(s);
const sealRoot=$('#seal-experience'),sealUI=$('#seal-ui');
const orbitUI=mountOrbitUI($('#seal-orbit'));
const getOffset=()=>document.documentElement.dataset.scene==='seal'?0:scrollY;
let hostVisible=false;
function syncHost(){
 hostVisible=document.documentElement.dataset.scene==='seal';
 sealRoot.style.visibility=hostVisible?'visible':'hidden';
 const ready=hostVisible&&document.documentElement.dataset.scenePhase==='idle';
 sealUI.dataset.ready=String(ready);sealUI.inert=!ready;sealUI.setAttribute('aria-hidden',String(!ready));
 orbitUI.setHost({active:hostVisible,ready});
}
syncHost();
document.documentElement.dataset.inspect='false';
const ui=Object.fromEntries(['scene','stamp','stamp-image','hint','feedback','state','water-state','play','reset','pause','seek','time','handoff','journey','journey-time'].map(id=>[id,$('#'+id)]));
const gsap=window.gsap,ScrollTrigger=window.ScrollTrigger,media=matchMedia('(prefers-reduced-motion: reduce)');
let resizeAnchor=null;
const time={value:0},clock={t:0},resolution={value:new THREE.Vector2(1,1)};
let timeline=null,driver=null,stage=null,renderer=null,layout=null,ticking=false,showDOM=true,refreshing=false,wasPlaying=false,lastState='';
let water=null,waveJourney=null,waveField=null,imprintTexture=null,scrollTrigger=null,skipDelta=true,waveClicks=0,lastWaterStatus=-1,waveFailure='',failed=false,lastWaveToken=null;
function draw(){
 if(stage){stage.updateClock();renderer.render(stage.scene,stage.camera);}
 const now=water?.clock.time??0;
 if(Math.abs(now-lastWaterStatus)>.18||lastWaterStatus<0){
   ui['water-state'].textContent=water?.active?`${water.staticMode?'静态水纹':'水纹'} ${now.toFixed(1)} s · 中心每 1.5 s · 点击 ${waveClicks}`:waveFailure?'水纹暂不可用 · 保留静态纸面':'';
   lastWaterStatus=now;
 }
}
function tick(seconds,deltaMs){time.value=seconds;const dt=skipDelta?0:Math.min(.05,deltaMs/1000);skipDelta=false;driver.tick(dt);waveJourney?.tick(dt);draw();}
function setTicking(active){
 active=active&&hostVisible&&!document.hidden&&!failed;
 if(active===ticking)return;ticking=active;
 if(active){skipDelta=true;gsap.ticker.add(tick);}else gsap.ticker.remove(tick);
}
function placeButton(r){
 Object.assign(ui.stamp.style,{left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});
 ui.feedback.style.top=(r.top+r.height+26)+'px';
}
function placeStaticButton(){
 const side=Math.min(258,innerWidth*.46,innerHeight*.28);
 const top=Math.min(innerHeight*.8-side/2,innerHeight-48-side);
 $('#seal-orbit').style.setProperty('--static-list-height',Math.max(64,top-32)+'px');
 placeButton({left:(innerWidth-side)/2,top,width:side,height:side});
}
function fallback(message){
 orbitUI.update({state:'STATIC',width:innerWidth,height:innerHeight,reducedMotion:true,fallback:true});
 failed=true;setTicking(false);timeline?.kill();scrollTrigger?.kill();
 placeStaticButton();
 ui.scene.style.visibility='hidden';sealRoot.style.background='#F5F3EC';sealRoot.classList.add('paper');
 const hasInk=Boolean(imprintTexture);
 ui.stamp.hidden=!hasInk;ui.stamp.disabled=!hasInk;ui.feedback.hidden=false;ui.feedback.textContent=hasInk?message:'印面未载入。';if(!hasInk){const login=document.createElement('a');login.href=new URL('../../login',import.meta.url).href;login.target='_top';login.textContent=' 前往登录';ui.feedback.append(login);}
 ui.state.textContent='STATIC · '+(hasInk?'静态入口':'印面素材未就绪');
 for(const k of ['play','reset','pause','seek','journey','handoff'])ui[k].disabled=true;
 $('#static-mode').checked=true;$('#static-mode').disabled=true;
}
ui.stamp.addEventListener('click',navigateToLogin);
function onFrame(f){
 syncHost();
 if(failed)return;
 orbitUI.update({state:f.state,screenProgress:f.screenProgress,width:layout.viewport.width,height:layout.viewport.height,reducedMotion:media.matches||$('#static-mode').checked});
 const priorState=lastState;
 if(f.token!==lastWaveToken){waveClicks=0;lastWaterStatus=-1;lastWaveToken=f.token;}
 waveJourney?.frame(f);
 stage.applyFrame(f);
 const terminal=f.state==='DONE'||f.state==='STATIC',changed=f.state!==lastState;
 const {useDOM,allowComparison,animatePaper}=terminal?finalPresentation(f.state,showDOM):{useDOM:false,allowComparison:false,animatePaper:false};
 if(changed){ui.feedback.hidden=true;if(!terminal)showDOM=true;lastState=f.state;}
 sealRoot.classList.toggle('paper',terminal);sealRoot.classList.toggle('stamping',f.state==='AUTO');sealRoot.dataset.phase=f.phase;
 sealRoot.style.background=terminal?'#F5F3EC':'#040a0a';
 stage.setInkPresentation(useDOM);
 ui.scene.style.visibility='visible';ui.stamp.hidden=!useDOM;ui.stamp.disabled=!useDOM;
 if(!['AUTO','DONE','STATIC'].includes(f.state)){waveClicks=0;lastWaterStatus=-1;}
 if(priorState!==f.state)lastWaterStatus=-1;
 if(f.state==='STATIC')placeStaticButton();else if(terminal)placeButton(stage.finalRect(layout.viewport));
 ui.pause.disabled=f.state!=='AUTO';ui.seek.disabled=f.state!=='AUTO';ui.handoff.disabled=!allowComparison;
 ui.handoff.textContent=useDOM?'比较：当前 DOM':'比较：当前 Canvas';
 ui.state.textContent=f.state+' · '+f.phase+' · 轮次 '+f.token;
 const actual=driver?.snapshot().actualY??scrollY;
 ui.journey.max=String(journeyStages.contact);ui.journey.value=String(Math.min(journeyStages.contact,actual/layout.viewport.height));ui['journey-time'].value=(actual/layout.viewport.height).toFixed(2)+' 屏';
 ui.seek.value=String(f.state==='AUTO'?f.autoTime:terminal?1.8:0);ui.time.value=(f.state==='AUTO'?f.autoTime:terminal?1.8:0).toFixed(2)+' s';
 if(terminal&&innerWidth<700&&changed)$('#tools').open=false;
 // DOM handoff changes only the ink; the same Canvas keeps the paper alive.
 setTicking(!terminal||Boolean(animatePaper&&water));if(!ticking)draw();
}
function getLayout(count){
 const r=ui.scene.getBoundingClientRect(),aspect=r.width/r.height;
 $('#scroll-space').style.height=scrollBounds(r.height).pageHeight+'px';
 renderer.setPixelRatio(Math.min(devicePixelRatio,1.75));renderer.setSize(r.width,r.height,false);renderer.getDrawingBufferSize(resolution.value);
 water?.resize(r.width,r.height);
 const bounds=new THREE.Box3().setFromBufferAttribute(new THREE.BufferAttribute(sampleTree(count,aspect),3));
 const next=fitStageLayout({cssWidth:r.width,cssHeight:r.height,waterY:treeLayout(aspect).waterY,sourceBounds:bounds});
 next.viewport.left=r.left;next.viewport.top=r.top;return next;
}
async function init(){
 const imprintURL=new URL('./assets/imprint.png',import.meta.url).href;
 imprintTexture=await new THREE.TextureLoader().loadAsync(imprintURL);
 ui['stamp-image'].src=imprintURL;await ui['stamp-image'].decode();
 renderer=new THREE.WebGLRenderer({canvas:ui.scene,antialias:true,powerPreference:'high-performance'});
 renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.NoToneMapping;renderer.setClearColor('#040a0a',1);
 const count=innerWidth<700?16000:28000;
 layout=getLayout(count);
 try{
   waveField=new WaterField(ui.scene,{renderer});water=new TerminalWater(ui.scene,{field:waveField});
   water.resize(layout.viewport.width,layout.viewport.height);waveJourney=new JourneyWaves(water);
 }catch(error){waveField?.dispose();waveField=null;water=null;waveJourney=null;waveFailure=error.message;console.warn('水纹不可用，保留原场景与静态纸面。',error);}
 stage=createStageScene({texture:imprintTexture,layout,resolution,time,count,waves:waveField?.material.uniforms});stage.setPixelRatio(Math.min(devicePixelRatio,1.75));
 driver=new JourneyDriver({layout,onFrame,getOffset,
   onAutoStart(token){timeline?.kill();clock.t=0;ui.pause.textContent='暂停';timeline=gsap.to(clock,{t:1.8,duration:1.8,ease:'none',onUpdate:()=>driver.auto(clock.t,token),onComplete:()=>driver.complete(token)});},
   onAutoCancel(){timeline?.kill();timeline=null;clock.t=0;}
 });
 gsap.registerPlugin(ScrollTrigger);
 scrollTrigger=ScrollTrigger.create({trigger:document.documentElement,start:0,end:'max',
   onUpdate:()=>{if(!refreshing)driver.scroll(scrollY);},
   onRefreshInit:()=>{resizeAnchor=driver?{...driver.snapshot(),previousHeight:layout.viewport.height,active:hostVisible&&document.documentElement.dataset.scenePhase==='idle'}:null;refreshing=true;$('#scroll-space').style.height=scrollBounds(ui.scene.getBoundingClientRect().height).pageHeight+'px';},
   onRefresh:()=>{layout=getLayout(count);stage.updateLayout(layout);stage.setPixelRatio(Math.min(devicePixelRatio,1.75));if(resizeAnchor?.active){
     const y=resizedJourneyY({...resizeAnchor,nextHeight:layout.viewport.height});
     if(Math.abs(y-resizeAnchor.actualY)>.1)scrollTo({top:driver.documentY(y),behavior:'instant'});
   }
   driver.refresh(layout,scrollY);refreshing=false;}
 });
 driver.scroll(scrollY,{snap:true});
 const updateMotion=()=>driver.setReducedMotion(media.matches||$('#static-mode').checked,scrollY);
 if(new URLSearchParams(location.search).get('static')==='1')$('#static-mode').checked=true;
 media.addEventListener('change',updateMotion);$('#static-mode').addEventListener('change',updateMotion);updateMotion();
 ui.journey.addEventListener('input',()=>{
   if(driver.snapshot().state==='STATIC')return;
   const v=Number(ui.journey.value),h=layout.viewport.height;
   scrollTo({top:driver.documentY(v>=journeyStages.contact?scrollBounds(h).contact+1:v*h),behavior:'instant'});driver.scroll(scrollY,{snap:true});
 });
 ui.play.addEventListener('click',()=>{
   if(driver.snapshot().state==='STATIC')return;
   driver.scroll(0,{snap:true});scrollTo({top:driver.documentY(scrollBounds(layout.viewport.height).contact+1),behavior:'instant'});driver.scroll(scrollY,{snap:true});
 });
 ui.reset.addEventListener('click',()=>{scrollTo({top:driver.documentY(0),behavior:'instant'});driver.scroll(scrollY,{snap:true});});
 ui.pause.addEventListener('click',()=>{if(!timeline||driver.snapshot().state!=='AUTO')return;timeline.paused(!timeline.paused());ui.pause.textContent=timeline.paused()?'继续':'暂停';lastWaterStatus=-1;draw();});
 ui.seek.addEventListener('input',()=>{
   if(!timeline||driver.snapshot().state!=='AUTO')return;
   const t=Number(ui.seek.value),token=driver.snapshot().token;
   timeline.pause().time(t,true);clock.t=t;driver.auto(t,token);ui.pause.textContent='继续';if(t>=1.8)driver.complete(token);
 });
 ui.handoff.addEventListener('click',()=>{showDOM=!showDOM;driver.present();});
 document.addEventListener('pointerdown',event=>{
   if(failed||sealUI.inert||event.button!==0||event.target.closest('#tools'))return;
   if(waveJourney?.pointer(event.clientX,event.clientY,ui.scene.getBoundingClientRect())){waveClicks++;lastWaterStatus=-1;}
 });
 document.addEventListener('visibilitychange',()=>{
 syncHost();
   if(document.hidden){wasPlaying=Boolean(timeline&&!timeline.paused()&&driver.snapshot().state==='AUTO');timeline?.pause();setTicking(false);}
   else if(!failed){driver.scroll(scrollY);if(wasPlaying&&driver.snapshot().state==='AUTO')timeline?.resume();wasPlaying=false;const s=driver.snapshot().state;setTicking(['BEFORE','APPROACH','AUTO'].includes(s)||(s==='DONE'&&Boolean(water)));}
 });
 ui.scene.addEventListener('webglcontextlost',event=>{event.preventDefault();fallback('动态场景暂不可用；保留静态印面入口。');});
 addEventListener('pagehide',event=>{setTicking(false);if(!event.persisted){orbitUI.dispose();timeline?.kill();scrollTrigger?.kill();water?.dispose();imprintTexture?.dispose();renderer?.dispose();}});
 addEventListener('pageshow',event=>{if(event.persisted&&!failed){driver.scroll(scrollY);const s=driver.snapshot().state;setTicking(s!=='STATIC'&&(s!=='DONE'||Boolean(water)));}});
}
addEventListener('scroll',()=>{if(failed)syncHost();},{passive:true});
for(const event of ['resize','pageshow'])addEventListener(event,()=>{if(failed){syncHost();placeStaticButton();}});
addEventListener('campus:scene-state',()=>{syncHost();});
addEventListener('campus:scene-change',()=>{
 syncHost();
 if(driver&&!failed){scrollTrigger?.refresh();driver.scroll(scrollY,{snap:true});}
 if(!hostVisible)setTicking(false);
});
init().catch(e=>{console.error(e);fallback('动态页面加载失败；当前提供静态点击入口。');});
