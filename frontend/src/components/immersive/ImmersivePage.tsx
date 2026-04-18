import { useEffect } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Volume2, VolumeX } from 'lucide-react';
import { useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { OrbAura } from './OrbAura';
import { ConversationCinetic } from './ConversationCinetic';
import { CognitivePanel } from './CognitivePanel';
import { Waveform } from './Waveform';
import { HudLayers } from './HudLayers';
import { StateRings } from './StateRings';
import { AvaInput } from './AvaInput';
import { MicButton } from './MicButton';
import { ListenButton } from './ListenButton';
import { useDaemonChat } from './useDaemonChat';
import { useViewportScale } from './useViewportScale';

export function ImmersivePage() {
  const state = useImmersiveStore((s) => s.state);
  const navigate = useNavigate();
  const vp = useViewportScale();
  const [muted, setMutedState] = useState(false);
  const daemon = useDaemonChat();

  function toggleMute() {
    const next = !muted;
    setMutedState(next);
    daemon.setMuted(next);
  }

  // Esc → back to chat (but not while typing)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        const active = document.activeElement;
        if (active instanceof HTMLInputElement && active.value) return;
        e.preventDefault();
        navigate('/chat');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);

  function handleAsk(text: string) {
    daemon.ask(text);
  }

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

      <AvaInput onAsk={handleAsk} />
      <MicButton onTranscript={handleAsk} />
      <ListenButton onTranscript={handleAsk} />

      {/* Back to chat (top-left under brand, top-right on narrow) */}
      <button
        onClick={() => navigate('/chat')}
        title="Retour au chat classique (Esc)"
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

      {/* Mute toggle (top-right under status) */}
      <button
        onClick={toggleMute}
        title={muted ? 'Activer la voix d\'Ava' : 'Couper la voix d\'Ava'}
        style={{
          position: 'fixed',
          top: vp.smallHud ? 56 : 100,
          right: 32,
          zIndex: 210,
          background: muted ? 'rgba(232,106,137,0.12)' : 'rgba(81,164,222,0.08)',
          border: `1px solid ${muted ? 'rgba(232,106,137,0.4)' : 'rgba(81,164,222,0.3)'}`,
          color: muted ? '#e86a89' : '#51a4de',
          fontFamily: 'inherit',
          fontSize: vp.isMobile ? 9 : 10,
          letterSpacing: '0.25em',
          padding: '6px 12px',
          cursor: 'pointer',
          textTransform: 'uppercase',
          display: 'flex', alignItems: 'center', gap: 6,
          transition: 'all 0.15s',
        }}
      >
        {muted ? <><VolumeX size={12} /> MUTE</> : <><Volume2 size={12} /> VOIX</>}
      </button>
    </div>
  );
}

export default ImmersivePage;
