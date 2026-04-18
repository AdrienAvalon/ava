import { useCallback, useRef } from 'react';

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
    // eslint-disable-next-line no-console
    console.log('[AvaMic] start() — requesting mic');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    streamRef.current = stream;
    // eslint-disable-next-line no-console
    console.log('[AvaMic] stream granted, tracks=', stream.getAudioTracks().length);
    // Pick the best supported mime type (Firefox prefers ogg/opus, Chrome webm)
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    const mime = candidates.find((m) => MediaRecorder.isTypeSupported(m)) || '';
    // eslint-disable-next-line no-console
    console.log('[AvaMic] selected mime:', mime || '(default)');
    const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    chunksRef.current = [];
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        chunksRef.current.push(e.data);
        // eslint-disable-next-line no-console
        console.log('[AvaMic] chunk', e.data.size, 'bytes (total chunks', chunksRef.current.length + ')');
      }
    };
    // Use a 250ms timeslice so dataavailable fires while recording (Firefox-safe)
    rec.start(250);
    recorderRef.current = rec;
    // eslint-disable-next-line no-console
    console.log('[AvaMic] recorder started, state=', rec.state);
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
      // eslint-disable-next-line no-console
      console.warn('[AvaMic] stopAndTranscribe called without an active recorder');
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
    // eslint-disable-next-line no-console
    console.log('[AvaMic] stopped. blob.size=', blob.size, 'mime=', mime, 'ext=', ext);
    if (blob.size === 0) {
      // eslint-disable-next-line no-console
      console.warn('[AvaMic] EMPTY blob — MediaRecorder did not emit any data. Try holding the button longer (> 500 ms) or check browser mic permission.');
      return '';
    }
    const fd = new FormData();
    fd.append('file', blob, `audio.${ext}`);
    fd.append('language', 'fr');
    // eslint-disable-next-line no-console
    console.log('[AvaMic] POST /v1/speech/transcribe', blob.size, 'bytes');
    const resp = await fetch('/v1/speech/transcribe', { method: 'POST', body: fd });
    if (!resp.ok) {
      // eslint-disable-next-line no-console
      console.warn('[AvaMic] transcribe HTTP', resp.status);
      throw new Error(`transcribe HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const text = String(data?.text ?? '').trim();
    // eslint-disable-next-line no-console
    console.log('[AvaMic] whisper text:', text ? JSON.stringify(text) : '(EMPTY — silence or unrecognized)');
    return text;
  }, [cleanup]);

  const isRecording = useCallback(() => recorderRef.current?.state === 'recording', []);

  return { start, stopAndTranscribe, cancel, isRecording };
}
