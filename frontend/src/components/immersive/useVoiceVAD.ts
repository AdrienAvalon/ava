import { useCallback, useEffect, useRef, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';

// Load at runtime to keep the main bundle small — onnxruntime-web is ~1.6 MB.
type MicVADType = {
  start: () => void;
  pause: () => void;
  destroy: () => void;
  stream?: MediaStream;
};

interface VADOptions {
  onSpeechEnd: (audio: Float32Array) => void | Promise<void>;
  onSpeechStart?: () => void;
}

/**
 * Continuous voice activity detection via silero VAD (browser-side ONNX).
 *
 * Captures the microphone permanently (when active), detects speech frames,
 * and invokes `onSpeechEnd(audio)` as a Float32Array @ 16 kHz when the user
 * stops talking. Also auto-pauses while Ava is speaking to avoid feedback.
 */
export function useVoiceVAD(opts: VADOptions) {
  const vadRef = useRef<MicVADType | null>(null);
  const [active, setActive] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const avaState = useImmersiveStore((s) => s.state);
  const pausedByAvaRef = useRef(false);
  const optsRef = useRef(opts);
  optsRef.current = opts;

  const start = useCallback(async () => {
    if (vadRef.current || active) return;
    try {
      // Dynamic import so we only pay the cost when the user enables VAD
      const mod = await import('@ricky0123/vad-web');
      // onnxruntime-web wasm files are self-hosted under /onnx/ (copied at
      // build time from node_modules/onnxruntime-web/dist/). Avoids depending
      // on jsDelivr from the CSP.
      try {
        const ort = await import('onnxruntime-web');
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (ort as any).env.wasm.wasmPaths = '/onnx/';
      } catch { /* fallback to default paths (will 404) */ }
      const vad = (await mod.MicVAD.new({
        onSpeechStart: () => {
          setSpeaking(true);
          optsRef.current.onSpeechStart?.();
        },
        onSpeechEnd: (audio: Float32Array) => {
          setSpeaking(false);
          optsRef.current.onSpeechEnd(audio);
        },
        // Tolerant thresholds — avoid false positives from ambient noise
        positiveSpeechThreshold: 0.55,
        negativeSpeechThreshold: 0.40,
      })) as unknown as MicVADType;
      vad.start();
      vadRef.current = vad;
      setActive(true);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('VAD init failed:', (e as Error).message);
      setActive(false);
    }
  }, [active]);

  const stop = useCallback(() => {
    try {
      const vad = vadRef.current;
      vad?.pause();
      vad?.destroy();
      // @ricky0123/vad-web does not always release the underlying MediaStream
      // on destroy() — kill tracks ourselves to clear the browser mic LED.
      vad?.stream?.getTracks().forEach((t) => t.stop());
    } catch {
      /* ignore */
    }
    vadRef.current = null;
    setActive(false);
    setSpeaking(false);
    pausedByAvaRef.current = false;
  }, []);

  // Auto-pause VAD when Ava is speaking (echo prevention)
  useEffect(() => {
    const vad = vadRef.current;
    if (!active || !vad) return;
    const shouldPause = avaState === 'speaking';
    if (shouldPause && !pausedByAvaRef.current) {
      try { vad.pause(); } catch { /* ignore */ }
      pausedByAvaRef.current = true;
    } else if (!shouldPause && pausedByAvaRef.current) {
      try { vad.start(); } catch { /* ignore */ }
      pausedByAvaRef.current = false;
    }
  }, [avaState, active]);

  // Cleanup on unmount
  useEffect(() => () => { stop(); }, [stop]);

  return { start, stop, active, speaking };
}

/** Convert a Float32Array @ sampleRate into a mono 16-bit PCM WAV Blob. */
export function float32ToWavBlob(audio: Float32Array, sampleRate = 16000): Blob {
  const length = audio.length;
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);

  const writeStr = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);       // PCM chunk size
  view.setUint16(20, 1, true);        // format = 1 (PCM)
  view.setUint16(22, 1, true);        // channels = mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);        // block align
  view.setUint16(34, 16, true);       // bits per sample
  writeStr(36, 'data');
  view.setUint32(40, length * 2, true);

  let offset = 44;
  for (let i = 0; i < length; i++) {
    const s = Math.max(-1, Math.min(1, audio[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}
