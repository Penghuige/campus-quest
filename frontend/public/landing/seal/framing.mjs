import { Box3, PerspectiveCamera, Vector3 } from './vendor/three/build/three.module.js';
import { journeyFrame,smooth,journeyStages } from './scroll-presentation.mjs';
import { SEAL_HALF_HEIGHT } from './seal-dimensions.mjs';
import { sealWorldCorners } from './geometry.mjs';
const boxCorners=b=>[b.min.x,b.max.x].flatMap(x=>[b.min.y,b.max.y].flatMap(y=>[b.min.z,b.max.z].map(z=>new Vector3(x,y,z))));
const ndcCorners=[-1,1].flatMap(x=>[-1,1].flatMap(y=>[-1,1].map(z=>new Vector3(x,y,z))));
const frustumEdges=[];
for(let i=0;i<8;i++)for(let j=i+1;j<8;j++){
  const a=ndcCorners[i],b=ndcCorners[j];
  if(Number(a.x!==b.x)+Number(a.y!==b.y)+Number(a.z!==b.z)===1)frustumEdges.push([i,j]);
}
export function fitStageLayout({cssWidth,cssHeight,waterY,sourceBounds}){
  if(!(cssWidth>0&&cssHeight>0&&Number.isFinite(waterY)))throw new RangeError('valid viewport and waterY required');
  const viewport={width:cssWidth,height:cssHeight,left:0,top:0};
  const layout={width:1,waterY,hoverGap:.7,startDistance:12,viewDistance:1.6/(1.2*Math.tan(Math.PI/15)),waterHalfExtent:12,viewport};
  const camera=new PerspectiveCamera(24,cssWidth/cssHeight,.1,80);
  const source=(sourceBounds??new Box3(new Vector3(-2.2,-1.5,-1.6),new Vector3(2.2,2.2,1.6))).clone().expandByScalar(.2);
  const setCamera=f=>{camera.position.copy(f.cameraPose.position);camera.quaternion.copy(f.cameraPose.rotation);camera.updateMatrixWorld();};
  const fits=(candidate,formedOnly=false)=>{
    for(let i=0;i<=200;i++){
      const f=journeyFrame(i/200*journeyStages.contact,candidate);setCamera(f);
      if(formedOnly&&f.morph<1)continue;
      const points=sealWorldCorners(f.bodyPose,1);
      const m=smooth(f.morph);
      if(m<1){
        const b=new Box3(source.min.clone().multiplyScalar(1-m).add(new Vector3(-.5,-SEAL_HALF_HEIGHT,-.5).multiplyScalar(m)),source.max.clone().multiplyScalar(1-m).add(new Vector3(.5,SEAL_HALF_HEIGHT,.5).multiplyScalar(m)));
        points.push(...boxCorners(b).map(p=>p.applyQuaternion(f.bodyPose.rotation).add(f.bodyPose.position)));
      }
      for(const p of points){const n=p.project(camera);if(Math.abs(n.x)>.82||Math.abs(n.y)>.88||Math.abs(n.z)>=1)return false;}
      const water=new Vector3(0,waterY,0).project(camera);
      if(Math.abs(water.y)>.88)return false;
    }return true;
  };
  // Fit the formed hero first. A wider source tree may change its own camera,
  // but must never push the final seal farther away and reduce its visual mass.
  function solve(key,formedOnly){
    const base=layout[key];let low=1,high=1;
    const candidate=scale=>({...layout,[key]:base*scale});
    while(!fits(candidate(high),formedOnly)&&high<12)high*=1.25;
    for(let i=0;i<22;i++){
      const mid=(low+high)/2;
      if(fits(candidate(mid),formedOnly))high=mid;else low=mid;
    }
    layout[key]=base*high;
  }
  solve('viewDistance',true);
  solve('startDistance',false);
  let extent=8;
  for(let i=0;i<=100;i++){
    setCamera(journeyFrame(i/100*journeyStages.contact,layout));
    const world=ndcCorners.map(p=>p.clone().unproject(camera));
    for(const [i,j]of frustumEdges){
      const a=world[i],b=world[j],dy=b.y-a.y;
      if(Math.abs(dy)<1e-10)continue;
      const t=(waterY-a.y)/dy;
      if(t>=0&&t<=1){const v=a.clone().lerp(b,t);extent=Math.max(extent,Math.abs(v.x),Math.abs(v.z));}
    }
  }
  layout.waterHalfExtent=extent*1.05;
  return layout;
}
