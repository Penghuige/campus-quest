(() => {
  'use strict';
  const hero = document.getElementById('hero');
  const paper = document.getElementById('paperLayer');
  const photo = document.querySelector('.photo-window');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const errors = [];
  let raf = 0;
  let heroVisible = null;
  window.addEventListener('error', e => errors.push(String(e.message)));
  function update() {
    raf = 0;
    const heroRect = hero.getBoundingClientRect();
    // Hero and photo scroll in ordinary document flow; the lower-edge mask stays fixed.
    const nextVisible = heroRect.bottom > 0 && heroRect.top < innerHeight && !document.hidden;
    if (nextVisible !== heroVisible) {
      heroVisible = nextVisible;
      hero.inert = !nextVisible;
      hero.setAttribute('aria-hidden', String(!nextVisible));
      window.dispatchEvent(new CustomEvent('campus:hero-visibility', {detail: {visible: nextVisible}}));
    }
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(update); }
  window.addEventListener('scroll', schedule, {passive: true});
  window.addEventListener('resize', schedule);
  window.addEventListener('pageshow', schedule);
  document.addEventListener('visibilitychange', schedule);
  reduced.addEventListener('change', schedule);
  window.__scrollProbe = Object.freeze({snapshot: () => {
    const rect = element => {
      const r = element.getBoundingClientRect();
      return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height};
    };
    return {scrollY, viewport:[innerWidth,innerHeight], scrollHeight:document.scrollingElement.scrollHeight,
      hero:rect(hero), paper:rect(paper), photo:rect(photo), heroVisible,photoMaskMode:'none',reduced:reduced.matches,
      mask:getComputedStyle(photo).maskImage,errors:errors.slice()};
  }});
  update();
})();
