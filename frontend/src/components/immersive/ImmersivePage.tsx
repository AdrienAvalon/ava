import { useImmersiveStore } from './immersiveStore';
import { OrbAura } from './OrbAura';
import { ConversationCinetic } from './ConversationCinetic';
import { CognitivePanel } from './CognitivePanel';
import { Waveform } from './Waveform';
import { HudLayers } from './HudLayers';
import { StateRings } from './StateRings';
import { useScenarioPlayer } from './useScenarioPlayer';
import type { ImmersiveState } from './immersiveStates';

const STATE_ORDER: ImmersiveState[] = ['idle', 'listening', 'thinking', 'speaking'];

export function ImmersivePage() {
  const state = useImmersiveStore((s) => s.state);
  const setState = useImmersiveStore((s) => s.setState);
  useScenarioPlayer(true);

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: '#000',
        color: '#b8cfe0',
        fontFamily: "'JetBrains Mono', 'Space Mono', 'Courier New', monospace",
        overflow: 'hidden',
      }}
    >
      {/* Global scanline overlay */}
      <div style={{
        position: 'fixed', inset: 0,
        background: 'repeating-linear-gradient(0deg, rgba(81,164,222,0.03) 0px, rgba(81,164,222,0.03) 1px, transparent 1px, transparent 3px)',
        pointerEvents: 'none', zIndex: 1000,
      }} />
      {/* CRT vignette */}
      <div style={{
        position: 'fixed', inset: 0,
        background: 'radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.8) 100%)',
        pointerEvents: 'none', zIndex: 999,
      }} />

      <OrbAura state={state} />
      <StateRings />
      <HudLayers />
      <CognitivePanel />
      <ConversationCinetic />
      <Waveform />

      {/* Manual state controls (bottom-center) */}
      <div
        style={{
          position: 'fixed',
          bottom: 28,
          left: '50%',
          transform: 'translateX(-50%)',
          display: 'flex',
          gap: 6,
          zIndex: 200,
        }}
      >
        {STATE_ORDER.map((s) => (
          <button
            key={s}
            onClick={() => setState(s)}
            style={{
              background: state === s ? 'rgba(81,164,222,0.3)' : 'rgba(81,164,222,0.08)',
              border: `1px solid ${state === s ? '#7fb9e8' : 'rgba(81,164,222,0.3)'}`,
              color: state === s ? '#fff' : '#51a4de',
              fontFamily: 'inherit',
              fontSize: 11,
              letterSpacing: '0.25em',
              padding: '9px 18px',
              cursor: 'pointer',
              textTransform: 'uppercase',
              boxShadow: state === s ? '0 0 16px rgba(81,164,222,0.5)' : 'none',
              transition: 'all 0.15s',
            }}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

export default ImmersivePage;
