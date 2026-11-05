import test from 'node:test';
import assert from 'node:assert/strict';
import {WaveClock,pointToField} from '../../public/landing/seal/runtime/interactive-ripple-2026-11-01/wave-clock.mjs';

const approx=(actual,want)=>assert.ok(Math.abs(actual-want)<1e-9,`${actual} != ${want}`);

// Catches frame-rate-dependent propagation and cadence (not a test of RAF).
test('equal elapsed time at 60 and 120 Hz advances the same wave steps and auto pulses',()=>{
  const run=(hz)=>{
    const clock=new WaveClock();const frames=[];
    for(let i=0;i<hz*10;i++)frames.push(...clock.advance(1/hz));
    return {steps:frames.length,auto:frames.flatMap(f=>f.drops.filter(d=>d.source==='auto').map(()=>f.time))};
  };
  const low=run(60),high=run(120);
  assert.equal(low.steps,600);assert.equal(high.steps,600);
  assert.deepEqual(low.auto,[1.5,3,4.5,6,7.5,9]);
  assert.deepEqual(high.auto,[1.5,3,4.5,6,7.5,9]);
});

// Catches a single mouse uniform overwriting a scheduled centre pulse.
test('a timed centre pulse and multiple same-step clicks are all retained',()=>{
  const clock=new WaveClock();
  for(let i=0;i<89;i++)clock.advance(1/60);
  clock.queue({x:0.3,y:0.5,radius:0.02,strength:0.03,source:'click'});
  clock.queue({x:0.7,y:0.5,radius:0.02,strength:0.03,source:'click'});
  const frames=clock.advance(1/60);
  assert.equal(frames.length,1);
  assert.deepEqual(frames[0].drops.map(d=>d.source),['click','click','auto']);
  assert.deepEqual(frames[0].drops.map(d=>d.x),[0.3,0.7,0.5]);
});

test('a long suspended frame has bounded catch-up rather than injecting a burst',()=>{
  const clock=new WaveClock();
  const frames=clock.advance(90);
  assert.ok(frames.length>0&&frames.length<=6);
  assert.equal(frames.flatMap(f=>f.drops).length,0);
  assert.ok(clock.time<=0.10000001);
});

test('auto toggle stops centre pulses while manual clicks remain usable',()=>{
  const clock=new WaveClock();clock.setAutomatic(false);
  const frames=[];
  for(let i=0;i<360;i++)frames.push(...clock.advance(1/60));
  assert.equal(frames.flatMap(f=>f.drops).length,0);
  clock.queue({x:0.2,y:0.6,radius:0.02,strength:0.03,source:'click'});
  assert.equal(clock.advance(1/60)[0].drops[0].source,'click');
  clock.setAutomatic(true);
  const resumed=[];
  for(let i=0;i<90;i++)resumed.push(...clock.advance(1/60));
  assert.equal(resumed.flatMap(f=>f.drops).filter(d=>d.source==='auto').length,1);
});

// A wide or tall viewport must not stretch circles or invert click locations.
test('screen coordinates share one square physical domain in landscape and portrait',()=>{
  const wide={left:10,top:20,width:1280,height:720};
  assert.deepEqual(pointToField(650,380,wide),{x:0.5,y:0.5});
  const dx=pointToField(770,380,wide),dy=pointToField(650,500,wide);
  approx(dx.x-0.5,0.09375);approx(0.5-dy.y,0.09375);
  assert.deepEqual(pointToField(10,20,{left:10,top:20,width:360,height:720}),{x:0.25,y:1});
});
