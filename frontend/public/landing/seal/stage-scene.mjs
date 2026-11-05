import * as THREE from './vendor/three/build/three.module.js';
import { createEnvironment,sampleTree,treeLayout } from './tree-environment.mjs';
import { createParticleField } from './particle-field.mjs';
import { paperCoverGLSL } from './paper-cover.mjs';
import { paperWavesGLSL } from './paper-waves.mjs';
import { makeReflectionMatrix } from './reflection.mjs';
import { imprintWorldCorners,projectImprint } from './geometry.mjs';
import { journeyFrame,journeyStages } from './scroll-presentation.mjs';
import { SEAL_HEIGHT } from './seal-dimensions.mjs';
export function createStageScene({texture,layout:initial,resolution,time,count,waves=null}){
  let layout=initial,domInk=false,inkOpacity=0;
  const scene=new THREE.Scene();
  const camera=new THREE.PerspectiveCamera(24,layout.viewport.width/layout.viewport.height,.1,80);
  const aspect={value:camera.aspect},reveal={value:0},dpr={value:1},waterY={value:layout.waterY};
  const environment=createEnvironment({time,dpr,aspect:camera.aspect});scene.add(environment.group);
  const paper=new THREE.Mesh(new THREE.PlaneGeometry(1,1),new THREE.ShaderMaterial({
    uniforms:{uPaper:{value:new THREE.Color('#F5F3EC')},uProgress:reveal,uAspect:aspect,uResolution:resolution,
      uWaveHeight:waves?.uHeight??{value:null},uWaveView:waves?.uView??{value:new THREE.Vector2(1,1)},
      uWaveGrid:waves?.uResolution??{value:256},uWaveClarity:waves?.uClarity??{value:1.2},uWavesEnabled:{value:waves?1:0}},
    vertexShader:'varying vec2 vUv;void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}',
    fragmentShader:'uniform vec3 uPaper;uniform float uProgress,uAspect;uniform vec2 uResolution;\n'+paperCoverGLSL+paperWavesGLSL+'\nvoid main(){vec2 uv=gl_FragCoord.xy/uResolution;gl_FragColor=vec4(paperWaveColor(uv),paperAt(uv,uProgress,uAspect));\n#include <colorspace_fragment>\n}',
    transparent:true,depthTest:false,depthWrite:false,toneMapped:false
  }));
  paper.rotation.x=-Math.PI/2;paper.renderOrder=10;scene.add(paper);
  const body=new THREE.Group();scene.add(body);
  const proxy=new THREE.Mesh(new THREE.BoxGeometry(1,SEAL_HEIGHT,1),new THREE.MeshBasicMaterial({
    colorWrite:false,depthTest:true,depthWrite:true,transparent:false,blending:THREE.NoBlending
  }));
  proxy.name='invisible-depth-proxy';body.add(proxy);
  const field=createParticleField({count,reveal,aspect,resolution,time,waterY,
    reflectionMatrix:{value:makeReflectionMatrix(layout.waterY)},cameraWorld:{value:camera.position},coverGLSL:paperCoverGLSL});
  body.add(field.reflection,field.core,field.glow);
  texture.colorSpace=THREE.SRGBColorSpace;
  // The approved face is already a complete transparent RGBA image.
  texture.offset.set(0,0);texture.repeat.set(1,1);
  const ink=new THREE.Mesh(new THREE.PlaneGeometry(1,1),new THREE.MeshBasicMaterial({
    map:texture,transparent:true,opacity:0,depthTest:true,depthWrite:false,toneMapped:false
  }));
  ink.rotation.x=-Math.PI/2;ink.renderOrder=30;
  ink.material.onBeforeCompile=shader=>{
    shader.uniforms.uInkReveal=reveal;shader.uniforms.uInkAspect=aspect;shader.uniforms.uResolution=resolution;
    shader.fragmentShader='uniform float uInkReveal,uInkAspect;uniform vec2 uResolution;\n'+paperCoverGLSL+'\n'+shader.fragmentShader;
    shader.fragmentShader=shader.fragmentShader.replace('#include <map_fragment>',
      '#include <map_fragment>\ndiffuseColor.a*=paperAt(gl_FragCoord.xy/uResolution,uInkReveal,uInkAspect);');
  };
  ink.material.customProgramCacheKey=()=> 'horizontal-seal-rgba-v2';
  scene.add(ink);
  function updateClock(){environment.updateCamera(camera.position);}
  function applyFrame(f){
    camera.position.copy(f.cameraPose.position);camera.quaternion.copy(f.cameraPose.rotation);camera.up.copy(f.cameraPose.up);camera.updateMatrixWorld();
    body.position.copy(f.bodyPose.position);body.quaternion.copy(f.bodyPose.rotation);
    field.uniforms.uMorph.value=f.morph;field.uniforms.uTighten.value=f.tighten;
    field.core.visible=field.glow.visible=f.visible;
    proxy.visible=f.visible&&f.morph===1;
    field.reflection.visible=f.paper<1;
    reveal.value=f.paper;ink.material.opacity=f.ink;inkOpacity=f.ink;
    paper.visible=f.paper>0;ink.visible=f.ink>0&&!domInk;
    environment.group.visible=f.paper<1;
    body.updateMatrixWorld(true);updateClock();
  }
  function updateLayout(next){
    layout=next;aspect.value=next.viewport.width/next.viewport.height;camera.aspect=aspect.value;camera.updateProjectionMatrix();
    waterY.value=next.waterY;
    field.uniforms.uReflectionMatrix.value.copy(makeReflectionMatrix(next.waterY));
    field.setTreePositions(sampleTree(count,aspect.value));
    field.uniforms.uTreePointScale.value=Math.sqrt(treeLayout(aspect.value).scale/.42);
    environment.setLayout({aspect:aspect.value,waterHalfExtent:next.waterHalfExtent});
    paper.position.y=next.waterY-.005;paper.scale.set(next.waterHalfExtent*2,next.waterHalfExtent*2,1);
    ink.position.y=next.waterY+.001;
  }
  function finalRect(viewport=layout.viewport){
    const c=camera.clone(),p=journeyFrame(journeyStages.contact,layout).cameraPose;
    c.position.copy(p.position);c.quaternion.copy(p.rotation);c.up.copy(p.up);c.updateMatrixWorld();
    return projectImprint(c,imprintWorldCorners(1,layout.waterY+.001),viewport);
  }
  function setPixelRatio(value){field.uniforms.uDpr.value=value;dpr.value=value*field.uniforms.uTreePointScale.value;}
  function setInkPresentation(useDOM){domInk=Boolean(useDOM);ink.visible=inkOpacity>0&&!domInk;}
  updateLayout(layout);
  return {scene,camera,body,proxy,paper,ink,field,environment,applyFrame,updateLayout,finalRect,updateClock,setPixelRatio,setInkPresentation};
}
