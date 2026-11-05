import test from 'node:test';
import assert from 'node:assert/strict';
import { Vector2 } from '../../public/landing/seal/vendor/three/build/three.module.js';
import { createParticleField } from '../../public/landing/seal/particle-field.mjs';
const options={count:1000,reveal:{value:0},aspect:{value:1},resolution:{value:new Vector2(800,800)},coverGLSL:'float paperAt(vec2 uv,float p,float a){return p;}'};
test('dense particle mass is deterministic, bounded and avoids an edge-heavy wire box',()=>{
  const a=createParticleField(options),b=createParticleField(options);
  const g=a.core.geometry;
  assert.deepEqual(g.getAttribute('position').array,b.core.geometry.getAttribute('position').array);
  const layers=Array.from(g.getAttribute('aLayer').array);
  assert.equal(layers.filter(v=>v===0).length,580);
  assert.equal(layers.filter(v=>v===1).length,40);
  assert.equal(layers.filter(v=>v===2).length,380);
  assert.ok(Array.from(g.getAttribute('aWhite').array).filter(Boolean).length<=40);
  for(let i=0;i<1000;i++){
    const p=g.getAttribute('position');
    assert.ok(Math.abs(p.getX(i))<=.5&&Math.abs(p.getY(i))<=.9+1e-7&&Math.abs(p.getZ(i))<=.5);
  }
});
