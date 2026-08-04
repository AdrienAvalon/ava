/**
 * Mémoire de conversation d'Ava — côté SERVEUR, par utilisateur.
 *
 * ⚠ REMPLACE `localStorage` (2026-08-04). L'historique vivait dans le navigateur : il
 *   repartait de zéro sur un autre appareil et disparaissait avec le cache. Demande de
 *   l'admin : « un historique par utilisateur […] peu importe le navigateur que
 *   j'utilise, une mémoire interne à Ava ».
 *
 * ⚠ DEUX MÉMOIRES, DEUX PORTÉES, et il ne faut pas les confondre :
 *   · CE module → l'HISTORIQUE, **cloisonné par personne** : chacun retrouve sa
 *     conversation et ne voit jamais celle d'un autre ;
 *   · `openjarvis.memory` (natif, activé côté serveur) → les FAITS, **centraux** :
 *     Ava apprend de tout le monde, ce qui est voulu — un assistant de maison qui
 *     réapprendrait la topologie à chaque interlocuteur serait absurde.
 */

/**
 * Jeton OIDC de l'utilisateur courant, lu directement dans le stockage d'`oidc-client-ts`.
 *
 * ⚠ POURQUOI PAS `useAuth()` : ce module est appelé depuis `useDaemonChat`, hors du
 *   cycle de rendu React (dans des callbacks asynchrones). Un hook y serait invalide.
 *   La clé suit le format d'`oidc-client-ts` : `oidc.user:<authority>:<client_id>`.
 *
 * ⚠ Rend `null` plutôt que de lever : sans identité, la conversation doit continuer —
 *   le serveur rangera simplement l'échange dans un espace « anonyme », isolé.
 */
function jetonOidc(): string | null {
  try {
    const prefixe = 'oidc.user:';
    for (let i = 0; i < sessionStorage.length; i++) {
      const cle = sessionStorage.key(i);
      if (!cle || !cle.startsWith(prefixe)) continue;
      const brut = sessionStorage.getItem(cle);
      if (!brut) continue;
      const u = JSON.parse(brut);
      // `id_token` porte les claims d'identité (`sub`, `preferred_username`) ;
      // `access_token` peut être opaque selon la configuration du realm.
      return u?.id_token || u?.access_token || null;
    }
  } catch {
    /* stockage indisponible (navigation privée stricte) */
  }
  return null;
}

function entetes(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' };
  const jeton = jetonOidc();
  if (jeton) h['X-Ava-Identity'] = jeton;
  return h;
}

export interface LigneServeur {
  role: string;
  texte: string;
  horodatage: number;
}

/**
 * Historique de l'utilisateur courant.
 *
 * ⚠ REND `null` QUAND LE SERVEUR EST MUET, ET `[]` QUAND IL RÉPOND VIDE — la distinction
 *   est load-bearing, et son absence était une FUITE (audit du 2026-08-04).
 *   Les deux cas rendaient `[]`, et l'appelant abandonnait l'hydratation dans les deux.
 *   Or « le serveur dit que tu n'as pas d'historique » doit VIDER le cache local, tandis
 *   que « le serveur est injoignable » doit le CONSERVER. Confondus, un utilisateur qui
 *   se connecte pour la première fois sur un navigateur partagé héritait de l'historique
 *   du précédent.
 */
export async function lireConversation(): Promise<LigneServeur[] | null> {
  try {
    const r = await fetch('/v1/ava/conversation', { headers: entetes() });
    if (!r.ok) return null; // 401/500 : on ne sait rien, on garde ce qu'on a
    const d = await r.json();
    return Array.isArray(d?.lignes) ? d.lignes : [];
  } catch {
    // ⚠ Un serveur injoignable ne doit pas empêcher de CONVERSER — on perd la mise à
    //   jour de la mémoire, pas l'usage d'Ava.
    return null;
  }
}

/**
 * Ajoute des lignes à l'historique de l'utilisateur courant.
 *
 * ⚠ L'appel n'est PAS attendu par l'appelant (`void`) : persister est un effet de bord,
 *   et faire patienter l'affichage d'une réponse déjà reçue pour un écrit en base
 *   ajouterait de la latence là où l'utilisateur regarde.
 */
export function ajouterConversation(lignes: { role: string; texte: string }[]): void {
  if (!lignes.length) return;
  const charge = lignes.map((l) => ({ ...l, horodatage: Date.now() / 1000 }));
  fetch('/v1/ava/conversation', {
    method: 'POST',
    headers: entetes(),
    body: JSON.stringify({ lignes: charge }),
  }).catch(() => {
    /* mémoire indisponible — la conversation continue */
  });
}

/** Efface l'historique de l'utilisateur courant, et de lui seul. */
export async function effacerConversation(): Promise<void> {
  try {
    await fetch('/v1/ava/conversation', { method: 'DELETE', headers: entetes() });
  } catch {
    /* rien à faire : l'affichage est vidé de toute façon */
  }
}
