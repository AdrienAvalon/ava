import { glslNoise3D } from './simplexNoise';

export const particleVertex = /* glsl */ `
  attribute float aSeed;
  uniform float time;
  uniform float breathing;
  uniform float noiseScale;
  uniform float noiseAmplitude;
  uniform float pointSize;
  uniform float radius;
  varying float vDist;
  varying float vSeed;
  ${glslNoise3D}
  void main() {
    vec3 p = position;
    float t = time * 0.25;
    float n = snoise(p * noiseScale + vec3(t, t*0.6, -t*0.8));
    float disp = n * noiseAmplitude + breathing * 0.12 * sin(time * 1.2 + aSeed * 6.283);
    vec3 displaced = p * (radius + disp);
    vec4 mv = modelViewMatrix * vec4(displaced, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = pointSize * (260.0 / -mv.z) * (0.6 + 0.4 * aSeed);
    vDist = disp;
    vSeed = aSeed;
  }
`;

export const particleFragment = /* glsl */ `
  uniform vec3 colorA;
  uniform vec3 colorB;
  uniform float opacity;
  varying float vDist;
  varying float vSeed;
  void main() {
    vec2 uv = gl_PointCoord - 0.5;
    float d = length(uv);
    if (d > 0.5) discard;
    float a = smoothstep(0.5, 0.15, d);
    float mixF = clamp(vDist * 3.5 + 0.25, 0.0, 1.0);
    vec3 col = mix(colorA, colorB, mixF);
    gl_FragColor = vec4(col * a, a * opacity);
  }
`;

export const lineVertex = /* glsl */ `
  attribute float aSeed;
  uniform float time;
  uniform float breathing;
  uniform float noiseScale;
  uniform float noiseAmplitude;
  uniform float radius;
  varying float vFade;
  ${glslNoise3D}
  void main() {
    vec3 p = position;
    float t = time * 0.25;
    float n = snoise(p * noiseScale + vec3(t, t*0.6, -t*0.8));
    float disp = n * noiseAmplitude + breathing * 0.12 * sin(time * 1.2 + aSeed * 6.283);
    vec3 displaced = p * (radius + disp);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(displaced, 1.0);
    vFade = 0.35 + 0.65 * (0.5 + 0.5 * sin(time * 0.4 + aSeed * 12.0));
  }
`;

export const lineFragment = /* glsl */ `
  uniform vec3 color;
  uniform float opacity;
  varying float vFade;
  void main() {
    gl_FragColor = vec4(color, opacity * vFade);
  }
`;
