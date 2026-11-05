import test from 'node:test';
import assert from 'node:assert/strict';
import { Vector3 } from '../../public/landing/seal/vendor/three/build/three.module.js';
const api=await import('../../public/landing/seal/camera-path.mjs').catch(()=>({}));
test('camera reaches exactly overhead with screen up toward world negative Z',()=>{
  assert.equal(typeof api.cameraPose,'function');
  const p=api.cameraPose(Math.PI/2,new Vector3(0,2,0),8);
  assert.ok(p.position.distanceTo(new Vector3(0,10,0))<1e-9);
  assert.ok(p.up.distanceTo(new Vector3(0,0,-1))<1e-9);
  assert.ok(new Vector3(0,0,-1).applyQuaternion(p.rotation).distanceTo(new Vector3(0,-1,0))<1e-9);
});
test('camera orientation stays finite and continuous through the overhead pole',()=>{
  assert.equal(typeof api.cameraPose,'function');
  let previous;
  for(let i=0;i<=900;i++){
    const p=api.cameraPose(i/900*Math.PI/2,new Vector3(),8);
    assert.ok([...p.position,...p.up,...p.rotation].every(Number.isFinite));
    assert.ok(Math.abs(p.rotation.length()-1)<1e-9);
    if(previous)assert.ok(previous.dot(p.up)>.999);
    previous=p.up;
  }
});
