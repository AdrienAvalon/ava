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
import os
from pathlib import Path

import pytest

# Les modules de test importent parfois ``openjarvis`` pendant la collecte, avant que
# la premiere fixture puisse s'executer. Cette seconde garde protege aussi une copie
# isolee de ce sous-arbre ; la garde principale vit dans le conftest racine.
os.environ["AVA_PERCEPTION"] = "0"

# ⚠ LES TROIS REGISTRES, PAS UN SEUL (étendu le 2026-08-04). Cette fixture ne réarmait
#   que `SpeechRegistry`, donc **aucun des tests n'observait jamais `ToolRegistry`** —
#   et `test_skills.py` neutralise volontairement le décorateur `@ToolRegistry.register`
#   pour charger les outils par chemin de fichier. Résultat : si la clé passait de
#   `home_assistant` à `homeassistant` lors d'une resynchro amont, ou si le décorateur
#   sautait sur un conflit de fusion, **le modèle ne verrait plus l'outil** — Ava
#   répondrait « je n'ai pas accès à la maison » sur une infra parfaitement saine — et
#   la suite resterait verte, tous les tests `ha_*` chargeant la classe par son chemin.
#   C'est exactement l'incident du 2026-04-27, pour lequel
#   `test_le_backend_est_enregistre`
#   a été écrit côté STT et **jamais transposé aux outils ni au TTS**.
_MODULES_PAR_CLE: tuple[tuple[str, str, str], ...] = (
    ("SpeechRegistry", "openai_ava", "ava_extensions.backends.openai_whisper_ava_stt"),
    ("TTSRegistry", "kokoro-fr", "ava_extensions.backends.kokoro_fr_tts"),
    ("ToolRegistry", "avalon_status", "ava_extensions.skills.avalon_status"),
    ("ToolRegistry", "home_assistant", "ava_extensions.skills.home_assistant"),
    ("ToolRegistry", "journal", "ava_extensions.skills.journal"),
    ("ToolRegistry", "logs", "ava_extensions.skills.logs"),
)


@pytest.fixture(autouse=True)
def _reenregistrer_les_extensions() -> None:
    """Garantit que les extensions d'Ava sont dans les registres, quel que soit l'ordre.

    Se place APRÈS la fixture de nettoyage d'OpenJarvis : pytest applique les fixtures
    du conftest le plus proche en dernier, donc ce rechargement gagne.

    ⚠ Réimporter ne suffit PAS : Python met les imports en cache, et un second `import`
      ne ré-exécute pas le décorateur d'enregistrement. Il faut `importlib.reload()`.
    """
    import openjarvis.core.registry as reg

    for nom_registre, cle, module in _MODULES_PAR_CLE:
        registre = getattr(reg, nom_registre, None)
        if registre is None or registre.contains(cle):
            continue
        try:
            importlib.reload(importlib.import_module(module))
        except Exception:  # noqa: BLE001 — un module absent est le sujet du test, pas une erreur de fixture
            pass


@pytest.fixture(scope="session")
def verificateur_lecture_seule():
    """Le détecteur de chemins d'écriture HTTP (`scripts/verifier-lecture-seule.py`).

    ⚠ Chargé PAR CHEMIN, et exposé en fixture plutôt qu'importé de fichier à fichier.
      Deux raisons : le script porte un tiret (donc n'est pas importable par son nom),
      et surtout le test doit porter sur le fichier RÉELLEMENT exécuté par la CI. Une
      copie du détecteur dans les tests dériverait — c'est exactement ce qui était
      arrivé au `grep` qu'il remplace, dupliqué entre le workflow et `test_skills.py`,
      les deux copies étant aveugles de la même façon.
    """
    import importlib.util

    chemin = (
        Path(__file__).resolve().parents[2] / "scripts" / "verifier-lecture-seule.py"
    )
    spec = importlib.util.spec_from_file_location("verifier_lecture_seule", chemin)
    assert spec and spec.loader, f"détecteur introuvable : {chemin}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
