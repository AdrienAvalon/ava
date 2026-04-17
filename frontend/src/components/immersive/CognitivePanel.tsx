import { useImmersiveStore } from './immersiveStore';

const ROWS: Array<{ key: keyof import('./immersiveStore').CognitiveSignals; label: string; fallback: string }> = [
  { key: 'intent',     label: 'INTENT',    fallback: '—' },
  { key: 'focus',      label: 'FOCUS',     fallback: '—' },
  { key: 'tool',       label: 'TOOL',      fallback: '—' },
  { key: 'reflection', label: 'RÉFLEXION', fallback: '0 tours' },
  { key: 'tone',       label: 'TON',       fallback: 'neutre' },
  { key: 'memory',     label: 'MÉMOIRE',   fallback: '— entrées' },
];

export function CognitivePanel() {
  const cognitive = useImmersiveStore((s) => s.cognitive);

  return (
    <div
      style={{
        position: 'fixed',
        top: '50%',
        right: 70,
        transform: 'translateY(-50%)',
        zIndex: 95,
        pointerEvents: 'none',
        fontSize: 10,
        letterSpacing: '0.22em',
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        alignItems: 'flex-end',
        fontFamily: "'JetBrains Mono', 'Space Mono', 'Courier New', monospace",
      }}
    >
      {ROWS.map(({ key, label, fallback }) => {
        const v = cognitive[key];
        const active = v !== null && v !== '';
        return (
          <div key={key} style={{ display: 'flex', gap: 10, alignItems: 'baseline', opacity: active ? 1 : 0.65, transition: 'opacity 0.3s' }}>
            <span style={{ color: '#5a7a95', fontSize: 9 }}>{label}</span>
            <span style={{
              color: active ? '#7fb9e8' : '#2a5a7a',
              minWidth: 130,
              textAlign: 'right',
              fontWeight: 500,
              opacity: active ? 1 : 0.5,
            }}>
              {v ?? fallback}
            </span>
          </div>
        );
      })}
    </div>
  );
}
