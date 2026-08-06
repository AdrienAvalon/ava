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
            "create table traces (query text, outcome text, feedback real, "
            "started_at real, total_tokens real, total_latency_seconds real)"
        )
        cx.executemany("insert into traces values (?,?,?,?,?,?)", lignes)
        cx.commit()
        cx.close()
        monkeypatch.setattr(introspection, "CHEMIN_TRACES", p)
        return p

    return poser


def _t(
    query="q", outcome="completed", age_s=60.0, jetons=1000.0, latence=5.0, note=None
):
    return (query, outcome, note, time.time() - age_s, jetons, latence)


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
    assert "aucune réponse n'est sortie" in r.content
    assert "recovered" in r.content


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


# ══ Les notes humaines — le seul jugement de QUALITÉ du système ═══════════════════


def test_les_reponses_NOTEES_sont_rendues(_base) -> None:
    """⚠ `outcome` dit si ça a MARCHÉ, la note dit si c'était BIEN. Deux questions
    orthogonales — le magasin de traces écrasait justement l'une avec l'autre."""
    _base([_t(query="bonne question", note=1.0), _t(query="mauvaise", note=0.0)])
    r = introspection.IntrospectionTool().execute(vue="notes")
    assert "2 réponse(s) notée(s), dont 1 jugée(s) bonne(s)" in r.content
    assert "👍" in r.content and "👎" in r.content


def test_AUCUNE_note_n_est_PAS_un_jugement(_base) -> None:
    """⚠ LE CONTRE-TEST QUI PORTE LE RISQUE. Zéro note ne veut dire ni « mes réponses sont
    bonnes » ni « elles sont mauvaises » : personne ne les a jugées. L'absence de mesure
    n'est pas une mesure — c'est la famille de défaut qui a déjà produit un « 5,9 % de
    réussite » entièrement fabriqué."""
    _base([_t(), _t()])
    c = introspection.IntrospectionTool().execute(vue="notes").content
    assert "Aucune réponse notée" in c
    assert "L'absence de note ne mesure rien" in c


def test_on_REFUSE_un_taux_sur_trop_peu_de_notes(_base) -> None:
    """⚠ « 100 % de satisfaction » sur une seule note se lit comme une mesure et n'en est
    pas une. Au 2026-08-06 il n'y avait qu'UNE note sur 220 traces, parce que rien ne
    rendait le `trace_id` à Adrien."""
    _base([_t(note=1.0)])
    c = introspection.IntrospectionTool().execute(vue="notes").content
    assert "trop peu de notes" in c
    assert "100" not in c


def test_les_verdicts_MACHINE_ne_comptent_PAS_comme_des_notes(_base) -> None:
    """⚠ DÉFAUT TROUVÉ EN LUI PARLANT, le 2026-08-06. Interrogée sur ses notes, elle a
    répondu « 5 réponses notées par Adrien, dont 1 bonne ». **Faux** : 4 des 5 venaient de
    la machine (`feedback = 0.0` écrit sur chaque échec), une seule était humaine. Elle
    lisait ses propres verdicts automatiques comme des jugements de l'admin — donc se
    croyait notée quatre fois négativement par quelqu'un qui ne l'avait jamais jugée.
    ⚠ La cause était en amont : le collecteur écrivait dans le champ réservé au jugement
    humain. Il ne le fait plus ; ce test verrouille la conséquence côté lecture."""
    _base([_t(outcome="tool_failure"), _t(outcome="incomplete"), _t(note=1.0)])
    c = introspection.IntrospectionTool().execute(vue="notes").content
    assert "1 réponse(s) notée(s)" in c, "seule la note HUMAINE doit compter"
