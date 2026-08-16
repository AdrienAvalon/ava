/**
 * Tests du découpage en phrases pour la synthèse vocale.
 *
 * ⚠ PREMIER FICHIER DE TESTS DU FRONTEND. `vitest` était installé et configuré
 *   (`npm run test`), et **aucun test n'existait** — ce qui est le pire des deux
 *   mondes : la commande passe, le rapport est vert, et rien n'est vérifié.
 *
 * ⚠ CE QUI EST TESTÉ ICI EST UN BUG AUDIBLE, mesuré sur de vraies réponses d'Ava avant
 *   correction. Le reliquat de texte à prononcer était recalculé par
 *   `sentences.join(' ').length` alors que `extractNewSentences` retourne déjà le
 *   décalage exact (`newEnd`). Les deux ne coïncident que si chaque séparation entre
 *   phrases fait EXACTEMENT un caractère — or les phrases sont `trim()`ées et
 *   `join(' ')` n'en réinjecte qu'un.
 *
 *   Le cas à espace unique fonctionnait parfaitement. C'est précisément ce qui a laissé
 *   le défaut en place : il ne se manifeste que sur les réponses en paragraphes, donc
 *   sur les réponses longues, donc rarement sur un test rapide et systématiquement en
 *   usage réel.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  completerTourDurable,
  createHydrationGate,
  demanderChatDurable,
  extractNewSentences,
  TURN_ID_HEADER,
  type Message,
} from './useDaemonChat';

afterEach(() => vi.restoreAllMocks());

/** Le reliquat tel que le calculait le code AVANT correction. */
function resteAvant(assembled: string): string {
  const { sentences } = extractNewSentences(assembled, 0);
  return assembled.slice(sentences.join(' ').length).trim();
}

/** Le reliquat tel qu'il est calculé maintenant. */
function resteApres(assembled: string): string {
  const { newEnd } = extractNewSentences(assembled, 0);
  return assembled.slice(newEnd).trim();
}

describe('extractNewSentences — le décalage de fin', () => {
  it('rend un décalage qui pointe exactement après la dernière phrase complète', () => {
    const texte = 'Bonjour. Ça va. Et toi';
    const { sentences, newEnd } = extractNewSentences(texte, 0);
    expect(sentences).toEqual(['Bonjour.', 'Ça va.']);
    expect(texte.slice(newEnd).trim()).toBe('Et toi');
  });

  it.each([
    // [nom, texte, ce qu'Ava DOIT prononcer en dernier]
    [
      'réponse en paragraphes (le format markdown que produit le modèle)',
      'Un deux trois.\n\nQuatre cinq six.\n\nSept huit neuf.\n\nEt la fin',
      'Et la fin',
    ],
    [
      'double espace après le point',
      'Bonjour.   Ça va.   Et toi',
      'Et toi',
    ],
    [
      "exemple synthétique d'Ava sur l'état de la maison",
      'Il fait 26,7 degrés dehors.\n\nUne personne est présente, une autre absente.\n\nLa baie serveur tire 738 watts',
      'La baie serveur tire 738 watts',
    ],
  ])('ne fait PAS répéter un fragment — %s', (_nom, texte, attendu) => {
    expect(resteApres(texte)).toBe(attendu);
  });

  it('le calcul PRÉCÉDENT produisait bien le défaut (contre-preuve)', () => {
    // ⚠ Ce test échouerait si le bug n'avait jamais existé — il documente ce qui a été
    //   mesuré, et empêche qu'on « resimplifie » le code vers `join(' ').length` en
    //   croyant les deux formes équivalentes.
    const texte = 'Un deux trois.\n\nQuatre cinq six.\n\nSept huit neuf.\n\nEt la fin';
    expect(resteAvant(texte)).toBe('f.\n\nEt la fin');
    expect(resteApres(texte)).toBe('Et la fin');
  });

  it('reste correct sur le cas à espace unique — celui qui masquait le défaut', () => {
    const texte = 'Bonjour. Ça va. Et toi';
    expect(resteAvant(texte)).toBe(resteApres(texte));
  });

  it("n'invente pas de phrase sur un texte sans terminateur", () => {
    const { sentences, newEnd } = extractNewSentences('juste un fragment', 0);
    expect(sentences).toEqual([]);
    expect(newEnd).toBe(0);
  });

  it('ne coupe pas sur les abréviations courantes', () => {
    // ⚠ Sans cette garde, « Dr. » ou « M. » ferait parler Ava en syllabes hachées.
    const { sentences } = extractNewSentences('Le Dr. Martin arrive. Ensuite on verra.', 0);
    expect(sentences).toEqual(['Le Dr. Martin arrive.', 'Ensuite on verra.']);
  });

  it('reprend là où il s’est arrêté quand on le rappelle (flux)', () => {
    // Le cas du streaming : on appelle plusieurs fois avec le buffer qui grandit.
    const partiel = 'Première phrase. Deuxième ph';
    const a = extractNewSentences(partiel, 0);
    expect(a.sentences).toEqual(['Première phrase.']);

    const complet = 'Première phrase. Deuxième phrase. Reste';
    const b = extractNewSentences(complet, a.newEnd);
    expect(b.sentences).toEqual(['Deuxième phrase.']);
    expect(complet.slice(b.newEnd).trim()).toBe('Reste');
  });
});

