import { useEffect } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Volume2, VolumeX } from 'lucide-react';
import { useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { OrbAura } from './OrbAura';
import { ConversationCinetic } from './ConversationCinetic';
import { TranscriptTerminal } from './TranscriptTerminal';
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

      {/* ⚠ EN MODE CONVERSATION, L'ORBE VA DANS UN COIN — elle ne peut pas rester
          centrée : le terminal occupe désormais l'espace utile et la masquerait
          entièrement. Réduire son échelle ne suffisait pas (le canvas reste plein écran,
          donc l'orbe reste au milieu, simplement plus petite et toujours cachée).
          Elle est donc cadrée en haut à droite, au-dessus du terminal — visible comme
          signe de présence, sans disputer la place à la conversation. */}
      <OrbAura
        state={state}
        groupScale={vp.orbScale}
        cadre={
          vp.orbeACote
            ? // Portable : colonne de droite, sur toute la hauteur — l'orbe garde sa
              // présence sans recouvrir la conversation.
              { inset: 'auto', top: 0, right: 0, bottom: 0, width: '32vw' }
            : vp.chatFirst
              ? // Téléphone : vignette en haut à droite, SOUS le bandeau (topBar = 46)
                // pour ne pas passer derrière les boutons CHAT et VOIX.
                { inset: 'auto', top: 46, right: 0, width: 180, height: 180 }
              : undefined
        }
      />
      <StateRings />
      <HudLayers />
      {!vp.hideCognitive && <CognitivePanel />}
      {/* ⚠ PAS LES DEUX EN MODE CONVERSATION. `ConversationCinetic` affiche le dernier
          échange en très grand au centre de l'écran ; le terminal affiche le même
          échange, en bas de sa liste. Sur un téléphone où le terminal occupe désormais
          toute la hauteur utile, les deux se superposent et disent la même chose deux
          fois — le texte géant passant PAR-DESSUS l'historique. Sur grand écran ils
          cohabitent sans se gêner, et le focal garde tout son sens. */}
      {!vp.chatFirst && <ConversationCinetic />}
      <TranscriptTerminal />
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
          // ⚠ La droite du bandeau est désormais RÉSERVÉE aux boutons : `HudLayers` place
          //   identité et statut à GAUCHE en mode conversation. C'est ce qui rend la
          //   superposition impossible — deux tentatives précédentes ont échoué en
          //   déplaçant ce bouton de quelques pixels sans toucher à ce qu'il heurtait.
          top: vp.chatFirst ? 8 : vp.smallHud ? 52 : 100,
          left: vp.chatFirst || vp.smallHud ? 'auto' : 32,
          right: vp.chatFirst ? 88 : vp.smallHud ? 96 : 'auto',
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
          // Même rangée que CHAT (côte à côte, pas empilés — empilés, ils mangeraient la
          // hauteur que la conversation vient de récupérer).
          top: vp.chatFirst ? 8 : vp.smallHud ? 52 : 100,
          right: vp.chatFirst ? 10 : vp.smallHud ? 16 : 32,
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
