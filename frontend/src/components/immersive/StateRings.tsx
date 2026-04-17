import { useEffect, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { STATES } from './immersiveStates';

/** Concentric SVG rings + corner brackets (targeting reticle GitS) + ripple on state change. */
export function StateRings() {
  const rippleKey = useImmersiveStore((s) => s.rippleKey);
  const state = useImmersiveStore((s) => s.state);
  const color = STATES[state].colorA;

  // Bump ripple on key change
  const [animKey, setAnimKey] = useState(0);
  useEffect(() => {
    if (rippleKey > 0) setAnimKey((k) => k + 1);
  }, [rippleKey]);

  return (
    <>
      {/* SVG rings (background) */}
      <svg
        viewBox="-340 -340 680 680"
        style={{
          position: 'fixed',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          width: 680,
          height: 680,
          zIndex: 2,
          pointerEvents: 'none',
          opacity: 0.4,
        }}
      >
        <circle cx="0" cy="0" r="320" fill="none" stroke="#51a4de" strokeWidth="0.5" strokeDasharray="2,6" opacity="0.3">
          <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="80s" repeatCount="indefinite" />
        </circle>
        <circle cx="0" cy="0" r="240" fill="none" stroke="#51a4de" strokeWidth="0.5" opacity="0.4" />
        <circle cx="0" cy="0" r="240" fill="none" stroke="#51a4de" strokeWidth="1" strokeDasharray="1,120" opacity="0.9">
          <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="14s" repeatCount="indefinite" />
        </circle>
        <circle cx="0" cy="0" r="180" fill="none" stroke="#51a4de" strokeWidth="0.3" opacity="0.25" />
        <line x1="-320" y1="0" x2="-260" y2="0" stroke="#51a4de" strokeWidth="0.5" opacity="0.4" />
        <line x1="260" y1="0" x2="320" y2="0" stroke="#51a4de" strokeWidth="0.5" opacity="0.4" />
        <line x1="0" y1="-320" x2="0" y2="-260" stroke="#51a4de" strokeWidth="0.5" opacity="0.4" />
        <line x1="0" y1="260" x2="0" y2="320" stroke="#51a4de" strokeWidth="0.5" opacity="0.4" />
        <g stroke="#51a4de" strokeWidth="1" fill="none" opacity="0.5">
          <polyline points="-230,-200 -230,-230 -200,-230" />
          <polyline points="200,-230 230,-230 230,-200" />
          <polyline points="230,200 230,230 200,230" />
          <polyline points="-200,230 -230,230 -230,200" />
        </g>
      </svg>

      {/* Ripple on state change */}
      <div
        key={animKey}
        style={{
          position: 'fixed',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%) scale(0.3)',
          width: 100,
          height: 100,
          border: `1px solid ${color}`,
          borderRadius: '50%',
          pointerEvents: 'none',
          zIndex: 5,
          opacity: 0,
          animation: animKey > 0 ? 'avaRipple 0.9s ease-out' : undefined,
        }}
      />
      <style>{`
        @keyframes avaRipple {
          0% { transform: translate(-50%, -50%) scale(0.3); opacity: 0.9; }
          60% { opacity: 0.3; }
          100% { transform: translate(-50%, -50%) scale(5); opacity: 0; }
        }
      `}</style>
    </>
  );
}
