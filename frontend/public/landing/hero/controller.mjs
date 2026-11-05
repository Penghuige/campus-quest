import {resolveMode} from './rules.mjs';

export function createHeroController({createRenderer,requestFrame,cancelFrame,now,onMode=()=>{}}){
  let input=null,mode='loading',renderer=null,raf=null,disposed=false,suspended=false,failed=false,last=0,rendererCreations=0;
  const canRun=()=>!disposed&&!suspended&&input?.active&&!input.hidden&&mode==='animated'&&renderer;
  function pause(){if(raf!==null){cancelFrame(raf);raf=null;}renderer?.pointer(null);last=0;}
  function release(){pause();renderer?.dispose();renderer=null;}
  function queue(){if(canRun()&&raf===null){if(!last)last=now();raf=requestFrame(tick);}}
  function tick(){raf=null;if(!canRun())return;const time=now();renderer.frame({now:time/1000,deltaSeconds:Math.max(0,time-last)/1000});last=time;queue();}
  function update(next){if(disposed)return;const previous=input;input={...next};const wanted=resolveMode(input);mode=wanted==='animated'&&failed?'static':wanted;
    if(mode!=='animated')release();
    if(mode==='animated'&&!suspended&&input.active&&!input.hidden){
      const creating=!renderer;
      if(creating){rendererCreations++;try{renderer=createRenderer();}catch{renderer=null;}if(!renderer){failed=true;mode='static';}}
      if(renderer){if(creating||!previous||previous.width!==input.width||previous.height!==input.height||previous.dpr!==input.dpr||!previous.active||previous.hidden){renderer.resize(input);}
        if(raf===null)renderer.clear();queue();}
    }else pause();
    onMode(mode);
  }
  function pointer(point){if(canRun())renderer.pointer(point);}
  function dispose(){if(disposed)return;disposed=true;release();}
  function pageHide(persisted){if(!persisted){dispose();return;}suspended=true;pause();}
  function pageShow(){if(disposed)return;suspended=false;renderer?.clear();if(input)update(input);}
  return {update,pointer,pageHide,pageShow,dispose,snapshot:()=>({mode,running:raf!==null,disposed,rendererCreations,renderer:renderer?.snapshot()||null})};
}
