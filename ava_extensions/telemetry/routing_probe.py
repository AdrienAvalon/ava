"""Sonde de routage — mesurer AVANT de router.

POURQUOI CETTE SONDE EXISTE (2026-08-04). L'objectif est un routage Haiku / Sonnet /
Opus selon la tâche. Le construire tout de suite reviendrait à régler des seuils sur des
suppositions : Ava n'a pas servi depuis trois mois, personne ne sait quelle proportion de
ses échanges appelle un outil, ni combien de jetons coûte une conversation ordinaire.
Un routeur mal réglé ne produit pas une panne — il produit **de mauvaises réponses**, ce
qui est bien plus difficile à diagnostiquer qu'un service qui tombe.

Cette sonde ne change RIEN au comportement. Elle observe, elle écrit une ligne JSON par
échange, et dans deux semaines les seuils se choisiront sur des données.

⚠ ELLE NE PATCHE PAS L'AMONT, ET C'EST SA PROPRIÉTÉ LA PLUS IMPORTANTE. Elle s'abonne à
  l'`EventBus` d'OpenJarvis (`INFERENCE_END`, `TOOL_CALL_START`, `CHAT_EXCHANGE_COMPLETED`)
  — un point d'extension prévu pour ça. Le dépôt compte déjà 4 fichiers amont patchés, et
  chacun est un conflit garanti à chaque synchronisation ; en ajouter un cinquième pour de
  la mesure temporaire serait un mauvais échange.

⚠ CE QU'ELLE MESURE, ET POURQUOI CES CHAMPS-LÀ :
  · `outil_appele` — **le signal décisif, et il n'est PAS la complexité du texte.**
    « Il fait quoi dehors ? » a un score de complexité très bas (court, une question, pas
    de code) et déclenche pourtant un appel d'outil vers le control plane. Or c'est
    exactement là que les petits modèles échouent : ils oublient l'outil ou inventent la
    réponse. Router sur la longueur enverrait donc les questions sur la maison au modèle
    le moins capable d'y répondre. On mesure les deux pour pouvoir le démontrer.
  · `complexite` — le score de l'amont (`score_complexity`), réutilisé tel quel. Inutile
    d'en écrire un autre : celui-ci pondère longueur, questions multiples, sous-tâches et
    domaine (code, maths, raisonnement).
  · `jetons_entree` / `jetons_sortie` — sans eux, « ça coûte cher » reste une impression.
  · `modele` — pour comparer ce qui a réellement servi, pas ce qu'on croit configuré.

⚠ AUCUN CONTENU DE CONVERSATION N'EST ÉCRIT. Ni la question, ni la réponse : ce journal
  vivrait sur une VM exposée et Ava parle de la maison, de la présence des personnes, de
  l'infrastructure. On garde la LONGUEUR de la question, pas la question.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ⚠ Sous `/var/log` on n'aurait pas le droit d'écrire (le daemon tourne en `avalon`), et
#   le fichier disparaîtrait au prochain nettoyage sous `/tmp`. Le répertoire de données
#   d'OpenJarvis est sauvegardé avec la VM.
CHEMIN = Path(
    os.environ.get(
        "AVA_ROUTING_LOG", str(Path.home() / ".openjarvis" / "routing-probe.jsonl")
    )
)

# ⚠ Un verrou : l'`EventBus` appelle ses abonnés « synchronously […] within the publishing
#   thread » (son propre docstring). Deux échanges concurrents écriraient donc en même
#   temps, et des lignes JSON entrelacées sont illisibles — donc inutilisables, ce qui
#   ruinerait la seule raison d'être de cette sonde.
_verrou = threading.Lock()

# État de l'échange en cours. Réinitialisé à chaque `CHAT_EXCHANGE_COMPLETED`.
_courant: dict[str, Any] = {}


def _score_complexite(texte: str) -> float | None:
    """Score de l'amont, ou None s'il n'est pas disponible.

    ⚠ `None` et non `0.0` : un zéro se lirait comme « question triviale » et fausserait
      toute la statistique dans le sens qui nous intéresse. Une mesure absente doit rester
      absente — c'est le défaut récurrent de ce projet.
    """
    try:
        from openjarvis.learning.routing.complexity import score_complexity

        return float(score_complexity(texte).score)
    except Exception as exc:  # noqa: BLE001 — une sonde ne casse jamais son hôte
        logger.debug("score_complexity indisponible: %s", exc)
        return None


def _ecrire(ligne: dict[str, Any]) -> None:
    try:
        CHEMIN.parent.mkdir(parents=True, exist_ok=True)
        with _verrou, CHEMIN.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        # ⚠ Une sonde qui fait tomber le service qu'elle observe est pire que pas de sonde.
        logger.debug("routing-probe: écriture impossible (%s)", exc)


def _sur_debut_outil(evenement: Any) -> None:
    d = getattr(evenement, "data", {}) or {}
    _courant.setdefault("outils", []).append(d.get("tool") or d.get("name") or "?")


def _sur_fin_inference(evenement: Any) -> None:
    d = getattr(evenement, "data", {}) or {}
    for cle, champs in (
        ("jetons_entree", ("prompt_tokens", "input_tokens", "tokens_in")),
        ("jetons_sortie", ("completion_tokens", "output_tokens", "tokens_out")),
        ("modele", ("model", "model_name")),
    ):
        for champ in champs:
            if d.get(champ) is not None:
                # ⚠ On ACCUMULE les jetons : un échange avec appel d'outil produit
                #   PLUSIEURS inférences (une pour décider de l'outil, une pour rédiger la
                #   réponse). Garder la dernière sous-estimerait le coût réel — soit
                #   exactement la grandeur qu'on cherche à mesurer.
                if cle.startswith("jetons") and isinstance(d[champ], int):
                    _courant[cle] = _courant.get(cle, 0) + d[champ]
                else:
                    _courant[cle] = d[champ]
                break


def _sur_echange_termine(evenement: Any) -> None:
    d = getattr(evenement, "data", {}) or {}
    question = str(d.get("user_message") or d.get("query") or d.get("prompt") or "")
    outils = _courant.get("outils") or []

    _ecrire(
        {
            "horodatage": round(time.time(), 3),
            # ⚠ La LONGUEUR, jamais le texte — cf. l'en-tête de ce fichier.
            "longueur_question": len(question),
            "complexite": _score_complexite(question) if question else None,
            "outil_appele": bool(outils),
            "outils": outils,
            "modele": _courant.get("modele"),
            "jetons_entree": _courant.get("jetons_entree"),
            "jetons_sortie": _courant.get("jetons_sortie"),
        }
    )
    _courant.clear()


def brancher(bus: Any) -> bool:
    """Abonne la sonde au bus. Rend True si elle est active.

    ⚠ Ne lève JAMAIS : si les noms d'événements changent lors d'une synchronisation amont,
      Ava doit continuer de fonctionner sans sa sonde — pas s'arrêter parce qu'un outil de
      mesure n'a pas trouvé son point d'accroche.
    """
    try:
        from openjarvis.core.events import EventType

        bus.subscribe(EventType.TOOL_CALL_START, _sur_debut_outil)
        bus.subscribe(EventType.INFERENCE_END, _sur_fin_inference)
        bus.subscribe(EventType.CHAT_EXCHANGE_COMPLETED, _sur_echange_termine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("routing-probe non branchée: %s", exc)
        return False
    logger.info("routing-probe active → %s", CHEMIN)
    return True
