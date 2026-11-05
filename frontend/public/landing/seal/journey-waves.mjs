/** Synchronizes historical water with the reversible journey's contact clock. */
export class JourneyWaves{
  constructor(water){this.water=water;this.state=null;this.token=null;this.autoTime=0;}
  frame({state,token,autoTime=0}){
    const active=['AUTO','DONE','STATIC'].includes(state),isStatic=state==='STATIC';
    if(!active){
      this.water.setActive(false);this.state=state;this.token=null;this.autoTime=0;return;
    }
    const target=state==='AUTO'?Math.max(0,Math.min(1.8,autoTime)):1.8;
    const restart=!this.water.active||this.token!==token
      ||(this.state==='STATIC'&&!isStatic)||(state==='AUTO'&&target<this.autoTime-1e-9);
    if(restart){this.water.reset();this.water.setStatic(isStatic);this.water.setActive(true);this.autoTime=0;}
    else this.water.setStatic(isStatic);
    if(!isStatic){
      // Fixed-step replay is bounded to 1.8s and rebuilds backward inspector seeks.
      let left=target-this.autoTime;
      while(left>1e-9){const delta=Math.min(1/60,left);this.water.update(delta);left-=delta;}
      this.autoTime=target;
    }
    this.state=state;this.token=token;
  }
  tick(dt){if(this.state==='DONE')this.water.update(dt);}
  pointer(...args){return this.state==='DONE'&&this.water.pointer(...args);}
}
