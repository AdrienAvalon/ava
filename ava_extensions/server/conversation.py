"""Mémoire de conversation d'Ava — côté SERVEUR, cloisonnée par utilisateur OIDC.

POURQUOI CE MODULE EXISTE (2026-08-04). L'historique vivait dans le `localStorage` du
navigateur : il repartait de zéro sur un autre appareil, et disparaissait avec le cache.
Demande de l'admin : « un historique par utilisateur […] peu importe le navigateur que
j'utilise, une mémoire interne à Ava ».

⚠ DEUX MÉMOIRES, DEUX PORTÉES — c'est l'architecture décidée avec l'admin, et elle est
  délibérée :

  ┌ HISTORIQUE (ce module) ──── CLOISONNÉ par personne ─────────────────────────┐
  │ « ce que TU m'as dit » — chacun retrouve sa conversation, sur n'importe      │
  │ quel appareil, et ne voit jamais celle d'un autre.                           │
  └─────────────────────────────────────────────────────────────────────────────┘
  ┌ FAITS (`openjarvis.memory`, natif) ──── CENTRAL, partagé ───────────────────┐
  │ « ce qu'Ava a APPRIS » — la chaudière est chez les parents, le NAS off-site  │
  │ est en append-only… Elle apprend de tout le monde, et c'est voulu : un       │
  │ assistant de maison qui réapprendrait la topologie à chaque interlocuteur    │
  │ serait absurde.                                                             │
  └─────────────────────────────────────────────────────────────────────────────┘

  La mémoire native est **mono-locataire** — aucune notion d'utilisateur dans tout
  `openjarvis/memory/` (vérifié : `user_id`, `owner`, `namespace` → zéro occurrence).
  Ce qui aurait été un défaut pour l'historique est exactement la propriété recherchée
  pour les faits. On n'a donc rien à corriger de ce côté : on l'active telle quelle.

  ⚠ CE QUE ÇA IMPLIQUE, ÉCRIT NOIR SUR BLANC : l'extracteur de faits ne sait pas
    distinguer « la chaufferie est au sous-sol » (utile à tous) d'un propos personnel
    tenu par une personne. Un fait tiré de la conversation de l'un PEUT donc remonter
    dans une réponse faite à l'autre. C'est le prix d'une mémoire centrale, accepté en
    connaissance de cause. Les faits sont dans `~/.openjarvis/memory_facts.jsonl` —
    lisible et éditable à la main si l'un d'eux n'a rien à y faire.

⚠ CE QUI IDENTIFIE L'UTILISATEUR, ET CE QUE ÇA VAUT VRAIMENT. Le frontend transmet son
  jeton OIDC Keycloak dans `X-Ava-Identity` ; on en lit le `sub` (identifiant stable,
  contrairement à l'e-mail ou au nom d'utilisateur qui peuvent changer — l'infra Avalon
  a précisément vécu une migration d'adresse en août 2026).
  **La signature n'est PAS vérifiée.** C'est un choix assumé et il doit être écrit :
  · le vrai rempart est en amont — Cloudflare Access devant `ava.avalon-network.com`,
    puis l'authentification du daemon ; personne n'atteint cet endpoint sans avoir déjà
    franchi les deux ;
  · le CP v2 applique la même doctrine (`core/auth.py`, décodage sans `verify_signature`,
    exclusion Semgrep documentée) — être cohérent vaut mieux qu'être subtil ici ;
  · CE QUE ÇA COÛTE : quelqu'un qui atteint déjà l'API peut forger un `sub` et lire la
    conversation d'un autre. Le cloisonnement protège donc de la CONFUSION entre
    utilisateurs, pas d'un attaquant ayant franchi CF Access.
  La correction de fond serait de valider la signature contre le JWKS de Keycloak
  (`auth.avalon-network.com/realms/master/protocol/openid-connect/certs`). À faire le
  jour où Ava servira des personnes qui ne se font pas mutuellement confiance.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHEMIN_BASE = Path.home() / ".openjarvis" / "ava-conversations.db"

# ⚠ Plafond par utilisateur. Une conversation n'est pas une archive : au-delà, le coût de
#   lecture croît sans que les tours anciens servent encore. 2000 lignes couvrent
#   plusieurs mois d'usage quotidien ; les plus anciennes sortent.
MAX_LIGNES = 2000

# ⚠ Un verrou : SQLite tolère les accès concurrents mais pas deux écritures simultanées
#   sur la même connexion. Le daemon est asynchrone — deux onglets ouverts suffisent.
_verrou = threading.Lock()


def _connexion() -> sqlite3.Connection:
    CHEMIN_BASE.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(CHEMIN_BASE, timeout=10)
    cx.execute(
        """
        CREATE TABLE IF NOT EXISTS lignes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            utilisateur TEXT NOT NULL,
            role       TEXT NOT NULL,
            texte      TEXT NOT NULL,
            horodatage REAL NOT NULL
        )
        """
    )
    # ⚠ L'index porte sur (utilisateur, id) et non sur `utilisateur` seul : toutes les
    #   lectures sont « les N dernières lignes DE CET utilisateur », donc triées par id.
    #   Sans la seconde colonne, SQLite trierait en mémoire à chaque requête.
    cx.execute("CREATE INDEX IF NOT EXISTS idx_util_id ON lignes(utilisateur, id)")
    return cx


def identite(entetes: Any) -> str | None:
    """Identifiant stable de l'appelant, ou `None` s'il n'a pas pu être établi.

    ⚠ REND `None` ET NON UNE CHAÎNE « anonyme » — corrigé le 2026-08-04 après une revue
      adversariale, et c'était une FUITE RÉELLE, pas théorique. Tous les chemins d'échec
      rendaient la même chaîne littérale, utilisée ensuite comme clé de cloisonnement :
      ce n'étaient donc pas des espaces distincts mais **un seul seau partagé**. Deux
      personnes dont le jeton a simplement expiré se retrouvaient dans la même
      conversation, et le `DELETE` de l'une effaçait celle de l'autre. Le docstring
      affirmait le contraire (« son propre espace, isolé des autres ») : c'était faux au
      sens qui compte.
      Déclenché par une panne ordinaire — jeton expiré, client qui n'envoie pas
      l'en-tête — donc bien plus probable qu'une attaque. L'appelant refuse maintenant
      d'écrire plutôt que de mutualiser.

    ⚠ LA PROVENANCE DU CLAIM EST PRÉFIXÉE (`sub:`, `un:`, `mail:`) : sans ça, les trois
      claims partagent un espace de noms plat, et un compte dont le `preferred_username`
      vaut le `sub` d'un autre lit sa conversation. Or `preferred_username` est
      modifiable par l'utilisateur dans plusieurs configurations Keycloak — cette
      confusion survivrait donc à la validation de signature.
    """
    try:
        brut = entetes.get("X-Ava-Identity") or ""
    except Exception:  # noqa: BLE001
        return None
    if not brut:
        return None
    jeton = brut.removeprefix("Bearer ").strip()
    try:
        # ⚠ Le corps d'un JWT est du base64url SANS remplissage : `urlsafe_b64decode`
        #   lève sur une longueur non multiple de 4. On complète nous-mêmes.
        corps = jeton.split(".")[1]
        corps += "=" * (-len(corps) % 4)
        charge = json.loads(base64.urlsafe_b64decode(corps))
        # ⚠ `isinstance` DANS le `try` : un corps JSON valide mais non-objet (`[1,2]`,
        #   `null`, `"x"`) décode sans erreur puis fait échouer `.get()` — un
        #   `AttributeError` qui remontait en HTTP 500 sur les trois routes, y compris
        #   `GET`. Le filet s'arrêtait une ligne trop tôt.
        if not isinstance(charge, dict):
            return None
    except Exception:  # noqa: BLE001
        logger.debug("jeton d'identité illisible")
        return None
    # ⚠ `sub` d'abord : c'est le SEUL identifiant stable. L'e-mail et le nom d'utilisateur
    #   changent — l'infra Avalon a migré l'adresse de `acros` le 2026-08-01, et deux
    #   consommateurs qui indexaient dessus ont cassé en silence.
    for prefixe, cle in (
        ("sub", "sub"),
        ("un", "preferred_username"),
        ("mail", "email"),
    ):
        valeur = charge.get(cle)
        if isinstance(valeur, str) and valeur:
            return f"{prefixe}:{valeur}"
    return None


def lire(utilisateur: str, limite: int = 200) -> list[dict[str, Any]]:
    """Les `limite` dernières lignes de CET utilisateur, du plus ancien au plus récent."""
    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            rangs = cx.execute(
                "SELECT role, texte, horodatage FROM lignes"
                " WHERE utilisateur = ? ORDER BY id DESC LIMIT ?",
                (utilisateur, limite),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("lecture de conversation impossible: %s", exc)
        return []
    return [
        {"role": r, "texte": t, "horodatage": h}
        for r, t, h in reversed(rangs)  # DESC pour la limite, remis dans l'ordre
    ]


# ⚠ LISTE BLANCHE DES RÔLES — sans elle, un client pouvait écrire une ligne
#   `role: "system"` au contenu arbitraire. Comme l'historique est REJOUÉ dans le
#   contexte du modèle à chaque tour, c'était une **injection de prompt persistante** :
#   une consigne posée une fois, respectée indéfiniment. Trouvé en revue adversariale.
ROLES_ADMIS = frozenset({"user", "assistant"})

# ⚠ Bornes de taille. `MAX_LIGNES` compte des LIGNES, pas des octets : 2000 lignes de
#   10 Mio feraient conserver ~20 Gio, et le plafond ne s'y opposerait pas. Contraste
#   relevé en revue : la route `speak` du même fichier borne déjà ses entrées à 500
#   caractères, celle-ci ne bornait rien.
MAX_CAR_TEXTE = 8000
MAX_LIGNES_PAR_ENVOI = 50


def ajouter(utilisateur: str, lignes: list[dict[str, Any]]) -> int:
    """Ajoute des lignes et applique le plafond. Rend le nombre écrit.

    ⚠ Tolérant aux entrées mal formées PLUTÔT QUE levant : la construction du lot se
      faisait hors du `try`, si bien qu'un élément non-dict ou un horodatage textuel
      produisait un HTTP 500. Une mémoire qui refuse une ligne doit refuser la ligne,
      pas la requête.
    """
    valides: list[tuple[str, str, str, float]] = []
    for ligne in lignes[:MAX_LIGNES_PAR_ENVOI]:
        if not isinstance(ligne, dict):
            continue
        texte = str(ligne.get("texte") or "")[:MAX_CAR_TEXTE]
        if not texte:
            continue
        role = str(ligne.get("role") or "")
        if role not in ROLES_ADMIS:
            continue
        # ⚠ `math.isfinite` : `float("NaN")` RÉUSSIT, mais SQLite stocke NaN comme NULL
        #   et la contrainte `NOT NULL` fait alors échouer TOUT l'`executemany`, qui est
        #   atomique. Une seule ligne empoisonnée effaçait donc le tour entier — question
        #   ET réponse — en rendant `{"ecrites": 0}` avec un HTTP 200. Un tour de
        #   conversation qui disparaît sans erreur visible est précisément ce que ce
        #   module doit empêcher.
        try:
            horodatage = float(ligne.get("horodatage") or time.time())
        except (TypeError, ValueError):
            horodatage = time.time()
        if not math.isfinite(horodatage):
            horodatage = time.time()
        valides.append((utilisateur, role, texte, horodatage))
    if not valides:
        return 0
    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            cx.executemany(
                "INSERT INTO lignes (utilisateur, role, texte, horodatage) VALUES (?,?,?,?)",
                valides,
            )
            # ⚠ Le plafond s'applique PAR UTILISATEUR, jamais globalement : un bavard
            #   effacerait sinon la mémoire des autres.
            cx.execute(
                "DELETE FROM lignes WHERE utilisateur = ? AND id NOT IN ("
                "  SELECT id FROM lignes WHERE utilisateur = ? ORDER BY id DESC LIMIT ?)",
                (utilisateur, utilisateur, MAX_LIGNES),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("écriture de conversation impossible: %s", exc)
        return 0
    return len(valides)


def effacer(utilisateur: str) -> int:
    """Efface la conversation de CET utilisateur uniquement."""
    try:
        with _verrou, contextlib.closing(_connexion()) as cx, cx:
            cur = cx.execute("DELETE FROM lignes WHERE utilisateur = ?", (utilisateur,))
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("effacement impossible: %s", exc)
        return 0
