export const FIXED_STEP=1/60;
export const MAX_DROPS=16;
export const centreDrop=()=>({x:0.5,y:0.5,radius:0.017,strength:0.012,source:'auto'});

export class WaveClock {
  constructor({interval=1.5}={}){
    this.interval=interval;this.automatic=true;this.reset();
  }
  reset(){this.time=0;this.steps=0;this.accumulator=0;this.nextAuto=this.interval;this.pending=[];}
  queue(drop){
    if(![drop.x,drop.y,drop.radius,drop.strength].every(Number.isFinite)||drop.radius<=0)return;
    this.pending.push({...drop});
  }
  setAutomatic(enabled){
    this.automatic=enabled;this.nextAuto=this.time+this.interval;
  }
  advance(delta){
    if(!Number.isFinite(delta)||delta<=0)return [];
    this.accumulator+=Math.min(delta,0.1);
    const frames=[];
    while(this.accumulator+1e-10>=FIXED_STEP){
      this.accumulator-=FIXED_STEP;this.steps++;this.time=this.steps*FIXED_STEP;
      if(this.automatic&&this.time+1e-10>=this.nextAuto){
        this.queue(centreDrop());this.nextAuto+=this.interval;
      }
      frames.push({time:this.time,drops:this.pending.splice(0,MAX_DROPS)});
    }
    return frames;
  }
}

export function pointToField(clientX,clientY,rect){
  const longest=Math.max(rect.width,rect.height);
  return {x:0.5+(clientX-rect.left-rect.width*0.5)/longest,y:0.5-(clientY-rect.top-rect.height*0.5)/longest};
}
