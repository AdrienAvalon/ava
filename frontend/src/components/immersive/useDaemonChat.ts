import { useEffect, useRef } from 'react';
import { chargerHistoriqueModele, useImmersiveStore } from './immersiveStore';
import { ajouterConversation, lireConversation } from './memoireServeur';
import { brancherAnalyseur, relacherAnalyseur } from './voixAmplitude';

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

/**
 * ⚠ LA VOIX D'AVA REPASSE EN SOUVERAIN (2026-08-04). Ces deux constantes valaient
 * `openai_tts` / `nova` : **chaque phrase prononcée par Ava était POSTée chez OpenAI** —
 * y compris « Adrien est présent, Aurélie est absente » ou l'état de l'infrastructure,
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
    lireConversation().then((lignes) => {
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
    });
    return () => { annule = true; };
  }, []);
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
        // ⚠ ON UTILISE `newEnd`, PAS `sentences.join(' ').length` — corrigé le
        //   2026-08-04. Les deux ne coïncident que si chaque séparation entre phrases
        //   fait EXACTEMENT un caractère : les phrases sont `.trim()`ées, et `join(' ')`
        //   ne réinjecte qu'un espace. Dès qu'il y a une ligne vide — c'est-à-dire dès
        //   qu'Ava répond en paragraphes, ce qu'elle fait constamment — le décalage
        //   dérive d'un caractère par séparateur, et **cumule**.
        //
        //   Mesuré sur une vraie réponse d'Ava :
        //     « Il fait 26,7 degrés dehors.⏎⏎Adrien est présent…⏎⏎La baie serveur… »
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
        history.current.push({ role: 'assistant', content: assembled });
        // ⚠ On persiste les DEUX lignes en un seul appel : la question et la réponse
        //   forment un tour. Les envoyer séparément laisserait, en cas de coupure entre
        //   les deux, une question sans réponse dans la mémoire d'Ava — elle croirait
        //   n'avoir jamais répondu.
        ajouterConversation([
          { role: 'user', texte: userText },
          { role: 'assistant', texte: assembled },
        ]);
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
