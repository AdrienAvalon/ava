import { useEffect, useRef } from 'react';
import { chargerHistoriqueModele, useImmersiveStore } from './immersiveStore';
import { entetesIdentite, lireConversation } from './memoireServeur';
import { brancherAnalyseur, relacherAnalyseur } from './voixAmplitude';

export interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export interface HydrationGate {
  wait: () => Promise<void>;
  complete: () => void;
}

/**
 * A one-shot barrier between server-history hydration and the first chat send.
 * Both a successful read and an explicit failure release it; a slow read does
 * not get to overwrite a message that was already appended locally.
 */
export function createHydrationGate(): HydrationGate {
  let completed = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  return {
    wait: () => pending,
    complete: () => {
      if (completed) return;
      completed = true;
      release();
    },
  };
}

export const TURN_ID_HEADER = 'X-Ava-Turn-Id';
const CHAT_RETRY_DELAYS_MS = [200, 700] as const;
const MAX_RETRY_AFTER_MS = 10_000;

type ChatFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

class NonRetryableChatError extends Error {}

function delaiRetryAfter(response: Response): number {
  const valeur = response.headers?.get('Retry-After');
  if (!valeur) return 0;
  const secondes = Number(valeur);
  if (!Number.isFinite(secondes) || secondes < 0) return 0;
  return Math.min(secondes * 1_000, MAX_RETRY_AFTER_MS);
}

