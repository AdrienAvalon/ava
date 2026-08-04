"""Ava — boot loader.

Importé depuis `src/openjarvis/__init__.py` dans un `try/except ImportError` pour rester
optionnel (le paquet amont fonctionne toujours sans `ava_extensions`).

⚠ CE FICHIER EST LE POINT UNIQUE OÙ LES EXTENSIONS S'ENREGISTRENT. Un backend, un outil
  ou un patch présent sur le disque mais jamais importé ici est du **code mort que rien
  ne signale** : le décorateur d'enregistrement ne s'exécute pas, le registre reste
  simplement dépourvu de l'entrée, et aucune erreur n'est levée.

⚠ CHAQUE GROUPE EST ISOLÉ, ET C'EST LA PROPRIÉTÉ LA PLUS IMPORTANTE DE CE FICHIER.
  Il était écrit avec des imports nus au niveau du module. Or l'appelant amont fait :

      try:
          import ava_extensions.boot
      except ImportError:
          pass

  Donc **un seul import en échec faisait disparaître TOUT le reste, sans un mot** — le
  STT, les deux outils, le TTS français. Ce n'est pas théorique : le 2026-08-03, un
  `uv sync` aux extras incomplets a retiré le SDK `anthropic`, exactement la dépendance
  qu'importe `patches/anthropic_enhancements.py`. Il aurait suffi que le moteur démarre
  par ailleurs pour qu'Ava tourne **sans aucun de ses outils**, en répondant normalement.
  Une panne qui se présente comme un fonctionnement normal est la pire de toutes.

  Désormais chaque groupe a son propre `try`, journalise ce qu'il perd, et laisse les
  autres se charger. Le contrôle `boot.py importe les 4 extensions` de la CI empêche par
  ailleurs qu'un module soit oublié dans cette liste.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _charger(description: str, importer) -> None:  # noqa: ANN001 - callable d'import
    """Exécute un groupe d'imports en isolant son échec.

    ⚠ On attrape `Exception`, pas seulement `ImportError` : un module d'extension peut
      échouer à l'exécution de son propre corps (une constante mal formée, un fichier de
      configuration absent). Le résultat serait identique — tout le reste perdu — pour
      une cause qui n'est pas un import manquant.
    """
    try:
        importer()
    except Exception as exc:  # noqa: BLE001 - un groupe cassé ne doit pas emporter les autres
        logger.warning(
            "Ava: %s indisponible (%s: %s)", description, type(exc).__name__, exc
        )


def _backends() -> None:
    from ava_extensions.backends import kokoro_fr_tts, openai_whisper_ava_stt  # noqa: F401


def _patches() -> None:
    # ⚠ Le groupe le plus fragile : il importe le SDK `anthropic`, fourni par l'extra
    #   `inference-cloud`. C'est celui qui a sauté le 2026-08-03.
    from ava_extensions.patches import anthropic_enhancements, system_prompt_loader  # noqa: F401


def _skills() -> None:
    # ⚠ TOUT NOUVEL OUTIL DOIT ÊTRE AJOUTÉ ICI (et la CI le vérifie).
    from ava_extensions.skills import (  # noqa: F401
        avalon_status,
        home_assistant,
        memoire,
    )


def _sonde_routage() -> None:
    """Sonde de routage — MESURER avant de router (2026-08-04).

    Ne change rien au comportement : elle écrit une ligne JSON par échange (longueur de
    question, score de complexité, appel d'outil, jetons, modèle) pour que les seuils
    d'un futur routage Haiku/Sonnet/Opus se choisissent sur des données. Un routeur mal
    réglé ne tombe pas en panne — il rend de MAUVAISES réponses, bien plus difficiles à
    diagnostiquer.
    """
    from openjarvis.core.events import get_event_bus

    from ava_extensions.telemetry.routing_probe import brancher

    brancher(get_event_bus())


_charger("backends voix (TTS/STT)", _backends)
_charger("patches SDK Anthropic", _patches)
_charger("outils Avalon", _skills)
_charger("sonde de routage", _sonde_routage)
