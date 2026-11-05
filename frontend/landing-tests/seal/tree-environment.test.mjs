import test from 'node:test';
import assert from 'node:assert/strict';
import { createEnvironment,sampleTree,treeLayout } from '../../public/landing/seal/tree-environment.mjs';
test('tree remains deterministic and responsive without changing its branch coordinates',()=>{
  const a=sampleTree(1000,1.6),b=sampleTree(1000,1.6);
  assert.deepEqual(a,b);assert.ok(a.every(Number.isFinite));
  const small=sampleTree(1000,.45),ratio=treeLayout(.45).scale/treeLayout(1.6).scale;
  for(let i=0;i<a.length;i++)assert.ok(Math.abs(small[i]-a[i]*ratio)<1e-6);
});
test('water and reflection share a stable nominal contact point at every clock phase',()=>{
  assert.ok(Math.abs(treeLayout(1.6).waterY-(-.66-3.2)*.42)<1e-9);
  const time={value:0},e=createEnvironment({time,dpr:{value:1},aspect:1.6});
  assert.equal(typeof e.sampleHeight,'function');
  for(const t of [0,.5,2,10])assert.ok(Math.abs(e.sampleHeight(0,0,t)-treeLayout(1.6).waterY)<1e-9);
  assert.equal(typeof e.updateCamera,'function');
});
test('water coverage grows independently of narrow tree scaling',()=>{
  const e=createEnvironment({time:{value:0},dpr:{value:1},aspect:.45});
  e.setLayout({aspect:.45,waterHalfExtent:40});
  const water=e.group.getObjectByName('original-water');
  e.group.updateMatrixWorld(true);
  const p=water.geometry.getAttribute('position');
  let max=0;
  for(let i=0;i<p.count;i++)max=Math.max(max,Math.abs(p.getX(i)*e.group.scale.x));
  assert.ok(max>=40-1e-4);
});
