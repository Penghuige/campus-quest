import { SealController } from './controller.mjs';
import { scrollBounds,journeyFrame } from './scroll-presentation.mjs';
import { autoFrame } from './motion.mjs';
import { followScroll } from './follow-scroll.mjs';
export class JourneyDriver {
  constructor({layout,onFrame=()=>{},onAutoStart=()=>{},onAutoCancel=()=>{}}){
    this.layout=layout;this.onFrame=onFrame;this.actualY=0;this.displayY=0;this.autoTime=0;
    this.controller=new SealController({...scrollBounds(layout.viewport.height),
      onChange:s=>{if(s.state==='AUTO'){this.autoTime=0;this.displayY=scrollBounds(this.layout.viewport.height).contact;}else if(s.state==='BEFORE'||s.state==='APPROACH')this.autoTime=0;this.present();},
      onStartAuto:t=>onAutoStart(t),onCancel:t=>onAutoCancel(t)});
  }
  snapshot(){return {...this.controller.snapshot(),displayY:this.displayY,actualY:this.actualY,autoTime:this.autoTime};}
  present(){
    const snap=this.controller.snapshot();
    let frame;
    if(snap.state==='AUTO')frame=autoFrame(this.autoTime,this.layout);
    else if(snap.state==='DONE'||snap.state==='STATIC'){frame=autoFrame(1.8,this.layout);frame.phase='final';}
    else frame=journeyFrame(this.displayY/this.layout.viewport.height,this.layout);
    this.onFrame({...frame,state:snap.state,token:snap.token,autoTime:this.autoTime});
  }
  scroll(y,{snap=false}={}){
    if(!Number.isFinite(y))throw new TypeError('scroll must be finite');
    this.actualY=y;
    const state=this.controller.snapshot().state;
    const active=state==='AUTO'||state==='DONE';
    if(snap||(active&&y<scrollBounds(this.layout.viewport.height).start))this.displayY=y;
    if(snap||active||state==='STATIC')this.controller.updateScroll(y);
    this.present();
  }
  tick(dt){
    const state=this.controller.snapshot().state;
    if(state!=='BEFORE'&&state!=='APPROACH')return;
    this.displayY=followScroll(this.displayY,this.actualY,dt);
    if(Math.abs(this.displayY-this.actualY)<.5)this.displayY=this.actualY;
    this.controller.updateScroll(Math.min(this.actualY,this.displayY));
    this.present();
  }
  auto(t,token){
    const s=this.controller.snapshot();if(s.state!=='AUTO'||s.token!==token)return false;
    this.autoTime=t;this.present();return true;
  }
  complete(token){return this.controller.complete(token);}
  refresh(layout,y){
    this.layout=layout;this.actualY=y;this.displayY=y;
    this.controller.reconfigure(scrollBounds(layout.viewport.height),y);this.present();
  }
  setReducedMotion(value,y=this.actualY){
    this.actualY=y;this.displayY=y;this.controller.setReducedMotion(value,y);this.present();
  }
}
