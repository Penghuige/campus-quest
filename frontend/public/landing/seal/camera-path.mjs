import { Quaternion, Vector3 } from './vendor/three/build/three.module.js';
export function cameraPose(elevation,focus,distance){
  if(!Number.isFinite(elevation)||!Number.isFinite(distance)||distance<=0)throw new RangeError('finite elevation and positive distance required');
  const rotation=new Quaternion().setFromAxisAngle(new Vector3(1,0,0),-elevation);
  return {
    position:new Vector3(0,0,distance).applyQuaternion(rotation).add(focus),
    rotation,
    up:new Vector3(0,1,0).applyQuaternion(rotation)
  };
}
