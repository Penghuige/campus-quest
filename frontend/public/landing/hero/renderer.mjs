import {imageCrop,fadeFactor} from './rules.mjs';

// The organic edge field is retained from the approved original hero.
const vertex=`attribute vec2 p; varying vec2 vUv; void main(){vUv=p*.5+.5;gl_Position=vec4(p,0.,1.);}`;
const fragment=`precision highp float;
varying vec2 vUv; uniform sampler2D uTrail,uImage; uniform vec2 uRes,uImageRes; uniform vec4 uCrop; uniform float uTime;
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;} vec2 mod289(vec2 x){return x-floor(x*(1.0/289.0))*289.0;} vec3 permute(vec3 x){return mod289(((x*34.0)+1.0)*x);} float snoise(vec2 v){const vec4 C=vec4(0.211324865405187,0.366025403784439,-0.577350269189626,0.024390243902439);vec2 i=floor(v+dot(v,C.yy));vec2 x0=v-i+dot(i,C.xx);vec2 i1=x0.x>x0.y?vec2(1.0,0.0):vec2(0.0,1.0);vec4 x12=x0.xyxy+C.xxzz;x12.xy-=i1;i=mod289(i);vec3 p=permute(permute(i.y+vec3(0.0,i1.y,1.0))+i.x+vec3(0.0,i1.x,1.0));vec3 m=max(0.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.0);m=m*m;m=m*m;vec3 x=2.0*fract(p*C.www)-1.0;vec3 h=abs(x)-0.5;vec3 ox=floor(x+0.5);vec3 a0=x-ox;m*=1.79284291400159-0.85373472095314*(a0*a0+h*h);vec3 g;g.x=a0.x*x0.x+h.x*x0.y;g.yz=a0.yz*x12.xz+h.yz*x12.yw;return 130.0*dot(m,g);}
float trailAt(vec2 uv){return texture2D(uTrail,vec2(uv.x,1.-uv.y)).r;}
void main(){
 vec2 uv=vUv,asp=vec2(uRes.x/uRes.y,1.);float t=uTime*.25;
 vec2 n=vec2(snoise(uv*asp*3.2+vec2(t,-t)),snoise(uv*asp*3.2+vec2(-t,t)+17.));
 vec2 n2=vec2(snoise(uv*asp*9.+t*2.),snoise(uv*asp*9.-t*2.+5.));
 float trail=trailAt(uv+n*.03+n2*.006);
 float density=.48,fiber=snoise(uv*asp*32.+vec2(t,-t)*.7);
 float support=smoothstep(.02,.28,trail);
 float deposit=trail+((density-.45)*.24+fiber*.012)*support;
 float footprint=smoothstep(.12,.56,deposit),body=smoothstep(.26,.77,deposit),wash=footprint*(1.-body);
 float alpha=clamp(body*(.50+density*.50)+wash*.25,0.,1.);
 // Only the mask moves. The source architecture remains geometrically stable.
 vec2 sampleUv=(uCrop.xy+vec2(uv.x,1.-uv.y)*uCrop.zw)/uImageRes;
 vec3 photo=texture2D(uImage,sampleUv).rgb;
 gl_FragColor=vec4(photo*alpha,alpha);
}`;

