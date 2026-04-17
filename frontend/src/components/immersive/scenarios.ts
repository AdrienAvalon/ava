import type { CognitiveSignals } from './immersiveStore';

export interface Scenario {
  user: string;
  listenMs: number;
  thinkMs: number;
  cognitive: CognitiveSignals;
  ava: string;
  speakSpeed: number;
  settleMs?: number;
}

export const demoScenarios: Scenario[] = [
  {
    user: 'Comment va mon infra ?',
    listenMs: 1200,
    thinkMs: 1800,
    cognitive: {
      intent: 'query_infra_status',
      focus: 'adrien',
      tool: 'avalon_status',
      reflection: '1 tour',
      tone: 'attentive',
      memory: '~60 entrées',
    },
    ava: "Tout roule. Score 96 sur 100. Seul Wazuh traîne à 17 — trois policies SCA en dessous de 65 %, rien de critique. Tu veux qu'on regarde ?",
    speakSpeed: 26,
  },
  {
    user: 'Où en est le projet Ava ?',
    listenMs: 900,
    thinkMs: 1500,
    cognitive: {
      intent: 'self_reflect',
      focus: 'projet_ava',
      tool: 'mem_search',
      reflection: '2 tours',
      tone: 'complice',
      memory: '~60 entrées',
    },
    ava: "M0 à M6 bouclés côté backend. Là, on finalise l'UI V2 — orbe GitS, typo cinétique, signaux cognitifs. Environ 90 %, reste M5 wake-word et M8 mémoire longue durée.",
    speakSpeed: 24,
  },
  {
    user: 'Pose une question à Matrix #ops',
    listenMs: 1100,
    thinkMs: 1200,
    cognitive: {
      intent: 'send_matrix_message',
      focus: '#ops',
      tool: 'matrix_send',
      reflection: '1 tour',
      tone: 'efficace',
      memory: '~60 entrées',
    },
    ava: "Envoi en cours sur la room #ops. Je te ping dès qu'on répond.",
    speakSpeed: 30,
  },
];
