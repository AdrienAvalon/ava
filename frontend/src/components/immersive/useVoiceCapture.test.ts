/**
 * Tests de la libération du micro.
 *
 * ⚠ CE QUI EST EN JEU N'EST PAS UNE FUITE DE RESSOURCE MAIS LA VIE PRIVÉE. Jusqu'au
 *   2026-08-04, `useVoiceCapture` n'avait AUCUN nettoyage au démontage : quitter la
 *   page en cours d'enregistrement (touche Échap → navigation) laissait les pistes
 *   actives, donc **le voyant micro du navigateur allumé jusqu'au rechargement complet
 *   de la page**, et le `MediaRecorder` émettant ses fragments toutes les 250 ms dans
 *   le vide. Sur un assistant installé à demeure dans une maison, c'est le genre de
 *   défaut qu'on ne veut pas.
 *
 * ⚠ CE QUE CES TESTS NE PROUVENT PAS, et il faut le dire : que le `useEffect` soit bien
 *   branché. Le vérifier demanderait `jsdom` + `@testing-library/react`, deux
 *   dépendances pour tester une garantie que React fournit (il appelle la fonction de
 *   nettoyage qu'on lui rend). Ce qui peut réellement casser — oublier les pistes,
 *   laisser une exception interrompre le nettoyage — est couvert ici.
 */

import { describe, expect, it, vi } from 'vitest';
import { libererMicro } from './useVoiceCapture';

function fauxFlux(nbPistes = 2) {
  const pistes = Array.from({ length: nbPistes }, () => ({ stop: vi.fn() }));
  return { flux: { getTracks: () => pistes }, pistes };
}

describe('libererMicro', () => {
  it('arrête TOUTES les pistes du flux', () => {
    // ⚠ « toutes » compte : un flux peut porter plusieurs pistes, et il suffit d'une
    //   seule encore active pour que le navigateur garde le voyant micro allumé.
    const { flux, pistes } = fauxFlux(3);
    libererMicro({ stop: vi.fn() }, flux);
    for (const p of pistes) expect(p.stop).toHaveBeenCalledOnce();
  });

  it("arrête l'enregistreur avant les pistes", () => {
    const ordre: string[] = [];
    const rec = { stop: () => ordre.push('recorder') };
    const flux = { getTracks: () => [{ stop: () => ordre.push('piste') }] };
    libererMicro(rec, flux);
    expect(ordre).toEqual(['recorder', 'piste']);
  });

  it('libère quand même les pistes si stop() LÈVE', () => {
    // ⚠ LE CAS QUI COMPTE. `MediaRecorder.stop()` lève `InvalidStateError` quand
    //   l'enregistreur est déjà inactif — situation banale : l'utilisateur a relâché le
    //   bouton puis navigué. Si l'exception remontait, les pistes ne seraient jamais
    //   arrêtées et le micro resterait ouvert. C'est exactement le défaut d'origine,
    //   qui se reproduirait sous une autre forme.
    const { flux, pistes } = fauxFlux(2);
    const rec = {
      stop: () => {
        throw new DOMException('already inactive', 'InvalidStateError');
      },
    };
    expect(() => libererMicro(rec, flux)).not.toThrow();
    for (const p of pistes) expect(p.stop).toHaveBeenCalledOnce();
  });

  it('ne lève pas quand rien n’est en cours (démontage sans enregistrement)', () => {
    // Le cas nominal : on ouvre la page, on ne parle pas, on s'en va.
    expect(() => libererMicro(null, null)).not.toThrow();
  });

  it('tolère un enregistreur absent mais un flux présent', () => {
    const { flux, pistes } = fauxFlux(1);
    libererMicro(null, flux);
    expect(pistes[0].stop).toHaveBeenCalledOnce();
  });
});
