"""Tests de l'outil `introspection`.

⚠ POURQUOI IL EXISTE. `evolutions` dit ce qui a CHANGÉ dans son code ; rien ne disait
  comment elle S'EN SORT. Sans mesure, une question comme « où est-ce que tu coinces ? »
  ne peut recevoir qu'une réponse devinée — et deviner, sur soi, produit des réponses
  agréables et fausses.

Ce fichier porte surtout sur les deux façons dont l'outil pourrait NUIRE : promettre une
donnée qu'il n'a pas, et faire passer « je n'ai pas pu regarder » pour « tout va bien ».
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ava_extensions.skills import introspection


@pytest.fixture
def _base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def poser(lignes: list[tuple]) -> Path:
        p = tmp_path / "traces.db"
        cx = sqlite3.connect(p)
        cx.execute(
            "create table traces (query text, outcome text, started_at real, "
            "total_tokens real, total_latency_seconds real)"
        )
        cx.executemany("insert into traces values (?,?,?,?,?)", lignes)
        cx.commit()
        cx.close()
        monkeypatch.setattr(introspection, "CHEMIN_TRACES", p)
        return p

    return poser


def _t(query="q", outcome="completed", age_s=60.0, jetons=1000.0, latence=5.0):
    return (query, outcome, time.time() - age_s, jetons, latence)


def test_le_resume_donne_le_taux_d_ABOUTISSEMENT(_base) -> None:
    _base([_t() for _ in range(8)] + [_t(outcome="tool_failure") for _ in range(2)])
    r = introspection.IntrospectionTool().execute()
    assert r.success is True
    assert "10 échanges" in r.content
    assert "80 %" in r.content


def test_l_ancien_libelle_success_compte_comme_ABOUTI(_base) -> None:
    """⚠ D'anciennes traces portent `success` au lieu de `completed`. Les ignorer ferait
    chuter le taux sans qu'aucune dégradation réelle ne se soit produite — un chiffre qui
    baisse pour une raison d'étiquetage est pire qu'un chiffre absent."""
    _base([_t(outcome="success"), _t(outcome="completed")])
    assert "100 %" in introspection.IntrospectionTool().execute().content


def test_les_ECHECS_sont_nommes_avec_leur_question(_base) -> None:
    _base([_t(query="Combien de redemarrages sur pbs ?", outcome="tool_failure")])
    r = introspection.IntrospectionTool().execute(vue="echecs")
    assert "pbs" in r.content


def test_un_echec_d_OUTIL_n_est_PAS_presente_comme_une_mauvaise_reponse(_base) -> None:
    """⚠ Confondre les deux ferait conclure à une dégradation là où il y a eu un BON
    réflexe. Cas mesuré : interrogée sur un hôte inexistant, elle a refusé d'inventer un
    chiffre — bonne réponse — et la trace porte `tool_failure` parce que l'outil, lui, a
    bien refusé."""
    _base([_t(outcome="tool_failure")])
    r = introspection.IntrospectionTool().execute(vue="echecs")
    assert "pas forcément une mauvaise réponse" in r.content


def test_AUCUN_echec_le_dit_franchement(_base) -> None:
    _base([_t(), _t()])
    assert (
        "Aucun échec" in introspection.IntrospectionTool().execute(vue="echecs").content
    )


def test_la_vue_COUTEUX_classe_par_jetons(_base) -> None:
    _base([_t(query="petite", jetons=100.0), _t(query="enorme", jetons=400000.0)])
    lignes = (
        introspection.IntrospectionTool().execute(vue="couteux").content.splitlines()
    )
    premiere = [x for x in lignes if "·" in x][0]
    assert "enorme" in premiere


def test_une_base_ILLISIBLE_n_est_PAS_un_bilan_vide(
    _base, tmp_path, monkeypatch
) -> None:
    """⚠ LE CONTRE-TEST QUI PORTE LE RISQUE. « Je n'ai pas pu regarder » et « je n'ai rien
    fait » se ressemblent, et une seule est vraie. Conclure à l'absence de problème parce
    qu'on n'a pas pu mesurer est la classe de défaut que ce système corrige partout."""
    monkeypatch.setattr(introspection, "CHEMIN_TRACES", tmp_path / "inexistante.db")
    r = introspection.IntrospectionTool().execute()
    assert r.success is False
    assert "n'arrive pas à lire" in r.content
    assert "Aucun" not in r.content


def test_une_periode_reellement_VIDE_est_distinguee(_base) -> None:
    _base([_t(age_s=40 * 86400)])  # hors de la fenêtre de 7 j
    r = introspection.IntrospectionTool().execute(depuis="7j")
    assert r.success is True
    assert "Aucun échange enregistré" in r.content


def test_AUCUNE_chaine_du_modele_n_atteint_la_requete(_base) -> None:
    """⚠ La vue et la fenêtre sont des `enum` ; une valeur inconnue retombe sur le défaut
    au lieu d'être interpolée. Le modèle choisit parmi des questions nommées."""
    _base([_t()])
    r = introspection.IntrospectionTool().execute(
        vue="'; drop table traces; --", depuis="x"
    )
    assert r.success is True
    assert "échanges" in r.content


def test_la_base_est_ouverte_en_LECTURE_SEULE(_base) -> None:
    """⚠ Ava écrit ses échanges dans cette base pendant qu'on la lit : une ouverture en
    écriture ferait d'une erreur de code une corruption de ses propres traces."""
    p = _base([_t()])
    introspection.IntrospectionTool().execute()
    assert p.exists()
    cx = sqlite3.connect(p)
    assert cx.execute("select count(*) from traces").fetchone()[0] == 1
    cx.close()
