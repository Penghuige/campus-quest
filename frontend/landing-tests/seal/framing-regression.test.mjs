import test from 'node:test';
import assert from 'node:assert/strict';
import { Box3,Vector3 } from '../../public/landing/seal/vendor/three/build/three.module.js';
import { fitStageLayout } from '../../public/landing/seal/framing.mjs';
test('a wider source tree must not shrink the fully formed seal',()=>{
 const input={cssWidth:1280,cssHeight:800,waterY:-1.6212};
 const normal=fitStageLayout({...input,sourceBounds:new Box3(new Vector3(-2.2,-1.5,-1.6),new Vector3(2.2,2.2,1.6))});
 const wide=fitStageLayout({...input,sourceBounds:new Box3(new Vector3(-3,-1.5,-2.2),new Vector3(3,2.2,2.2))});
 assert.ok(Math.abs(normal.viewDistance-wide.viewDistance)<1e-6);
 assert.ok(wide.startDistance>normal.startDistance);
});
