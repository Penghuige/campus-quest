import { journeyFrame, clamp,journeyStages } from './scroll-presentation.mjs';
import { SEAL_HALF_HEIGHT } from './seal-dimensions.mjs';
const sine=t=>(1-Math.cos(Math.PI*t))/2;
const track=(t,a,b,fn=sine)=>fn(clamp((t-a)/(b-a)));
export function autoFrame(seconds,layout){
  if(!Number.isFinite(seconds))throw new TypeError('time must be finite');
  const t=Math.max(0,Math.min(1.8,seconds));
  const f=journeyFrame(journeyStages.contact,layout);
  f.phase='auto';
  f.bodyPose.position.y+=.28*track(t,.28,.64);
  const exit=(layout.viewDistance+SEAL_HALF_HEIGHT-.28)*Math.tan(Math.PI/15)+.75;
  f.bodyPose.position.z=-exit*track(t,.56,1.52,p=>p**3);
  f.visible=t<1.52;
  f.ink=track(t,.04,.26);f.paper=track(t,0,1.68);
  f.tighten=t<=.14?track(t,0,.14):1-track(t,.14,.28);
  return f;
}
