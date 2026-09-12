"""Tests de l'observabilite des traces.

⚠ CE QUI EST EN JEU. On va donner a Ava une perception continue de l'infrastructure,
  puis une forme d'autonomie. Le precedent a ne pas rejouer est `ai_analysis` dans le
  control plane : **151 tentatives, 0 analyse delivree sur 29 jours**, en annoncant
  `_health: "ok"` du debut a la fin. Une IA dont les echecs sont invisibles reproduit
  ce defaut, et elle le reproduira en plus grand puisqu'elle agira.

  Ces tests verrouillent les deux conditions pour que ca n'arrive pas :
  ses traces sont ecrites la ou on les cherche, et ses ECHECS y figurent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.patches import traces_observabilite  # noqa: F401  (s'applique a l'import)


# ══ 1. Le chemin des traces ══════════════════════════════════════════════════════


def test_le_tilde_est_developpe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ LE BUG QUI A RENDU LE CORPUS INTROUVABLE.

    `TraceStore.__init__` faisait `str(db_path)` sans `expanduser()`, la ou
    `TelemetryStore` le fait bien. Resultat mesure sur la VM le 2026-08-04 :
    **43 traces et 106 etapes** dans `/home/avalon/ava/~/.openjarvis/traces.db` — un
    repertoire litteralement nomme `~` — et **0** dans le chemin documente.

    ⚠ Et ce repertoire est ignore par git via la regle `*~` ecrite pour les sauvegardes
      d'editeur : un `git clean -fdx` effacerait le seul substrat d'apprentissage dont
      on dispose, sans que rien ne le signale.
    """
    from openjarvis.traces.store import TraceStore

    # ⚠ On se place dans un repertoire temporaire : sans cela, le test verifierait
    #   l'absence d'un repertoire `~` dans le depot, ou un residu d'une execution
    #   PRECEDENTE le ferait echouer alors que le patch fonctionne. Un test dont le
    #   verdict depend de l'etat laisse par un autre test ne mesure pas ce qu'il croit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    store = TraceStore("~/.openjarvis/traces.db")
    chemin = Path(store._db_path)

    assert "~" not in str(chemin), f"le tilde n'a pas ete developpe : {chemin}"
    assert chemin.is_absolute()
    assert chemin.exists(), "le fichier n'a pas ete cree au bon endroit"
    # ⚠ Le repertoire pathologique ne doit PAS avoir ete cree. C'est LUI le vrai
    #   dommage : il est ignore par git via la regle `*~`, donc un `git clean -fdx`
    #   emporterait le corpus sans que rien ne le signale.
    assert not (tmp_path / "~").exists()


def test_memory_reste_memory() -> None:
    """`:memory:` n'est pas un chemin : le developper le transformerait en fichier
    nomme `:memory:` et casserait les tests amont qui l'utilisent."""
    from openjarvis.traces.store import TraceStore

    store = TraceStore(":memory:")
    assert store._db_path == ":memory:"


def test_un_chemin_absolu_est_INCHANGE(tmp_path: Path) -> None:
    """Contre-test : le patch ne doit toucher que le tilde."""
    from openjarvis.traces.store import TraceStore

    cible = tmp_path / "sous" / "traces.db"
    store = TraceStore(str(cible))
    assert Path(store._db_path) == cible


# ══ 2. Les echecs sont traces ════════════════════════════════════════════════════


class _AgentQuiEchoue:
    """Un agent qui leve, comme le faisait le SDK Anthropic le 2026-08-03."""

    agent_id = "test-agent"

    def run(self, entree: str, context: Any = None, **kwargs: Any) -> Any:
        raise ValueError("temperature is deprecated for this model")


class _AgentQuiRepond:
    agent_id = "test-agent"

    def run(self, entree: str, context: Any = None, **kwargs: Any) -> Any:
        from openjarvis.agents._stubs import AgentResult

        return AgentResult(content="ca marche", turns=1, metadata={"messages": []})


def _collecteur(agent: Any, chemin: Path) -> Any:
    from openjarvis.traces.collector import TraceCollector
    from openjarvis.traces.store import TraceStore

    return TraceCollector(agent, store=TraceStore(str(chemin)))


def _traces(chemin: Path) -> list[dict[str, Any]]:
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM traces")]
    finally:
        c.close()


def test_un_echange_qui_ECHOUE_laisse_une_trace(tmp_path: Path) -> None:
    """⚠ LE TEST CENTRAL DE CE FICHIER.

    `TraceCollector.run` construit sa `Trace` APRES l'appel de l'agent : une exception
    court-circuitait l'ecriture. Les 8 reponses en HTTP 500 du 2026-08-03 n'existaient
    donc **nulle part** — ni dans `traces.db`, ni dans la sonde de routage, ni dans
    Loki. On ne pouvait pas savoir qu'Ava echouait, et encore moins pourquoi.
    """
    chemin = tmp_path / "traces.db"
    collecteur = _collecteur(_AgentQuiEchoue(), chemin)

    with pytest.raises(ValueError):
        collecteur.run("il fait quoi dehors ?")

    lignes = _traces(chemin)
    assert len(lignes) == 1, "l'echec n'a laisse aucune trace"
    assert lignes[0]["query"] == "il fait quoi dehors ?"
    assert lignes[0]["outcome"] == "error"


