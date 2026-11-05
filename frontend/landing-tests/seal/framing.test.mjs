import test from 'node:test';
import assert from 'node:assert/strict';
import { Box3, PerspectiveCamera, Vector3 } from '../../public/landing/seal/vendor/three/build/three.module.js';
const api=await import('../../public/landing/seal/framing.mjs').catch(()=>({}));
const {journeyFrame}=await import('../../public/landing/seal/scroll-presentation.mjs').catch(()=>({}));
import { sealWorldCorners } from '../../public/landing/seal/geometry.mjs';
test('camera fitting keeps model fixed and all body corners inside desktop and narrow frames',()=>{
  assert.equal(typeof api.fitStageLayout,'function');
  for(const [width,height] of [[1280,800],[390,844],[390,700],[2560,1440]]){
    const layout=api.fitStageLayout({cssWidth:width,cssHeight:height,waterY:-1.6,sourceBounds:new Box3(new Vector3(-2.2,-1.5,-1.6),new Vector3(2.2,2.2,1.6))});
    const camera=new PerspectiveCamera(24,width/height,.1,80);
    for(let i=0;i<=200;i++){
      const f=journeyFrame(i/200*7.6,layout);
      camera.position.copy(f.cameraPose.position);camera.quaternion.copy(f.cameraPose.rotation);camera.updateMatrixWorld();
      assert.equal(f.width,1);
      for(const p of sealWorldCorners(f.bodyPose,1)){
        const n=p.project(camera);assert.ok(Math.abs(n.x)<=.821&&Math.abs(n.y)<=.881&&Math.abs(n.z)<1);
      }
    }
    assert.ok(layout.waterHalfExtent>2);
  }
});
