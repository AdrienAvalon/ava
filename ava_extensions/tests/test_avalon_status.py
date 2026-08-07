"""Tests du rendu de l'outil `avalon_status`.

⚠ Le module s'enregistre auprès du `ToolRegistry` à l'import : on neutralise le
  décorateur le temps du chargement, comme les autres tests d'outils de ce dépôt.
  ⚠ Et on n'en tire AUCUNE garantie sur l'enregistrement lui-même — c'est
  précisément ce que cette neutralisation rend invisible (défaut vécu le 2026-08-07,
  cf. `test_logs_outil.py::test_le_DECORATEUR_enregistre_bien_la_CLASSE`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def avalon_status() -> Any:
    import openjarvis.core.registry as reg

    original = reg.ToolRegistry.register
    reg.ToolRegistry.register = staticmethod(lambda cle: lambda c: c)  # type: ignore[assignment]
    try:
        chemin = Path(__file__).resolve().parents[1] / "skills" / "avalon_status.py"
        spec = importlib.util.spec_from_file_location("_test_avalon_status", chemin)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        reg.ToolRegistry.register = original  # type: ignore[assignment]
    return m


def test_l_HOTE_du_control_plane_est_RELAYE_au_modele(avalon_status: Any) -> None:
    """⚠ IL EXISTE POUR LEVER UNE COLLISION DE NOMS, pas pour décorer.

    Mesuré le 2026-08-07 : interrogée TROIS fois sur « le module `ava_chat` du control
    plane tourne-t-il sur ta VM ? » — c'est faux, il tourne sur AVA — Ava a confirmé les
    trois fois. Sa preuve : « l'hôte `ava` porte 41 conteneurs, c'est là que tourne
    ava_chat ». Elle avait la bonne information et en tirait la mauvaise conclusion,
    parce que **l'hôte s'appelle `ava` et elle s'appelle Ava**.

    Une règle de persona ne lève pas une ambiguïté de nommage — elle a été posée puis
    mesurée sans effet sur ce cas précis. Seule une donnée explicite le peut, et elle
    doit dire les DEUX moitiés : où ça tourne, et que ce n'est pas chez elle.
    """
    rendu = avalon_status._format_summary(
        {"score": {"global": 98}, "tourne_sur": "ava"}
    )
    assert "« ava »" in rendu
    assert "PAS sur ta VM" in rendu


def test_un_hote_ABSENT_n_invente_RIEN(avalon_status: Any) -> None:
    """⚠ Un control plane plus ancien que cet outil ne rend pas ce champ. Écrire une
    phrase avec un nom vide (« tourne sur «  » ») serait pire que se taire : elle a
    l'autorité d'une mesure et ne porte rien."""
    for absent in ({}, {"tourne_sur": ""}, {"tourne_sur": "   "}):
        rendu = avalon_status._format_summary({"score": {"global": 98}, **absent})
        assert "PAS sur ta VM" not in rendu