async function attendreRetry(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  await new Promise<void>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export function creerTurnId(): string {
  return globalThis.crypto.randomUUID();
}

export interface DurableChatOptions {
  messages: Message[];
  turnId: string;
  signal: AbortSignal;
  model?: string;
  maxTokens?: number;
  fetchImpl?: ChatFetch;
  retryDelaysMs?: readonly number[];
  waitImpl?: (ms: number, signal: AbortSignal) => Promise<void>;
}

/**
 * Call the server with one stable turn id until the committed answer is observed.
 * A response lost after the server commit is replayed from SQLite on the next attempt,
 * so retries never require a second durable write or a second successful model answer.
 */
export async function demanderChatDurable({
  messages,
  turnId,
  signal,
  model = MODEL,
  maxTokens = MAX_TOKENS,
  fetchImpl = fetch,
  retryDelaysMs = CHAT_RETRY_DELAYS_MS,
  waitImpl = attendreRetry,
}: DurableChatOptions): Promise<string> {
  const headers = entetesIdentite({
    'Content-Type': 'application/json',
    [TURN_ID_HEADER]: turnId,
  });
  const body = JSON.stringify({
    model,
    messages,
    stream: false,
    max_tokens: maxTokens,
  });
  let lastError: unknown = new Error('Ava chat failed');

  for (let attempt = 0; attempt <= retryDelaysMs.length; attempt += 1) {
    let waitMs = retryDelaysMs[attempt] ?? 0;
    try {
      const response = await fetchImpl('/v1/chat/completions', {
        method: 'POST',
        headers,
        signal,
        body,
      });
      if (!response.ok) {
        const error = new Error(`HTTP ${response.status}`);
        if (response.status === 425 || response.status === 429) {
          waitMs = Math.max(waitMs, delaiRetryAfter(response));
        } else if (response.status < 500) {
          throw new NonRetryableChatError(error.message);
        }
        throw error;
      }
      const data = await response.json();
      const content: string = data?.choices?.[0]?.message?.content ?? '';
      if (!content) throw new Error('réponse durable vide');
      return content;
    } catch (error) {
      if ((error as Error)?.name === 'AbortError') throw error;
      if (error instanceof NonRetryableChatError) throw error;
      lastError = error;
      if (attempt >= retryDelaysMs.length) break;
      await waitImpl(waitMs, signal);
    }
  }
  // A final 425 deliberately remains unresolved: the server still owns the pending
  // key and will never regenerate it automatically. Reload/reconciliation can recover
  // a later commit; sending a different UUID is a new action, not a retry of this one.
  throw lastError;
}

/**
 * Add a turn to the model context only after the server has durably acknowledged it.
 * Failed or aborted questions remain visible in the UI transcript but can never leak
 * into the next model request as an unanswered user message.
 */
export async function completerTourDurable(
  history: Message[],
  userText: string,
  request: (messages: Message[]) => Promise<string>,
): Promise<string> {
  const userMessage: Message = { role: 'user', content: userText };
  const assistantText = await request([...history, userMessage]);
  history.push(userMessage, { role: 'assistant', content: assistantText });
  return assistantText;
}

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

/**
 * ⚠ LA VOIX D'AVA REPASSE EN SOUVERAIN (2026-08-04). Ces deux constantes valaient
 * `openai_tts` / `nova` : **chaque phrase prononcée par Ava était POSTée chez OpenAI** —
 * y compris « une personne est présente, une autre est absente » ou l'état de
 * l'infrastructure,
 * c'est-à-dire exactement les données que `conversation.py` qualifie de personnelles et
 * que la persona d'Ava lui prescrit de ne pas laisser fuir vers un cloud tiers.
 *
 * Et la documentation du projet affirmait le contraire, en quatre endroits : « Souverain,
 * CPU-only, 0 €/mois », « Pas de clé API TTS — économie 15-22 €/mois », « Budget mensuel
 * TTS = 0 € », et `openai_tts` rangé parmi les backends **rejetés au POC**. Le code et la
 * doc se contredisaient, ce qui est le pire des deux mondes : on croit avoir une garantie
 * qu'on n'a pas.
 *
 * ⚠ CE QUI REND LA BASCULE POSSIBLE AUJOURD'HUI, ce n'est pas un changement d'avis mais
 * la correction du cache d'instances dans `/v1/ava/speak` : le backend était réinstancié
 * à chaque requête, donc **rechargeait son modèle à chaque phrase**. Mesuré sur la VM :
 *   · à froid (chargement inclus) : 3,2 s d'audio en 5,3 s → ratio 1,64× ;
 *   · à chaud, instance réutilisée : 3,1 s d'audio en 0,7 s → **ratio 0,22×**.
 * Kokoro était donc réputé « trop lent » à cause d'un défaut serveur, pas de ses
 * performances. C'est ce qui a probablement motivé le passage à OpenAI à l'époque.
 *
 * ⚠ LE BACKEND ET LA VOIX VONT PAR PAIRE : `ff_siwis` est une voix Kokoro, `nova` une
 * voix OpenAI. Changer l'un sans l'autre donne une voix inconnue du backend — donc un
 * 502 sur chaque phrase, et une Ava muette. Les surcharges d'environnement existent pour
 * pouvoir revenir en arrière sans reconstruire, mais elles doivent bouger ensemble.
 */
const TTS_BACKEND = import.meta.env.VITE_TTS_BACKEND || 'kokoro-fr';
const TTS_VOICE = import.meta.env.VITE_TTS_VOICE || 'ff_siwis';
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
  // ⚠ L'analyseur est branché EN DÉRIVATION : la source va à la fois vers la sortie
  //   audio ET vers lui. C'est ce qui permet à l'orbe de pulser sur la VRAIE voix
  //   d'Ava — silences entre les mots, attaques de syllabes, respiration des phrases —
  //   plutôt que sur une animation fabriquée dont le rythme n'aurait aucun rapport
  //   avec ce qui est prononcé. L'œil fait très bien la différence.
  brancherAnalyseur(ctx, src);
  src.onended = () => {
    relacherAnalyseur();
    onEnded();
  };
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
export function extractNewSentences(
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
  const hydrationGate = useRef<HydrationGate>(createHydrationGate());

  /**
   * ⚠ HYDRATATION DEPUIS LE SERVEUR — c'est ce qui rend la mémoire d'Ava indépendante du
   *   navigateur. Le cache local a déjà peuplé l'écran (affichage immédiat) ; ici on
   *   remplace par la vérité serveur, qui suit l'utilisateur d'un appareil à l'autre.
   *
   * ⚠ On met à jour AUSSI `history.current` : sans ça, l'écran afficherait la
   *   conversation venue du serveur pendant qu'Ava, elle, ne connaîtrait que le cache
   *   local — deux mémoires divergentes, et une IA qui se contredit sans qu'aucune
   *   erreur n'apparaisse.
   */
  // ⚠ Vider l'affichage doit vider le CONTEXTE MODÈLE. Sans cet effet, `history.current`
  //   gardait la conversation effacée et la renvoyait au modèle à la question suivante.
  const effacements = useImmersiveStore((s) => s.effacements);
  useEffect(() => {
    if (effacements > 0) history.current = [];
  }, [effacements]);

  useEffect(() => {
    let annule = false;
    const gate = hydrationGate.current;
    void (async () => {
      try {
        const lignes = await lireConversation();
        // ⚠ `null` = serveur muet → on garde le cache. `[]` = le serveur AFFIRME qu'il n'y
        //   a pas d'historique → on vide, y compris le contexte modèle. Confondre les deux
        //   faisait hériter un nouvel utilisateur de la conversation du précédent.
        if (annule || lignes === null) return;
        const s = useImmersiveStore.getState();
        s.hydraterDepuisServeur(
          lignes.map((l, i) => ({
            id: -(lignes.length - i), // ids négatifs : jamais en collision avec le compteur local
            role: (l.role === 'assistant' ? 'ava' : l.role) as 'user' | 'ava' | 'system',
            text: l.texte,
            at: new Date(l.horodatage * 1000).toLocaleTimeString('fr-FR', {
              hour: '2-digit', minute: '2-digit',
            }),
          })),
        );
        history.current = chargerHistoriqueModele() as Message[];
      } finally {
        // Success, empty history, HTTP failure and network failure all make an
        // explicit decision before the first request can consume history.current.
        gate.complete();
      }
    })();
    return () => {
      annule = true;
      gate.complete();
    };
  }, []);
  const inFlight = useRef(false);
  const currentSource = useRef<AudioBufferSourceNode | null>(null);
  const mutedRef = useRef(false);

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

    // The first message must not race the asynchronous replacement of
    // history.current by the server history. A failed hydration still
    // releases this gate explicitly and preserves the local cache.
    await hydrationGate.current.wait();
    if (!inFlight.current) return; // reset/unmount while hydration was pending

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
      focus: 'interlocuteur',
      tool: null,
      reflection: `${Math.floor(history.current.length / 2)} tours retenus`,
      tone: 'attentive',
      memory: `${history.current.length} messages`,
    });

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
       *   et « il fait 26,7 °C dehors, deux personnes sont présentes ».
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
      const turnId = creerTurnId();
      // Identity and the private overlay are server-owned. The client contributes
      // only a stable idempotency key; it enters model history only after the same
      // HTTP response has been committed under the verified principal.
      assembled = await completerTourDurable(
        history.current,
        userText,
        (messages) => demanderChatDurable({ messages, turnId, signal }),
      );
      if (assembled) {
        s.setState('speaking');
        s.setUserMsg('');
        s.setAvaMsg(assembled);
        s.streamAva(assembled);
        // Le texte arrive d'un bloc : on découpe pour que le TTS parle par phrases
        // plutôt que d'attaquer 300 mots d'une traite.
        // ⚠ ON UTILISE `newEnd`, PAS `sentences.join(' ').length` — corrigé le
        //   2026-08-04. Les deux ne coïncident que si chaque séparation entre phrases
        //   fait EXACTEMENT un caractère : les phrases sont `.trim()`ées, et `join(' ')`
        //   ne réinjecte qu'un espace. Dès qu'il y a une ligne vide — c'est-à-dire dès
        //   qu'Ava répond en paragraphes, ce qu'elle fait constamment — le décalage
        //   dérive d'un caractère par séparateur, et **cumule**.
        //
        //   Mesuré sur une vraie réponse d'Ava :
        //     « Il fait 26,7 degrés dehors.⏎⏎Une personne est présente…⏎⏎La baie serveur… »
        //     newEnd = 67, join(' ').length = 64
        //     → Ava prononçait « . La baie serveur tire 738 watts » — donc un point
        //       isolé, puis la répétition de la fin de la phrase précédente.
        //   Le cas à espace unique fonctionnait parfaitement : c'est pourquoi le défaut
        //   a survécu, tout en s'entendant à chaque réponse un peu longue.
        const { sentences, newEnd } = extractNewSentences(assembled, 0);
        for (const sentence of sentences) enqueueSentence(sentence);
        const reste = assembled.slice(newEnd).trim();
        if (reste) enqueueSentence(reste);
      }
      if (assembled) {
        // The server persisted the pair before returning it. There is deliberately
        // no second frontend POST: it would reintroduce the close/retry race.
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
