export function resolveMode({width,canHover,finePointer,reducedMotion,webglAvailable,imageStatus}) {
  if (imageStatus === 'error') return 'fallback';
  if (imageStatus !== 'ready') return 'loading';
  return width <= 767 || !canHover || !finePointer || reducedMotion || !webglAvailable ? 'static' : 'animated';
}

export function imageCrop({width,height,imageWidth,imageHeight,mobile}) {
  const scale = Math.max(width/imageWidth,height/imageHeight) * (mobile ? 1.5 : 1);
  const sw=width/scale, sh=height/scale;
  return {sx:(imageWidth-sw)/2,sy:mobile?(imageHeight-sh)/2:imageHeight-sh,sw,sh};
}

// Q11: preserve the curve, but reach each prior ink level in twice the time.
export const fadeFactor = seconds => (1-.022) ** (Math.max(0,seconds)*60/2);

export function resolveLoginTarget(value,baseUrl) {
  if(typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value,baseUrl);
    return ['https:','http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
