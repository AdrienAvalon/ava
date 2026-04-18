import { useRef } from 'react';
import { useImmersiveStore } from './immersiveStore';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const MODEL = 'claude-sonnet-4-6';
const MAX_TOKENS = 800;

const TTS_BACKEND = 'kokoro-fr';
const TTS_VOICE = 'ff_siwis';
const TTS_MIN_CHARS = 4; // don't synthesize dust

async function synthesize(text: string, signal: AbortSignal): Promise<HTMLAudioElement | null> {
  const clean = text.trim();
  if (!clean || clean.length < TTS_MIN_CHARS) return null;
  const resp = await fetch('/v1/ava/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      text: clean,
      voice_id: TTS_VOICE,
      backend: TTS_BACKEND,
      speed: 1.0,
      output_format: 'wav',
    }),
  });
  if (!resp.ok) throw new Error(`TTS HTTP ${resp.status}`);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.preload = 'auto';
  audio.addEventListener('ended', () => URL.revokeObjectURL(url), { once: true });
  audio.addEventListener('error', () => URL.revokeObjectURL(url), { once: true });
  return audio;
}

/**
 * Splits an assembled text buffer into complete sentences that ended since
 * `lastEnd`. Returns the list of new sentences + the new "consumed" offset.
 * Keeps trailing (in-flight) partial sentence for the next call.
 */
function extractNewSentences(
  assembled: string,
  lastEnd: number,
): { sentences: string[]; newEnd: number } {
  const sentences: string[] = [];
  let i = lastEnd;
  let segStart = lastEnd;
  while (i < assembled.length) {
    const ch = assembled[i];
    // treat . ! ? : ; and newline as sentence terminators
    if (ch === '.' || ch === '!' || ch === '?' || ch === '\n') {
      // Skip common abbreviations like M., Mr., Dr., etc
      // (crude: if the previous non-space chars form 1-3 capital letters, treat as abbrev)
      const before = assembled.slice(Math.max(0, i - 3), i).trim();
      const isAbbrev = /^(M|Mr|Mme|Dr|St|Mme|etc|cf)$/i.test(before) && ch === '.';
      if (!isAbbrev) {
        const sentence = assembled.slice(segStart, i + 1).trim();
        if (sentence.length > 0) {
          sentences.push(sentence);
        }
        segStart = i + 1;
      }
    }
    i++;
  }
  return { sentences, newEnd: segStart };
}

/**
 * Bridges the immersive UI with the OpenJarvis daemon.
 * - Drives the orb state machine (listening → thinking → speaking → idle)
 * - Streams the chat reply into the visual typewriter
 * - Synthesizes audio sentence-by-sentence in parallel to reduce TTS latency
 * - Plays audio segments in order, no overlap
 */
