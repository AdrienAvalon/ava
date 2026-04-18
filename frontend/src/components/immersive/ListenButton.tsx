import { useRef, useState } from 'react';
import { Ear, EarOff, Loader2 } from 'lucide-react';
import { useVoiceVAD, float32ToWavBlob } from './useVoiceVAD';
import { useViewportScale } from './useViewportScale';
import { useImmersiveStore } from './immersiveStore';

interface ListenButtonProps {
  onTranscript: (text: string) => void;
  disabled?: boolean;
}

/**
 * Toggle for continuous listening via silero VAD (browser-side).
 * When ON, speech is auto-detected, transcribed via Whisper, and sent to Ava.
 * Auto-pauses while Ava is speaking to avoid self-triggering.
 */
export function ListenButton({ onTranscript, disabled }: ListenButtonProps) {
  const vp = useViewportScale();
  const avaState = useImmersiveStore((s) => s.state);
  const [transcribing, setTranscribing] = useState(false);
  const latestTranscribePromise = useRef<Promise<void> | null>(null);

  const vad = useVoiceVAD({
    onSpeechEnd: async (audio) => {
      if (audio.length < 16000 * 0.3) return; // < 300ms → ignore noise
      const blob = float32ToWavBlob(audio, 16000);
      const p = (async () => {
        setTranscribing(true);
        try {
          const fd = new FormData();
          fd.append('file', blob, 'speech.wav');
          fd.append('language', 'fr');
          const resp = await fetch('/v1/speech/transcribe', { method: 'POST', body: fd });
          if (!resp.ok) throw new Error(`transcribe HTTP ${resp.status}`);
          const data = await resp.json();
          const text = String(data?.text ?? '').trim();
          if (text) onTranscript(text);
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn('VAD transcribe failed:', (e as Error).message);
        } finally {
          setTranscribing(false);
        }
      })();
      latestTranscribePromise.current = p;
    },
  });

  function handleToggle() {
    if (disabled) return;
    if (vad.active) vad.stop();
    else vad.start();
  }

  const size = vp.isMobile ? 44 : 50;

  // Color: accent rose when hearing you speak, violet during transcribe, cyan otherwise
  const color =
    transcribing ? '#d4c4ff' :
    vad.speaking ? '#ff6b8a' :
    vad.active ? '#4fd8c5' :
    '#51a4de';

  const label =
    transcribing ? 'TRANSCRIPTION' :
    vad.speaking ? 'J\'ÉCOUTE' :
    vad.active ? 'ÉCOUTE CONTINUE' :
    'ÉCOUTE CONTINUE OFF';

  const pausedByAva = vad.active && avaState === 'speaking' && !vad.speaking && !transcribing;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: vp.isMobile ? 152 : 212,
        left: '50%',
        transform: 'translateX(calc(-50% + 55px))',
        zIndex: 150,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 4,
        pointerEvents: 'auto',
      }}
    >
      <button
        onClick={handleToggle}
        disabled={disabled}
        title="Activer/désactiver l'écoute continue (silero VAD)"
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          background: vad.active ? 'rgba(79,216,197,0.12)' : 'rgba(81,164,222,0.06)',
          border: `1px solid ${color}`,
          color,
          cursor: disabled ? 'not-allowed' : 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transition: 'all 0.2s',
          boxShadow: vad.speaking ? `0 0 20px ${color}aa` : vad.active ? `0 0 8px ${color}44` : 'none',
          animation: vad.speaking ? 'avaListenPulse 0.9s ease-in-out infinite' : undefined,
          opacity: pausedByAva ? 0.5 : 1,
        }}
      >
        {transcribing
          ? <Loader2 size={18} style={{ animation: 'avaSpin 0.9s linear infinite' }} />
          : vad.active
          ? <Ear size={18} />
          : <EarOff size={18} />}
      </button>
      <div
        style={{
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: 8,
          letterSpacing: '0.25em',
          color,
          opacity: 0.75,
          textTransform: 'uppercase',
          whiteSpace: 'nowrap',
        }}
      >
        {label}
      </div>
      <style>{`
        @keyframes avaListenPulse {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.08); }
        }
      `}</style>
    </div>
  );
}
