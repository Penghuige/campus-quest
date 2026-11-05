import * as THREE from 'three';
import {GPUComputationRenderer} from '../../vendor/three/examples/jsm/misc/GPUComputationRenderer.js';
import {MAX_DROPS} from './wave-clock.mjs';

// Adapted from the MIT-licensed three.js r179 webgl_gpgpu_water example.
// https://github.com/mrdoob/three.js/blob/r179/examples/webgl_gpgpu_water.html
// State remains current / previous height, advanced in a shared GPU field.
const computeShader=`
uniform vec4 uDrops[16];
uniform int uDropCount;
uniform float uPropagation;
void main(){
  vec2 cell=1.0/resolution.xy;
  vec2 uv=gl_FragCoord.xy*cell;
  vec2 state=texture2D(heightmap,uv).rg;
  float h=state.r;
  float lap=texture2D(heightmap,uv+vec2(cell.x,0.)).r
           +texture2D(heightmap,uv-vec2(cell.x,0.)).r
           +texture2D(heightmap,uv+vec2(0.,cell.y)).r
           +texture2D(heightmap,uv-vec2(0.,cell.y)).r-4.*h;
  // Damped velocity form of the official two-frame finite difference update.
  float next=h+(h-state.g)*0.994+uPropagation*lap;
  float edge=min(min(uv.x,1.-uv.x),min(uv.y,1.-uv.y));
  next*=mix(0.88,1.,smoothstep(0.,0.11,edge));
  for(int i=0;i<16;i++){
    if(i>=uDropCount)break;
    vec4 drop=uDrops[i];
    float q=length(uv-drop.xy)/max(drop.z,0.001);
    // Balanced displacement: a raised centre and shallow surrounding trough.
    // This avoids accumulating water volume from a permanent positive source.
    next+=drop.w*(1.-q*q)*exp(-q*q);
  }
  gl_FragColor=vec4(next,h,0.,1.);
}`;

const vertexShader=`
varying vec2 vUv;
void main(){vUv=uv;gl_Position=vec4(position.xy*2.,0.,1.);}
`;

const fragmentShader=`
uniform sampler2D uHeight;
uniform vec2 uView;
uniform float uResolution;
uniform vec3 uPaper;
uniform float uClarity;
varying vec2 vUv;
void main(){
  vec2 uv=(vUv-0.5)*uView+0.5;
  vec2 cell=vec2(1./uResolution);
  float l=texture2D(uHeight,uv-vec2(cell.x,0.)).r;
  float r=texture2D(uHeight,uv+vec2(cell.x,0.)).r;
  float b=texture2D(uHeight,uv-vec2(0.,cell.y)).r;
  float t=texture2D(uHeight,uv+vec2(0.,cell.y)).r;
  vec2 slope=vec2(r-l,t-b)/(2.*cell);
  vec3 N=normalize(vec3(-slope*0.9,1.));
  vec3 L=normalize(vec3(-0.65,0.8,1.8));
  vec3 H=normalize(L+vec3(0.,0.,1.));
  float diffuse=dot(N,L)-L.z;
  float specular=pow(max(dot(N,H),0.),32.)-pow(H.z,32.);
  float shade=clamp((diffuse*0.17+specular*0.018)*uClarity,-0.075,0.045);
  gl_FragColor=vec4(clamp(uPaper+vec3(shade),0.,1.),1.);
  #include <colorspace_fragment>
}`;

export class WaterField{
  constructor(canvas,{resolution=256,renderer}={}){
    if(!Number.isInteger(resolution)||resolution<32||resolution>384)throw new RangeError('水波网格必须在 32–384 之间，以保持传播步长稳定');
    this.resolution=resolution;
    this.ownsRenderer=!renderer;
    this.renderer=renderer||new THREE.WebGLRenderer({canvas,antialias:false,alpha:false,powerPreference:'high-performance'});
    if(!this.renderer.extensions.has('EXT_color_buffer_float')){
      if(this.ownsRenderer)this.renderer.dispose();throw new Error('当前设备未提供浮点水波计算能力');
    }
    this.compute=new GPUComputationRenderer(resolution,resolution,this.renderer);
    this.zero=this.compute.createTexture();this.zero.image.data.fill(0);
    this.variable=this.compute.addVariable('heightmap',computeShader,this.zero);
    this.compute.setVariableDependencies(this.variable,[this.variable]);
    if(this.renderer.extensions.has('OES_texture_float_linear')){
      this.variable.minFilter=THREE.LinearFilter;this.variable.magFilter=THREE.LinearFilter;
    }
    const u=this.variable.material.uniforms;
    u.uDrops={value:Array.from({length:MAX_DROPS},()=>new THREE.Vector4())};
    u.uDropCount={value:0};
    // Keep the same domain propagation speed when QA uses a smaller grid.
    u.uPropagation={value:0.16*(resolution/256)**2};
    const error=this.compute.init();if(error){this.compute.dispose();if(this.ownsRenderer)this.renderer.dispose();throw new Error(error);}
    this.scene=new THREE.Scene();this.camera=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
    this.material=new THREE.ShaderMaterial({
      uniforms:{uHeight:{value:this.texture},uView:{value:new THREE.Vector2(1,1)},uResolution:{value:resolution},uPaper:{value:new THREE.Color('#f5f3ec')},uClarity:{value:1.2}},
      vertexShader,fragmentShader,depthTest:false,depthWrite:false,
    });
    this.quad=new THREE.Mesh(new THREE.PlaneGeometry(1,1),this.material);this.scene.add(this.quad);
  }
  get texture(){return this.compute.getCurrentRenderTarget(this.variable).texture;}
  step(drops=[]){
    const u=this.variable.material.uniforms;u.uDropCount.value=Math.min(drops.length,MAX_DROPS);
    for(let i=0;i<u.uDropCount.value;i++){
      const d=drops[i];u.uDrops.value[i].set(d.x,d.y,d.radius,d.strength);
    }
    this.compute.compute();this.material.uniforms.uHeight.value=this.texture;
  }
  clear(){
    for(const target of this.variable.renderTargets)this.compute.renderTexture(this.zero,target);
    this.variable.material.uniforms.uDropCount.value=0;
    this.material.uniforms.uHeight.value=this.texture;
  }
  resize(width,height){
    const longest=Math.max(width,height);
    this.material.uniforms.uView.value.set(width/longest,height/longest);
    if(this.ownsRenderer){
      this.renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
      this.renderer.setSize(width,height,false);this.render();
    }
  }
  setClarity(value){this.material.uniforms.uClarity.value=value;this.render();}
  render(){this.renderer.setRenderTarget(null);this.renderer.render(this.scene,this.camera);}
  readHeights(){
    const data=new Float32Array(this.resolution*this.resolution*4);
    this.renderer.readRenderTargetPixels(this.compute.getCurrentRenderTarget(this.variable),0,0,this.resolution,this.resolution,data);
    return Float32Array.from({length:this.resolution*this.resolution},(_,i)=>data[i*4]);
  }
  dispose(){
    this.compute.dispose();this.quad.geometry.dispose();this.material.dispose();if(this.ownsRenderer)this.renderer.dispose();
  }
}
