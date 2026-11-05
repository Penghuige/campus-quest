import {mountHero} from './entry.mjs';import {HERO_CONFIG} from './config.mjs';
const hero=mountHero(document.getElementById('hero'),HERO_CONFIG);
window.__heroProbe=Object.freeze({snapshot:hero.snapshot});
