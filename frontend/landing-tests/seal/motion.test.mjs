import test from 'node:test';
import assert from 'node:assert/strict';
import { PerspectiveCamera } from '../../public/landing/seal/vendor/three/build/three.module.js';
const api=await import('../../public/landing/seal/motion.mjs').catch(()=>({}));
const {journeyFrame}=await import('../../public/landing/seal/scroll-presentation.mjs').catch(()=>({}));
import { sealWorldCorners } from '../../public/landing/seal/geometry.mjs';
const layout={width:1,waterY:-1.6,hoverGap:.7,startDistance:12,viewDistance:8,viewport:{width:1280,height:800}};
test('auto contact exactly joins scroll pose and holds through .28s',()=>{
  assert.equal(typeof api.autoFrame,'function');
  const contact=journeyFrame(7.6,layout);
  for(const t of [0,.14,.28]){
    const f=api.autoFrame(t,layout);
    assert.ok(f.bodyPose.position.distanceTo(contact.bodyPose.position)<1e-9);
    assert.ok(f.cameraPose.position.distanceTo(contact.cameraPose.position)<1e-9);
  }
});
test('normal separation and viewport-top departure keep the body above water',()=>{
  assert.equal(typeof api.autoFrame,'function');
  const camera=new PerspectiveCamera(24,1.6,.1,80);
  const cp=api.autoFrame(0,layout).cameraPose;
  camera.position.copy(cp.position);camera.quaternion.copy(cp.rotation);camera.updateMatrixWorld();
  for(let i=0;i<=180;i++){
    const frame=api.autoFrame(i/100,layout);
    assert.equal(frame.width,1);
    assert.ok(sealWorldCorners(frame.bodyPose,1).every(p=>p.y>=layout.waterY-1e-9));
  }
  const exit=api.autoFrame(1.52,layout);
  assert.equal(api.autoFrame(1.519,layout).visible,true);assert.equal(exit.visible,false);
  for(const p of sealWorldCorners(exit.bodyPose,1)){const n=p.project(camera);assert.ok(n.y>1&&n.z<1&&n.z>-1);}
  assert.ok(Math.abs(api.autoFrame(.64,layout).bodyPose.position.y-(layout.waterY+.9+.28))<1e-9);
  assert.ok(api.autoFrame(1,layout).bodyPose.position.z<0);
});
test('ink and paper finish within1.8s and seeking clamps',()=>{
  assert.equal(typeof api.autoFrame,'function');
  assert.equal(api.autoFrame(0,layout).paper,0);
  assert.equal(api.autoFrame(.26,layout).ink,1);
  assert.equal(api.autoFrame(1.68,layout).paper,1);
  assert.deepEqual(api.autoFrame(-1,layout),api.autoFrame(0,layout));
  assert.deepEqual(api.autoFrame(5,layout),api.autoFrame(1.8,layout));
});
