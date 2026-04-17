import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Play, Square } from 'lucide-react';
import { useImmersiveStore } from './immersiveStore';
import { OrbAura } from './OrbAura';
import { ConversationCinetic } from './ConversationCinetic';
import { CognitivePanel } from './CognitivePanel';
import { Waveform } from './Waveform';
import { HudLayers } from './HudLayers';
import { StateRings } from './StateRings';
import { AvaInput } from './AvaInput';
import { useScenarioPlayer } from './useScenarioPlayer';
import { useDaemonChat } from './useDaemonChat';
import { useViewportScale } from './useViewportScale';
import type { ImmersiveState } from './immersiveStates';

const STATE_ORDER: ImmersiveState[] = ['idle', 'listening', 'thinking', 'speaking'];

export function ImmersivePage() {
  const state = useImmersiveStore((s) => s.state);
  const setState = useImmersiveStore((s) => s.setState);
  const navigate = useNavigate();
  const vp = useViewportScale();
  const [demoMode, setDemoMode] = useState(false);
  const scenario = useScenarioPlayer(false); // don't autostart — wait for user toggle or input
  const daemon = useDaemonChat();

  // Toggle demo (PLAY / STOP)
  useEffect(() => {
    if (demoMode) {
      scenario.run();
    } else {
      scenario.stop();
    }
  }, [demoMode, scenario]);

  // Esc → back to chat
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // If typing in input, let default blur behavior first
        const active = document.activeElement;
        if (active instanceof HTMLInputElement && active.value) {
          return;
        }
        e.preventDefault();
        navigate('/');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);

  function handleAsk(text: string) {
    // If demo is running, stop it so we don't clash
    if (demoMode) setDemoMode(false);
    daemon.ask(text);
  }

  const btnSize = vp.isMobile ? 8 : vp.isTablet ? 9 : 11;
  const btnPad = vp.isMobile ? '6px 10px' : '9px 18px';

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

      <OrbAura state={state} groupScale={vp.orbScale} />
      <StateRings />
      <HudLayers />
      {!vp.hideCognitive && <CognitivePanel />}
      <ConversationCinetic />
      <Waveform />

      <AvaInput onAsk={handleAsk} disabled={demoMode} />

      {/* Back to chat */}
      <button
        onClick={() => navigate('/')}
        title="Retour au chat (Esc)"
        style={{
          position: 'fixed',
          top: vp.smallHud ? 16 : 100,
          left: vp.smallHud ? 'auto' : 32,
          right: vp.smallHud ? 16 : 'auto',
          zIndex: 210,
          background: 'rgba(81,164,222,0.08)',
          border: '1px solid rgba(81,164,222,0.3)',
          color: '#51a4de',
          fontFamily: 'inherit',
          fontSize: vp.isMobile ? 9 : 10,
          letterSpacing: '0.25em',
          padding: '6px 12px',
          cursor: 'pointer',
          textTransform: 'uppercase',
          display: 'flex', alignItems: 'center', gap: 6,
          transition: 'all 0.15s',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = 'rgba(81,164,222,0.2)';
          e.currentTarget.style.borderColor = '#51a4de';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = 'rgba(81,164,222,0.08)';
          e.currentTarget.style.borderColor = 'rgba(81,164,222,0.3)';
        }}
      >
        <ArrowLeft size={12} /> CHAT
      </button>

      {/* Play / Stop demo */}
      {!vp.isMobile && (
        <button
          onClick={() => setDemoMode(!demoMode)}
          title={demoMode ? 'Arrêter la démo scenario' : 'Lancer la démo scenario'}
          style={{
            position: 'fixed',
            top: 28,
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 210,
            background: demoMode ? 'rgba(232,106,137,0.15)' : 'rgba(81,164,222,0.08)',
            border: `1px solid ${demoMode ? 'rgba(232,106,137,0.5)' : 'rgba(81,164,222,0.3)'}`,
            color: demoMode ? '#e86a89' : '#51a4de',
            fontFamily: 'inherit',
            fontSize: 10,
            letterSpacing: '0.3em',
            padding: '6px 14px',
            cursor: 'pointer',
            textTransform: 'uppercase',
            display: 'flex', alignItems: 'center', gap: 8,
            transition: 'all 0.15s',
          }}
        >
          {demoMode ? <><Square size={10} /> STOP DEMO</> : <><Play size={10} /> PLAY DEMO</>}
        </button>
      )}

      {/* Manual state controls (bottom-center, hidden during demo to avoid confusion) */}
      {!demoMode && (
        <div
          style={{
            position: 'fixed',
            bottom: vp.isMobile ? 16 : 28,
            left: '50%',
            transform: 'translateX(-50%)',
            display: 'flex',
            gap: vp.isMobile ? 3 : 6,
            zIndex: 200,
            flexWrap: 'wrap',
            justifyContent: 'center',
            maxWidth: '90vw',
            opacity: 0.6,
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
                fontSize: btnSize,
                letterSpacing: '0.25em',
                padding: btnPad,
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
      )}
    </div>
  );
}

export default ImmersivePage;
