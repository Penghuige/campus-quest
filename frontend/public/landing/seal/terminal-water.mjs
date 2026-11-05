import {WaterField} from './runtime/interactive-ripple-2026-11-01/water-field.mjs';
import {WaveClock,FIXED_STEP,centreDrop,pointToField} from './runtime/interactive-ripple-2026-11-01/wave-clock.mjs';

const validSize=(width,height)=>Number.isFinite(width)&&Number.isFinite(height)&&width>0&&height>0;

/**
 * Reuses one terminal field. The host owns visibility, pointerdown and its ticker;
 * pause update while hidden and start the resumed host delta from zero.
 * reset()/setActive(false) leave an empty inactive field; the next entry replays.
 */
export class TerminalWater{
  constructor(canvas,{field}={}){
    this.field=field??new WaterField(canvas);
    this.clock=new WaveClock({interval:1.5});
    this.active=false;
    this.staticMode=false;
    this.disposed=false;
  }

  resize(width,height){
    if(this.disposed||!validSize(width,height))return;
    // WaterField.resize changes the view only, retaining the GPU field and time.
    this.field.resize(width,height);
  }

  setActive(enabled){
    if(this.disposed)return;
    enabled=Boolean(enabled);
    if(enabled===this.active)return;
    if(!enabled){this.reset();return;}
    this.active=true;
    this.#restart();
  }

  update(dt){
    if(this.disposed||!this.active||this.staticMode)return;
    this.#simulate(this.clock.advance(dt));
  }

  render(){
    if(!this.disposed)this.field.render();
  }

  /** Call on pointerdown, including clicks on the overlaid DOM imprint. */
  pointer(clientX,clientY,rect){
    if(this.disposed||!this.active||this.staticMode||!rect)return false;
    if(![clientX,clientY,rect.left,rect.top].every(Number.isFinite)||!validSize(rect.width,rect.height))return false;
    if(clientX<rect.left||clientX>rect.left+rect.width||clientY<rect.top||clientY>rect.top+rect.height)return false;
    const point=pointToField(clientX,clientY,rect);
    this.clock.queue({...point,radius:.014,strength:.016,source:'click'});
    return true;
  }

  reset(){
    if(this.disposed)return;
    this.active=false;
    this.clock.reset();
    this.field.clear();
  }

  setStatic(enabled){
    if(this.disposed)return;
    enabled=Boolean(enabled);
    if(enabled===this.staticMode)return;
    this.staticMode=enabled;
    if(this.active)this.#restart();
  }

  dispose(){
    if(this.disposed)return;
    this.disposed=true;
    this.active=false;
    this.clock.reset();
    this.field.dispose();
  }

  #restart(){
    this.clock.reset();
    this.field.clear();
    this.clock.queue(centreDrop());
    if(this.staticMode){
      // Same deterministic frozen frame as the independent water experiment.
      // It includes the initial centre pulse and its normal 1.5-second repeat.
      for(let frame=0;frame<121;frame++)this.#simulate(this.clock.advance(FIXED_STEP));
    }
  }

  #simulate(frames){
    for(const frame of frames)this.field.step(frame.drops);
  }
}
