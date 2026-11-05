import {orbitCardsFrame,orbitMode} from './orbit-presentation.mjs';

/** Owns only the text layer; the existing host owns scroll, frames and clocks. */
export function mountOrbitUI(root){
 const cards=[...root.querySelectorAll('.orbit-card')];
 const title=root.querySelector('.orbit-title'),reading=root.querySelector('.orbit-reading');
 let host={active:false,ready:false},last={state:'BEFORE',screenProgress:0,width:innerWidth,height:innerHeight,reducedMotion:false};
 let fontsReady=false,entered=false,disposed=false;
 const fonts=Promise.allSettled([
  document.fonts.load('500 24px OrbitSerif',reading.textContent),
  document.fonts.load('400 12px OrbitSans',reading.textContent),
 ]);
 fonts.then(()=>{if(!disposed){fontsReady=true;render();}});

 function render(){
  if(disposed)return;
  const mode=orbitMode({...last,...host});
  root.dataset.mode=mode;root.hidden=mode==='hidden';
  root.setAttribute('aria-hidden',String(mode==='hidden'));
  reading.tabIndex=mode==='static'?0:-1;
  root.dataset.paused=String(document.hidden);
  if(mode!=='animated')return;
  const f=orbitCardsFrame(last.screenProgress??0,{width:last.width,height:last.height});
  root.dataset.layout=f.mode;
  if(fontsReady&&!entered&&(last.screenProgress??0)<.72){entered=true;root.dataset.entered='true';}
  title.style.opacity=String(f.title.opacity);title.style.transform=`translate(-50%,${f.title.y}px)`;
  for(const c of f.cards){
   const element=cards[c.index];
   element.style.opacity=String(c.opacity);element.style.zIndex=String(c.zIndex);
   element.style.transform=`translate3d(${c.x}px,${c.y}px,0) translate(-50%,-50%) rotateY(${c.yaw}deg) scale(${c.scale})`;
   element.dataset.primary=String(c.primary);
  }
 }
 return {
  setHost(next){
   if(host.active&&!next.active){entered=false;delete root.dataset.entered;}
   host={...next};render();
  },
  update(next){last={...last,...next};render();},
  dispose(){disposed=true;root.hidden=true;delete root.dataset.entered;},
 };
}
