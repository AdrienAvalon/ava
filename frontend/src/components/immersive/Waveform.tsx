import { useEffect, useRef } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { STATES } from './immersiveStates';

const BARS = 56;

export function Waveform() {
  const state = useImmersiveStore((s) => s.state);
  const barsRef = useRef<(HTMLDivElement | null)[]>([]);
  const valuesRef = useRef<Float32Array>(new Float32Array(BARS));
  const targetsRef = useRef<Float32Array>(new Float32Array(BARS));

  useEffect(() => {
    const id = setInterval(() => {
      const amp = STATES[state].waveAmp;
      const color = STATES[state].colorA;
      for (let i = 0; i < BARS; i++) {
        const center = 1 - Math.abs(i - BARS / 2) / (BARS / 2);
        const rnd = Math.random() * Math.random();
        targetsRef.current[i] = rnd * amp * (0.4 + center * 0.6);
      }
      for (let i = 0; i < BARS; i++) {
        valuesRef.current[i] += (targetsRef.current[i] - valuesRef.current[i]) * 0.25;
        const bar = barsRef.current[i];
        if (bar) {
          bar.style.height = Math.max(2, valuesRef.current[i] * 38) + 'px';
          bar.style.background = color;
        }
      }
    }, 50);
    return () => clearInterval(id);
  }, [state]);

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 80,
        left: '50%',
        transform: 'translateX(-50%)',
        display: 'flex',
        gap: 2,
        alignItems: 'center',
        height: 40,
        zIndex: 80,
        pointerEvents: 'none',
        opacity: 0.8,
      }}
    >
      {Array.from({ length: BARS }).map((_, i) => (
        <div
          key={i}
          ref={(el) => { barsRef.current[i] = el; }}
          style={{
            width: 2,
            background: '#51a4de',
            borderRadius: 1,
            minHeight: 2,
            boxShadow: '0 0 4px currentColor',
            transition: 'height 60ms linear, background-color 0.3s',
          }}
        />
      ))}
    </div>
  );
}
