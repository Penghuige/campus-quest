import { Matrix4 } from './vendor/three/build/three.module.js';
export function makeReflectionMatrix(waterY){
  if(!Number.isFinite(waterY))throw new TypeError('finite waterY required');
  return new Matrix4().set(1,0,0,0, 0,-1,0,2*waterY, 0,0,1,0, 0,0,0,1);
}
