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
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    streamRef.current = stream;
    // Pick the best supported mime type
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    const mime = candidates.find((m) => MediaRecorder.isTypeSupported(m)) || '';
    const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    chunksRef.current = [];
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
    };
    rec.start();
    recorderRef.current = rec;
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
    if (!rec) return '';
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
    if (blob.size === 0) return '';
    const fd = new FormData();
    fd.append('file', blob, `audio.${ext}`);
    fd.append('language', 'fr');
    const resp = await fetch('/v1/speech/transcribe', { method: 'POST', body: fd });
    if (!resp.ok) {
      throw new Error(`transcribe HTTP ${resp.status}`);
    }
    const data = await resp.json();
    return String(data?.text ?? '').trim();
  }, [cleanup]);

  const isRecording = useCallback(() => recorderRef.current?.state === 'recording', []);

  return { start, stopAndTranscribe, cancel, isRecording };
}
