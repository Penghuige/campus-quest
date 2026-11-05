import { makeReflectionMatrix } from './reflection.mjs';
import { SEAL_HEIGHT,SEAL_HALF_HEIGHT } from './seal-dimensions.mjs';
import * as THREE from './vendor/three/build/three.module.js';

const vertexShader = /* glsl */`
  attribute vec3 aNormal;
  attribute float aLayer;
  attribute float aSize;
  attribute float aWhite;
  attribute float aTone;
  attribute vec3 aTreePosition;
  attribute float aPhase;
  uniform float uDpr;
  uniform float uTighten;
  uniform float uTime;
  uniform float uMorph;
  uniform float uWaterY;
  uniform mat4 uReflectionMatrix;
  uniform vec3 uCameraWorld;
  uniform float uTreePointScale;
  varying float vLayer;
  varying float vWhite;
  varying float vShade;
  varying float vVisibility;
  varying float vTone;
  varying float vMorph;
  varying float vDepth;
  varying float vPulse;
  varying float vReflectionFade;
  varying float vGlint;

  vec3 sourceWorldPosition(float morph) {
    vec3 tree = aTreePosition;
    float height = max(0.,tree.y-uWaterY);
    tree.x += sin(uTime*.75+tree.y*1.65+aPhase)*(.025+smoothstep(2.5,7.,height)*.14)*(1.-morph);
    tree.z += sin(uTime*.55+aPhase)*.025*(1.-morph);
    return (modelMatrix*vec4(mix(tree,position,morph),1.)).xyz;
  }

  void main() {
    float morph = smoothstep(0., 1., clamp(uMorph, 0., 1.));
    vec3 world = sourceWorldPosition(morph);
    vMorph = morph;
    vPulse = mix(.62 + .38 * sin(aPhase + uTime * 1.4), 1., morph);
    vReflectionFade = 1.;
    vGlint = .55 + .45 * sin(aPhase + uTime * .62);
    #ifdef PARTICLE_REFLECTION
      float sourceHeight = max(0., world.y - uWaterY);
      world = (uReflectionMatrix * vec4(world,1.)).xyz;
      float breakup = sin(sourceHeight * 7.5 + world.x * 1.7 - uTime * 1.4 + aPhase)
        * (.025 + sourceHeight * .018) * mix(1., 1.55, morph);
      world.x += breakup + sin(world.z * 3. + uTime * .55) * sourceHeight * .012;
      world.z += cos(world.x * 2.2 - uTime * .38 + aPhase) * sourceHeight * .008;
      float distanceFade = exp(-sourceHeight * .22) * (1. - smoothstep(8., 11., sourceHeight));
      float bands = .38 + .62 * pow(.5 + .5 * sin(sourceHeight * 8. - uTime * 1.3 + aPhase * .15), 3.);
      vReflectionFade = distanceFade * bands;
    #endif
    vec4 mv = viewMatrix * vec4(world, 1.);
    vDepth = clamp((-mv.z - 8.) / 14., 0., 1.);
    vec3 normal = normalize(mat3(modelMatrix) * aNormal);
    vec3 viewDirection = normalize(uCameraWorld-world);
    float facing = smoothstep(-.18, .36, dot(normal, viewDirection));
    vec3 keyLight = normalize(vec3(-.45, .75, .8));
    vec3 fillLight = normalize(vec3(.7, -.18, .48));
    vShade = .42 + .44 * max(0., dot(normal, keyLight))
      + .14 * max(0., dot(normal, fillLight));
    vVisibility = mix(.38, 1., facing);
    vLayer = aLayer;
    vWhite = aWhite;
    vTone = aTone;
    float sealSize = aSize * 1.35 * uDpr * (12. / max(.1, -mv.z));
    sealSize *= 1. - .1 * clamp(uTighten, 0., 1.);
    float treeSize = aSize * 1.4 * mix(1.28, .72, vDepth) * uDpr * uTreePointScale * (27. / max(.1, -mv.z));
    float size = mix(treeSize, sealSize, morph);
    #ifdef PARTICLE_REFLECTION
      size = aSize * mix(1.1, 1.416, morph) * (.85 + vGlint * .2)
        * uDpr * mix(uTreePointScale, 1., morph) * (26. / max(.1, -mv.z));
      gl_PointSize = clamp(size, 1., 18.);
    #elif defined(PARTICLE_GLOW)
      size *= mix(2.2, 3.9, morph);
      gl_PointSize = clamp(size, 1., 24.);
    #else
      gl_PointSize = clamp(size, 1., 14.);
    #endif
    gl_Position = projectionMatrix * mv;
  }
`;

