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

async function synthesizeAndPlay(
  text: string,
  signal: AbortSignal,
): Promise<HTMLAudioElement | null> {
  if (!text.trim()) return null;
  const resp = await fetch('/v1/ava/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      text,
      voice_id: TTS_VOICE,
      backend: TTS_BACKEND,
      speed: 1.0,
      output_format: 'wav',
    }),
  });
  if (!resp.ok) {
    throw new Error(`TTS HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.preload = 'auto';
  // Revoke URL once playback is truly done to avoid leaks
  audio.addEventListener('ended', () => URL.revokeObjectURL(url), { once: true });
  audio.addEventListener('error', () => URL.revokeObjectURL(url), { once: true });
  return audio;
}

/**
 * Bridges the immersive UI with the real OpenJarvis daemon.
 * - Drives state machine (listening → thinking → speaking → idle)
 * - Streams the assistant reply into the store (feeds typewriter naturally)
 * - Keeps conversation history across turns
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

    let assembled = '';
    let firstToken = true;

    try {
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortCtrl.current.signal,
        body: JSON.stringify({
          model: MODEL,
          messages: history.current,
          stream: true,
          max_tokens: MAX_TOKENS,
        }),
      });

      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }

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
            }
            // Some backends emit tool_calls in delta.tool_calls — surface to cognitive panel
            const toolCalls = data?.choices?.[0]?.delta?.tool_calls;
            if (Array.isArray(toolCalls) && toolCalls.length > 0 && toolCalls[0]?.function?.name) {
              s.setCognitive({ tool: toolCalls[0].function.name });
            }
          } catch {
            // ignore malformed SSE chunk
          }
        }
      }

      if (assembled) {
        history.current.push({ role: 'assistant', content: assembled });
      } else if (firstToken) {
        // Non-streaming fallback path — server returned a complete message
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

    // === Voice synthesis (Kokoro FR) ===
    // Fire and await audio playback before settling the orb. Skipped if muted
    // or if the abort controller has been signaled.
    if (assembled && !mutedRef.current && !abortCtrl.current?.signal.aborted) {
      try {
        const audio = await synthesizeAndPlay(assembled, abortCtrl.current!.signal);
        if (audio) {
          currentAudio.current = audio;
          await new Promise<void>((resolve) => {
            audio.addEventListener('ended', () => resolve(), { once: true });
            audio.addEventListener('error', () => resolve(), { once: true });
            audio.play().catch(() => resolve());
          });
          currentAudio.current = null;
        }
      } catch (e: unknown) {
        const name = (e as Error)?.name;
        if (name !== 'AbortError') {
          // TTS failure is non-fatal — the text is already on screen.
          // eslint-disable-next-line no-console
          console.warn('TTS failed:', (e as Error)?.message);
        }
      }
    }

    // Settle
    await sleep(900);
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
