"""Tests de l'outil `journal` — Ava relit ce qu'elle a percu.

⚠ POURQUOI CET OUTIL A FAILLI NE JAMAIS EXISTER. La perception enregistrait depuis des
  heures — trois faits en deux heures le soir du 2026-08-04, dont « le score est passe de
  98 a 96 » — et **aucun outil ne les relisait**. Ava percevait, memorisait, et restait
  incapable de repondre a « il s'est passe quoi cet apres-midi ? ».

  C'est le defaut corrige le MATIN MEME sur l'outil `memoire` (l'amont extrayait des
  faits que rien ne consommait), reproduit une couche plus haut le meme jour. Ecrire une
  memoire et la RELIER au modele sont deux gestes distincts, et le second se fait
  oublier parce que le premier « marche ».
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.perception.qualification import Changement, Niveau


@pytest.fixture(autouse=True)
def _base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from ava_extensions.perception import memoire_perception as mp

    monkeypatch.setattr(mp, "CHEMIN", tmp_path / "perception.db")
    return mp


@pytest.fixture
def outil() -> Any:
    import importlib.util

    chemin = Path(__file__).resolve().parents[1] / "skills" / "journal.py"
    import openjarvis.core.registry as reg

    original = reg.ToolRegistry.register
    reg.ToolRegistry.register = staticmethod(lambda cle: lambda c: c)  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location("_test_journal", chemin)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        reg.ToolRegistry.register = original  # type: ignore[assignment]


def _poser(mp: Any, sujet: str, texte: str, niveau: Niveau, il_y_a: float = 60) -> None:
    mp.enregistrer([Changement(sujet, texte, niveau, "maison")], time.time() - il_y_a)


# ══ Ce que l'outil rend ══════════════════════════════════════════════════════════


def test_il_rend_ce_qui_a_ete_percu(_base: Any, outil: Any) -> None:
    """⚠ LE TEST QUI JUSTIFIE L'OUTIL. Sans lui, cette donnee existe et reste
    inaccessible — la pire des situations, parce qu'on croit la mémoire en place."""
    _poser(_base, "Aurélie", "Aurélie est rentré·e", Niveau.NOTABLE)
    r = outil.JournalTool().execute()
    assert r.success is True
    assert "Aurélie est rentré·e" in r.content


def test_les_horodatages_sont_dits_a_l_ORAL(_base: Any, outil: Any) -> None:
    """⚠ Ava repond a la VOIX. « 2026-08-04T18:47:12 » se prononce atrocement et n'aide
    personne ; « il y a 20 min » est ce qu'un humain dirait."""
    _poser(_base, "x", "quelque chose", Niveau.NOTABLE, il_y_a=1200)
    contenu = outil.JournalTool().execute().content
    assert "il y a 20 min" in contenu
    assert "T" not in contenu.split("—")[0] or ":" in contenu


def test_un_fait_important_est_MARQUE(_base: Any, outil: Any) -> None:
    _poser(_base, "Ballon", "« Ballon » ne répond plus", Niveau.INTERRUPT)
    assert "⚠" in outil.JournalTool().execute().content


# ══ Les bornes — la lecon de Loki ════════════════════════════════════════════════


def test_la_periode_est_un_ENUM_pas_une_duree_libre(outil: Any) -> None:
    """⚠ Un modele qui pourrait ecrire « 900 jours » obtiendrait une reponse vide et en
    conclurait qu'il ne s'est rien passe — alors que la retention est de 90 jours. Une
    borne explicite se dit ; une borne implicite se decouvre au pire moment.
    C'est la lecon de Loki, ou une requete plausible et ruineuse est acceptee sans
    broncher (`max_query_bytes_read = 0B`, ~112 Go pour 30 jours)."""
    spec = outil.JournalTool().spec
    enum = spec.parameters["properties"]["periode"]["enum"]
    assert enum and all(isinstance(x, str) for x in enum)
    assert "type" in spec.parameters["properties"]["periode"]


def test_une_periode_INCONNUE_retombe_sur_un_defaut_sain(
    _base: Any, outil: Any
) -> None:
    _poser(_base, "x", "fait du jour", Niveau.NOTABLE)
    r = outil.JournalTool().execute(periode="depuis la nuit des temps")
    assert r.success is True
    assert "aujourd'hui" in r.content