const fragmentPreamble = /* glsl */`
  uniform float uPaperMix;
  uniform float uAspect;
  uniform vec2 uResolution;
  uniform vec3 uJade;
  uniform vec3 uPaperJade;
  uniform vec3 uWhite;
  uniform vec3 uGlowColor;
  uniform vec3 uTreeNear;
  uniform vec3 uTreeFar;
  uniform vec3 uTreeWhite;
  uniform float uTreeGain;
  varying float vLayer;
  varying float vWhite;
  varying float vShade;
  varying float vVisibility;
  varying float vTone;
  varying float vMorph;
  varying float vDepth;
  varying float vPulse;
  varying float vReflectionFade;
  varying float vGlint;
`;

/** Fixed positions remain unchanged between approach, contact and replay. */
export function createParticleField({ count, reveal, aspect, resolution, coverGLSL,
  treePositions, time = { value: 0 }, morph = { value: 1 }, waterY = { value: -.58 }, reflectionMatrix = { value: makeReflectionMatrix(waterY.value) }, cameraWorld = { value: new THREE.Vector3(0,0,12) },
  treePointScale = { value: 1 } }) {
  if (!Number.isInteger(count) || count < 1) throw new RangeError('count must be a positive integer');
  if (typeof coverGLSL !== 'string' || !coverGLSL.trim()) throw new TypeError('coverGLSL must define paperAt');

  let seed = 46217;
  const random = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const surfaceCount = Math.floor(count * .58);
  const edgeCount = Math.floor(count * .04);
  const faceWeights = [1, 1, SEAL_HEIGHT, SEAL_HEIGHT, SEAL_HEIGHT, SEAL_HEIGHT];
  const faceArea=faceWeights.reduce((sum,v)=>sum+v,0);
  const halfExtents = [.5, SEAL_HALF_HEIGHT, .5];
  const positions = new Float32Array(count * 3);
  const normals = new Float32Array(count * 3);
  const layers = new Float32Array(count);
  const sizes = new Float32Array(count);
  const whites = new Float32Array(count);
  const tones = new Float32Array(count);
  const phases = new Float32Array(count);
  const glowIndices = [];

  for (let i = 0; i < count; i++) {
    const point = [(random() - .5), (random() - .5) * SEAL_HEIGHT, (random() - .5)];
    const normal = [0, 0, 0];
    let layer;
    if (i < surfaceCount) {
      layer = 0;
      let pick = random() * faceArea;
      let face = 0;
      while (face < 5 && pick > faceWeights[face]) pick -= faceWeights[face++];
      const axis = face < 2 ? 1 : face < 4 ? 0 : 2;
      const sign = face % 2 === 0 ? -1 : 1;
      point[axis] = sign * halfExtents[axis];
      normal[axis] = sign;
    } else if (i < surfaceCount + edgeCount) {
      layer = 1;
      // Edges parallel to the long axis receive proportionately more samples.
      const direction = random() * (2+SEAL_HEIGHT);
      const freeAxis = direction < 1 ? 0 : direction < 1+SEAL_HEIGHT ? 1 : 2;
      for (let axis = 0; axis < 3; axis++) {
        if (axis === freeAxis) continue;
        const sign = random() < .5 ? -1 : 1;
        point[axis] = sign * halfExtents[axis];
        normal[axis] = sign * Math.SQRT1_2;
      }
    } else {
      layer = 2;
      const length = Math.hypot(point[0] / .5, point[1] / SEAL_HALF_HEIGHT, point[2] / .5) || 1;
      for (let axis = 0; axis < 3; axis++) normal[axis] = point[axis] / halfExtents[axis] / length;
    }
    positions.set(point, i * 3);
    normals.set(normal, i * 3);
    layers[i] = layer;
    sizes[i] = (layer === 2 ? 1.0 : 1.25) + random() ** 3 * 1.1;
    whites[i] = i % 25 === 7 && i < Math.floor(count*.04)*25 ? 1 : 0;
    tones[i] = .86 + random() * .28;
    phases[i] = (Math.imul(i + 1, 0x9e3779b1) >>> 0) / 4294967296 * 20;
    if (whites[i]) glowIndices.push(i);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('aNormal', new THREE.BufferAttribute(normals, 3));
  geometry.setAttribute('aLayer', new THREE.BufferAttribute(layers, 1));
  geometry.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
  geometry.setAttribute('aWhite', new THREE.BufferAttribute(whites, 1));
  geometry.setAttribute('aTone', new THREE.BufferAttribute(tones, 1));
  geometry.setAttribute('aPhase', new THREE.BufferAttribute(phases, 1));
  const treeAttribute = new THREE.BufferAttribute(new Float32Array(positions), 3);
  geometry.setAttribute('aTreePosition', treeAttribute);

  const uniforms = {
    uDpr: { value: 1 },
    uTighten: { value: 0 },
    uPaperMix: reveal,
    uAspect: aspect,
    uResolution: resolution,
    uTime: time,
    uMorph: morph,
    uWaterY: waterY,
    uReflectionMatrix: reflectionMatrix,
    uCameraWorld: cameraWorld,
    uTreePointScale: treePointScale,
    uJade: { value: new THREE.Color('#8DCCB2') },
    uPaperJade: { value: new THREE.Color('#3E7C63') },
    uWhite: { value: new THREE.Color('#EFF5E4') },
    uGlowColor: { value: new THREE.Color('#91E8CC') },
    uTreeNear: { value: new THREE.Color('#1FD1FF') },
    uTreeFar: { value: new THREE.Color('#0529B8') },
    uTreeWhite: { value: new THREE.Color('#9EF7FF') },
    uTreeGain: { value: 1.3 }
  };
  const shaderPrefix = `${fragmentPreamble}\n${coverGLSL}\n`;
  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader,
    fragmentShader: shaderPrefix + /* glsl */`
      void main() {
        float r = length(gl_PointCoord - .5) * 2.;
        if (r > 1.) discard;
        vec2 uv = gl_FragCoord.xy / max(uResolution, vec2(1.));
        float paper = clamp(paperAt(uv, uPaperMix, uAspect), 0., 1.);
        float volume = step(1.5, vLayer);
        float edge = step(.5, vLayer) - volume;
        float visibility = mix(vVisibility, .72, volume);
        float kernel = pow(max(0., 1. - r), .7);
        kernel = max(kernel, exp(-r * r * 17.) * .76);
        float alpha = kernel * .94 * visibility;
        // Dense edges should describe the shape without becoming solid wire lines.
        alpha *= mix(1., .62, edge) * mix(1., 1.12, paper);
        vec3 jade = mix(uJade, uPaperJade, paper);
        jade *= mix(vShade, .7, volume) * vTone * mix(1.65, 1., paper);
        vec3 color = mix(jade, uWhite * vTone, vWhite);
        float treeCore = 1. - smoothstep(0., .38, r);
        float treeHalo = 1. - smoothstep(.16, 1., r);
        vec3 treeColor = mix(uTreeNear, uTreeFar, vDepth);
        treeColor = mix(treeColor, uTreeWhite, treeCore * .55) * uTreeGain;
        float treeAlpha = (treeCore + treeHalo * .38) * vPulse * mix(1., .48, vDepth);
        gl_FragColor = vec4(mix(treeColor, color, vMorph), mix(treeAlpha, alpha, vMorph));
        #include <colorspace_fragment>
      }
    `,
    transparent: true,
    depthTest: false,
    depthWrite: false,
    blending: THREE.NormalBlending,
    toneMapped: false
  });
  const core = new THREE.Points(geometry, material);
  core.frustumCulled = false;
  core.renderOrder = 40;

  // Only the existing bright-point subset receives a halo; no new positions or RT.
  const glowGeometry = new THREE.BufferGeometry();
  for (const [name, attribute] of Object.entries(geometry.attributes)) {
    const array = new Float32Array(glowIndices.length * attribute.itemSize);
    for (let i = 0; i < glowIndices.length; i++) {
      const start = glowIndices[i] * attribute.itemSize;
      array.set(attribute.array.subarray(start, start + attribute.itemSize), i * attribute.itemSize);
    }
    glowGeometry.setAttribute(name, new THREE.BufferAttribute(array, attribute.itemSize));
  }
  const glowMaterial = new THREE.ShaderMaterial({
    uniforms,
    defines: { PARTICLE_GLOW: 1 },
    vertexShader,
    fragmentShader: shaderPrefix + /* glsl */`
      void main() {
        float r = length(gl_PointCoord - .5) * 2.;
        if (r > 1.) discard;
        vec2 uv = gl_FragCoord.xy / max(uResolution, vec2(1.));
        float paper = clamp(paperAt(uv, uPaperMix, uAspect), 0., 1.);
        float halo = exp(-r * r * 5.5) * (1. - smoothstep(.72, 1., r));
        float sealAlpha = halo * .25 * vVisibility * mix(1., .025, paper);
        float treeAlpha = halo * .24 * vPulse * mix(1., .48, vDepth);
        gl_FragColor = vec4(mix(uTreeNear, uGlowColor, vMorph), mix(treeAlpha, sealAlpha, vMorph));
        #include <colorspace_fragment>
      }
    `,
    transparent: true,
    depthTest: false,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    toneMapped: false
  });
  const glow = new THREE.Points(glowGeometry, glowMaterial);
  glow.frustumCulled = false;
  glow.renderOrder = 41;

  const reflectionMaterial = new THREE.ShaderMaterial({
    uniforms,
    defines: { PARTICLE_REFLECTION: 1 },
    vertexShader,
    fragmentShader: shaderPrefix + /* glsl */`
      void main() {
        float r = length(gl_PointCoord - .5) * 2.;
        if (r > 1.) discard;
        float pointCore = 1. - smoothstep(0., mix(.4, .22, vMorph), r);
        float halo = 1. - smoothstep(mix(.11, .03, vMorph), 1., r);
        vec3 treeColor = mix(uTreeFar, uTreeNear, clamp(pointCore + vGlint * .2, 0., 1.));
        vec3 sealColor = mix(uPaperJade, uJade, halo);
        float alpha = (pointCore * mix(1., .28, vMorph) + halo * mix(.38, .82, vMorph))
          * vReflectionFade * mix(.5, .27, vMorph);
        gl_FragColor = vec4(mix(treeColor, sealColor, vMorph), alpha);
        #include <colorspace_fragment>
      }
    `,
    transparent: true,
    depthTest: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    toneMapped: false
  });
  const reflection = new THREE.Points(geometry, reflectionMaterial);
  reflection.frustumCulled = false;
  reflection.renderOrder = -1;

  const glowTreeAttribute = glowGeometry.getAttribute('aTreePosition');
  function setTreePositions(array) {
    if (!(array instanceof Float32Array)) throw new TypeError('tree positions must be a Float32Array');
    if (array.length !== count * 3) throw new RangeError('tree positions must contain count xyz triples');
    for (const value of array) {
      if (!Number.isFinite(value)) throw new TypeError('tree positions must be finite');
    }
    treeAttribute.array.set(array);
    treeAttribute.needsUpdate = true;
    for (let i = 0; i < glowIndices.length; i++) {
      const start = glowIndices[i] * 3;
      glowTreeAttribute.array.set(array.subarray(start, start + 3), i * 3);
    }
    glowTreeAttribute.needsUpdate = true;
  }
  if (treePositions !== undefined) setTreePositions(treePositions);
  // Parent all three Points to the same pose group so their modelMatrix is identical.
  return { core, glow, reflection, material, uniforms, setTreePositions };
}
