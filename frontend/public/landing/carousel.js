/* Disposable scroll-handoff probe: autonomous photos, independent of scroll input. */
(() => {
  'use strict';

  const HOLD = 2000;
  const FADE = 1400;
  const ORDER = ['F110', 'F123', 'F083', 'C015'];
  const stage = document.querySelector('.photo-window');
  const images = stage ? [...stage.querySelectorAll('img[data-id]')] : [];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const events = [];
  const errors = [];
  let index = 0;
  let elapsed = 0;
  let last = null;
  let raf = 0;
  let visible = false;
  let ready = false;
  let running = false;
  let fading = false;
  let swaps = 0;

  function record(type, detail = {}) {
    events.push({ type, at: performance.now(), ...detail });
    if (events.length > 180) events.shift();
  }

  function progress() {
    return Math.max(0, Math.min(1, (elapsed - HOLD) / FADE));
  }

  function paint() {
    const amount = progress();
    const opacity = amount * amount * (3 - 2 * amount);
    const next = (index + 1) % images.length;
    images.forEach((img, i) => {
      img.style.zIndex = i === next && amount > 0 ? '2' : i === index ? '1' : '0';
      // Keep the outgoing image opaque; the incoming layer covers it as it fades in.
      img.style.opacity = i === index ? '1' : i === next && amount > 0 ? String(opacity) : '0';
    });
  }

  function frame(now) {
    raf = 0;
    if (!running) return;
    if (last !== null) {
      const delta = now - last;
      if (delta <= 250) elapsed += delta;
      else record('render-gap', { ms: delta });
    }
    last = now;

    if (elapsed >= HOLD && !fading) {
      fading = true;
      record('fade-start', {
        from: images[index].dataset.id,
        to: images[(index + 1) % images.length].dataset.id,
        elapsed
      });
    }
    if (elapsed >= HOLD + FADE) {
      record('swap', {
        from: images[index].dataset.id,
        to: images[(index + 1) % images.length].dataset.id,
        elapsed
      });
      index = (index + 1) % images.length;
      elapsed = 0;
      fading = false;
      swaps++;
    }
    paint();
    raf = requestAnimationFrame(frame);
  }

  function sync() {
    const shouldRun = ready && visible && !document.hidden && !reduced.matches;
    if (shouldRun === running) return;
    running = shouldRun;
    last = null;
    record(running ? 'resume' : 'pause', {
      index: images[index]?.dataset.id ?? null,
      elapsed
    });
    if (running && !raf) raf = requestAnimationFrame(frame);
    if (!running && raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  }

  Object.defineProperty(window, '__carouselPreview', {
    configurable: false,
    writable: false,
    value: Object.freeze({
      snapshot: () => ({
        order: images.map(img => img.dataset.id),
        index,
        current: images[index]?.dataset.id ?? null,
        elapsed,
        progress: progress(),
        hold: HOLD,
        fade: FADE,
        running,
        visible,
        ready,
        reduced: reduced.matches,
        swaps,
        events: events.map(event => ({ ...event })),
        errors: errors.slice(),
        images: images.map(img => ({
          id: img.dataset.id,
          complete: img.complete,
          width: img.naturalWidth,
          height: img.naturalHeight,
          opacity: Number(img.style.opacity)
        }))
      })
    })
  });

  if (!stage || images.length !== ORDER.length || images.some((img, i) => img.dataset.id !== ORDER[i])) {
    errors.push('Expected .photo-window with F110, F123, F083, C015 in that order.');
    record('configuration-error');
    return;
  }

  paint();
  const observer = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting && entry.intersectionRatio >= 0.02;
    sync();
  }, { threshold: 0.02 });
  observer.observe(stage);
  document.addEventListener('visibilitychange', sync);
  reduced.addEventListener('change', sync);

  Promise.all(images.map(img => img.decode().catch(error => {
    const message = img.dataset.id + ': ' + String(error);
    errors.push(message);
    record('image-error', { id: img.dataset.id, message });
  }))).then(() => {
    ready = errors.length === 0;
    record('images-ready', { ready });
    sync();
  });
})();
