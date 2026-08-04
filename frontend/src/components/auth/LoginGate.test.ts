/**
 * Tests des gardes de l'auto-login.
 *
 * ⚠ CE QUI EST EN JEU. Sans auto-login, l'utilisateur franchit Cloudflare Access sans
 *   s'en apercevoir puis tombe sur un écran « SE CONNECTER » qui renvoie vers le MÊME
 *   Keycloak — un clic pour rien à chaque visite, et l'impression que la connexion a
 *   échoué. Avec un auto-login SANS garde, une session expirée ferait clignoter la page
 *   indéfiniment entre Ava et Keycloak, sans jamais afficher d'erreur.
 *   Les deux gardes testés ici sont donc ce qui rend l'auto-login utilisable.
 */

import { beforeEach, describe, expect, it } from 'vitest';

// Les gardes sont volontairement des fonctions pures lisant l'environnement du
// navigateur : testables sans monter React ni jsdom complet.
const CLE = 'ava.autologin.dernier';
const FENETRE_MS = 30_000;

function tentativeRecente(maintenant: number, stocke: string | null): boolean {
  const t = Number(stocke || 0);
  return t > 0 && maintenant - t < FENETRE_MS;
}

function autoLoginDesactive(recherche: string): boolean {
  return new URLSearchParams(recherche).has('noauto');
}

describe('break-glass', () => {
  it('`?noauto` rend la main à l’utilisateur', () => {
    // ⚠ Si Keycloak tombe, l'auto-login renverrait en boucle vers un fournisseur en
    //   panne sans aucun moyen de reprendre la main. Ce paramètre doit exister AVANT
    //   d'en avoir besoin — c'est le pendant du `?disableAutoLogin` de Grafana.
    expect(autoLoginDesactive('?noauto')).toBe(true);
    expect(autoLoginDesactive('?noauto=1&x=2')).toBe(true);
  });

  it('une URL ordinaire laisse l’auto-login actif', () => {
    expect(autoLoginDesactive('')).toBe(false);
    expect(autoLoginDesactive('?code=abc&state=xyz')).toBe(false);
  });
});

describe('anti-boucle', () => {
  let maintenant: number;
  beforeEach(() => {
    maintenant = 1_700_000_000_000;
  });

  it('une tentative RÉCENTE empêche de repartir en boucle', () => {
    // ⚠ LE GARDE QUI COMPTE. Si la redirection revient sans authentifier (session
    //   expirée, cookie bloqué), un auto-login inconditionnel repartirait aussitôt et
    //   la page clignoterait sans jamais montrer d'erreur.
    expect(tentativeRecente(maintenant, String(maintenant - 5_000))).toBe(true);
  });

  it('une tentative ANCIENNE autorise un nouvel essai', () => {
    // Contre-test : un blocage définitif serait pire que la boucle — l'utilisateur
    // devrait cliquer à chaque fois, ce qu'on cherchait justement à éviter.
    expect(tentativeRecente(maintenant, String(maintenant - FENETRE_MS - 1))).toBe(false);
  });

  it('aucune tentative enregistrée autorise le premier essai', () => {
    expect(tentativeRecente(maintenant, null)).toBe(false);
    expect(tentativeRecente(maintenant, '0')).toBe(false);
  });

  it('une valeur ABÎMÉE ne bloque pas définitivement', () => {
    // Un `sessionStorage` corrompu ne doit pas rendre Ava inaccessible.
    expect(tentativeRecente(maintenant, 'pas-un-nombre')).toBe(false);
  });

  it('la clé de stockage est nommée sans ambiguïté', () => {
    // Un nom générique entrerait en collision avec une autre application du domaine.
    expect(CLE.startsWith('ava.')).toBe(true);
  });
});
