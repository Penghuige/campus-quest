import test from 'node:test';
import assert from 'node:assert/strict';
import { PerspectiveCamera, Vector3 } from '../../public/landing/seal/vendor/three/build/three.module.js';
import * as g from '../../public/landing/seal/geometry.mjs';
const cameraAPI=await import('../../public/landing/seal/camera-path.mjs').catch(()=>({}));
test('four bottom corners meet horizontal water while the stamp remains upright',()=>{
  assert.equal(typeof g.contactPose,'function');
  for(const width of [.5,1,2]){
    const pose=g.contactPose(width,-1.5);
    const points=g.sealWorldCorners(pose,width);
    assert.equal(points.filter(p=>Math.abs(p.y+1.5)<1e-9).length,4);
    assert.ok(points.every(p=>p.y>=-1.5-1e-9));
    assert.ok(new Vector3(0,1,0).applyQuaternion(pose.rotation).distanceTo(new Vector3(0,1,0))<1e-9);
  }
});
test('horizontal imprint corners have a stable upright order under overhead projection',()=>{
  assert.equal(typeof g.imprintWorldCorners,'function');
  const corners=g.imprintWorldCorners(1,-1.5);
  assert.deepEqual(corners.map(v=>v.toArray()),[[-.5,-1.5,-.5],[.5,-1.5,-.5],[.5,-1.5,.5],[-.5,-1.5,.5]]);
  const camera=new PerspectiveCamera(24,1, .1,80);
  const cp=cameraAPI.cameraPose(Math.PI/2,new Vector3(0,-.7,0),8);
  camera.position.copy(cp.position);camera.quaternion.copy(cp.rotation);camera.updateMatrixWorld();
  const rect=g.projectImprint(camera,corners,{width:800,height:800,left:0,top:0});
  const [tl,tr,br,bl]=rect.corners;
  assert.ok(tl.x<tr.x&&tl.y<bl.y&&tr.y<br.y);
  assert.ok(Math.abs(rect.width-rect.height)<1e-8);
  assert.ok(Math.abs(rect.left+rect.width/2-400)<1e-8);
});
