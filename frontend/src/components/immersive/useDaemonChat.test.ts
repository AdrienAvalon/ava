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

import { describe, expect, it } from 'vitest';
import { extractNewSentences } from './useDaemonChat';

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
      "réponse réelle d'Ava sur l'état de la maison",
      'Il fait 26,7 degrés dehors.\n\nAdrien est présent, Aurélie absente.\n\nLa baie serveur tire 738 watts',
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
