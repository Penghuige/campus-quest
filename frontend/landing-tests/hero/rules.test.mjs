import test from 'node:test';
import assert from 'node:assert/strict';
let rules={};
try{rules=await import('../../public/landing/hero/rules.mjs');}catch(error){if(error.code!=='ERR_MODULE_NOT_FOUND')throw error;}
const ready={width:1280,canHover:true,finePointer:true,reducedMotion:false,webglAvailable:true,imageStatus:'ready'};
test('a ready fine-pointer desktop receives animated painting',()=>{
 assert.equal(typeof rules.resolveMode,'function','hero mode rules are not implemented');
 assert.equal(rules.resolveMode(ready),'animated');
});
for(const [name,changes] of Object.entries({narrow:{width:767},noHover:{canHover:false},coarse:{finePointer:false},reduced:{reducedMotion:true},noGL:{webglAvailable:false}})){
 test(`${name} receives a static image instead of painting`,()=>{
  assert.equal(typeof rules.resolveMode,'function','hero mode rules are not implemented');
  assert.equal(rules.resolveMode({...ready,...changes}),'static');
 });
}
test('768px is eligible for desktop mode while image loading and error remain readable states',()=>{
 assert.equal(typeof rules.resolveMode,'function','hero mode rules are not implemented');
 assert.equal(rules.resolveMode({...ready,width:768}),'animated');
 assert.equal(rules.resolveMode({...ready,imageStatus:'loading'}),'loading');
 assert.equal(rules.resolveMode({...ready,imageStatus:'error'}),'fallback');
});
test('desktop bottom cover preserves the bottom edge of the source image',()=>{
 assert.equal(typeof rules.imageCrop,'function','hero crop is not implemented');
 assert.deepEqual(rules.imageCrop({width:1280,height:720,imageWidth:1536,imageHeight:1024,mobile:false}),{sx:0,sy:160,sw:1536,sh:864});
});
test('phone axis framing stays inside the image and keeps stone lettering outside the frame',()=>{
 assert.equal(typeof rules.imageCrop,'function','hero crop is not implemented');
 const m=rules.imageCrop({width:375,height:812,imageWidth:1536,imageHeight:1024,mobile:true});
 assert.ok(Math.abs(m.sy-1024/6)<1e-6 && m.sy+m.sh<875);
 assert.ok(m.sx>=0 && m.sx+m.sw<=1536);
 assert.ok(Math.abs(m.sw/m.sh-375/812)<1e-9);
});
test('the approved slower decay doubles retention and stays refresh independent',()=>{
 assert.equal(typeof rules.fadeFactor,'function','hero decay is not implemented');
 assert.ok(Math.abs(rules.fadeFactor(.5)**2-rules.fadeFactor(1))<1e-12);
 assert.ok(Math.abs(rules.fadeFactor(1/120)**120-rules.fadeFactor(1/60)**60)<1e-12);
 // Q11: stretch the original retention curve to twice its duration.
 assert.ok(rules.fadeFactor(2)>.25 && rules.fadeFactor(2)<.28,'two seconds now retains the original one-second ink level');
 assert.ok(rules.fadeFactor(4)>.06 && rules.fadeFactor(4)<.08,'four seconds now retains the original two-second ink level');
 assert.ok(rules.fadeFactor(8)<.01,'the trail still returns to paper');
 assert.equal(rules.fadeFactor(-1),1);
});
test('configured login resolves ordinary navigation and rejects executable schemes',()=>{
 assert.equal(typeof rules.resolveLoginTarget,'function','login navigation is not implemented');
 assert.equal(rules.resolveLoginTarget('/campus/login','https://example.test/about'),'https://example.test/campus/login');
 assert.equal(rules.resolveLoginTarget('https://app.example.test/login','https://example.test/'),'https://app.example.test/login');
 for(const value of [null,'','javascript:alert(1)','data:text/html,hello','http://%'])assert.equal(rules.resolveLoginTarget(value,'https://example.test/'),null);
});
