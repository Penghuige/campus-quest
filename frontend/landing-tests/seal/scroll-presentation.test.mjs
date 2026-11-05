import test from 'node:test';
import assert from 'node:assert/strict';
import { Vector3,Quaternion } from '../../public/landing/seal/vendor/three/build/three.module.js';
const api=await import('../../public/landing/seal/scroll-presentation.mjs').catch(()=>({}));
const layout={width:1,waterY:-1.6,hoverGap:.7,startDistance:12,viewDistance:8,viewport:{width:1280,height:800}};
test('formed seal continues turning throughout camera ascent and aligns before contact',()=>{
  assert.equal(typeof api.journeyFrame,'function');
  const [a,b,c]=[5.2,6,6.8].map(s=>api.journeyFrame(s,layout));
  assert.equal(a.morph,1);assert.ok(a.angle<b.angle&&b.angle<c.angle);
  assert.ok(Math.abs(c.angle-4*Math.PI)<1e-9);
  assert.ok(c.cameraPose.up.distanceTo(new Vector3(0,0,-1))<1e-9);
});

test('one gathering turn preserves formed orientation and the full ascent remainder',()=>{
 const opening=api.journeyFrame(.4,layout),formed=api.journeyFrame(5.2,layout),top=api.journeyFrame(6.8,layout);
 assert.ok(Math.abs(opening.angle)<1e-9);
 assert.ok(Math.abs(formed.angle-(2*Math.PI+.55))<1e-9);
 assert.ok(Math.abs(top.angle-formed.angle-(2*Math.PI-.55))<1e-9);
 for(const [f,oldAngle] of [[formed,8*Math.PI+.55],[top,10*Math.PI]]){
  const old=new Quaternion().setFromAxisAngle(new Vector3(0,1,0),oldAngle);
  assert.ok(Math.abs(f.bodyPose.rotation.dot(old))>1-1e-10);
 }
 for(const s of [.4,5.2,6.8]){
  const e=1e-5,mid=api.journeyFrame(s,layout).angle;
  const before=(mid-api.journeyFrame(s-e,layout).angle)/e,after=(api.journeyFrame(s+e,layout).angle-mid)/e;
  assert.ok(Math.abs(before-after)<.001,`${s}: ${before} vs ${after}`);
 }
 assert.equal(api.journeyFrame(2.4,layout).screenProgress,2.4);
});
test('fixed size, absolute reversible poses and continuous stage boundaries',()=>{
  assert.equal(typeof api.journeyFrame,'function');
  for(const s of [0,.4,1,2.8,4,5.2,6,6.8,7.6]){
    const frame=api.journeyFrame(s,layout);assert.equal(frame.width,1);
    api.journeyFrame(7.6-s,layout);
    assert.deepEqual(api.journeyFrame(s,layout),frame);
  }
  for(const s of [.4,2.8,5.2,6.8,7.6]){
    const a=api.journeyFrame(s-1e-7,layout),b=api.journeyFrame(s+1e-7,layout);
    assert.ok(a.bodyPose.position.distanceTo(b.bodyPose.position)<1e-5);
    assert.ok(a.cameraPose.position.distanceTo(b.cameraPose.position)<1e-5);
    assert.ok(Math.abs(a.angle-b.angle)<1e-5);
  }
});
test('scroll content remains beyond contact after viewport height changes',()=>{
  for(const h of [700,800,844]){
    const b=api.scrollBounds(h);
    assert.equal(b.start,6.8*h);assert.equal(b.contact,7.6*h);
    assert.ok(b.pageHeight-h>b.contact);
  }
});
