import { OIDC_AUTHORITY, OIDC_CLIENT_ID } from '../auth/oidcIdentity';

/**
 * Mémoire de conversation d'Ava — côté SERVEUR, par utilisateur.
 *
 * ⚠ REMPLACE `localStorage` (2026-08-04). L'historique vivait dans le navigateur : il
 *   repartait de zéro sur un autre appareil et disparaissait avec le cache. Demande de
 *   l'admin : « un historique par utilisateur […] peu importe le navigateur que
 *   j'utilise, une mémoire interne à Ava ».
 *
 * ⚠ DEUX MÉMOIRES, DEUX STATUTS, et il ne faut pas les confondre :
 *   · CE module → l'HISTORIQUE, **cloisonné par principal vérifié** : chacun retrouve sa
 *     conversation et ne voit jamais celle d'un autre ;
 *   · `memory_facts.jsonl` → ancien magasin partagé, **en quarantaine** : aucun chemin
 *     conversationnel ne le lit ni ne l'alimente. Il reste sauvegardé uniquement pour
 *     une future migration gouvernée et attribuée.
 */

/**
 * Jeton OIDC de l'utilisateur courant, lu directement dans le stockage d'`oidc-client-ts`.
 *
 * ⚠ POURQUOI PAS `useAuth()` : ce module est appelé depuis `useDaemonChat`, hors du
 *   cycle de rendu React (dans des callbacks asynchrones). Un hook y serait invalide.
 *   La clé suit le format d'`oidc-client-ts` : `oidc.user:<authority>:<client_id>`.
 *
 * ⚠ Rend `null` plutôt que de lever : le chat peut continuer sans profil, mais les
 *   routes d'historique refusent alors toute écriture plutôt que de créer un seau
 *   anonyme partagé.
 */
export function jetonOidc(): string | null {
  try {
    const cle = `oidc.user:${OIDC_AUTHORITY}:${OIDC_CLIENT_ID}`;
    const brut = sessionStorage.getItem(cle);
    if (!brut) return null;
    const u = JSON.parse(brut);
    // `id_token` porte les claims d'identité (`sub`, `preferred_username`) ;
    // `access_token` peut être opaque selon la configuration du realm.
    return u?.id_token || u?.access_token || null;
  } catch {
    /* stockage indisponible (navigation privée stricte) */
  }
  return null;
}

export function entetesIdentite(
  extra: Record<string, string> = {},
): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  const jeton = jetonOidc();
  if (jeton) h['X-Ava-Identity'] = jeton;
  return h;
}

export interface LigneServeur {
  role: string;
  texte: string;
  horodatage: number;
  turn_id?: string | null;
}

const DELAI_HYDRATATION_MS = 8_000;

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
  const controleur = new AbortController();
  const delai = globalThis.setTimeout(() => controleur.abort(), DELAI_HYDRATATION_MS);
  try {
    const r = await fetch('/v1/ava/conversation', {
      headers: entetesIdentite(),
      signal: controleur.signal,
    });
    if (!r.ok) return null; // 401/500 : on ne sait rien, on garde ce qu'on a
    const d = await r.json();
    return Array.isArray(d?.lignes) ? d.lignes : [];
  } catch {
    // ⚠ Un serveur injoignable ne doit pas empêcher de CONVERSER — on perd la mise à
    //   jour de la mémoire, pas l'usage d'Ava.
    return null;
  } finally {
    globalThis.clearTimeout(delai);
  }
}

/**
 * Efface l'historique de l'utilisateur courant, et de lui seul.
 *
 * Le booléen est l'acquittement serveur : un rejet HTTP ou réseau ne doit jamais
 * autoriser l'appelant à vider sa copie locale et masquer l'échec durable.
 */
export async function effacerConversation(): Promise<boolean> {
  try {
    const reponse = await fetch('/v1/ava/conversation', {
      method: 'DELETE',
      headers: entetesIdentite(),
    });
    return reponse.ok;
  } catch {
    return false;
  }
}
