import test from 'node:test';
import assert from 'node:assert/strict';
import { Vector3 } from '../../public/landing/seal/vendor/three/build/three.module.js';
const api=await import('../../public/landing/seal/reflection.mjs').catch(()=>({}));
test('world reflection uses one plane and is its own inverse',()=>{
  assert.equal(typeof api.makeReflectionMatrix,'function');
  const m=api.makeReflectionMatrix(-1.6),p=new Vector3(2,1.4,-3);
  const r=p.clone().applyMatrix4(m);
  assert.ok(r.distanceTo(new Vector3(2,-4.6,-3))<1e-9);
  assert.ok(r.applyMatrix4(m).distanceTo(p)<1e-9);
});
