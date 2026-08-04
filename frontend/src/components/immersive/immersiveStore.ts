import { create } from 'zustand';
import type { ImmersiveState } from './immersiveStates';

export interface CognitiveSignals {
  intent: string | null;
  focus: string | null;
  tool: string | null;
  reflection: string | null;
  tone: string | null;
  memory: string | null;
}

/**
 * Une ligne de la conversation.
 *
 * ⚠ POURQUOI UN HISTORIQUE EXISTE MAINTENANT (2026-08-03).
 * Le store ne gardait que `userMsg` et `avaMsg` — deux chaînes ÉCRASÉES à chaque
 * échange. La conversation n'était donc conservée NULLE PART : ni à l'écran, ni en
 * mémoire. Dès qu'Ava répondait, la question disparaissait ; dès qu'on reposait une
 * question, la réponse précédente était perdue. On ne pouvait ni relire, ni vérifier
 * ce qui avait été dit.
 * C'était conforme à la spec v2 (« le texte conversationnel est le focal point »,
 * un échange en 34px au centre) mais cette lecture du besoin était incomplète : une
 * présence qui ne se souvient pas de ce qu'elle vient de dire n'est pas immersive,
 * elle est amnésique.
 */
export interface TurnLine {
  id: number;
  role: 'user' | 'ava' | 'system';
  text: string;
  /** Horodatage local, figé à la création — sert d'ancre visuelle dans le terminal. */
  at: string;
  /** `true` tant qu'Ava écrit : la ligne se met à jour au lieu d'en créer une nouvelle. */
  streaming?: boolean;
}

/** Ce que le SERVEUR utilise réellement — jamais ce qu'on croit qu'il utilise.
 *
 * ⚠ Ces quatre valeurs étaient écrites EN DUR dans le HUD, et deux étaient fausses :
 *   « kokoro-ff_siwis » alors que le client demande OpenAI, « whisper-large-v3 » alors
 *   que le backend STT serveur est hors service. Un tableau de bord qui affirme une
 *   configuration qu'il ne lit pas est pire qu'un tableau vide : il fait croire qu'on sait.
 *   `null` signifie « pas encore observé » et s'affiche « — ». C'est honnête : avant le
 *   premier échange, on ignore ce que le serveur emploiera.
 */
export interface RuntimeInfo {
  model: string | null;
  engine: string | null;
  tts: string | null;
  stt: string | null;
}

interface ImmersiveStore {
  state: ImmersiveState;
  runtime: RuntimeInfo;
  setRuntime: (r: Partial<RuntimeInfo>) => void;
  userMsg: string;
  avaMsg: string;
  /** Historique complet de la session, du plus ancien au plus récent. */
  transcript: TurnLine[];
  cognitive: CognitiveSignals;
  rippleKey: number; // bump to trigger ripple animation

  setState: (s: ImmersiveState) => void;
  setUserMsg: (m: string) => void;
  setAvaMsg: (m: string) => void;
  /** Ajoute une ligne close (utilisateur, ou message système). */
  pushLine: (role: TurnLine['role'], text: string) => void;
  /** Ouvre une ligne d'Ava en cours d'écriture, puis la met à jour au fil du flux. */
  streamAva: (text: string) => void;
  /** Ferme la ligne d'Ava en cours (fin de réponse). */
  endAvaStream: () => void;
  clearTranscript: () => void;
  /** Remplace l'historique par celui du serveur (source de vérité inter-appareils). */
  hydraterDepuisServeur: (lignes: TurnLine[]) => void;
  setCognitive: (c: Partial<CognitiveSignals>) => void;
  clearCognitive: () => void;
}

const emptyCognitive: CognitiveSignals = {
  intent: null,
  focus: null,
  tool: null,
  reflection: null,
  tone: null,
  memory: null,
};

/**
 * ⚠ PLAFOND DÉLIBÉRÉ. Une session vocale peut durer des heures ; sans limite, le
 * tableau croît indéfiniment et le rendu React ralentit progressivement — une lenteur
 * qui s'installe sans jamais produire d'erreur, donc qu'on attribue à autre chose.
 * 400 lignes couvrent très largement une session de travail ; au-delà, les plus
 * anciennes sortent.
 */