export function createInkRenderer({canvas,image,onContextLost=()=>{}}) {
  let gl;
  try {gl=canvas.getContext('webgl',{premultipliedAlpha:true,alpha:true,antialias:false});} catch {return null;}
  if(!gl)return null;
  let disposed=false,lost=false,frames=0,imageUploads=0,width=1,height=1,previous=null,vx=0,vy=0;
  const textures=[],shaders=[];let program,buffer;
  const trail=document.createElement('canvas'),tc=trail.getContext('2d');
  const deposit=document.createElement('canvas'),dc=deposit.getContext('2d');
  if(!tc||!dc)return null;
  trail.width=trail.height=deposit.width=deposit.height=1;
  let depositAge=0;
  const points=[];
  function dispose(){if(disposed)return;disposed=true;points.length=0;canvas.removeEventListener('webglcontextlost',contextLost);if(!lost){textures.forEach(t=>gl.deleteTexture(t));shaders.forEach(s=>gl.deleteShader(s));if(buffer)gl.deleteBuffer(buffer);if(program)gl.deleteProgram(program);}}
  function contextLost(event){event.preventDefault();if(lost||disposed)return;lost=true;points.length=0;onContextLost();}
  const upload=(unit,source)=>{gl.activeTexture(gl.TEXTURE0+unit);gl.bindTexture(gl.TEXTURE_2D,textures[unit]);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,source);};
  try {
    const compile=(type,source)=>{const s=gl.createShader(type);shaders.push(s);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};
    program=gl.createProgram();gl.attachShader(program,compile(gl.VERTEX_SHADER,vertex));gl.attachShader(program,compile(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);const p=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(p);gl.vertexAttribPointer(p,2,gl.FLOAT,false,0,0);
    for(let i=0;i<2;i++){const texture=gl.createTexture();textures.push(texture);gl.activeTexture(gl.TEXTURE0+i);gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);}
    upload(1,image);imageUploads++;
  } catch {dispose();return null;}
  const uniform=Object.fromEntries(['uTrail','uImage','uRes','uImageRes','uCrop','uTime'].map(n=>[n,gl.getUniformLocation(program,n)]));
  gl.uniform1i(uniform.uTrail,0);gl.uniform1i(uniform.uImage,1);
  const imageWidth=image.naturalWidth||image.width,imageHeight=image.naturalHeight||image.height;
  gl.uniform2f(uniform.uImageRes,imageWidth,imageHeight);
  canvas.addEventListener('webglcontextlost',contextLost);
  function clear(){if(disposed||lost)return;for(const [ctx,c] of [[tc,trail],[dc,deposit]]){ctx.globalAlpha=1;ctx.fillStyle='black';ctx.fillRect(0,0,c.width,c.height);}depositAge=0;points.length=0;previous=null;vx=vy=0;gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);}
  function resize(size){if(disposed||lost)return;const w=Math.max(1,size.width),h=Math.max(1,size.height),d=Math.min(1.5,Math.max(1,size.dpr||1));
    if(width===w&&height===h&&canvas.width===Math.round(w*d)&&canvas.height===Math.round(h*d))return;
    const old=document.createElement('canvas');old.width=trail.width;old.height=trail.height;old.getContext('2d').drawImage(trail,0,0);
    width=w;height=h;canvas.width=Math.round(w*d);canvas.height=Math.round(h*d);trail.width=Math.max(1,Math.round(w*.25));trail.height=Math.max(1,Math.round(h*.25));tc.globalAlpha=1;tc.fillStyle='black';tc.fillRect(0,0,trail.width,trail.height);tc.drawImage(old,0,0,trail.width,trail.height);
    deposit.width=trail.width;deposit.height=trail.height;dc.globalAlpha=1;dc.drawImage(trail,0,0);depositAge=0;
    previous=null;points.length=0;gl.viewport(0,0,canvas.width,canvas.height);gl.uniform2f(uniform.uRes,w,h);const c=imageCrop({width:w,height:h,imageWidth,imageHeight,mobile:false});gl.uniform4f(uniform.uCrop,c.sx,c.sy,c.sw,c.sh);
  }
  function stamp(x,y,r){x*=.25;y*=.25;r*=.25;const g=tc.createRadialGradient(x,y,0,x,y,r);g.addColorStop(0,'rgba(255,255,255,.55)');g.addColorStop(.5,'rgba(255,255,255,.4125)');g.addColorStop(1,'rgba(255,255,255,0)');tc.fillStyle=g;tc.beginPath();tc.arc(x,y,r,0,Math.PI*2);tc.fill();}
  function pointer(point){if(disposed||lost)return;if(!point){points.length=0;previous=null;vx=vy=0;return;}if(Number.isFinite(point.x)&&Number.isFinite(point.y)) {points.push({x:point.x,y:point.y});if(points.length>256)points.shift();}}
  function frame({now,deltaSeconds}){if(disposed||lost)return;
    // Re-sample a fixed deposit at its total age. Repeated 8-bit compositing
    // would round a small high-refresh fade to zero and leave permanent ink.
    depositAge+=Math.max(0,deltaSeconds);tc.globalAlpha=1;tc.fillStyle='black';tc.fillRect(0,0,trail.width,trail.height);tc.globalAlpha=fadeFactor(depositAge);tc.drawImage(deposit,0,0);tc.globalAlpha=1;
    for(const point of points){const dx=previous?point.x-previous.x:0,dy=previous?point.y-previous.y:0;vx+=(dx-vx)*.25;vy+=(dy-vy)*.25;const base=Math.min(width,height)*.075,r=base+Math.min(Math.hypot(vx,vy),60)*1.6+Math.sin(now*2.3)*base*.15;
      if(previous){const count=Math.max(1,Math.ceil(Math.hypot(dx,dy)/(r*.25)));for(let i=1;i<=count;i++)stamp(previous.x+dx*i/count,previous.y+dy*i/count,r);}else stamp(point.x,point.y,r);previous=point;}
    if(points.length){dc.globalAlpha=1;dc.drawImage(trail,0,0);depositAge=0;}
    points.length=0;upload(0,trail);gl.uniform1f(uniform.uTime,now);gl.drawArrays(gl.TRIANGLES,0,6);frames++;
  }
  clear();
  return {resize,pointer,frame,clear,dispose,snapshot:()=>({frames,imageUploads,disposed,lost})};
}