def test_le_nombre_de_lignes_est_PLAFONNE(_base: Any, outil: Any) -> None:
    """Une journee chargee produit des dizaines de faits ; les rendre tous remplirait le
    contexte du modele pour rien."""
    for i in range(120):
        _poser(_base, f"s{i}", f"fait {i}", Niveau.MEMOIRE, il_y_a=i)
    assert outil.JournalTool().execute().content.count("  · ") <= outil.MAX_LIGNES


# ══ Le silence qui ne ment pas ══════════════════════════════════════════════════


def test_rien_a_dire_DISTINGUE_les_deux_causes(_base: Any, outil: Any) -> None:
    """⚠ LA REPONSE LA PLUS TROMPEUSE DE TOUT CE PROJET serait « rien ne s'est passe »
    alors qu'on n'a simplement rien observe. Un filtre trop etroit doit s'entendre comme
    tel — c'est la meme exigence que « aucun souvenir » vs « rien sur ce sujet » dans
    l'outil `memoire`."""
    contenu = outil.JournalTool().execute(sujet="inexistant").content
    assert "Rien de noté" in contenu
    assert "pas dans ce que je surveille" in contenu


def test_un_filtre_par_SUJET_fonctionne(_base: Any, outil: Any) -> None:
    _poser(_base, "Aurélie", "Aurélie est rentré·e", Niveau.NOTABLE)
    _poser(_base, "Ballon", "« Ballon » ne répond plus", Niveau.INTERRUPT)
    contenu = outil.JournalTool().execute(sujet="Ballon").content
    assert "Ballon" in contenu
    assert "Aurélie" not in contenu


def test_seulement_important_ECARTE_le_bruit(_base: Any, outil: Any) -> None:
    """⚠ `INTERRUPT` seulement, pas `NOTABLE` : inclure les allees et venues noierait une
    vraie panne dans une question du type « qu'est-ce qui a cloche ? »."""
    _poser(_base, "exterieur", "exterieur : 31 °C", Niveau.MEMOIRE)
    _poser(_base, "Aurélie", "Aurélie est rentré·e", Niveau.NOTABLE)
    _poser(_base, "Ballon", "« Ballon » ne répond plus", Niveau.INTERRUPT)
    contenu = outil.JournalTool().execute(seulement_important=True).content
    assert "Ballon" in contenu
    assert "31 °C" not in contenu and "Aurélie" not in contenu


# ══ La recurrence — « encore ce disjoncteur » ═══════════════════════════════════


def test_la_recurrence_permet_de_dire_ENCORE(_base: Any, outil: Any) -> None:
    """⚠ C'est ce qui separe un assistant qui CONSTATE d'un assistant qui se souvient.
    « C'est la troisieme fois ce mois-ci » demande un comptage sur une fenetre plus
    large que celle affichee — sinon Ava redecouvre chaque panne a neuf."""
    for j in (1, 8, 20):
        _poser(
            _base, "Disjoncteur", "ne répond plus", Niveau.INTERRUPT, il_y_a=j * 86400
        )
    _poser(_base, "Disjoncteur", "ne répond plus", Niveau.INTERRUPT, il_y_a=600)
    contenu = outil.JournalTool().execute(sujet="Disjoncteur").content
    assert "4 occurrences" in contenu


def test_pas_de_recurrence_SANS_sujet(_base: Any, outil: Any) -> None:
    """Un total global n'apprend rien : « 47 occurrences » toutes causes confondues ne
    se dit pas et ne s'interprete pas."""
    _poser(_base, "x", "fait", Niveau.NOTABLE)
    assert "occurrences" not in outil.JournalTool().execute().content


# ══ Robustesse ═════════════════════════════════════════════════════════════════


def test_une_base_ILLISIBLE_ne_leve_pas(_base: Any, outil: Any, tmp_path: Path) -> None:
    """⚠ Un outil qui leve prive Ava de reponse ET remonte une trace incomprehensible a
    l'utilisateur. On degrade en disant qu'on ne peut pas relire."""
    (tmp_path / "perception.db").write_bytes(b"pas une base")
    r = outil.JournalTool().execute()
    assert r.success is False
    assert "journal" in r.content.lower()


def test_l_outil_est_DISTINCT_de_memoire(outil: Any) -> None:
    """⚠ Les deux memoires n'ont pas la meme nature, et les confondre produirait des
    reponses fausses : `memoire` porte ce qui est VRAI sans date, `journal` ce qui est
    ARRIVE et quand. La description doit le dire au modele, c est elle qui guide le choix."""
    d = outil.JournalTool().spec.description
    assert "memoire" in d and "date" in d.lower()
