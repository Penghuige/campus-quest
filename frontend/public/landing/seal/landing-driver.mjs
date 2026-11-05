import {JourneyDriver} from './journey-driver.mjs';

/** Maps the full landing document to the original seal journey's local scroll. */
export class LandingJourneyDriver extends JourneyDriver {
  constructor({getOffset=()=>0,...options}){
    super(options);
    this.getOffset=getOffset;
  }

  scroll(globalY,options){
    const offset=this.getOffset();
    // The exposed black stage stays at its opening pose throughout the reveal,
    // including a fast reverse from an unfinished journey back under the white page.
    return super.scroll(Math.max(0,globalY-offset),globalY<=offset?{...options,snap:true}:options);
  }

  refresh(layout,globalY){
    return super.refresh(layout,Math.max(0,globalY-this.getOffset()));
  }

  setReducedMotion(value,globalY=this.documentY(this.snapshot().actualY)){
    return super.setReducedMotion(value,Math.max(0,globalY-this.getOffset()));
  }

  documentY(localY){
    return this.getOffset()+localY;
  }
}
