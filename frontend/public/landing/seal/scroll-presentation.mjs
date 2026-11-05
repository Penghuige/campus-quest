import { Quaternion, Vector3 } from './vendor/three/build/three.module.js';
import { cameraPose } from './camera-path.mjs';
import { SEAL_HALF_HEIGHT } from './seal-dimensions.mjs';
export const clamp=v=>Math.max(0,Math.min(1,v));
export const smooth=v=>{const t=clamp(v);return t*t*(3-2*t);};
export const journeyStages=Object.freeze({introEnd:.4,rotationJoin:2.8,morphEnd:5.2,ascentEnd:6.8,contact:7.6,page:9});
export const scrollBounds=h=>({start:journeyStages.ascentEnd*h,contact:journeyStages.contact*h,pageHeight:journeyStages.page*h});
const LOW=Math.PI/12;
const FORMED_ANGLE=2*Math.PI+.55,GATHER_LENGTH=journeyStages.morphEnd-journeyStages.introEnd;
const GATHER_RAMP=.8,GATHER_SPEED=FORMED_ANGLE/(GATHER_LENGTH-GATHER_RAMP/2);
// One gathering pass, with the same formed orientation modulo full turns.
function gatherAngle(s){
  const d=Math.max(0,Math.min(GATHER_LENGTH,s-journeyStages.introEnd));
  if(d<GATHER_RAMP){const q=d/GATHER_RAMP;return GATHER_SPEED*GATHER_RAMP*(q**3-.5*q**4);}
  return GATHER_SPEED*(d-GATHER_RAMP/2);
}
export function journeyFrame(screenProgress,layout){
  const s=Math.max(0,screenProgress);
  // Return raw morph progress: the CPU pose and GPU particles each smooth it
  // once. Shape evolves across both early turns and the following turns.
  const q=clamp((s-journeyStages.introEnd)/(journeyStages.morphEnd-journeyStages.introEnd)),m=smooth(q);
  const ascent=clamp((s-journeyStages.morphEnd)/1.6),press=smooth((s-journeyStages.ascentEnd)/.8);
  const hoverCenter=new Vector3(0,layout.waterY+SEAL_HALF_HEIGHT+layout.hoverGap,0);
  const bodyPosition=hoverCenter.clone().multiplyScalar(m);
  if(s>=journeyStages.ascentEnd)bodyPosition.y=layout.waterY+SEAL_HALF_HEIGHT+layout.hoverGap*(1-press);
  const extra=2*Math.PI-.55;
  const k=GATHER_SPEED*1.6/extra;
  const h=(k-2)*ascent**3+(3-2*k)*ascent**2+k*ascent;
  const angle=s<journeyStages.morphEnd?gatherAngle(s):FORMED_ANGLE+extra*h;
  const elevation=LOW+(Math.PI/2-LOW)*smooth(ascent);
  // Keep the low-angle focus .45W above the base: added height grows upward
  // without pushing the camera away from the existing square footprint.
  const bias=(SEAL_HALF_HEIGHT-.45)*m*Math.cos(elevation)/Math.cos(LOW);
  const focus=bodyPosition.clone().add(new Vector3(0,-bias,0));
  const distance=layout.startDistance+(layout.viewDistance-layout.startDistance)*m;
  return {
    screenProgress:s,
    width:1,phase:s<journeyStages.introEnd?'tree':s<journeyStages.morphEnd?'morph':s<journeyStages.ascentEnd?'ascent':s<journeyStages.contact?'press':'contact',
    morph:q,angle,bodyPose:{position:bodyPosition,rotation:new Quaternion().setFromAxisAngle(new Vector3(0,1,0),angle)},
    cameraPose:cameraPose(elevation,focus,distance),ink:0,paper:0,visible:true,tighten:0
  };
}
