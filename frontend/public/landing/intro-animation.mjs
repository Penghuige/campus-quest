// Preserve the approved SplitText values while sharing the host's GSAP instance.
const title=document.getElementById('introTitle');
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const errors=[];
let started=false,split,tween,runs=0,completed=false;
export async function startIntroAnimation(){
 if(started)return;started=true;
 const {gsap,SplitText,ScrollTrigger}=window;
 gsap.registerPlugin(SplitText,ScrollTrigger);
 title.dataset.enterState='waiting';title.dataset.enterRuns='0';
 await document.fonts.ready;
 if(reduced.matches){title.dataset.enterState='reduced';return;}
 try{
  split=new SplitText(title,{
   type:'chars',smartWrap:true,autoSplit:false,linesClass:'split-line',wordsClass:'split-word',charsClass:'split-char',reduceWhiteSpace:false,
   onSplit:self=>{
    if(completed)return gsap.set(self.chars,{opacity:1,y:0});
    tween=gsap.fromTo(self.chars,{opacity:0,y:40},{opacity:1,y:0,duration:1.25,ease:'power3.out',stagger:50/1000,
     scrollTrigger:{trigger:title,start:'top 90%-=100px',once:true,fastScrollEnd:true,anticipatePin:.4},
     onStart:()=>{if(completed){gsap.set(self.chars,{opacity:1,y:0});return;}if(!runs)runs++;title.dataset.enterState='running';title.dataset.enterRuns=String(runs);},
     onComplete:()=>{completed=true;title.dataset.enterState='complete';tween?.scrollTrigger?.kill();tween?.kill();},
     willChange:'transform, opacity',force3D:true});
    return tween;
   }
  });
  ScrollTrigger.refresh();
 }catch(error){errors.push(String(error));title.dataset.enterState='error';split?.revert();}
}
reduced.addEventListener('change',()=>{
 if(!reduced.matches)return;
 tween?.progress(1);tween?.scrollTrigger?.kill();title.dataset.enterState='reduced';
});
window.__introProbe=Object.freeze({snapshot:()=>({animation:{state:title.dataset.enterState,runs,completed},errors:errors.slice(),reduced:reduced.matches})});
