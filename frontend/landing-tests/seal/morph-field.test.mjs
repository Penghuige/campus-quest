import test from 'node:test';
import assert from 'node:assert/strict';
import { Vector2 } from '../../public/landing/seal/vendor/three/build/three.module.js';
import { createParticleField } from '../../public/landing/seal/particle-field.mjs';

const count = 256;
const positions = (change = false) => Float32Array.from({ length: count * 3 }, (_, offset) => {
  const i = Math.floor(offset / 3);
  return change ? [-100 - i * 3, 50 + i, 80 + i * 4][offset % 3]
    : [100 + i, 20 + i * 2, -50 - i][offset % 3];
});
const options = () => ({ count, reveal: { value: 0 }, aspect: { value: 1 },
  resolution: { value: new Vector2(800, 800) },
  coverGLSL: 'float paperAt(vec2 uv,float progress,float aspect){return progress;}' });
function dispose(field) {
  field.core.geometry.dispose();
  field.glow.geometry.dispose();
  field.core.material.dispose();
  field.glow.material.dispose();
  field.reflection?.material.dispose();
}

// Catches resize updates that change the main tree while leaving rare halos/reflection behind.
test('tree coordinate updates reach the main field, reflection and the existing glow subset', () => {
  const initial = positions();
  const field = createParticleField({ ...options(), treePositions: initial });
  assert.ok(field.core.geometry.getAttribute('aTreePosition'), 'main field must accept tree world coordinates');
  assert.ok(field.reflection?.isPoints, 'tree field must provide the mirrored points object');
  assert.equal(typeof field.setTreePositions, 'function');
  const attribute = field.core.geometry.getAttribute('aTreePosition');
  const glowAttribute = field.glow.geometry.getAttribute('aTreePosition');
  assert.deepEqual(attribute.array, initial);
  assert.ok(glowAttribute.count > 0, 'fixture must exercise the sparse glow mapping');
  const glowSources = Array.from({ length: glowAttribute.count }, (_, i) => glowAttribute.getX(i) - 100);
  const replacement = positions(true);
  const previousVersion = attribute.version;
  field.setTreePositions(replacement);
  assert.equal(field.core.geometry.getAttribute('aTreePosition'), attribute, 'resize must retain the live GPU attribute');
  assert.ok(attribute.version > previousVersion, 'resize must mark the attribute for upload');
  assert.deepEqual(attribute.array, replacement);
  assert.equal(field.reflection.geometry.getAttribute('aTreePosition'), attribute);
  for (let i = 0; i < glowSources.length; i++) {
    assert.deepEqual(Array.from(glowAttribute.array.subarray(i * 3, i * 3 + 3)),
      Array.from(replacement.subarray(glowSources[i] * 3, glowSources[i] * 3 + 3)));
  }
  dispose(field);
});

// Catches independently maintained reflection shape programs or cloned animation uniforms.
test('reflection uses the same source geometry, vertex program and live animation uniforms', () => {
  const time = { value: 3 };
  const morph = { value: .4 };
  const waterY = { value: -.7 };
  const spin = { value: Math.PI / 2 };
  const treePointScale = { value: .63 };
  const field = createParticleField({ ...options(), treePositions: positions(), time, morph, waterY, spin, treePointScale });
  assert.ok(field.reflection?.isPoints, 'reflection must exist');
  assert.equal(field.reflection.geometry, field.core.geometry);
  assert.equal(field.reflection.material.vertexShader, field.material.vertexShader);
  assert.equal(field.reflection.material.defines.PARTICLE_REFLECTION, 1);
  for (const material of [field.material, field.glow.material, field.reflection.material]) {
    assert.equal(material.uniforms.uTime, time);
    assert.equal(material.uniforms.uMorph, morph);
    assert.equal(material.uniforms.uWaterY, waterY);
    assert.ok(material.uniforms.uReflectionMatrix, 'layers must receive shared plane reflection');
    assert.equal(material.uniforms.uReflectionMatrix, field.uniforms.uReflectionMatrix);
    assert.equal(material.uniforms.uCameraWorld, field.uniforms.uCameraWorld);
    assert.equal(material.uniforms.uTreePointScale, treePointScale, 'all layers must receive the live tree pixel scale');
  }
  assert.equal(field.reflection.frustumCulled, false);
  dispose(field);
});

// Catches an incomplete coordinate upload that poisons only part of a live field.
test('invalid tree resize coordinates leave all prior coordinates intact', () => {
  const field = createParticleField({ ...options(), treePositions: positions() });
  assert.equal(typeof field.setTreePositions, 'function');
  const before = field.core.geometry.getAttribute('aTreePosition').array.slice();
  const glowBefore = field.glow.geometry.getAttribute('aTreePosition').array.slice();
  assert.throws(() => field.setTreePositions(new Float32Array(3)), RangeError);
  const invalid = positions(true);
  invalid[50] = NaN;
  assert.throws(() => field.setTreePositions(invalid), TypeError);
  assert.deepEqual(field.core.geometry.getAttribute('aTreePosition').array, before);
  assert.deepEqual(field.glow.geometry.getAttribute('aTreePosition').array, glowBefore);
  dispose(field);
});

test('the seal-only interface defaults to the unchanged seal endpoint', () => {
  const field = createParticleField(options());
  assert.ok(field.uniforms.uMorph, 'morph uniform must exist');
  assert.equal(field.uniforms.uMorph.value, 1);
  assert.equal(field.uniforms.uTime.value, 0);
  assert.ok(field.uniforms.uReflectionMatrix);
  assert.ok(field.uniforms.uTreePointScale, 'omitting tree pixel scale must retain desktop sizes');
  assert.equal(field.uniforms.uTreePointScale.value, 1);
  dispose(field);
});
