import * as THREE from './vendor/three/build/three.module.js';

// The original tree's ellipsoids and tapered branch segments, with their
// original relative particle counts. No cube target or lettering is sampled.
const canopies = [
  [-2.5, 5.3, 0, 2.8, 1.7, 2.75],
  [-0.8, 6.35, 0.15, 2.7, 1.8, 2.8],
  [1.35, 6.2, -0.1, 2.8, 1.8, 2.85],
  [3, 5.1, 0.1, 2.45, 1.55, 2.4],
  [0, 4.85, 0, 3.9, 1.6, 3.2],
  [-3.7, 4.5, 0.2, 1.8, 1.25, 1.9],
  [0, 5.45, 1.75, 2.55, 1.6, 2.1],
  [0.25, 5.3, -1.8, 2.65, 1.55, 2.15],
];
const branchTips = [[-3.3, 5.3], [-1.7, 6], [-0.4, 6.5], [1.8, 6.2], [3.4, 5.25]];
const components = canopies.map(shape => ({ shape, weight: 3500 }));
const branch = (start, end, width, weight) => components.push({ start, end, width, weight });
branch([0, 0, 0], [0, 3.8, 0], 1.7, 3600);
branchTips.forEach(([x, y], index) => {
  const fork = [x * 0.34, 3.5 + index % 2 * 0.35, (index - 2) * 0.18];
  branch([0, 1.7, 0], fork, 0.8, 850);
  branch(fork, [x, y, (index % 2 - 0.5) * 0.6], 0.48, 900);
});
for (const sign of [-1, 1]) {
  for (let index = 0; index < 4; index++) {
    const start = [(index - 1.5) * 0.34, 2.5 + index * 0.27, sign * 0.18];
    const fork = [(index - 1.5) * 0.62, 3.75 + index * 0.31, sign * (1.25 + index * 0.32)];
    const end = [(index - 1.5) * 1.02, 5.15 + index * 0.28, sign * (3.05 + index % 2 * 0.55)];
    branch(start, fork, 0.58, 620);
    branch(fork, end, 0.34, 560);
    branch(end, [end[0] + (index % 2 ? -0.55 : 0.55), end[1] + 0.48, end[2] + sign * 0.48], 0.16, 260);
  }
}
branchTips.forEach(([x, y], index) => {
  for (let twig = 0; twig < 3; twig++) {
    const start = [x * 0.62, y - 0.72 + twig * 0.18, (twig - 1) * 0.32];
    const end = [x + (twig - 1) * 0.72, y + 0.25 + twig * 0.36, (twig - 1) * 0.85 + (index - 2) * 0.12];
    branch(start, end, 0.24, 430);
    branch(end, [end[0] + (twig - 1) * 0.42, end[1] + 0.42, end[2] + (1 - twig) * 0.26], 0.13, 230);
  }
});
const totalWeight = components.reduce((sum, component) => sum + component.weight, 0);

