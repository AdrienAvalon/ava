"""Tests du rapport d'apprentissage."""

from __future__ import annotations
import sqlite3
import time
from pathlib import Path
import pytest
from ava_extensions.apprentissage import rapport


def _base(tmp: Path, traces: list[tuple], etapes: list[tuple]) -> Path:
    p = tmp / "traces.db"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE traces (trace_id TEXT, query TEXT, result TEXT, outcome TEXT, metadata TEXT, started_at REAL, total_tokens INT, model TEXT)"
    )
    c.execute("CREATE TABLE trace_steps (trace_id TEXT, step_type TEXT, input TEXT)")
    c.executemany("INSERT INTO traces VALUES (?,?,?,?,?,?,?,?)", traces)
    c.executemany("INSERT INTO trace_steps VALUES (?,?,?)", etapes)
    c.commit()
    c.close()
    return p


@pytest.fixture(autouse=True)
def _isole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rapport, "CHEMIN_TRACES", tmp_path / "traces.db")
    return tmp_path


def test_un_corpus_MINCE_refuse_de_conclure(_isole: Path) -> None:
    """⚠ LE TEST CENTRAL. Un rapport tire de douze echanges se lirait comme un verdict.
    Dire « aucun outil ne manque » sur un corpus mince est FAUX, et on le croirait —
    c'est la classe de defaut que ce projet documente partout."""
    t = time.time()
    _base(
        _isole,
        [(f"t{i}", "q", "r", None, None, t, 10, "m") for i in range(5)],
        [(f"t{i}", "generate", None) for i in range(5)],
    )
    r = rapport.analyser()
    assert r["suffisant"] is False
    assert "trop mince" in rapport.formuler(r)


def test_un_corpus_SUFFISANT_conclut(_isole: Path) -> None:
    t = time.time()
    _base(
        _isole,
        [(f"t{i}", f"question {i}", "r", None, None, t, 10, "m") for i in range(40)],
        [(f"t{i}", "generate", None) for i in range(40)],
    )
    r = rapport.analyser()
    assert r["suffisant"] is True
    assert "trop mince" not in rapport.formuler(r)


def test_les_traces_MONO_ETAPE_sont_ecartees(_isole: Path) -> None:
    """⚠ RESTRICTION LOAD-BEARING. Le chemin de streaming produit des traces sans
    `generate` ni `tool_call`. Les compter comme « questions sans outil » gonflerait le
    signal d'un tiers avec des cas ou l'agent n'a pas ete sollicite."""
    t = time.time()
    _base(
        _isole,
        [
            ("a", "q", "r", None, None, t, 1, "m"),
            ("b", "q", "r", None, None, t, 1, "m"),
        ],
        [("a", "generate", None)],
    )
    r = rapport.analyser()
    assert r["analysables"] == 1 and r["mono_etape"] == 1


def test_les_outils_appeles_sont_comptes(_isole: Path) -> None:
    t = time.time()
    _base(
        _isole,
        [("a", "q", "r", None, None, t, 1, "m")],
        [
            ("a", "generate", None),
            ("a", "tool_call", '{"tool": "journal"}'),
            ("a", "tool_call", '{"name": "memoire"}'),
        ],
    )
    r = rapport.analyser()
    assert r["outils_utilises"] == {"journal": 1, "memoire": 1}


def test_un_outil_ACTIF_mais_JAMAIS_appele_est_signale(_isole: Path) -> None:
    """⚠ Un outil active et jamais utilise est soit inutile, soit mal decrit. Les deux
    se corrigent, mais pas de la meme facon — et l'ignorer laisse un outil mort."""
    t = time.time()
    _base(
        _isole,
        [("a", "q", "r", None, None, t, 1, "m")],
        [("a", "generate", None), ("a", "tool_call", '{"tool": "journal"}')],
    )
    texte = rapport.formuler(
        rapport.analyser(), outils_actifs=["journal", "home_assistant"]
    )
    assert "Jamais appelés" in texte and "home_assistant" in texte


def test_les_ERREURS_sont_remontees_avec_leur_type(_isole: Path) -> None:
    """Le type distingue ce que le code HTTP confond — deux causes derriere un meme 500."""
    t = time.time()
    _base(
        _isole,
        [("a", "q", "", "error", '{"error_type": "BadRequestError"}', t, 0, "m")],
        [("a", "generate", None)],
    )
    r = rapport.analyser()
    assert r["erreurs"] == 1 and r["types_erreur"] == {"BadRequestError": 1}
    assert "BadRequestError" in rapport.formuler(r)


def test_une_base_ABSENTE_ne_leve_pas(_isole: Path) -> None:
    r = rapport.analyser()
    assert r["suffisant"] is False and r["traces"] == 0
    assert "rien à analyser" in rapport.formuler(r)


def test_la_fenetre_temporelle_est_respectee(_isole: Path) -> None:
    t = time.time()
    _base(
        _isole,
        [
            ("vieux", "q", "r", None, None, t - 30 * 86400, 1, "m"),
            ("neuf", "q", "r", None, None, t, 1, "m"),
        ],
        [("vieux", "generate", None), ("neuf", "generate", None)],
    )
    assert rapport.analyser(depuis_secondes=86400)["traces"] == 1
