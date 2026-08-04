import { useCallback, useEffect, useRef } from 'react';

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
 * Libère le micro : arrête l'enregistreur puis toutes les pistes du flux.
 *
 * ⚠ FONCTION SÉPARÉE ET EXPORTÉE POUR ÊTRE TESTÉE SANS DOM. Tester le hook lui-même
 *   demanderait `jsdom` + `@testing-library/react` — deux dépendances pour vérifier
 *   une garantie que React fournit déjà (il APPELLE la fonction de nettoyage). Ce qui
 *   peut réellement casser, c'est la logique de libération : oublier les pistes, ou
 *   laisser `stop()` lever et interrompre le reste du nettoyage. C'est elle qu'on teste.
 *   La limite est assumée : rien ici ne prouve que le `useEffect` est bien branché.
 *
 * ⚠ `stop()` sur un enregistreur déjà inactif lève `InvalidStateError`. L'exception est
 *   absorbée **avant** l'arrêt des pistes : si elle remontait, le flux resterait ouvert
 *   et le voyant micro allumé — précisément le défaut qu'on corrige.
 */
export function libererMicro(
  recorder: { stop: () => void } | null,
  stream: { getTracks: () => Array<{ stop: () => void }> } | null,
): void {
  try {
    recorder?.stop();
  } catch {
    /* déjà arrêté */
  }
  stream?.getTracks().forEach((t) => t.stop());
}

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
    libererMicro(recorderRef.current, streamRef.current);
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  // ⚠ NETTOYAGE AU DÉMONTAGE — il n'y en avait AUCUN jusqu'au 2026-08-04, et ça se
  //   voyait à l'œil nu sans qu'on fasse le lien. `cleanup()` n'était appelé que depuis
  //   `cancel()` et `stopAndTranscribe()` ; `MicButton`, son seul consommateur, ne
  //   l'invoque dans aucun de ses trois effets.
  //
  //   Scénario, tout à fait ordinaire : on clique le micro, `getUserMedia` est accordé,
  //   l'enregistrement démarre — puis on appuie sur **Échap**, que `ImmersivePage`
  //   traduit par une navigation. Le composant est démonté, et :
  //     · les pistes du flux ne sont jamais arrêtées → **le voyant micro du navigateur
  //       reste allumé jusqu'au rechargement complet de la page**. Sur un assistant
  //       personnel installé à demeure, un micro qui reste ouvert n'est pas une fuite de
  //       ressource, c'est un problème de vie privée ;
  //     · le `MediaRecorder` continue d'émettre ses fragments toutes les 250 ms dans le
  //       vide.
  //
  //   On lit les refs dans la fonction de nettoyage — c'est leur usage légitime : elles
  //   portent la ressource impérative à libérer, et leur valeur au moment du démontage
  //   est précisément celle qu'on veut.
  useEffect(() => {
    return () => {
      libererMicro(recorderRef.current, streamRef.current);
      streamRef.current = null;
      recorderRef.current = null;
    };
  }, []);

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
    // 30s ceiling: avoid wedging the UI if Whisper or the relay hangs.
    const ctrl = new AbortController();
    const timeoutId = setTimeout(() => ctrl.abort(), 30000);
    let resp: Response;
    try {
      resp = await fetch('/v1/speech/transcribe', {
        method: 'POST',
        body: fd,
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }
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
