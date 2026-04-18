import { useEffect, useRef, useState } from 'react';
import { Mic, MicOff, Loader2 } from 'lucide-react';
import { useVoiceCapture } from './useVoiceCapture';
import { useViewportScale } from './useViewportScale';

interface MicButtonProps {
  onTranscript: (text: string) => void;
  disabled?: boolean;
}

/**
 * Push-to-talk microphone button.
 *   - Click to start/stop a capture session.
 *   - Hold spacebar to record (when no input is focused).
 *   - Recording → ripple animation on the orb palette.
 *   - After release → transcription (Whisper backend) → onTranscript.
 */
export function MicButton({ onTranscript, disabled }: MicButtonProps) {
  const capture = useVoiceCapture();
  const [state, setState] = useState<'idle' | 'recording' | 'transcribing'>('idle');
  const vp = useViewportScale();
  const errorRef = useRef<string | null>(null);

  const isBusy = state !== 'idle';

  async function startRec() {
    if (disabled || isBusy) return;
    errorRef.current = null;
    try {
      await capture.start();
      setState('recording');
    } catch (e) {
      errorRef.current = (e as Error).message || 'mic error';
      setState('idle');
      // eslint-disable-next-line no-console
      console.warn('Mic start failed:', e);
    }
  }

  async function stopAndSend() {
    if (state !== 'recording') return;
    setState('transcribing');
    try {
      const text = await capture.stopAndTranscribe();
      if (text) onTranscript(text);
    } catch (e) {
      errorRef.current = (e as Error).message || 'transcribe error';
      // eslint-disable-next-line no-console
      console.warn('Transcribe failed:', e);
    } finally {
      setState('idle');
    }
  }

  function onClick() {
    if (state === 'idle') startRec();
    else if (state === 'recording') stopAndSend();
  }

  // Hold-spacebar push-to-talk (ignored when typing in an input)
  useEffect(() => {
    const isTyping = () => {
      const el = document.activeElement;
      return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement;
    };
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.repeat) return;
      if (isTyping() || disabled) return;
      e.preventDefault();
      if (state === 'idle') startRec();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space') return;
      if (isTyping()) return;
      if (state === 'recording') {
        e.preventDefault();
        stopAndSend();
      }
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, disabled]);

  const size = vp.isMobile ? 52 : 60;
  const color =
    state === 'recording' ? '#ff6b8a' :
    state === 'transcribing' ? '#d4c4ff' :
    '#51a4de';

  const label =
    state === 'recording' ? 'STOP' :
    state === 'transcribing' ? '...' :
    'PARLER';

  return (
    <div
      style={{
        position: 'fixed',
        bottom: vp.isMobile ? 150 : 210,
        left: '50%',
        transform: 'translateX(calc(-50% - 55px))',
        zIndex: 150,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 6,
        pointerEvents: 'auto',
      }}
    >
      <button
        onClick={onClick}
        disabled={disabled || state === 'transcribing'}
        title="Parler à Ava (ou maintenir la barre espace)"
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          background: state === 'recording' ? 'rgba(232,106,137,0.18)' : 'rgba(81,164,222,0.10)',
          border: `1.5px solid ${color}`,
          color,
          cursor: disabled || state === 'transcribing' ? 'not-allowed' : 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transition: 'all 0.2s',
          boxShadow: state === 'recording' ? `0 0 24px ${color}aa` : `0 0 8px ${color}33`,
          animation: state === 'recording' ? 'avaMicPulse 1.1s ease-in-out infinite' : undefined,
        }}
      >
        {state === 'transcribing'
          ? <Loader2 size={24} style={{ animation: 'avaSpin 0.9s linear infinite' }} />
          : state === 'recording'
          ? <MicOff size={24} />
          : <Mic size={24} />}
      </button>
      <div
        style={{
          fontFamily: "'JetBrains Mono', 'Space Mono', monospace",
          fontSize: 9,
          letterSpacing: '0.28em',
          color,
          opacity: 0.9,
          textTransform: 'uppercase',
        }}
      >
        {label}
      </div>
      <style>{`
        @keyframes avaMicPulse {
          0%, 100% { transform: scale(1); box-shadow: 0 0 24px ${color}aa; }
          50% { transform: scale(1.07); box-shadow: 0 0 40px ${color}ff; }
        }
        @keyframes avaSpin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
