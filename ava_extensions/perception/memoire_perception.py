"""L'état de perception d'Ava — ce qu'elle a vu, et qui doit survivre à un redémarrage.

⚠ POURQUOI UNE PERSISTANCE PLUTÔT QU'UNE VARIABLE. C'est la leçon centrale du chantier,
  et elle est mesurée : `modules/ai_analysis` du control plane garde sa ligne de base
  dans des variables globales. Le CP a redémarré **64 fois en 29 jours** (médiane
  1,8 h), le daemon d'Ava **60 fois le 04/08**. À chaque fois la baseline repart de
  zéro, donc le module est aveugle au changement — précisément au moment où
  l'infrastructure vient de bouger, puisqu'un redémarrage suit souvent un déploiement.

  Sans cette base, Ava serait la plus bavarde et la plus inutile juste après avoir
  perdu la mémoire : elle annoncerait l'arrivée des quatre habitants et la panne de
  tout ce qui est déjà en panne.

⚠ CE FICHIER GARDE DEUX CHOSES DISTINCTES, et les confondre serait une erreur :
  · le **dernier état observé** (une ligne, écrasée) — sert à comparer ;
  · le **journal des faits** (append) — sert à se souvenir. C'est lui qui permettra à
    Ava de dire « Aurélie est rentrée il y a vingt minutes » ou « c'est la troisième
    fois ce mois-ci ». Le premier répond à « qu'est-ce qui a changé », le second à
    « qu'est-ce qui s'est passé ».

⚠ SQLite et non un JSON : l'écriture doit être atomique. Un fichier JSON réécrit
  pendant un arrêt du service laisse un état tronqué, donc illisible au démarrage
  suivant — et un état illisible se comporte exactement comme un premier démarrage,
  c'est-à-dire qu'il fait taire la perception au lieu de la faire échouer bruyamment.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ava_extensions.perception.qualification import Changement, Niveau

logger = logging.getLogger(__name__)

CHEMIN = Path(
    os.environ.get(
        "AVA_PERCEPTION_DB", str(Path.home() / ".openjarvis" / "perception.db")
    )
).expanduser()

# ⚠ Rétention du journal. 90 jours couvre « c'est la troisième fois ce trimestre »
#   sans faire grossir indéfiniment un fichier que personne ne surveille. À comparer
#   aux 2,4 jours du tampon `/ops-messages` du CP, qui est aujourd'hui la seule
#   mémoire du récit — c'est justement ce trou qu'on comble.
RETENTION_JOURS = int(os.environ.get("AVA_PERCEPTION_RETENTION_J", "90"))

_verrou = threading.Lock()


def _connexion() -> sqlite3.Connection:
    CHEMIN.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CHEMIN, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dernier_etat (
               cle         TEXT PRIMARY KEY,
               contenu     TEXT NOT NULL,
               horodatage  REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS faits (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               horodatage  REAL NOT NULL,
               domaine     TEXT NOT NULL,
               niveau      TEXT NOT NULL,
               sujet       TEXT NOT NULL,
               fait        TEXT NOT NULL,
               details     TEXT
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faits_ts ON faits(horodatage)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faits_sujet ON faits(sujet)")
    return conn


def lire_dernier_etat(cle: str = "dashboard") -> dict[str, Any]:
    """Le dernier tableau de bord observé, ou `{}` au tout premier démarrage.

    ⚠ Rend `{}` sur TOUTE anomalie (fichier absent, JSON illisible, base corrompue).
      C'est délibéré : un état illisible doit produire le comportement du premier
      démarrage — silence et prise de ligne de base — plutôt qu'une exception qui
      arrêterait la perception. Le contraire ferait taire Ava définitivement sur une
      simple corruption.
    """
    try:
        with contextlib.closing(_connexion()) as conn:
            ligne = conn.execute(
                "SELECT contenu FROM dernier_etat WHERE cle = ?", (cle,)
            ).fetchone()
        if not ligne:
            return {}
        charge = json.loads(ligne[0])
        return charge if isinstance(charge, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: etat illisible, reprise a zero (%s)", exc)
        return {}


def ecrire_dernier_etat(etat: dict[str, Any], cle: str = "dashboard") -> bool:
    """Remplace la ligne de base. Rend False si l'écriture a échoué."""
    try:
        contenu = json.dumps(etat, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("perception: etat non serialisable (%s)", exc)
        return False
    try:
        with _verrou, contextlib.closing(_connexion()) as conn, conn:
            conn.execute(
                "INSERT INTO dernier_etat (cle, contenu, horodatage) VALUES (?,?,?) "
                "ON CONFLICT(cle) DO UPDATE SET contenu=excluded.contenu, "
                "horodatage=excluded.horodatage",
                (cle, contenu, time.time()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: ecriture de l'etat impossible (%s)", exc)
        return False


def enregistrer(faits: list[Changement], horodatage: float | None = None) -> int:
    """Ajoute des faits au journal. Rend le nombre écrit.

    ⚠ ON ENREGISTRE TOUS LES NIVEAUX, y compris `MEMOIRE`. C'est tout l'intérêt : la
      température d'hier après-midi et l'heure à laquelle la lumière du salon s'est
      éteinte ne valent rien pris isolément, et tout quand on demande « il faisait quoi
      hier soir ? ». Filtrer à l'écriture rendrait ces questions définitivement
      insolubles ; filtrer à la lecture ne coûte rien.
    """
    if not faits:
        return 0
    ts = time.time() if horodatage is None else horodatage
    lignes = [
        (
            ts,
            f.domaine,
            f.niveau.value,
            f.sujet,
            f.fait,
            json.dumps(f.details, ensure_ascii=False, default=str),
        )
        for f in faits
    ]
    try:
        with _verrou, contextlib.closing(_connexion()) as conn, conn:
            conn.executemany(
                "INSERT INTO faits (horodatage, domaine, niveau, sujet, fait, details) "
                "VALUES (?,?,?,?,?,?)",
                lignes,
            )
        return len(lignes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: journalisation impossible (%s)", exc)
        return 0


def relire(
    depuis_secondes: float = 86400,
    niveau_min: Niveau | None = None,
    sujet: str | None = None,
    limite: int = 200,
) -> list[dict[str, Any]]:
    """Les faits récents, du plus récent au plus ancien.

    ⚠ C'est la fonction que l'outil de mémoire épisodique appellera. Elle est bornée
      par construction (`limite`) : un modèle qui demanderait « tout » recevrait quand
      même une réponse de taille finie. La leçon vient de Loki, où une requête
      plausible et ruineuse est acceptée sans broncher.
    """
    ordre = {Niveau.MEMOIRE: 0, Niveau.NOTABLE: 1, Niveau.INTERRUPT: 2}
    admis = (
        [n.value for n, r in ordre.items() if r >= ordre[niveau_min]]
        if niveau_min
        else None
    )
    requete = "SELECT horodatage, domaine, niveau, sujet, fait, details FROM faits WHERE horodatage >= ?"
    params: list[Any] = [time.time() - max(0.0, depuis_secondes)]
    if admis:
        requete += f" AND niveau IN ({','.join('?' * len(admis))})"
        params.extend(admis)
    if sujet:
        requete += " AND sujet LIKE ?"
        params.append(f"%{sujet}%")
    requete += " ORDER BY horodatage DESC LIMIT ?"
    params.append(max(1, min(int(limite), 1000)))

    try:
        with contextlib.closing(_connexion()) as conn:
            lignes = conn.execute(requete, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: relecture impossible (%s)", exc)
        return []

    resultat = []
    for ts, domaine, niveau, suj, fait, details in lignes:
        try:
            d = json.loads(details) if details else {}
        except Exception:  # noqa: BLE001
            d = {}
        resultat.append(
            {
                "horodatage": ts,
                "domaine": domaine,
                "niveau": niveau,
                "sujet": suj,
                "fait": fait,
                "details": d,
            }
        )
    return resultat


def lisible() -> bool:
    """La base est-elle exploitable ? Distingue « rien à dire » de « je ne peux pas lire ».

    ⚠ POURQUOI CETTE FONCTION EXISTE — défaut trouvé par un test, pas par relecture.
      `relire()` ne lève jamais : c'est voulu, une perception ne doit pas tomber sur une
      base abîmée. Mais elle rend `[]` aussi bien quand rien ne s'est passé que quand le
      fichier est corrompu. L'outil `journal` répondait donc « rien de noté sur cette
      période » sur une base illisible — la réponse la plus trompeuse possible, et
      exactement le défaut récurrent de ce projet : *une source qui ne porte pas la
      donnée répond « rien » sans erreur.*
      Même distinction que `null` vs `[]` côté client (`memoireServeur.ts`), qui avait
      été introduite pour la même raison.
    """
    try:
        with contextlib.closing(_connexion()) as conn:
            conn.execute("SELECT 1 FROM faits LIMIT 1").fetchone()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: base illisible (%s)", exc)
        return False


def compter(sujet: str, depuis_secondes: float = 2592000) -> int:
    """Combien de fois un sujet est apparu dans le journal.

    ⚠ C'est ce qui permettra de dire « encore ce disjoncteur » plutôt que de le
      constater à neuf à chaque fois. L'humour et la complicité viennent de là — de la
      connaissance partagée d'un historique — pas d'un prompt plus drôle.
    """
    try:
        with contextlib.closing(_connexion()) as conn:
            ligne = conn.execute(
                "SELECT COUNT(*) FROM faits WHERE sujet LIKE ? AND horodatage >= ?",
                (f"%{sujet}%", time.time() - max(0.0, depuis_secondes)),
            ).fetchone()
        return int(ligne[0]) if ligne else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: comptage impossible (%s)", exc)
        return 0


def purger(retention_jours: int | None = None) -> int:
    """Supprime les faits trop anciens. Rend le nombre supprimé.

    ⚠ Une base qui grossit sans limite finit par être supprimée à la main, donc par
      tout perdre. Mieux vaut une rétention choisie qu'une purge subie.
    """
    jours = RETENTION_JOURS if retention_jours is None else retention_jours
    try:
        with _verrou, contextlib.closing(_connexion()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM faits WHERE horodatage < ?",
                (time.time() - jours * 86400,),
            )
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: purge impossible (%s)", exc)
        return 0
