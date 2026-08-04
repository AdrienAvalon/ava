"""Fixtures locales aux tests des extensions Ava.

⚠ POURQUOI CE FICHIER EXISTE — un test INSTABLE, attrapé le 2026-08-04.
  `test_le_backend_est_enregistre` passait seul et ÉCHOUAIT dans la suite complète.
  Cause : `tests/conftest.py` (hérité d'OpenJarvis) déclare une fixture `autouse=True`
  qui **vide tous les registres avant chaque test** — `SpeechRegistry` compris. Nos
  tests héritaient de ce nettoyage, et le backend s'y trouvait donc absent.

  ⚠ Réimporter le module ne suffit PAS à réparer : Python met les imports en cache, et
    un second `import` ne ré-exécute pas le décorateur `@SpeechRegistry.register`. Il
    faut un `importlib.reload()` explicite.

  ⚠ ET C'EST UN DÉFAUT QU'IL FALLAIT CORRIGER, PAS CONTOURNER EN IGNORANT LE TEST. Un
    test rouge une fois sur deux est pire qu'un test absent : on apprend à ignorer la
    couleur, et le jour où il signale une vraie régression, personne ne regarde. C'est
    exactement ce qui a permis à la disparition du backend STT de durer trois mois.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _reenregistrer_les_extensions() -> None:
    """Garantit que les extensions d'Ava sont dans les registres, quel que soit l'ordre.

    Se place APRÈS la fixture de nettoyage d'OpenJarvis : pytest applique les fixtures
    du conftest le plus proche en dernier, donc ce rechargement gagne.
    """
    from openjarvis.core.registry import SpeechRegistry

    if not SpeechRegistry.contains("openai_ava"):
        module = importlib.import_module(
            "ava_extensions.backends.openai_whisper_ava_stt"
        )
        importlib.reload(module)
