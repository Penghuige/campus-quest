import test from 'node:test';
import assert from 'node:assert/strict';
import { journeyFrame } from '../../public/landing/seal/scroll-presentation.mjs';
const api=await import('../../public/landing/seal/journey-driver.mjs').catch(()=>({}));
const layout={width:1,waterY:-1.6,hoverGap:.7,startDistance:12,viewDistance:8,waterHalfExtent:40,viewport:{width:1280,height:800}};
function fixture(y=0){
 assert.equal(typeof api.JourneyDriver,'function');
 const frames=[],starts=[],cancels=[];
 const d=new api.JourneyDriver({layout,onFrame:f=>frames.push(f),onAutoStart:t=>starts.push(t),onAutoCancel:t=>cancels.push(t)});
 d.scroll(y,{snap:true});return {d,frames,starts,cancels};
}
test('contact waits for displayed progress and starts once after the exact contact frame',()=>{
 const {d,frames,starts}=fixture();d.scroll(6080);
 assert.equal(starts.length,0);d.tick(.016);assert.equal(starts.length,0);
 for(let i=0;i<300;i++)d.tick(1/60);
 assert.equal(starts.length,1);assert.equal(d.snapshot().state,'AUTO');
 const f=frames.find(f=>f.state==='AUTO');
 assert.ok(f.bodyPose.position.distanceTo(journeyFrame(7.6,layout).bodyPose.position)<1e-9);
});
test('internal reverse continues, leaving upper boundary cancels camera and rejects stale auto',()=>{
 const {d,frames,starts,cancels}=fixture(6100);const token=starts[0];
 d.auto(.64,token);d.scroll(5600);
 assert.equal(d.snapshot().state,'AUTO');assert.equal(d.snapshot().autoTime,.64);
 d.scroll(5300);
 assert.equal(d.snapshot().state,'BEFORE');assert.equal(cancels.length,1);
 assert.ok(frames.at(-1).cameraPose.position.distanceTo(journeyFrame(5300/800,layout).cameraPose.position)<1e-9);
 assert.equal(d.auto(1.8,token),false);assert.equal(d.complete(token),false);
});
test('restored bottom starts once, resize preserves clock and static uses terminal camera',()=>{
 const {d,starts,frames}=fixture(6100);assert.equal(starts.length,1);
 d.auto(.64,starts[0]);
 const next={...layout,viewport:{width:390,height:700}};
 d.refresh(next,6100);assert.equal(d.snapshot().autoTime,.64);assert.equal(d.snapshot().token,starts[0]);
 d.setReducedMotion(true,6100);assert.equal(d.snapshot().state,'STATIC');
 assert.equal(frames.at(-1).paper,1);
 assert.ok(frames.at(-1).cameraPose.up.z<-0.999);
});
