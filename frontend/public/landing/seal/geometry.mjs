import { Quaternion, Vector3 } from './vendor/three/build/three.module.js';
import { SEAL_HALF_HEIGHT } from './seal-dimensions.mjs';
export function contactPose(width,waterY){
  if(!Number.isFinite(width)||width<=0||!Number.isFinite(waterY))throw new RangeError('finite positive width and finite waterY required');
  return {position:new Vector3(0,waterY+SEAL_HALF_HEIGHT*width,0),rotation:new Quaternion()};
}
export function sealWorldCorners(pose,width){
  return [-.5,.5].flatMap(x=>[-SEAL_HALF_HEIGHT,SEAL_HALF_HEIGHT].flatMap(y=>[-.5,.5].map(z=>
    new Vector3(x*width,y*width,z*width).applyQuaternion(pose.rotation).add(pose.position))));
}
export function imprintWorldCorners(width,waterY){
  return [[-.5,-.5],[.5,-.5],[.5,.5],[-.5,.5]].map(([x,z])=>new Vector3(x*width,waterY,z*width));
}
export function projectImprint(camera,points,{width,height,left=0,top=0}){
  camera.updateMatrixWorld();
  const corners=points.map(p=>{const n=p.clone().project(camera);return {x:left+(n.x+1)*width/2,y:top+(1-n.y)*height/2};});
  const xs=corners.map(p=>p.x),ys=corners.map(p=>p.y);
  return {left:Math.min(...xs),top:Math.min(...ys),width:Math.max(...xs)-Math.min(...xs),height:Math.max(...ys)-Math.min(...ys),corners};
}
