import { useRef } from 'react';
import { useImmersiveStore } from './immersiveStore';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const MODEL = 'claude-sonnet-4-6';
const MAX_TOKENS = 800;

const TTS_BACKEND = "openai_tts";
const TTS_VOICE = "nova";
const TTS_MIN_CHARS = 4; // don't synthesize dust

/**
 * Clean a text segment for TTS: strip emojis and markdown noise that would
 * otherwise be read aloud ("emoji cerveau", "étoile étoile", backticks...).
 * The visible conversation keeps the original (emojis make the chat alive);
 * only what Ava *pronounces* goes through this filter.
 */
function cleanForTTS(text: string): string {
  return text
    // Emojis + pictographs (Unicode Extended_Pictographic incl. joiners)
    .replace(/[\p{Extended_Pictographic}\u200D\uFE0F]/gu, '')
    // Variation selectors, zero-width, symbols that trip up phonemizers
    .replace(/[\u2000-\u206F\u2070-\u209F\u20A0-\u20CF]/g, ' ')
    // Markdown emphasis markers: **bold**, *italic*, __bold__, _italic_
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s).,!?:;]|$)/g, '$1$2')
    // Inline/code block backticks
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    // Markdown headings # / ## / ### at line start
    .replace(/^#{1,6}\s+/gm, '')
    // List markers at line start (-, *, +, 1.)
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    // URLs → read only the label if [text](url), else drop the URL
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '')
    // Collapse whitespace
    .replace(/\s+/g, ' ')
    .trim();
}

// Shared AudioContext — instantiated lazily on the first user gesture so the
// browser's autoplay policy does not block playback later.
let sharedAudioCtx: AudioContext | null = null;
function getAudioCtx(): AudioContext {
  if (!sharedAudioCtx) {
    const AC = (window as unknown as { AudioContext: typeof AudioContext; webkitAudioContext?: typeof AudioContext }).AudioContext
      || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    sharedAudioCtx = new AC();
  }
  return sharedAudioCtx;
}

async function synthesize(text: string, signal: AbortSignal): Promise<AudioBuffer | null> {
  const spoken = cleanForTTS(text);
  if (!spoken || spoken.length < TTS_MIN_CHARS) return null;
  const resp = await fetch('/v1/ava/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      text: spoken,
      voice_id: TTS_VOICE,
      backend: TTS_BACKEND,
      speed: 1.0,
      output_format: 'wav',
    }),
  });
  if (!resp.ok) throw new Error(`TTS HTTP ${resp.status}`);
  const buf = await resp.arrayBuffer();
  const ctx = getAudioCtx();
  if (ctx.state === 'suspended') {
    try { await ctx.resume(); } catch { /* no gesture yet — caller should retry after click */ }
  }
  // decodeAudioData is more permissive than <audio> for odd WAV rates (24kHz mono)
  return await ctx.decodeAudioData(buf);
}

function playBuffer(
  buffer: AudioBuffer,
  onEnded: () => void,
): AudioBufferSourceNode {
  const ctx = getAudioCtx();
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.onended = onEnded;
  try {
    src.start();
  } catch {
    // best-effort: invoke ended to release the playback chain
    queueMicrotask(onEnded);
  }
  return src;
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
  const currentSource = useRef<AudioBufferSourceNode | null>(null);
  const mutedRef = useRef(false);
  // Persona loaded once from /v1/ava/persona — injected as a system message
  // because OpenJarvis's streaming /v1/chat/completions path does not apply
  // the agent's configured system prompt.
  const personaRef = useRef<string | null>(null);
  const personaPromise = useRef<Promise<string> | null>(null);

  function loadPersona(): Promise<string> {
    if (personaRef.current !== null) return Promise.resolve(personaRef.current);
    if (personaPromise.current) return personaPromise.current;
    personaPromise.current = fetch('/v1/ava/persona')
      .then((r) => (r.ok ? r.json() : { system_prompt: '' }))
      .then((d) => {
        const text = (d?.system_prompt as string) || '';
        personaRef.current = text;
        return text;
      })
      .catch(() => {
        personaRef.current = '';
        return '';
      });
    return personaPromise.current;
  }

  function setMuted(muted: boolean) {
    mutedRef.current = muted;
    if (muted && currentSource.current) {
      try { currentSource.current.stop(); } catch { /* ignore */ }
      currentSource.current = null;
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
    // ⚠ L'historique est alimenté EN PLUS de la vue centrale, jamais à sa place :
    //   `setUserMsg`/`setAvaMsg` pilotent le focal 34px de la spec v2, `pushLine` et
    //   `streamAva` nourrissent le terminal consultable. Les deux répondent à des
    //   besoins différents — la présence, et la mémoire.
    s.pushLine('user', userText);
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

    // Build the messages array sent to the daemon. Prepend the persona as a
    // system message if available (streaming path does not auto-inject it).
    const persona = await loadPersona();
    const messages = persona
      ? [{ role: 'system' as const, content: persona }, ...history.current]
      : [...history.current];

    // Sentence speech pipeline
    // Kokoro is CPU-bound — parallel synthesis saturates the backend and makes
    // each request slower, so we chain synthesis serially. Playback is a
    // separate chain that waits for its synth Promise, so audio N+1 can be
    // ready while audio N is still playing (pipelined).
    let synthChain: Promise<AudioBuffer | null> = Promise.resolve(null);
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
        const buffer = await thisSynth;
        if (!buffer) return;
        await new Promise<void>((resolve) => {
          const src = playBuffer(buffer, () => {
            currentSource.current = null;
            resolve();
          });
          currentSource.current = src;
        });
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
          messages,
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
              s.streamAva(assembled);

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
        s.endAvaStream();
      } else if (firstToken) {
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg('(réponse vide)');
        s.pushLine('system', '(réponse vide)');
      }
    } catch (e: unknown) {
      const name = (e as Error)?.name;
      if (name !== 'AbortError') {
        const msg = (e as Error)?.message ?? String(e);
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg(`Erreur daemon : ${msg}`);
        // ⚠ Une erreur DOIT figurer dans l'historique. Sans ça, le terminal montre une
        //   question restée sans réponse et on cherche un défaut d'affichage — alors
        //   que le serveur a répondu, par un échec. C'est exactement ce qui s'est passé
        //   pendant trois mois avec le crédit API épuisé.
        s.pushLine('system', `Erreur daemon : ${msg}`);
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
    if (currentSource.current) {
      try { currentSource.current.stop(); } catch { /* ignore */ }
      currentSource.current = null;
    }
    history.current = [];
    inFlight.current = false;
  }

  function isBusy() {
    return inFlight.current;
  }

  return { ask, reset, isBusy, setMuted };
}
