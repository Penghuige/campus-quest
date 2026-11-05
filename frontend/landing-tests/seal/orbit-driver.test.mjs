import test from 'node:test';
import assert from 'node:assert/strict';
import {JourneyDriver} from '../../public/landing/seal/journey-driver.mjs';
import {orbitCardsFrame,orbitMode} from '../../public/landing/seal/orbit-presentation.mjs';
import * as orbitAPI from '../../public/landing/seal/orbit-presentation.mjs';
const layout={width:1,waterY:-1.6,hoverGap:.7,startDistance:12,viewDistance:8,viewport:{width:1366,height:800}};
function fixture(){
 const frames=[];const driver=new JourneyDriver({layout,onFrame:f=>frames.push(f)});
 return {driver,frames};
}

test('cards follow the displayed tree progress rather than a raw scroll jump',()=>{
 const {driver,frames}=fixture();driver.scroll(2000);driver.tick(.05);
 const f=frames.at(-1);
 assert.equal(typeof f.screenProgress,'number');
 assert.equal(f.screenProgress,driver.snapshot().displayY/800);
 assert.ok(f.screenProgress<1);
 assert.equal(orbitCardsFrame(f.screenProgress,layout.viewport).cards.find(c=>c.primary)?.index,0);
 for(let n=0;n<100;n++)driver.tick(.05);
 assert.equal(orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport).cards.find(c=>c.primary)?.index,2);
 const stable=orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport);
 driver.tick(2);
 assert.deepEqual(orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport),stable);
});

test('contact hides cards, cancellation restores exact poses and static mode retains information',()=>{
 const {driver,frames}=fixture();driver.scroll(2000,{snap:true});
 assert.equal(typeof frames.at(-1).screenProgress,'number');
 const saved=orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport);
 driver.scroll(6100,{snap:true});
 assert.equal(orbitMode({...frames.at(-1),active:true,ready:true}),'hidden');
 const token=driver.snapshot().token;
 driver.scroll(2000,{snap:true});
 assert.equal(driver.complete(token),false);
 assert.deepEqual(orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport),saved);
 driver.setReducedMotion(true);
 assert.equal(orbitMode({...frames.at(-1),active:true,ready:true}),'static');
 driver.setReducedMotion(false);
 assert.deepEqual(orbitCardsFrame(frames.at(-1).screenProgress,layout.viewport),saved);
});

test('height changes preserve the currently displayed reading card',()=>{
 assert.equal(typeof orbitAPI.resizedJourneyY,'function');
 const {driver,frames}=fixture();driver.scroll(1318.4,{snap:true});
 const next={...layout,viewport:{width:844,height:390}};
 const y=orbitAPI.resizedJourneyY({...driver.snapshot(),previousHeight:800,nextHeight:390});
 driver.refresh(next,y);
 assert.equal(orbitCardsFrame(frames.at(-1).screenProgress,next.viewport).cards.find(c=>c.primary)?.index,1);
 assert.ok(Math.abs(y-642.72)<1e-9);
});

test('same-height refreshes and automatic playback keep existing absolute scroll semantics',()=>{
 assert.equal(typeof orbitAPI.resizedJourneyY,'function');
 for(const state of ['AUTO','DONE','STATIC'])assert.equal(orbitAPI.resizedJourneyY({state,actualY:6100,displayY:6080,previousHeight:800,nextHeight:700}),6100);
 assert.equal(orbitAPI.resizedJourneyY({state:'BEFORE',actualY:2000,displayY:1500,previousHeight:800,nextHeight:800}),2000);
});