describe('createHydrationGate — ordre historique puis envoi', () => {
  it('bloque le premier envoi jusqu’à une résolution explicite', async () => {
    const gate = createHydrationGate();
    let released = false;
    const waiting = gate.wait().then(() => { released = true; });

    await Promise.resolve();
    expect(released).toBe(false);
    gate.complete();
    await waiting;
    expect(released).toBe(true);
  });

  it('tolère plusieurs chemins de fin sans relancer la barrière', async () => {
    const gate = createHydrationGate();
    gate.complete();
    gate.complete();
    await expect(gate.wait()).resolves.toBeUndefined();
  });
});

function chatResponse(status: number, content = '', retryAfter?: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(retryAfter ? { 'Retry-After': retryAfter } : {}),
    json: async () => ({
      choices: [{ message: { role: 'assistant', content } }],
    }),
  } as Response;
}

describe('demanderChatDurable — retry HTTP idempotent', () => {
  const turnId = '4d593ddf-cf92-4d85-9d5d-68a961f5827b';
  const messages: Message[] = [{ role: 'user', content: 'question synthétique' }];
  const waitImpl = async () => undefined;

  it('réutilise le même UUID et le même corps si la première réponse réseau est perdue', async () => {
    const fetchImpl = vi.fn()
      .mockRejectedValueOnce(new TypeError('connexion perdue'))
      .mockResolvedValueOnce(chatResponse(200, 'réponse rejouée'));

    await expect(demanderChatDurable({
      messages,
      turnId,
      signal: new AbortController().signal,
      fetchImpl,
      retryDelaysMs: [0],
      waitImpl,
    })).resolves.toBe('réponse rejouée');

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const first = fetchImpl.mock.calls[0][1] as RequestInit;
    const second = fetchImpl.mock.calls[1][1] as RequestInit;
    expect(first.headers).toMatchObject({ [TURN_ID_HEADER]: turnId });
    expect(second.headers).toMatchObject({ [TURN_ID_HEADER]: turnId });
    expect(second.body).toBe(first.body);
  });

  it('réessaie un 503 transitoire', async () => {
    const transient = vi.fn()
      .mockResolvedValueOnce(chatResponse(503))
      .mockResolvedValueOnce(chatResponse(200, 'ok'));
    await expect(demanderChatDurable({
      messages,
      turnId,
      signal: new AbortController().signal,
      fetchImpl: transient,
      retryDelaysMs: [0],
      waitImpl,
    })).resolves.toBe('ok');
    expect(transient).toHaveBeenCalledTimes(2);
  });

  it.each([401, 409, 422])('ne réessaie pas le refus HTTP %i', async (status) => {
    const denied = vi.fn().mockResolvedValue(chatResponse(status));
    await expect(demanderChatDurable({
      messages,
      turnId,
      signal: new AbortController().signal,
      fetchImpl: denied,
      retryDelaysMs: [0, 0],
      waitImpl,
    })).rejects.toThrow(`HTTP ${status}`);
    expect(denied).toHaveBeenCalledTimes(1);
  });

  it('réessaie un 425 avec le même tour jusqu’au replay durable', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(chatResponse(425, '', '5'))
      .mockResolvedValueOnce(chatResponse(200, 'réponse persistée'));
    const wait = vi.fn().mockResolvedValue(undefined);

    await expect(demanderChatDurable({
      messages,
      turnId,
      signal: new AbortController().signal,
      fetchImpl,
      retryDelaysMs: [0],
      waitImpl: wait,
    })).resolves.toBe('réponse persistée');

    expect(wait).toHaveBeenCalledWith(5_000, expect.any(AbortSignal));
    const first = fetchImpl.mock.calls[0][1] as RequestInit;
    const second = fetchImpl.mock.calls[1][1] as RequestInit;
    expect(first.headers).toMatchObject({ [TURN_ID_HEADER]: turnId });
    expect(second.headers).toMatchObject({ [TURN_ID_HEADER]: turnId });
    expect(second.body).toBe(first.body);
  });

  it('ne transforme pas une réponse 200 vide en tour validé', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(chatResponse(200, ''))
      .mockResolvedValueOnce(chatResponse(200, 'réponse complète'));

    await expect(demanderChatDurable({
      messages,
      turnId,
      signal: new AbortController().signal,
      fetchImpl,
      retryDelaysMs: [0],
      waitImpl,
    })).resolves.toBe('réponse complète');
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });
});

describe('completerTourDurable — contexte modèle transactionnel', () => {
  it('ajoute question et réponse ensemble après accusé durable', async () => {
    const history: Message[] = [{ role: 'assistant', content: 'avant' }];

    await expect(completerTourDurable(
      history,
      'question',
      async (candidate) => {
        expect(candidate).toEqual([
          { role: 'assistant', content: 'avant' },
          { role: 'user', content: 'question' },
        ]);
        return 'réponse';
      },
    )).resolves.toBe('réponse');
    expect(history).toEqual([
      { role: 'assistant', content: 'avant' },
      { role: 'user', content: 'question' },
      { role: 'assistant', content: 'réponse' },
    ]);
  });

  it.each(['échec HTTP', 'abandon utilisateur'])(
    'ne laisse aucune question orpheline après %s',
    async (raison) => {
      const history: Message[] = [{ role: 'assistant', content: 'avant' }];
      const erreur = raison === 'abandon utilisateur'
        ? new DOMException('Aborted', 'AbortError')
        : new Error('HTTP 503');

      await expect(completerTourDurable(
        history,
        'question non validée',
        async () => { throw erreur; },
      )).rejects.toThrow();
      expect(history).toEqual([{ role: 'assistant', content: 'avant' }]);
    },
  );
});
