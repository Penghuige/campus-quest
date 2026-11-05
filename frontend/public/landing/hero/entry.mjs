import {createHeroController} from './controller.mjs';
import {createInkRenderer} from './renderer.mjs';
import {resolveLoginTarget} from './rules.mjs';

export function mountHero(root,{loginUrl=null}={}){
  const image=root.querySelector('.campus-painting'),canvas=root.querySelector('.ink-reveal'),feedback=root.querySelector('[role=status]');
  const media={hover:matchMedia('(hover:hover)'),fine:matchMedia('(pointer:fine)'),reduced:matchMedia('(prefers-reduced-motion:reduce)')};
  const cleanups=[];let disposed=false,visible=true,glAvailable=true,dprMedia;
  let imageStatus=image.complete?(image.naturalWidth?'ready':'error'):'loading';
  const listen=(target,type,fn)=>{target.addEventListener(type,fn);cleanups.push(()=>target.removeEventListener(type,fn));};
  const controller=createHeroController({createRenderer:()=>createInkRenderer({canvas,image,onContextLost:()=>{glAvailable=false;update();}}),requestFrame:cb=>requestAnimationFrame(cb),cancelFrame:id=>cancelAnimationFrame(id),now:()=>performance.now(),onMode:mode=>{root.dataset.mode=mode;}});
  const oldEntry=root.querySelector('.direct-entry'),target=resolveLoginTarget(loginUrl,location.href);
  const entry=document.createElement(target?'a':'button');entry.className=oldEntry.className;entry.innerHTML=oldEntry.innerHTML;
  if(target){entry.href=target;entry.target='_top';}else{entry.type='button';listen(entry,'click',()=>{feedback.textContent='登录入口暂时不可用，请刷新重试';});}oldEntry.replaceWith(entry);
  function update(){if(disposed)return;const rect=root.getBoundingClientRect(),html=document.documentElement;
    controller.update({width:rect.width,height:rect.height,dpr:devicePixelRatio||1,canHover:media.hover.matches,finePointer:media.fine.matches,reducedMotion:media.reduced.matches,webglAvailable:glAvailable,imageStatus,active:visible&&!root.hidden&&(!html.dataset.scene||html.dataset.scene==='hero')&&(!html.dataset.scenePhase||html.dataset.scenePhase==='idle'),hidden:document.hidden});
  }
  function bindDpr(){dprMedia?.removeEventListener('change',onDpr);dprMedia=matchMedia(`(resolution:${devicePixelRatio}dppx)`);dprMedia.addEventListener('change',onDpr);}
  function onDpr(){bindDpr();update();}
  function dispose(){if(disposed)return;disposed=true;controller.dispose();dprMedia?.removeEventListener('change',onDpr);cleanups.splice(0).forEach(fn=>fn());observer.disconnect();}
  listen(image,'load',()=>{imageStatus='ready';update();});listen(image,'error',()=>{imageStatus='error';update();});
  listen(window,'resize',update);for(const m of Object.values(media))listen(m,'change',update);bindDpr();
  listen(document,'visibilitychange',update);listen(window,'pagehide',e=>{if(e.persisted)controller.pageHide(true);else dispose();});listen(window,'pageshow',()=>{controller.pageShow();update();});
  listen(window,'campus:hero-visibility',e=>{visible=e.detail.visible;update();});
  listen(window,'campus:scene-state',update);listen(window,'campus:scene-change',update);
  const observer=new MutationObserver(update);observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-scene','data-scene-phase']});
  listen(root,'pointermove',event=>{if(event.pointerType!=='mouse'||event.target.closest('a,button,input,select,textarea,[role=button]')){controller.pointer(null);return;}const rect=root.getBoundingClientRect();controller.pointer({x:event.clientX-rect.left,y:event.clientY-rect.top});});
  listen(root,'pointerleave',()=>controller.pointer(null));
  update();return {dispose,snapshot:()=>controller.snapshot()};
}
