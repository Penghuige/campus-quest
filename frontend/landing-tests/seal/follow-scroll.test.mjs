import test from 'node:test';
import assert from 'node:assert/strict';
const api=await import('../../public/landing/seal/follow-scroll.mjs').catch(()=>({}));
test('same elapsed time has identical follow at60Hz and120Hz',()=>{
  assert.equal(typeof api.followScroll,'function');
  const run=n=>{let y=0;for(let i=0;i<n;i++)y=api.followScroll(y,1000,1/n);return y;};
  assert.ok(Math.abs(run(60)-run(120))<1e-6);
  assert.ok(run(60)>990&&run(60)<1000);
  assert.equal(api.followScroll(5,10,0),5);
});
