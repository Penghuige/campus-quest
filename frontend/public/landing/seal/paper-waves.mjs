// Exact display model from the approved independent water experiment.
// The longest-side domain is shared by clicks, the height field and screen UVs.
export const paperWavesGLSL=/* glsl */`
uniform sampler2D uWaveHeight;
uniform vec2 uWaveView;
uniform float uWaveGrid,uWaveClarity,uWavesEnabled;
vec3 paperWaveColor(vec2 screenUV){
  if(uWavesEnabled<.5)return uPaper;
  vec2 uv=(screenUV-.5)*uWaveView+.5;
  vec2 cell=vec2(1./uWaveGrid);
  float l=texture2D(uWaveHeight,uv-vec2(cell.x,0.)).r;
  float r=texture2D(uWaveHeight,uv+vec2(cell.x,0.)).r;
  float b=texture2D(uWaveHeight,uv-vec2(0.,cell.y)).r;
  float t=texture2D(uWaveHeight,uv+vec2(0.,cell.y)).r;
  vec2 slope=vec2(r-l,t-b)/(2.*cell);
  vec3 N=normalize(vec3(-slope*.9,1.));
  vec3 L=normalize(vec3(-.65,.8,1.8));
  vec3 H=normalize(L+vec3(0.,0.,1.));
  float diffuse=dot(N,L)-L.z;
  float specular=pow(max(dot(N,H),0.),32.)-pow(H.z,32.);
  float shade=clamp((diffuse*.17+specular*.018)*uWaveClarity,-.075,.045);
  return clamp(uPaper+vec3(shade),0.,1.);
}
`;
