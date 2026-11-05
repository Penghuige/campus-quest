// Absolute poses: the host supplies its already-smoothed scroll progress.
const clamp=v=>Math.max(0,Math.min(1,v));
const smooth=v=>{const t=clamp(v);return t*t*(3-2*t);};
const STEP=2*Math.PI/5;

export function resizedJourneyY({state,actualY,displayY,previousHeight,nextHeight}){
 if(previousHeight!==nextHeight&&(state==='BEFORE'||state==='APPROACH'))return displayY/previousHeight*nextHeight;
 return actualY;
}

export function orbitMode({state,active,ready,reducedMotion=false,fallback=false}){
 if(!active||!ready)return 'hidden';
 if(fallback||reducedMotion||state==='STATIC')return 'static';
 return state==='BEFORE'||state==='APPROACH'?'animated':'hidden';
}

export function orbitCardsFrame(screenProgress,{width:W,height:H}){
 if(![screenProgress,W,H].every(Number.isFinite)||W<=0||H<=0)throw new RangeError('finite progress and positive viewport required');
 const q=clamp((screenProgress-.4)/4.8),u=5*q,slot=Math.floor(u),t=u-slot;
 const paced=slot+(t<=.6?.1*smooth(t/.6):.1+.9*smooth((t-.6)/.4));
 const envelope=smooth(q/.04)*(1-smooth((q-.92)/.08));
 const active=screenProgress>=.4&&screenProgress<5.2;
 const relay=W<=900||H<=540;
 const rx=Math.max(0,Math.min(.34*W,520,W/2-24-132)),cy=.5*H;
 const ry=Math.max(0,Math.min(.28*H,cy-142,H-173-cy));
 const cards=Array.from({length:5},(_,index)=>{
  const local=u-index,primary=active&&local>=0&&local<.6;
  if(relay){
   const opacity=local<0?smooth((local+.2)/.2):local<=.6?1:1-smooth((local-.6)/.2);
   const dx=local<0?18*(1-opacity):local>.6?-18*(1-opacity):0;
   return {index,x:W/2+dx,y:Math.max(68,Math.min(.70*H,H-163)),scale:.94+.06*opacity,
    yaw:0,depth:opacity,opacity:opacity*envelope,zIndex:1,primary};
  }
  const theta=Math.PI/2-.05*STEP+STEP*paced-index*STEP;
  const depth=(Math.sin(theta)+1)/2;
  const visibility=depth<=.5?.35*smooth(depth*2):.35+.65*smooth((depth-.5)*2);
  const completedPass=1-smooth(local-2.5);
  return {index,x:W/2+Math.cos(theta)*rx,y:cy+Math.sin(theta)*ry,
   scale:.68+.32*depth,yaw:-28*Math.cos(theta),depth,
   opacity:visibility*envelope*completedPass,zIndex:Math.round(depth*20),primary};
 });
 return {mode:relay?'relay':'orbit',title:{opacity:1-smooth((screenProgress-.4)/.32),y:-16*smooth((screenProgress-.4)/.32)},cards};
}
