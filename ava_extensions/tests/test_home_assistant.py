"""Tests du rendu de l'outil `home_assistant`.

⚠ Le module s'enregistre auprès du `ToolRegistry` à l'import : on neutralise le
  décorateur le temps du chargement, comme les autres tests d'outils de ce dépôt.
  ⚠ Et on n'en tire AUCUNE garantie sur l'enregistrement lui-même — c'est
  précisément ce que cette neutralisation rend invisible (cf.
  `test_logs_outil.py::test_le_DECORATEUR_enregistre_bien_la_CLASSE`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def outil() -> Any:
    import openjarvis.core.registry as reg

    original = reg.ToolRegistry.register
    reg.ToolRegistry.register = staticmethod(lambda cle: lambda c: c)  # type: ignore[assignment]
    try:
        chemin = Path(__file__).resolve().parents[1] / "skills" / "home_assistant.py"
        spec = importlib.util.spec_from_file_location("_test_home_assistant", chemin)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        reg.ToolRegistry.register = original  # type: ignore[assignment]
    return m


def test_le_DEPUIS_QUAND_du_chauffage_atteint_le_MODELE(outil: Any) -> None:
    """⚠ TREIZIÈME OCCURRENCE DE « COLLECTÉ MAIS NON RELAYÉ », et celle-ci je l'ai créée
    le jour même en livrant le producteur sans le consommateur.

    Mesure du 2026-08-07 : à « le chauffage des parents est coupé depuis ce matin ? »,
    Ava a répondu « ça confirme ta remarque » — les vannes étaient en hors-gel depuis
    QUATRE jours. Le control plane a été corrigé pour publier `depuis_h` ; interrogée
    juste après, elle répondait toujours « je n'ai pas cette date », parce que ce
    rendu-ci ne lisait pas le champ.
    """
    lignes = outil._chauffage(
        {
            "chauffage": {
                "Radiateur salon": {
                    "mode": "hors-gel",
                    "ouverture": 0,
                    "depuis_h": 88.3,
                }
            }
        }
    )
    assert "depuis 4 j" in lignes[0]


def test_une_duree_COURTE_reste_en_HEURES(outil: Any) -> None:
    """⚠ « depuis 0 j » pour trois heures serait faux ET absurde. Le seuil est à 48 h."""
    lignes = outil._chauffage(
        {"chauffage": {"X": {"mode": "confort", "depuis_h": 3.2}}}
    )
    assert "depuis 3 h" in lignes[0]


def test_un_chauffage_SANS_date_ne_rend_AUCUNE_duree(outil: Any) -> None:
    """⚠ LE CONTRE-TEST. Un control plane plus ancien que cet outil ne publie pas le
    champ, et un appareil non daté n'en a pas non plus. Écrire « depuis 0 h » aurait
    l'autorité d'une mesure sans rien porter — c'est le défaut qu'on corrige, inversé."""
    for cas in (
        {"mode": "hors-gel"},
        {"mode": "hors-gel", "depuis_h": 0},
        {"mode": "hors-gel", "depuis_h": None},
    ):
        lignes = outil._chauffage({"chauffage": {"X": cas}})
        assert "depuis" not in lignes[0]
