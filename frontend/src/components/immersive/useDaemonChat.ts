import { useRef } from 'react';
import { chargerHistoriqueModele, useImmersiveStore } from './immersiveStore';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * ⚠ LE MODÈLE EST CODÉ EN DUR ICI, ET C'EST UN PIÈGE COÛTEUX (constaté le 2026-08-04).
 *
 * Le frontend envoie ce nom dans le corps de la requête, donc il **écrase la
 * configuration du serveur**. On a passé `default_model` à `claude-sonnet-5` côté VM en
 * croyant avoir changé le modèle d'Ava : le navigateur a continué d'envoyer
 * `claude-sonnet-4-6`, et rien ne l'a signalé — les deux existent, les deux répondent.
 *
 * ⚠ Le HUD affiche cette même constante : il annonçait donc fidèlement un modèle que le
 *   serveur n'avait pas choisi. Deux sources de vérité pour une seule valeur, dont une
 *   invisible depuis la machine.
 *
 * Correction de fond possible (non faite) : ne PAS envoyer `model` du tout et laisser le
 * serveur décider — c'est lui qui porte la configuration. Elle demande de vérifier que le
 * daemon retombe bien sur `config.server.model` quand le champ est absent, et de revoir
 * le HUD, qui n'aurait alors rien à afficher avant le premier échange.
 */
const MODEL = 'claude-sonnet-5';
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
  // ⚠ ON LIT CE QUE LE SERVEUR A RÉELLEMENT EMPLOYÉ, pas ce qu'on a demandé. Le client
  //   envoie `backend` dans sa requête ; s'y fier reviendrait à afficher notre propre
  //   intention. L'en-tête de réponse `X-Ava-TTS-Backend` est le seul contrôle qui fait
  //   foi — c'est d'ailleurs ce qu'un audit a recommandé après avoir trouvé le HUD
  //   affichant « kokoro » pendant que le client demandait OpenAI.
  useImmersiveStore.getState().setRuntime({
    tts: resp.headers.get('X-Ava-TTS-Backend') || TTS_BACKEND,
  });
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
  /**
   * ⚠ INITIALISÉ DEPUIS LE STOCKAGE, PAS À VIDE — c'est ce qui donne sa mémoire à Ava
   *   d'une session à l'autre (demande de l'admin, 2026-08-04).
   *
   *   Persister l'affichage sans persister CECI donnerait le pire des deux mondes :
   *   l'utilisateur reverrait sa conversation d'hier à l'écran, et Ava n'en aurait
   *   aucun souvenir — elle se contredirait dès le premier message, sans qu'aucune
   *   erreur n'apparaisse. Le défaut se présenterait comme « l'IA est incohérente »
   *   plutôt que comme « il manque un chargement ».
   */
  const history = useRef<Message[]>(chargerHistoriqueModele() as Message[]);
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
    // Le modèle et le moteur sont ceux que le client demande : le daemon ne les renvoie
    // pas. C'est donc une intention, pas une observation — mais elle est au moins tirée
    // d'une constante unique au lieu d'être recopiée dans le HUD.
    s.setRuntime({ model: MODEL, engine: 'anthropic/cloud' });
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

    try {
      /**
       * ⚠ `stream: false` EST CE QUI DONNE SES OUTILS À AVA — c'est la décision la plus
       *   importante de ce fichier, et elle n'est pas évidente.
       *
       *   Le serveur route ainsi (`server/routes.py`) :
       *     · `stream: true`  → flux direct du moteur, **l'agent est contourné** ;
       *     · `stream: false` + pas de `tools` → **`_handle_agent`**, qui exécute la
       *       boucle d'outils de l'agent.
       *
       *   En streaming, Ava répondait donc « je n'ai pas accès à ton infrastructure » —
       *   ce qui était exact : le modèle ne recevait aucun outil. Les outils existaient,
       *   étaient enregistrés, répondaient parfaitement quand on les appelait
       *   directement… et n'étaient jamais proposés au modèle.
       *   Mesuré après bascule : « 97/100, deux points qui grattent : ansible… grafana… »
       *   et « il fait 26,7 °C dehors, Adrien et Aurélie sont présents ».
       *
       * ⚠ CE QUE ÇA COÛTE, ASSUMÉ : plus d'affichage token par token. La réponse arrive
       *   d'un bloc, après ~19 s quand un outil est appelé. C'est le bon compromis : une
       *   Ava qui écrit joliment mais ignore l'état réel de la maison n'est pas le
       *   produit qu'on construit. L'attente est signalée par l'état `thinking` (l'orbe
       *   change) plutôt que par du texte qui défile.
       *
       * ⚠ NE PAS « rétablir le streaming » sans vérifier les outils : le symptôme du
       *   retour en arrière serait une Ava redevenue amnésique sur son environnement,
       *   sans qu'aucune erreur n'apparaisse nulle part.
       */
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal,
        body: JSON.stringify({
          model: MODEL,
          messages,
          stream: false,
          max_tokens: MAX_TOKENS,
        }),
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      // ── Réponse non-streamée : un seul objet JSON ────────────────────────────────
      const data = await resp.json();
      const contenu: string = data?.choices?.[0]?.message?.content ?? '';
      if (contenu) {
        s.setState('speaking');
        s.setUserMsg('');
        assembled = contenu;
        s.setAvaMsg(assembled);
        s.streamAva(assembled);
        // Le texte arrive d'un bloc : on découpe pour que le TTS parle par phrases
        // plutôt que d'attaquer 300 mots d'une traite.
        const { sentences } = extractNewSentences(assembled, 0);
        for (const sentence of sentences) enqueueSentence(sentence);
        const reste = assembled.slice(sentences.join(' ').length).trim();
        if (reste) enqueueSentence(reste);
      }
      if (assembled) {
        history.current.push({ role: 'assistant', content: assembled });
      } else {
        // ⚠ Une réponse vide doit se VOIR. Sans ce cas, l'interface resterait figée sur
        //   « réfléchit » sans rien afficher, et l'on croirait à un blocage réseau.
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg('(réponse vide)');
        s.pushLine('system', '(réponse vide)');
      }
      s.endAvaStream();
      s.setState('idle');
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
