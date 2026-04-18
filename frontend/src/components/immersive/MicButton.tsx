import { useEffect, useState } from 'react';
import { Mic, MicOff, Loader2, MicVocal } from 'lucide-react';
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
  const [permission, setPermission] = useState<'unknown' | 'granted' | 'denied' | 'prompt'>('unknown');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const vp = useViewportScale();

  // Check mic permission at mount and keep it in sync
  useEffect(() => {
    let cancelled = false;
    let handle: PermissionStatus | null = null;
    (async () => {
      if (!('permissions' in navigator)) return;
      try {
        const p = await (navigator.permissions as unknown as {
          query: (d: { name: string }) => Promise<PermissionStatus>;
        }).query({ name: 'microphone' });
        if (cancelled) return;
        handle = p;
        setPermission(p.state as 'granted' | 'denied' | 'prompt');
        p.onchange = () => {
          if (!cancelled) setPermission(p.state as 'granted' | 'denied' | 'prompt');
        };
      } catch {
        /* browser without permissions.query → fall back to live attempts */
      }
    })();
    return () => { cancelled = true; if (handle) handle.onchange = null; };
  }, []);

  const isBusy = state !== 'idle';

  async function startRec() {
    if (disabled || isBusy) return;
    setErrorMsg(null);
    try {
      await capture.start();
      setState('recording');
    } catch (e) {
      const err = e as Error;
      setState('idle');
      if (err.name === 'NotAllowedError' || err.name === 'SecurityError') {
        setPermission('denied');
        setErrorMsg('Micro refusé. Autorise ava.avalon-network.com dans les paramètres du navigateur.');
      } else if (err.name === 'NotFoundError' || err.name === 'OverconstrainedError') {
        setErrorMsg('Aucun micro détecté.');
      } else {
        setErrorMsg(err.message || 'Erreur micro');
      }
      // eslint-disable-next-line no-console
      console.warn('Mic start failed:', err);
    }
  }

  async function stopAndSend() {
    if (state !== 'recording') return;
    setState('transcribing');
    try {
      const text = await capture.stopAndTranscribe();
      if (text) {
        setErrorMsg(null);
        onTranscript(text);
      } else {
        setErrorMsg("Je n'ai rien compris — réessaye un peu plus fort, ou plus proche du micro.");
        setTimeout(() => setErrorMsg(null), 4000);
      }
    } catch (e) {
      setErrorMsg((e as Error).message || 'transcribe error');
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

  // Safety net: stop recording after 15s if user forgets to release/click again
  useEffect(() => {
    if (state !== 'recording') return;
    const t = setTimeout(() => {
      // eslint-disable-next-line no-console
      console.log('[AvaMic] auto-stop after 15s');
      stopAndSend();
    }, 15000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

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
  const denied = permission === 'denied';
  const color =
    denied ? '#ff6b8a' :
    state === 'recording' ? '#ff6b8a' :
    state === 'transcribing' ? '#d4c4ff' :
    '#51a4de';

  const label =
    denied ? 'MIC REFUSÉ' :
    state === 'recording' ? 'STOP' :
    state === 'transcribing' ? '...' :
    'PARLER';

  return (
    <>
      {/* Big visible status banner top-center while active — so the user
          knows the click actually did something. */}
      {(state === 'recording' || state === 'transcribing') && (
        <div
          style={{
            position: 'fixed',
            top: vp.smallHud ? 120 : 150,
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 220,
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 13,
            letterSpacing: '0.4em',
            textTransform: 'uppercase',
            padding: '10px 22px',
            background: state === 'recording' ? 'rgba(232,106,137,0.14)' : 'rgba(212,196,255,0.14)',
            border: `1px solid ${state === 'recording' ? '#ff6b8a' : '#d4c4ff'}`,
            color: state === 'recording' ? '#ff6b8a' : '#d4c4ff',
            boxShadow: state === 'recording' ? '0 0 24px rgba(232,106,137,0.35)' : '0 0 18px rgba(212,196,255,0.25)',
            pointerEvents: 'none',
            whiteSpace: 'nowrap',
          }}
        >
          {state === 'recording'
            ? '🎙 ENREGISTREMENT... parle maintenant'
            : '💭 TRANSCRIPTION...'}
        </div>
      )}
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
        title={denied ? 'Micro refusé — clique sur le cadenas dans la barre URL pour autoriser' : 'Parler à Ava (ou maintenir la barre espace)'}
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
          : denied
          ? <MicOff size={24} />
          : <Mic size={24} />}
      </button>
      {errorMsg && (
        <div
          style={{
            position: 'absolute',
            bottom: -38,
            left: '50%',
            transform: 'translateX(-50%)',
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 9,
            color: '#ff6b8a',
            letterSpacing: '0.12em',
            whiteSpace: 'nowrap',
            maxWidth: 360,
            textAlign: 'center',
            pointerEvents: 'none',
          }}
        >
          {errorMsg}
        </div>
      )}
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
    </>
  );
}
