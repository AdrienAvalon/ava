import { useCallback, useRef } from 'react';

// Debug logs are gated on import.meta.env.DEV or localStorage.AVA_DEBUG=1 to
// keep production console clean. Toggle in DevTools: localStorage.AVA_DEBUG=1
const _debugEnabled = (): boolean =>
  Boolean(import.meta.env.DEV) ||
  (typeof localStorage !== 'undefined' && localStorage.getItem('AVA_DEBUG') === '1');
// eslint-disable-next-line no-console, @typescript-eslint/no-explicit-any
const debug = (...args: any[]): void => { if (_debugEnabled()) console.log(...args); };
// eslint-disable-next-line no-console, @typescript-eslint/no-explicit-any
const debugWarn = (...args: any[]): void => { if (_debugEnabled()) console.warn(...args); };

/**
 * Captures the microphone via MediaRecorder, sends the audio blob to
 * the Whisper transcription endpoint, and returns the recognized text.
 *
 * Usage:
 *   const { start, stopAndTranscribe, cancel } = useVoiceCapture();
 *   await start();   // opens mic
 *   const text = await stopAndTranscribe();  // returns Whisper output
 */
export function useVoiceCapture() {
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const start = useCallback(async () => {
    if (recorderRef.current) return;
    debug('[AvaMic] start() — requesting mic');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    streamRef.current = stream;
    debug('[AvaMic] stream granted, tracks=', stream.getAudioTracks().length);
    // Pick the best supported mime type (Firefox prefers ogg/opus, Chrome webm)
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    const mime = candidates.find((m) => MediaRecorder.isTypeSupported(m)) || '';
    debug('[AvaMic] selected mime:', mime || '(default)');
    const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    chunksRef.current = [];
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        chunksRef.current.push(e.data);
        debug('[AvaMic] chunk', e.data.size, 'bytes (total chunks', chunksRef.current.length + ')');
      }
    };
    // Use a 250ms timeslice so dataavailable fires while recording (Firefox-safe)
    rec.start(250);
    recorderRef.current = rec;
    debug('[AvaMic] recorder started, state=', rec.state);
  }, []);

  const cleanup = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  const cancel = useCallback(() => {
    try {
      recorderRef.current?.stop();
    } catch {
      /* ignore */
    }
    cleanup();
  }, [cleanup]);

  const stopAndTranscribe = useCallback(async (): Promise<string> => {
    const rec = recorderRef.current;
    if (!rec) {
      debugWarn('[AvaMic] stopAndTranscribe called without an active recorder');
      return '';
    }
    const mime = rec.mimeType || 'audio/webm';
    const ext = /webm/.test(mime) ? 'webm' : /ogg/.test(mime) ? 'ogg' : /mp4/.test(mime) ? 'm4a' : 'wav';
    const blob: Blob = await new Promise((resolve) => {
      rec.onstop = () => resolve(new Blob(chunksRef.current, { type: mime }));
      try {
        rec.stop();
      } catch {
        resolve(new Blob([], { type: mime }));
      }
    });
    cleanup();
    debug('[AvaMic] stopped. blob.size=', blob.size, 'mime=', mime, 'ext=', ext);
    if (blob.size === 0) {
      debugWarn('[AvaMic] EMPTY blob — MediaRecorder did not emit any data. Try holding the button longer (> 500 ms) or check browser mic permission.');
      return '';
    }
    const fd = new FormData();
    fd.append('file', blob, `audio.${ext}`);
    fd.append('language', 'fr');
    debug('[AvaMic] POST /v1/speech/transcribe', blob.size, 'bytes');
    const resp = await fetch('/v1/speech/transcribe', { method: 'POST', body: fd });
    if (!resp.ok) {
      debugWarn('[AvaMic] transcribe HTTP', resp.status);
      throw new Error(`transcribe HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const text = String(data?.text ?? '').trim();
    debug('[AvaMic] whisper text:', text ? JSON.stringify(text) : '(EMPTY — silence or unrecognized)');
    return text;
  }, [cleanup]);

  const isRecording = useCallback(() => recorderRef.current?.state === 'recording', []);

  return { start, stopAndTranscribe, cancel, isRecording };
}
