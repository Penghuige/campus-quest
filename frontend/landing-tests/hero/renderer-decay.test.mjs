import test from 'node:test';import assert from 'node:assert/strict';import {createInkRenderer} from '../../public/landing/hero/renderer.mjs';

// Canvas2D's default 8-bit storage rounds each compositing write. Model that
// browser constraint at a stamped centre pixel and inspect the real renderer's
// uploaded trail, rather than testing the floating-point fade formula again.
function eightBitCanvas(){
  const canvas={width:320,height:180,value:0,addEventListener(){},removeEventListener(){}};
  const ctx={globalAlpha:1,fillStyle:'black',fillRect(){canvas.value=Math.round(canvas.value*(1-this.globalAlpha));},drawImage(source){canvas.value=Math.round(source.value*this.globalAlpha+canvas.value*(1-this.globalAlpha));},createRadialGradient(){return {addColorStop(){}};},beginPath(){},arc(){},fill(){const a=.55*this.globalAlpha;canvas.value=Math.round(255*a+canvas.value*(1-a));}};
  let unit=0;const gl={lastTrail:0,VERTEX_SHADER:1,FRAGMENT_SHADER:2,TEXTURE0:0,
    createShader:()=>({}),createProgram:()=>({}),createBuffer:()=>({}),createTexture:()=>({}),getShaderParameter:()=>true,getProgramParameter:()=>true,getAttribLocation:()=>0,getUniformLocation:()=>0,
    activeTexture(n){unit=n;},texImage2D(...args){if(unit===0)this.lastTrail=args.at(-1).value;}};
  for(const name of ['shaderSource','compileShader','attachShader','linkProgram','useProgram','bindBuffer','bufferData','enableVertexAttribArray','vertexAttribPointer','bindTexture','texParameteri','uniform1i','uniform1f','uniform2f','uniform4f','viewport','clearColor','clear','drawArrays','deleteTexture','deleteShader','deleteBuffer','deleteProgram'])gl[name]=()=>{};
  canvas.getContext=kind=>kind==='2d'?ctx:gl;return {canvas,gl};
}
function stoppedTrail(fps){const original=globalThis.document;globalThis.document={createElement:()=>eightBitCanvas().canvas};try{
  const {canvas,gl}=eightBitCanvas();const renderer=createInkRenderer({canvas,image:{width:320,height:180}});assert.ok(renderer);renderer.resize({width:320,height:180,dpr:1});
  for(let i=0;i<30;i++){renderer.pointer({x:160,y:90});renderer.frame({now:i/fps,deltaSeconds:1/fps});}
  const painted=gl.lastTrail;assert.ok(painted>180,'fixture must first accumulate a visible stroke');
  for(let i=0;i<fps*4;i++)renderer.frame({now:1+i/fps,deltaSeconds:1/fps});const remaining=gl.lastTrail;renderer.dispose();return {painted,remaining};
}finally{globalThis.document=original;}}
test('240Hz quantized Canvas trail cannot retain a visible stopped stroke',()=>{const result=stoppedTrail(240);assert.ok(result.remaining<32,JSON.stringify(result));});
test('60, 120 and 144Hz stopped strokes also return below the reveal threshold',()=>{for(const fps of [60,120,144])assert.ok(stoppedTrail(fps).remaining<32,`${fps}Hz retains a stopped stroke`);});
