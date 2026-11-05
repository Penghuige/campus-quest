import test from 'node:test';
import assert from 'node:assert/strict';
const api=await import('../../public/landing/seal/orbit-presentation.mjs').catch(()=>({}));
const vp={width:1366,height:768};
function frame(s,v=vp){assert.equal(typeof api.orbitCardsFrame,'function');return api.orbitCardsFrame(s,v);}

// A modulo-only primary check would repeat the first card; every card gets one run.
test('five main reading windows occur once in the approved order',()=>{
 const order=[];let last=-1;
 for(let n=0;n<=5200;n++){
  const cards=frame(n/1000).cards,primary=cards.filter(c=>c.primary);
  assert.ok(primary.length<=1);
  if(primary.length&&primary[0].index!==last){last=primary[0].index;order.push(last);}
 }
 assert.deepEqual(order,[0,1,2,3,4]);
 for(const s of [0,.39,5.2,5.3,6.8,7.6])assert.ok(frame(s).cards.every(c=>c.opacity===0));
});

test('reverse and skipped scroll samples reproduce absolute poses without a clock',()=>{
 const samples=[.4,.7,1.3,2.2,3.9,4.9,5.2],saved=samples.map(s=>frame(s));
 for(let i=samples.length-1;i>=0;i--)assert.deepEqual(frame(samples[i]),saved[i]);
 assert.deepEqual(frame(2.2),saved[3]);
});

test('reading slows near the front and hands over continuously at slot boundaries',()=>{
 const start=.4,slot=.96;
 for(let i=0;i<5;i++){
  const a=frame(start+i*slot+.10*slot).cards[i],b=frame(start+i*slot+.50*slot).cards[i];
  assert.ok(a.primary&&b.primary);assert.ok(Math.abs(a.yaw)<3&&Math.abs(b.yaw)<3);
  assert.ok(a.scale>.99&&b.scale>.99);
 }
 for(const s of [.4,.976,1.36,2.32,3.28,4.24,4.816,5.2]){
  const a=frame(s-1e-7),b=frame(s+1e-7);
  for(let i=0;i<5;i++)for(const key of ['x','y','scale','yaw','opacity'])
   assert.ok(Math.abs(a.cards[i][key]-b.cards[i][key])<.002,`${s} ${i} ${key}`);
 }
});

test('visible cards fit viewport and leave sixteen pixels above the real SCROLL cue',()=>{
 for(const [width,height] of [[320,568],[390,844],[844,390],[1024,768],[1366,768],[1920,1080]]){
  for(let n=0;n<=520;n++)for(const c of frame(n/100,{width,height}).cards){
   for(const key of ['x','y','scale','yaw','depth','opacity','zIndex'])assert.ok(Number.isFinite(c[key]));
   if(c.opacity<=.1)continue;
   assert.ok(c.x-114*c.scale>=16&&c.x+114*c.scale<=width-16);
   assert.ok(c.y-52*c.scale>=16);
   assert.ok(c.y+52*c.scale<=height-111+1e-6);
  }
 }
});

test('desktop handovers do not overlap two strongly readable cards',()=>{
 for(let n=40;n<520;n++){
  const cards=frame(n/100).cards.filter(c=>c.opacity>.75);
  for(let i=0;i<cards.length;i++)for(let j=i+1;j<cards.length;j++){
   const a=cards[i],b=cards[j];
   assert.ok(Math.abs(a.x-b.x)>114*(a.scale+b.scale)||Math.abs(a.y-b.y)>52*(a.scale+b.scale));
  }
 }
});

test('narrow and short screens relay centrally without superimposed sentences',()=>{
 for(const v of [{width:390,height:844},{width:844,height:390},{width:1366,height:500}]){
  for(let n=40;n<520;n++){
   const f=frame(n/100,v);assert.equal(f.mode,'relay');
   assert.ok(f.cards.filter(c=>c.opacity>.05).length<=1);
   for(const c of f.cards.filter(c=>c.primary))assert.equal(c.x,v.width/2);
  }
 }
 assert.equal(frame(.7).mode,'orbit');
});

test('title leaves gently as cards enter and does not survive into ascent',()=>{
 assert.equal(frame(0).title.opacity,1);
 assert.ok(frame(.56).title.opacity>0&&frame(.56).title.opacity<1);
 assert.equal(frame(.72).title.opacity,0);
 assert.equal(frame(5.2).title.opacity,0);
});

test('host lifecycle and terminal modes select hidden or readable static information',()=>{
 assert.equal(typeof api.orbitMode,'function');
 const base={active:true,ready:true,reducedMotion:false,fallback:false};
 for(const state of ['BEFORE','APPROACH'])assert.equal(api.orbitMode({...base,state}),'animated');
 for(const state of ['AUTO','DONE'])assert.equal(api.orbitMode({...base,state}),'hidden');
 assert.equal(api.orbitMode({...base,state:'STATIC'}),'static');
 assert.equal(api.orbitMode({...base,state:'BEFORE',reducedMotion:true}),'static');
 assert.equal(api.orbitMode({...base,state:'BEFORE',fallback:true}),'static');
 assert.equal(api.orbitMode({...base,state:'STATIC',active:false}),'hidden');
 assert.equal(api.orbitMode({...base,state:'BEFORE',ready:false}),'hidden');
});