const MAX_LIGNES = 400;

/**
 * ⚠ L'HISTORIQUE SURVIT AU RECHARGEMENT DE LA PAGE (2026-08-04).
 *
 * Il ne vivait qu'en mémoire : un F5 et toute la conversation disparaissait. L'admin l'a
 * signalé en une phrase — « j'ai rafraîchi la page et il n'y avait plus rien » — et c'est
 * une critique juste : un terminal qui perd tout au moindre rechargement ne remplit pas
 * la fonction qu'on lui demandait, à savoir « voir tout l'historique ».
 *
 * ⚠ `localStorage` — CHOIX EXPLICITE DE L'ADMIN (2026-08-04) : « il faudrait que
 *   l'historique survive et Ava s'en serve évidemment ». J'avais d'abord pris
 *   `sessionStorage`, qui perd tout à la fermeture du navigateur ; c'était trop prudent
 *   pour le besoin réel. La contrepartie est réelle et assumée : la conversation porte
 *   des données personnelles (présence des gens dans la maison, état de
 *   l'infrastructure) et reste donc sur le disque du navigateur jusqu'à effacement
 *   explicite. Le bouton « vider » du terminal l'efface pour de bon.
 *
 * ⚠ CE STOCKAGE SERT AUSSI À AVA, PAS SEULEMENT À L'AFFICHAGE — et c'est la moitié
 *   qu'on oublie. Restaurer les lignes à l'écran sans restaurer le contexte envoyé au
 *   modèle donne le pire des deux : l'utilisateur VOIT la conversation d'hier, Ava n'en
 *   a aucun souvenir et se contredit. `useDaemonChat` reconstruit donc son historique
 *   d'échanges à partir d'ici (cf. `chargerHistoriqueModele`).
 *
 * ⚠ Toute lecture/écriture est protégée : un navigateur en navigation privée stricte,
 *   ou un quota atteint, fait LEVER ces API. Une conversation ne doit pas casser parce
 *   que son journal ne peut pas être écrit.
 */
const CLE_STOCKAGE = 'ava.transcript.v1';

function chargerTranscript(): TurnLine[] {
  try {
    const brut = localStorage.getItem(CLE_STOCKAGE);
    if (!brut) return [];
    const lignes = JSON.parse(brut);
    if (!Array.isArray(lignes)) return [];
    // ⚠ Aucune ligne n'est réputée « en cours d'écriture » après un rechargement : le
    //   flux qui l'alimentait est mort avec la page. Sans ce nettoyage, la dernière
    //   réponse d'Ava garderait son curseur clignotant pour toujours.
    return lignes.map((l: TurnLine) => ({ ...l, streaming: false })).slice(-MAX_LIGNES);
  } catch {
    return [];
  }
}

function sauverTranscript(lignes: TurnLine[]): void {
  try {
    localStorage.setItem(CLE_STOCKAGE, JSON.stringify(lignes));
  } catch {
    /* quota atteint ou stockage indisponible — la conversation continue sans journal */
  }
}

/**
 * Reconstruit le contexte à envoyer au modèle à partir du transcript persisté.
 *
 * ⚠ SANS ÇA, LA PERSISTANCE EST UN DÉCOR. L'utilisateur reverrait sa conversation à
 *   l'écran pendant qu'Ava, elle, repartirait de zéro — et se contredirait au premier
 *   message. C'est la moitié invisible de la demande « que l'historique survive et
 *   qu'Ava s'en serve ».
 *
 * ⚠ Les lignes `system` sont ÉCARTÉES : ce sont des messages d'erreur affichés à
 *   l'utilisateur (« Erreur réseau… »), pas des tours de conversation. Les renvoyer au
 *   modèle lui ferait croire qu'il a produit ces phrases.
 *
 * ⚠ Plafond à 40 tours : au-delà, le contexte renvoyé à chaque requête coûterait plus
 *   cher que la conversation elle-même — et les tours anciens n'aident plus. Le
 *   terminal, lui, garde ses 400 lignes à l'écran : afficher est gratuit, réexpédier
 *   ne l'est pas.
 */
