// Shared, history-free coverage: fixed spatial fBM perturbs only the radial edge.
// uResolution is the physical drawing-buffer size in every consuming material.
export const paperCoverGLSL = /* glsl */`
float paperHash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float paperValueNoise(vec2 p) {
  vec2 cell = floor(p);
  vec2 f = fract(p);
  vec2 blend = f * f * (3. - 2. * f);
  float a = paperHash(cell);
  float b = paperHash(cell + vec2(1., 0.));
  float c = paperHash(cell + vec2(0., 1.));
  float d = paperHash(cell + vec2(1., 1.));
  return mix(mix(a, b, blend.x), mix(c, d, blend.x), blend.y);
}

float paperFbm4(vec2 p) {
  float sum = 0.;
  float amplitude = .5;
  mat2 rotation = mat2(.8, -.6, .6, .8);
  for (int octave = 0; octave < 4; octave++) {
    sum += amplitude * paperValueNoise(p);
    p = rotation * p * 2.03 + vec2(31.4, 17.2);
    amplitude *= .5;
  }
  return sum / .9375;
}

float paperAt(vec2 uv, float progress, float aspect) {
  if (progress <= 0.) return 0.;
  if (progress >= 1.) return 1.;
  vec2 q = (uv - .5) * vec2(aspect, 1.);
  float halfFeather = 2.5 / max(uResolution.y, 1.);
  float irregularity = .005;
  float rough = (paperFbm4(q * 9. + vec2(7.1, 5.3)) * 2. - 1.) * irregularity;
  float distanceField = length(q) + rough;
  float startRadius = -irregularity - halfFeather;
  float maxRadius = length(vec2(aspect * .5, .5)) + irregularity + halfFeather;
  float radius = mix(startRadius, maxRadius, progress);
  // The nominal transition spans five physical pixels; no interior texture/blur.
  return 1. - smoothstep(radius - halfFeather, radius + halfFeather, distanceField);
}
`;