def test_l_exception_d_origine_REMONTE_intacte(tmp_path: Path) -> None:
    """⚠ Un patch d'observabilite qui avalerait l'exception serait pire que le defaut
    qu'il corrige : l'appelant croirait avoir reussi. La trace est un effet de bord,
    jamais un detournement du flot."""
    collecteur = _collecteur(_AgentQuiEchoue(), tmp_path / "t.db")
    with pytest.raises(ValueError, match="temperature is deprecated"):
        collecteur.run("question")


def test_le_TYPE_d_exception_est_conserve(tmp_path: Path) -> None:
    """⚠ Le type distingue ce que le code HTTP confond. Les 8 erreurs du 2026-08-03
    avaient DEUX causes — un parametre deprecie, puis un credit epuise — et rendaient
    le meme 500. Sans le type, on cherche le mauvais defaut."""
    chemin = tmp_path / "t.db"
    collecteur = _collecteur(_AgentQuiEchoue(), chemin)
    with pytest.raises(ValueError):
        collecteur.run("q")
    import json

    meta = json.loads(_traces(chemin)[0]["metadata"] or "{}")
    assert meta.get("error_type") == "ValueError"
    assert "temperature is deprecated" in meta.get("error", "")


def test_un_echange_REUSSI_n_est_pas_marque_en_erreur(tmp_path: Path) -> None:
    """⚠ CONTRE-TEST INDISPENSABLE. Un patch qui marquerait tout en `error` rendrait la
    boucle d'apprentissage inutilisable — elle ne verrait plus que des echecs, donc
    plus aucun. Verifier qu'il attrape ne prouve rien sans verifier qu'il laisse
    passer."""
    chemin = tmp_path / "t.db"
    collecteur = _collecteur(_AgentQuiRepond(), chemin)
    collecteur.run("bonjour")

    lignes = _traces(chemin)
    assert len(lignes) == 1
    assert lignes[0]["outcome"] != "error"
    assert lignes[0]["result"] == "ca marche"


@pytest.mark.parametrize("failure", ("agent", "filter", "deferred"))
@pytest.mark.parametrize("with_store", (False, True))
def test_private_or_deferred_failure_has_no_observability_bypass(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    with_store: bool,
) -> None:
    """A rejected request cannot be persisted or logged by the error wrapper."""
    from openjarvis.agents._stubs import AgentResult
    from openjarvis.core.events import EventBus, EventType
    from openjarvis.traces.collector import TraceCollector
    from openjarvis.traces.store import TraceStore

    canary = "PRIVATE-OBSERVABILITY-FAILURE"

    class Agent:
        agent_id = "private-agent"

        def run(self, *args: Any, **kwargs: Any) -> AgentResult:
            if failure != "filter":
                raise ValueError(canary)
            return AgentResult(content=canary, turns=1, metadata={"messages": []})

    def reject(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(canary)

    bus = EventBus(record_history=True)
    store = TraceStore(tmp_path / "private.db") if with_store else None
    collector = TraceCollector(
        Agent(), store=store, bus=bus, defer_persistence=failure == "deferred"
    )
    caplog.clear()
    expected = ValueError if failure == "deferred" else RuntimeError
    with pytest.raises(expected):
        collector.run(canary, content_filter=None if failure == "deferred" else reject)

    assert store is None or store.list_traces() == []
    assert collector.last_trace is None
    assert not any(
        event.event_type == EventType.TRACE_COMPLETE for event in bus.history
    )
    assert canary not in caplog.text
    assert not any(
        record.name == traces_observabilite.__name__ for record in caplog.records
    )
    if store is not None:
        store.close()


def test_l_echec_n_ecrase_PAS_les_traces_precedentes(tmp_path: Path) -> None:
    """La succession reussite → echec doit donner deux lignes distinctes : c'est la
    PAIRE echec/succes qui porte l'information (« la formulation qui a marche est la
    specification de ce qui manquait »)."""
    chemin = tmp_path / "t.db"
    _collecteur(_AgentQuiRepond(), chemin).run("premiere question")
    with pytest.raises(ValueError):
        _collecteur(_AgentQuiEchoue(), chemin).run("deuxieme question")

    lignes = _traces(chemin)
    assert len(lignes) == 2
    assert {x["outcome"] for x in lignes} == {None, "error"} or "error" in {
        x["outcome"] for x in lignes
    }


def test_un_store_ABSENT_ne_fait_pas_tomber_l_echange(tmp_path: Path) -> None:
    """⚠ Le chemin de consignation s'execute DANS un gestionnaire d'exception. S'il
    levait, il remplacerait la cause reelle par une erreur d'instrumentation — et l'on
    chercherait le mauvais defaut. C'est le pire resultat possible pour du code dont
    l'unique role est de rendre les pannes lisibles."""
    from openjarvis.traces.collector import TraceCollector

    collecteur = TraceCollector(_AgentQuiEchoue(), store=None)
    with pytest.raises(ValueError, match="temperature"):
        collecteur.run("question")


def test_les_patches_sont_IDEMPOTENTS() -> None:
    """`boot.py` peut reimporter le module ; un double patch empilerait les wrappers et
    ecrirait deux traces par echec."""
    from openjarvis.traces.collector import TraceCollector
    from openjarvis.traces.store import TraceStore

    avant_run = TraceCollector.run
    avant_init = TraceStore.__init__
    traces_observabilite.appliquer()
    traces_observabilite.appliquer()
    assert TraceCollector.run is avant_run
    assert TraceStore.__init__ is avant_init
