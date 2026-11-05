import test from 'node:test';
import assert from 'node:assert/strict';
import { journeyFrame } from '../../public/landing/seal/scroll-presentation.mjs';

const adapter = await import('../../public/landing/seal/landing-driver.mjs').catch(() => ({}));
const layout = {
  width:1, waterY:-1.6, hoverGap:.7, startDistance:12, viewDistance:8,
  waterHalfExtent:40, viewport:{width:1280,height:800}
};

function fixture(initialOffset=1600){
  assert.equal(typeof adapter.LandingJourneyDriver, 'function', 'LandingJourneyDriver adapter must be exported');
  let offset=initialOffset;
  const frames=[],starts=[],cancels=[];
  const driver=new adapter.LandingJourneyDriver({layout,getOffset:()=>offset,
    onFrame:frame=>frames.push(frame),onAutoStart:token=>starts.push(token),onAutoCancel:token=>cancels.push(token)});
  return {driver,frames,starts,cancels,setOffset:value=>{offset=value;}};
}

test('white page reveal keeps the tree at its original opening frame',()=>{
  const {driver,frames,starts}=fixture();
  for(const globalY of [0,800,1599,1600]){
    driver.scroll(globalY,{snap:true});
    driver.tick(1/60);
    const frame=frames.at(-1),opening=journeyFrame(0,layout);
    assert.equal(frame.phase,'tree');
    assert.equal(frame.morph,0);
    assert.equal(frame.angle,0);
    assert.ok(frame.cameraPose.position.distanceTo(opening.cameraPose.position)<1e-9);
    assert.equal(driver.snapshot().actualY,0);
  }
  assert.equal(starts.length,0);
});

test('returning into the white reveal immediately restores the opening tree without easing',()=>{
  const {driver,frames}=fixture();
  driver.scroll(6000,{snap:true});
  assert.equal(frames.at(-1).phase,'ascent');
  driver.scroll(1599);
  const frame=frames.at(-1);
  assert.equal(frame.phase,'tree');
  assert.equal(frame.morph,0);
  assert.equal(frame.angle,0);
  assert.equal(driver.snapshot().displayY,0);
});

test('scroll inside the fully revealed stage keeps the original gradual follow',()=>{
  const {driver,frames}=fixture();
  driver.scroll(1600,{snap:true});
  driver.scroll(6000);
  assert.equal(driver.snapshot().actualY,4400);
  assert.equal(driver.snapshot().displayY,0);
  assert.equal(frames.at(-1).phase,'tree');
  driver.tick(1/60);
  assert.ok(driver.snapshot().displayY>0);
  assert.ok(driver.snapshot().displayY<4400);
});

test('contact begins at the original local distance after white has fully cleared',()=>{
  const {driver,starts}=fixture();
  driver.scroll(7679,{snap:true});
  assert.equal(driver.snapshot().state,'APPROACH');
  assert.equal(starts.length,0);
  driver.scroll(7680,{snap:true});
  assert.equal(driver.snapshot().state,'AUTO');
  assert.equal(driver.snapshot().actualY,6080);
  assert.equal(starts.length,1);
  driver.scroll(7681,{snap:true});
  assert.equal(starts.length,1);
});

test('reverse scroll uses the local upper boundary and allows a fresh replay',()=>{
  const {driver,starts,cancels,frames}=fixture();
  driver.scroll(7700,{snap:true});
  const first=starts[0];
  driver.auto(.64,first);
  driver.scroll(7200);
  assert.equal(driver.snapshot().state,'AUTO');
  assert.equal(driver.snapshot().autoTime,.64);
  driver.scroll(7039);
  assert.equal(driver.snapshot().state,'BEFORE');
  assert.equal(cancels.length,1);
  assert.equal(driver.complete(first),false);
  driver.scroll(800,{snap:true});
  assert.equal(frames.at(-1).phase,'tree');
  assert.equal(frames.at(-1).morph,0);
  driver.scroll(7680,{snap:true});
  assert.equal(starts.length,2);
  assert.notEqual(starts[1],first);
});

test('refresh recomputes the offset and viewport without interpreting it as contact',()=>{
  const {driver,starts,setOffset}=fixture();
  driver.scroll(7000,{snap:true});
  setOffset(1900);
  const resized={...layout,viewport:{width:390,height:700}};
  driver.refresh(resized,7230);
  assert.equal(driver.snapshot().actualY,5330);
  assert.equal(driver.snapshot().state,'APPROACH');
  assert.equal(starts.length,0);
  driver.scroll(7230,{snap:true});
  assert.equal(driver.snapshot().state,'AUTO');
  assert.equal(starts.length,1);
});

test('reduced-motion default position is not offset twice and document targets follow layout',()=>{
  const {driver,setOffset}=fixture();
  driver.scroll(7600,{snap:true});
  driver.setReducedMotion(true);
  assert.equal(driver.snapshot().state,'STATIC');
  assert.equal(driver.snapshot().actualY,6000);
  driver.setReducedMotion(false);
  assert.equal(driver.snapshot().state,'APPROACH');
  assert.equal(driver.snapshot().actualY,6000);
  assert.equal(driver.documentY(6080),7680);
  setOffset(1900);
  assert.equal(driver.documentY(6080),7980);
  driver.setReducedMotion(true,1000);
  assert.equal(driver.snapshot().actualY,0);
});
