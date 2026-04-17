import { useEffect, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { STATES } from './immersiveStates';

export function ConversationCinetic() {
  const userMsg = useImmersiveStore((s) => s.userMsg);
  const avaMsg = useImmersiveStore((s) => s.avaMsg);
  const state = useImmersiveStore((s) => s.state);
  const cfg = STATES[state];

  // Blinking cursor shown only when avaMsg is non-empty
  const [blink, setBlink] = useState(true);
  useEffect(() => {
    const id = setInterval(() => setBlink((b) => !b), 450);
    return () => clearInterval(id);
  }, []);

  return (
    <div
      style={{
        position: 'fixed',
        top: '50%',
        left: '50%',
        transform: 'translate(-50%, -50%)',
        width: 'min(900px, 72vw)',
        maxHeight: '60vh',
        zIndex: 90,
        pointerEvents: 'none',
        textAlign: 'center',
      }}
    >
      {userMsg && (
        <div
          style={{
            fontSize: 12,
            color: '#5a7a95',
            letterSpacing: '0.3em',
            textTransform: 'uppercase',
            marginBottom: '1.4rem',
            fontFamily: "'JetBrains Mono', 'Space Mono', 'Courier New', monospace",
          }}
        >
          <span style={{ color: '#2a5a7a', fontWeight: 600 }}>⌈ ADRIEN ⌉&nbsp;&nbsp;</span>
          {userMsg}
        </div>
      )}
      <div
        style={{
          fontFamily: "'Courier New', 'Space Mono', monospace",
          fontSize: 34,
          fontWeight: 300,
          letterSpacing: '0.02em',
          lineHeight: 1.4,
          color: cfg.colorB,
          textShadow: `0 0 28px ${hexToRgba(cfg.colorA, 0.45)}`,
          minHeight: '2.2em',
          transition: 'color 0.8s, text-shadow 0.8s',
        }}
      >
        {avaMsg}
        {avaMsg && (
          <span
            style={{
              display: 'inline-block',
              width: 3,
              height: '1em',
              background: 'currentColor',
              marginLeft: 3,
              verticalAlign: 'middle',
              opacity: blink ? 1 : 0,
            }}
          />
        )}
      </div>
    </div>
  );
}

function hexToRgba(hex: string, alpha: number): string {
  const m = hex.replace('#', '');
  const r = parseInt(m.substring(0, 2), 16);
  const g = parseInt(m.substring(2, 4), 16);
  const b = parseInt(m.substring(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