export function chargerHistoriqueModele(): { role: 'user' | 'assistant'; content: string }[] {
  return chargerTranscript()
    .filter((l) => l.role === 'user' || l.role === 'ava')
    .slice(-40)
    .map((l) => ({
      role: l.role === 'ava' ? ('assistant' as const) : ('user' as const),
      content: l.text,
    }));
}

let compteur = 0;
const heure = () =>
  new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });

export const useImmersiveStore = create<ImmersiveStore>((set) => ({
  state: 'idle',
  userMsg: '',
  avaMsg: '',
  runtime: { model: null, engine: null, tts: null, stt: null },
  transcript: chargerTranscript(),
  cognitive: { ...emptyCognitive },
  rippleKey: 0,

  setState: (s) => set((prev) => ({ state: s, rippleKey: prev.rippleKey + 1 })),
  setRuntime: (r) => set((prev) => ({ runtime: { ...prev.runtime, ...r } })),
  setUserMsg: (userMsg) => set({ userMsg }),
  setAvaMsg: (avaMsg) => set({ avaMsg }),

  pushLine: (role, text) =>
    set((prev) => {
      const transcript = [
        ...prev.transcript,
        { id: ++compteur, role, text, at: heure() },
      ].slice(-MAX_LIGNES);
      sauverTranscript(transcript);
      return { transcript };
    }),

  // ⚠ Le flux d'Ava MET À JOUR la dernière ligne au lieu d'en créer une par fragment.
  //   Sans ça, une réponse de 300 mots produirait des centaines de lignes d'une syllabe
  //   — le terminal deviendrait illisible et le rendu s'effondrerait.
  streamAva: (text) =>
    set((prev) => {
      const derniere = prev.transcript[prev.transcript.length - 1];
      if (derniere?.role === 'ava' && derniere.streaming) {
        const copie = prev.transcript.slice(0, -1);
        return { transcript: [...copie, { ...derniere, text }] };
      }
      // ⚠ `as const` sur le rôle : sans lui, TypeScript infère `string` et refuse
      //   l'affectation à `TurnLine['role']`, qui est une union littérale.
      const ouverte: TurnLine = {
        id: ++compteur, role: 'ava', text, at: heure(), streaming: true,
      };
      return { transcript: [...prev.transcript, ouverte].slice(-MAX_LIGNES) };
    }),

  endAvaStream: () =>
    set((prev) => {
      const derniere = prev.transcript[prev.transcript.length - 1];
      if (!derniere?.streaming) return {};
      const copie = prev.transcript.slice(0, -1);
      // ⚠ On persiste ICI, a la FIN du flux, et pas a chaque fragment : `streamAva`
      //   est appele des dizaines de fois par reponse, et serialiser 400 lignes a
      //   chaque token ferait ramer la page pour rien.
      const transcript = [...copie, { ...derniere, streaming: false }];
      sauverTranscript(transcript);
      return { transcript };
    }),

  clearTranscript: () => {
    sauverTranscript([]);
    set({ transcript: [] });
  },

  /**
   * Remplace l'historique par celui du SERVEUR.
   *
   * ⚠ Le cache local (`localStorage`) reste utilisé comme affichage IMMÉDIAT au
   *   chargement — le serveur répond en quelques dizaines de millisecondes, mais une
   *   page qui s'ouvre vide puis se remplit donne l'impression d'avoir tout perdu.
   *   Le serveur fait ensuite autorité : c'est lui qui suit l'utilisateur d'un
   *   appareil à l'autre.
   */
  hydraterDepuisServeur: (lignes) =>
    set(() => {
      const transcript = lignes.slice(-MAX_LIGNES);
      sauverTranscript(transcript);
      return { transcript };
    }),
  setCognitive: (c) => set((prev) => ({ cognitive: { ...prev.cognitive, ...c } })),
  clearCognitive: () => set({ cognitive: { ...emptyCognitive } }),
}));
