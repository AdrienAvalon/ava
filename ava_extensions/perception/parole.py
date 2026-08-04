"""La parole d'Ava — quand elle a quelque chose à dire, et à quelles conditions.

⚠ LE JETON MATRIX NE VIT PAS ICI, ET C'EST TOUT LE MONTAGE. Ava passe par
  `POST /api/v1/ava/message` du control plane, qui détient déjà le jeton Matrix pour
  `matrix_alerting`. Elle ne connaît qu'un **jeton de parole** dont le seul pouvoir est
  d'émettre dans un salon FIXE — elle ne peut ni lire l'historique, ni rejoindre un
  autre salon, ni usurper une identité, ni toucher au registre du CP.

  Ce n'est pas de la précaution abstraite : Ava vit en DMZ et exécute du code
  communautaire ainsi qu'un LLM. L'infrastructure a **déjà refusé** le montage inverse
  — un jeton Matrix posé sur une VM exposée — pour Home Assistant, le 2026-08-02. On
  applique la même doctrine que pour l'accès à la maison : un service dual-homé sert
  d'intermédiaire, et le pouvoir reste de son côté.

⚠ CE FICHIER PORTE LA DISCIPLINE DE SILENCE, ET C'EST SA VRAIE RAISON D'ÊTRE.
  Décider d'émettre est trivial ; décider de SE TAIRE est ce qui fait la différence
  entre un organe et une source de bruit. Trois garde-fous, chacun né d'un défaut réel
  de cette infrastructure :

  1. **Seuls les faits `INTERRUPT` sont dits.** Les températures, les lumières et la
     présence sont perçues et mémorisées, jamais annoncées — sinon Ava parlerait
     plusieurs fois par heure et on la couperait au bout d'une journée.
  2. **Anti-répétition.** Un même sujet n'est redit qu'après un délai. Le digest des
     piles a crié chaque matin pendant des mois sur une sonde au DP figé : le signal
     était juste, sa répétition l'a rendu invisible.
  3. **Plafond horaire.** Au-delà, on se tait et on l'écrit dans le journal. Une panne
     en cascade produit des dizaines de faits ; les envoyer tous transformerait le
     salon en journal d'application, ce que #ops n'est pas.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ava_extensions.perception.qualification import Changement, Niveau

logger = logging.getLogger(__name__)

CP_BASE = os.environ.get("AVA_CP_BASE", "http://192.168.100.31:8100")

# ⚠ Le jeton arrive par un FICHIER, jamais par une variable d'environnement : sur cette
#   infrastructure les secrets sont des fichiers `/run/secrets/` ou `/etc/docker/secrets/`,
#   et une variable d'environnement se retrouve dans `/proc/<pid>/environ`, dans les
#   dumps de plantage et dans les journaux de démarrage.
CHEMIN_JETON = Path(
    os.environ.get(
        "AVA_VOICE_TOKEN_FILE", str(Path.home() / ".openjarvis" / "cp_voice_token")
    )
).expanduser()

# ⚠ Ces trois valeurs sont la discipline de silence, exprimée en nombres.
#   `REPETITION_S` : deux heures avant de redire le même sujet. Assez long pour ne pas
#   harceler, assez court pour qu'une panne qui dure reste présente à l'esprit.
REPETITION_S = float(os.environ.get("AVA_PAROLE_REPETITION_S", "7200"))
#   `PLAFOND_HORAIRE` : au-delà, on se tait. Une cascade produit des dizaines de faits.
PLAFOND_HORAIRE = int(os.environ.get("AVA_PAROLE_PLAFOND_H", "6"))
_TIMEOUT_S = 10.0

_verrou = threading.Lock()
_derniers: dict[str, float] = {}
_emissions: list[float] = []


def _jeton() -> str:
    try:
        return CHEMIN_JETON.read_text().strip()
    except Exception:  # noqa: BLE001
        return ""


def _emettre(texte: str) -> bool:
    """Envoie un message par le control plane. Ne lève jamais."""
    jeton = _jeton()
    if not jeton:
        # ⚠ En WARNING et non en DEBUG : une Ava muette parce qu'un secret manque doit
        #   se voir dans Loki. Un silence dont on ignore la cause est indistinguable
        #   d'une infrastructure calme.
        logger.warning("parole: jeton absent (%s) — Ava reste muette", CHEMIN_JETON)
        return False
    corps = json.dumps({"texte": texte}).encode("utf-8")
    requete = urllib.request.Request(
        f"{CP_BASE}/api/v1/ava/message",
        data=corps,
        headers={
            "Content-Type": "application/json",
            "X-CP-Voice-Token": jeton,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=_TIMEOUT_S) as reponse:
            return 200 <= reponse.status < 300
    except urllib.error.HTTPError as exc:
        # ⚠ On journalise le CORPS, pas seulement le code. `_post_matrix` du module
        #   `matrix_alerting` ne garde que le code, et c'est l'une des raisons pour
        #   lesquelles ses 151 échecs sont restés inexpliqués 29 jours durant.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        logger.warning("parole: refusee (%s) — %s", exc.code, detail)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("parole: control plane injoignable (%s)", exc)
        return False


# ⚠ Alias stable du transport reel. Les tests remplacent `_emettre` pour eprouver la
#   DECISION (qui parle, quand se taire) ; ceux qui eprouvent le TRANSPORT doivent
#   pouvoir retrouver la vraie fonction. Sans cet alias ils mesureraient le simulacre.
_emettre_reel = _emettre


def _autorise(fait: Changement, maintenant: float) -> tuple[bool, str]:
    """Ava a-t-elle le droit de dire ceci, maintenant ? Rend (oui, motif du refus)."""
    if fait.niveau is not Niveau.INTERRUPT:
        return False, "niveau insuffisant"
    dernier = _derniers.get(fait.sujet)
    if dernier is not None and (maintenant - dernier) < REPETITION_S:
        return False, f"deja dit il y a {int((maintenant - dernier) / 60)} min"
    recentes = [t for t in _emissions if maintenant - t < 3600]
    if len(recentes) >= PLAFOND_HORAIRE:
        return False, f"plafond horaire atteint ({PLAFOND_HORAIRE})"
    return True, ""


def dire(faits: list[Changement]) -> int:
    """Dit ce qui mérite de l'être. Rend le nombre de messages émis.

    ⚠ CETTE FONCTION SE TAIT BEAUCOUP PLUS SOUVENT QU'ELLE NE PARLE, et c'est la
      mesure de sa réussite. Chaque refus est journalisé avec son motif : un silence
      qu'on ne peut pas expliquer est un silence qu'on finit par croire cassé.
    """
    if not faits:
        return 0
    emis = 0
    with _verrou:
        maintenant = time.time()
        # Purge de la fenêtre glissante — sinon la liste croît indéfiniment.
        _emissions[:] = [t for t in _emissions if maintenant - t < 3600]
        for fait in faits:
            ok, motif = _autorise(fait, maintenant)
            if not ok:
                if fait.niveau is Niveau.INTERRUPT:
                    logger.info("parole: tue « %s » (%s)", fait.sujet, motif)
                continue
            if _emettre(fait.fait):
                _derniers[fait.sujet] = maintenant
                _emissions.append(maintenant)
                emis += 1
                logger.info("parole: dit « %s »", fait.fait)
    return emis


def reinitialiser() -> None:
    """Vide l'état d'anti-répétition (tests uniquement)."""
    with _verrou:
        _derniers.clear()
        _emissions.clear()
