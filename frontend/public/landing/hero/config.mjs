import {getLoginURL} from '../navigation.mjs';
export const HERO_CONFIG=Object.freeze({loginUrl:getLoginURL(new URL('../navigation.mjs',import.meta.url).href)});