export function useDaemonChat() {
  const history = useRef<Message[]>([]);
  const abortCtrl = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const currentAudio = useRef<HTMLAudioElement | null>(null);
  const mutedRef = useRef(false);

  function setMuted(muted: boolean) {
    mutedRef.current = muted;
    if (muted && currentAudio.current) {
      currentAudio.current.pause();
      currentAudio.current = null;
    }
  }

  async function ask(userText: string) {
    if (!userText.trim() || inFlight.current) return;
    inFlight.current = true;

    abortCtrl.current?.abort();
    abortCtrl.current = new AbortController();
    const { signal } = abortCtrl.current;

    const s = useImmersiveStore.getState();
    s.setUserMsg(userText);
    s.setAvaMsg('');
    s.setState('listening');
    await sleep(250);

    s.setState('thinking');
    s.setCognitive({
      intent: 'processing',
      focus: 'adrien',
      tool: null,
      reflection: `${Math.floor(history.current.length / 2)} tours retenus`,
      tone: 'attentive',
      memory: `${history.current.length} messages`,
    });

    history.current.push({ role: 'user', content: userText });

    // Sentence speech pipeline
    // Kokoro is CPU-bound — parallel synthesis saturates the backend and makes
    // each request slower, so we chain synthesis serially. Playback is a
    // separate chain that waits for its synth Promise, so audio N+1 can be
    // ready while audio N is still playing (pipelined).
    let synthChain: Promise<HTMLAudioElement | null> = Promise.resolve(null);
    let playbackChain: Promise<void> = Promise.resolve();
    const playbackErrors: string[] = [];

    function enqueueSentence(text: string) {
      if (mutedRef.current || signal.aborted) return;
      // Queue this synth to start when the previous synth finishes.
      const thisSynth = synthChain.then(async () => {
        if (mutedRef.current || signal.aborted) return null;
        try {
          return await synthesize(text, signal);
        } catch (e) {
          if ((e as Error)?.name !== 'AbortError') {
            playbackErrors.push((e as Error)?.message ?? String(e));
          }
          return null;
        }
      });
      synthChain = thisSynth;
      // Playback chain waits for its synth and for the previous playback to end.
      playbackChain = playbackChain.then(async () => {
        if (mutedRef.current || signal.aborted) return;
        const audio = await thisSynth;
        if (!audio) return;
        currentAudio.current = audio;
        await new Promise<void>((resolve) => {
          audio.addEventListener('ended', () => resolve(), { once: true });
          audio.addEventListener('error', () => resolve(), { once: true });
          audio.play().catch(() => resolve());
        });
        currentAudio.current = null;
      });
    }

    let assembled = '';
    let lastSentenceEnd = 0;
    let firstToken = true;

    try {
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal,
        body: JSON.stringify({
          model: MODEL,
          messages: history.current,
          stream: true,
          max_tokens: MAX_TOKENS,
        }),
      });

      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let lineEnd: number;
        while ((lineEnd = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, lineEnd).trim();
          buffer = buffer.slice(lineEnd + 1);
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]' || payload === '') continue;
          try {
            const data = JSON.parse(payload);
            const delta: string = data?.choices?.[0]?.delta?.content ?? '';
            if (delta) {
              if (firstToken) {
                s.setState('speaking');
                s.setUserMsg('');
                firstToken = false;
              }
              assembled += delta;
              s.setAvaMsg(assembled);

              // Extract newly-finished sentences and dispatch them to TTS.
              const { sentences, newEnd } = extractNewSentences(assembled, lastSentenceEnd);
              lastSentenceEnd = newEnd;
              for (const sentence of sentences) {
                enqueueSentence(sentence);
              }
            }
            const toolCalls = data?.choices?.[0]?.delta?.tool_calls;
            if (Array.isArray(toolCalls) && toolCalls.length > 0 && toolCalls[0]?.function?.name) {
              s.setCognitive({ tool: toolCalls[0].function.name });
            }
          } catch {
            // ignore malformed SSE chunk
          }
        }
      }

      // Tail: any remaining partial sentence (no terminator at the end)
      if (assembled.length > lastSentenceEnd) {
        const tail = assembled.slice(lastSentenceEnd).trim();
        if (tail) enqueueSentence(tail);
      }

      if (assembled) {
        history.current.push({ role: 'assistant', content: assembled });
      } else if (firstToken) {
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg('(réponse vide)');
      }
    } catch (e: unknown) {
      const name = (e as Error)?.name;
      if (name !== 'AbortError') {
        const msg = (e as Error)?.message ?? String(e);
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg(`Erreur daemon : ${msg}`);
      }
    }

    // Wait for all queued audio playback to drain (speaking state held)
    try {
      await playbackChain;
    } catch {
      // defensive
    }
    if (playbackErrors.length > 0) {
      // eslint-disable-next-line no-console
      console.warn('TTS errors:', playbackErrors);
    }

    // Settle
    await sleep(800);
    s.setState('idle');
    s.clearCognitive();
    await sleep(1500);
    s.setAvaMsg('');
    inFlight.current = false;
  }

  function reset() {
    abortCtrl.current?.abort();
    if (currentAudio.current) {
      currentAudio.current.pause();
      currentAudio.current = null;
    }
    history.current = [];
    inFlight.current = false;
  }

  function isBusy() {
    return inFlight.current;
  }

  return { ask, reset, isBusy, setMuted };
}