function randomSource(seed) {
  return () => {
    seed |= 0;
    seed = seed + 0x6D2B79F5 | 0;
    let value = Math.imul(seed ^ seed >>> 15, 1 | seed);
    value ^= value + Math.imul(value ^ value >>> 7, 61 | value);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
}

/** Original-space points become world-space (x, y - 3.2, z) * scale. */
export function treeLayout(aspect = 1.6) {
  const validAspect = Number.isFinite(aspect) && aspect > 0 ? aspect : 1.6;
  // Width fit includes the near side of the canopy at fov 24, camera z 12.
  const scale = 0.42 * Math.min(1, validAspect / 1.15);
  return { scale, waterY: (-0.66 - 3.2) * scale };
}

export function sampleTree(count, aspect = 1.6) {
  const points = new Float32Array(count * 3);
  const { scale } = treeLayout(aspect);
  const random = randomSource(0x48534C54);
  for (let index = 0; index < count; index++) {
    let selection = random() * totalWeight;
    let component = components[components.length - 1];
    for (const candidate of components) {
      selection -= candidate.weight;
      if (selection < 0) { component = candidate; break; }
    }
    let x, y, z;
    if (component.shape) {
      const [cx, cy, cz, rx, ry, rz] = component.shape;
      const vertical = random() * 2 - 1;
      const angle = random() * Math.PI * 2;
      const radius = Math.cbrt(random());
      const horizontal = Math.sqrt(1 - vertical * vertical);
      x = cx + Math.cos(angle) * horizontal * radius * rx;
      y = cy + vertical * radius * ry;
      z = cz + Math.sin(angle) * horizontal * radius * rz;
    } else {
      const t = random();
      const width = (1 - t) * component.width + 0.07;
      x = component.start[0] + (component.end[0] - component.start[0]) * t + (random() - 0.5) * width;
      y = component.start[1] + (component.end[1] - component.start[1]) * t + (random() - 0.5) * width;
      z = component.start[2] + (component.end[2] - component.start[2]) * t + (random() - 0.5) * width;
    }
    points[index * 3] = x * scale;
    points[index * 3 + 1] = (y - 3.2) * scale;
    points[index * 3 + 2] = z * scale;
  }
  return points;
}


const waveGLSL = `
float wave(vec2 p){float r=length(p);return sin(r*3.1-uTime*1.8)*.12*exp(-r*.065)+sin(p.x*1.35+uTime*.7)*.045+sin(p.y*1.8-uTime*.55)*.035;}
`;
function waterWave(x,z,t){const r=Math.hypot(x,z);return Math.sin(r*3.1-t*1.8)*.12*Math.exp(-r*.065)+Math.sin(x*1.35+t*.7)*.045+Math.sin(z*1.8-t*.55)*.035;}
const waterVertex = `
uniform float uTime,uOriginWave;
varying vec3 vP,vWorld;varying float vH;
${waveGLSL}
void main(){vec3 p=position;float h=wave(p.xz)-uOriginWave;p.y+=h;vP=p;vH=h;vec4 world=modelMatrix*vec4(p,1.);vWorld=world.xyz;gl_Position=projectionMatrix*viewMatrix*world;}
`;
const waterFragment = `
uniform float uTime,uOriginWave;uniform vec3 uCameraWorld;
varying vec3 vP,vWorld;varying float vH;
${waveGLSL}
void main(){
 // Evaluate the same continuous wave at the fragment, not an interpolated
 // vertex height, so an enlarged water grid cannot facet the specular rings.
 float e=.06,h=wave(vP.xz)-uOriginWave;
 float hx=wave(vP.xz+vec2(e,0.))-uOriginWave,hz=wave(vP.xz+vec2(0.,e))-uOriginWave;
 vec3 n=normalize(vec3(h-hx,e,h-hz)),l=normalize(vec3(-.35,1.,.25)),eye=normalize(uCameraWorld-vWorld);
 float spec=pow(max(dot(reflect(-l,n),eye),0.),36.);
 float w=.5+.5*sin(length(vP.xz)*4.-uTime*1.7),rings=pow(w,14.4),halo=pow(w,5.5),fade=exp(-length(vP.xz)*.1);
 float shimmer=pow(max(0.,sin(vP.x*2.8+uTime*.85)*cos(vP.z*4.2-uTime*.62)),18.)*fade;
 vec3 col=mix(vec3(.006,.028,.061),vec3(.022,.374,.792),rings*.5+spec);
 col+=vec3(.088,.605,1.)*(shimmer*.3+spec*.24+halo*fade*.045);
 gl_FragColor=vec4(col,.48+fade*.18+rings*.11+halo*fade*.025+shimmer*.13);
}
`;
const rippleVertex = `
attribute float aAngle,aBand,aJitter,aSize,aPhase;
uniform float uTime,uPixelRatio,uOriginWave,uRippleLift;
varying float vAlpha,vSpark;
${waveGLSL}
void main(){
 float cycle=mod(aBand*2.25+uTime*.48,13.5),r=.7+cycle+aJitter,angle=aAngle+sin(uTime*.12+aPhase)*.012;
 vec3 p=vec3(cos(angle)*r,-.66,sin(angle)*r);p.y+=wave(p.xz)-uOriginWave+uRippleLift;
 float inner=smoothstep(.3,1.6,cycle),outer=1.-smoothstep(9.5,13.5,cycle),flick=.68+.32*sin(aPhase+uTime*.75);
 vAlpha=inner*outer*flick;vSpark=pow(flick,3.);vec4 mv=modelViewMatrix*vec4(p,1.);gl_Position=projectionMatrix*mv;
 gl_PointSize=clamp(aSize*(1.+vSpark*.32)*uPixelRatio*(25./max(.1,-mv.z)),1.,20.);
}
`;
const rippleFragment = `
varying float vAlpha,vSpark;
void main(){float d=length(gl_PointCoord-.5),core=smoothstep(.18,0.,d),halo=smoothstep(.5,.06,d);vec3 col=mix(vec3(.015,.28,.9),vec3(.25,.92,1.),core+vSpark*.2);gl_FragColor=vec4(col,(core+halo*.38)*vAlpha*.72);}
`;
const planeVertex = `
  varying vec2 vUv;
  void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}
`;

/** Background only: the caller owns the render loop and main/reflected tree. */
export function createEnvironment({ time, dpr, aspect = 1.6 }) {
  const uTime = typeof time === 'object' ? time : { value: time ?? 0 };
  const uPixelRatio = typeof dpr === 'object' ? dpr : { value: dpr ?? 1 };
  const uOriginWave={value:0},uCameraWorld={value:new THREE.Vector3(0,0,12)},uRippleLift={value:.015/.42};
  const group = new THREE.Group();
  group.name = 'tree-water-environment';
  group.renderOrder = -1;
  const water = new THREE.Mesh(new THREE.PlaneGeometry(42, 26, 220, 140).rotateX(-Math.PI / 2), new THREE.ShaderMaterial({
    uniforms: { uTime,uOriginWave,uCameraWorld },
    transparent: true,
    depthTest: false,
    depthWrite: false,
    side: THREE.DoubleSide,
    vertexShader: waterVertex,
    fragmentShader: waterFragment,
  }));
  water.name = 'original-water';
  water.position.y = -0.66;
  water.renderOrder = -10;
  group.add(water);

  const random = randomSource(0x52495050);
  const rippleCount = 2100;
  const rippleGeometry = new THREE.BufferGeometry();
  rippleGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(rippleCount * 3), 3));
  const values = Object.fromEntries(['aAngle', 'aBand', 'aJitter', 'aSize', 'aPhase'].map(name => [name, new Float32Array(rippleCount)]));
  for (let index = 0; index < rippleCount; index++) {
    values.aAngle[index] = random() * Math.PI * 2;
    values.aBand[index] = index % 6;
    values.aJitter[index] = (random() - 0.5) * 0.42;
    values.aSize[index] = (0.75 + random() * 1.65) * 1.2;
    values.aPhase[index] = random() * 20;
  }
  for (const [name, array] of Object.entries(values)) rippleGeometry.setAttribute(name, new THREE.BufferAttribute(array, 1));
  const ripples = new THREE.Points(rippleGeometry, new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
    uniforms: { uTime, uPixelRatio,uOriginWave,uRippleLift },
    vertexShader: rippleVertex,
    fragmentShader: rippleFragment,
  }));
  ripples.name = 'original-ripples';
  ripples.renderOrder = -6;
  // The positions are generated by the vertex shader, outside its zero buffer.
  ripples.frustumCulled = false;
  group.add(ripples);

  const mist = new THREE.Mesh(new THREE.PlaneGeometry(18, 12), new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
    uniforms: { uTime },
    vertexShader: planeVertex,
    fragmentShader: `
      varying vec2 vUv;
      uniform float uTime;
      void main(){
        vec2 p=vUv-.5;
        float radial=exp(-dot(p*vec2(1.1,1.45),p*vec2(1.1,1.45))*8.);
        float mist=.86+.14*sin(uTime*.18+vUv.y*5.);
        gl_FragColor=vec4(.015,.22,.55,radial*mist*.2);
      }
    `,
  }));
  mist.name = 'original-mist';
  mist.position.set(0, 3.5, -3.5);
  mist.renderOrder = -30;
  group.add(mist);

  const shaft = new THREE.Mesh(new THREE.PlaneGeometry(4.5, 12), new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
    vertexShader: planeVertex,
    fragmentShader: `
      varying vec2 vUv;
      void main(){
        float x=abs(vUv.x-.5)*2.;
        float shaft=pow(max(0.,1.-x),4.)*sin(vUv.y*3.14159);
        gl_FragColor=vec4(.03,.42,.9,shaft*.055);
      }
    `,
  }));
  shaft.name = 'original-light-shaft';
  shaft.position.set(0, 2.6, -2.8);
  shaft.renderOrder = -20;
  group.add(shaft);

  const starCount = 950;
  const starPositions = new Float32Array(starCount * 3);
  const starSizes = new Float32Array(starCount);
  const starPhases = new Float32Array(starCount);
  for (let index = 0; index < starCount; index++) {
    starPositions.set([(random() - 0.5) * 34, 1 + random() * 16, -5 - random() * 13], index * 3);
    starSizes[index] = 0.45 + random() * 1.25;
    starPhases[index] = random() * 20;
  }
  const starGeometry = new THREE.BufferGeometry();
  starGeometry.setAttribute('position', new THREE.BufferAttribute(starPositions, 3));
  starGeometry.setAttribute('aSize', new THREE.BufferAttribute(starSizes, 1));
  starGeometry.setAttribute('aPhase', new THREE.BufferAttribute(starPhases, 1));
  const stars = new THREE.Points(starGeometry, new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
    uniforms: { uTime, uPixelRatio,uOriginWave,uRippleLift },
    vertexShader: `
      attribute float aSize,aPhase;
      uniform float uTime,uPixelRatio;
      varying float vGlow;
      void main(){
        float twinkle=.5+.5*sin(aPhase+uTime*.34);
        vec4 mv=modelViewMatrix*vec4(position,1.);
        gl_Position=projectionMatrix*mv;
        gl_PointSize=aSize*1.85*(.86+twinkle*.3)*uPixelRatio*(28./-mv.z);
        vGlow=.42+.58*twinkle;
      }
    `,
    fragmentShader: `
      varying float vGlow;
      void main(){
        float d=length(gl_PointCoord-.5);
        float core=smoothstep(.22,0.,d),halo=smoothstep(.5,.05,d);
        gl_FragColor=vec4(vec3(1.),(core+halo*.68)*vGlow);
      }
    `,
  }));
  stars.name = 'original-stars';
  stars.renderOrder = -8;
  group.add(stars);


  const baseCoordinates=water.geometry.getAttribute('position').array.slice();
  function setLayout({aspect:nextAspect=aspect,waterHalfExtent=12}={}){
    const {scale}=treeLayout(nextAspect);
    group.scale.setScalar(scale);group.position.set(0,-3.2*scale,0);
    uRippleLift.value=.015/scale;
    const pos=water.geometry.getAttribute('position');
    const stretch=v=>Math.sign(v)*Math.expm1(Math.abs(v)*5)/Math.expm1(5);
    for(let i=0;i<pos.count;i++){
      const x=baseCoordinates[i*3]/21,z=baseCoordinates[i*3+2]/13;
      pos.setXYZ(i,stretch(x)*waterHalfExtent/scale,0,stretch(z)*waterHalfExtent/scale);
    }
    pos.needsUpdate=true;water.geometry.computeBoundingSphere();
  }
  function updateCamera(position){uCameraWorld.value.copy(position);uOriginWave.value=waterWave(0,0,uTime.value);}
  function sampleHeight(x,z,t){
    const scale=group.scale.x;
    return group.position.y-.66*scale+(waterWave(x/scale,z/scale,t)-waterWave(0,0,t))*scale;
  }
  setLayout({aspect});
  return {group,setLayout,updateCamera,sampleHeight};
}
