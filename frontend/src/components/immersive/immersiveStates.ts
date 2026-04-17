export type ImmersiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

export interface StateConfig {
  colorA: string;
  colorB: string;
  noiseAmp: number;
  breathing: number;
  speed: number;
  radius: number;
  pointSize: number;
  linkOp: number;
  wireOp: number;
  waveAmp: number;
}

export const STATES: Record<ImmersiveState, StateConfig> = {
  idle:      { colorA: '#3a8fc8', colorB: '#a5d5f5', noiseAmp: 0.05, breathing: 0.4, speed: 0.22, radius: 1.25, pointSize: 0.50, linkOp: 0.18, wireOp: 0.08, waveAmp: 0.08 },
  listening: { colorA: '#4fd8c5', colorB: '#7fb9e8', noiseAmp: 0.08, breathing: 0.8, speed: 0.5,  radius: 1.30, pointSize: 0.55, linkOp: 0.26, wireOp: 0.10, waveAmp: 0.5 },
  thinking:  { colorA: '#9a8de0', colorB: '#7fb9e8', noiseAmp: 0.13, breathing: 1.2, speed: 1.0,  radius: 1.20, pointSize: 0.50, linkOp: 0.42, wireOp: 0.14, waveAmp: 0.15 },
  speaking:  { colorA: '#e86a89', colorB: '#c4a5d5', noiseAmp: 0.10, breathing: 1.1, speed: 1.4,  radius: 1.35, pointSize: 0.62, linkOp: 0.30, wireOp: 0.12, waveAmp: 1.0 },
};
