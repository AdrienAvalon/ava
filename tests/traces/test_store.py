"""Tests for the trace SQLite store."""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, Trace, TraceStep
from openjarvis.traces.store import TraceStore


def _make_trace(
    query: str = "test query",
    agent: str = "orchestrator",
    model: str = "qwen3:8b",
    engine: str = "ollama",
    outcome: str | None = None,
    feedback: float | None = None,
    num_steps: int = 2,
) -> Trace:
    """Helper to create a trace with steps."""
    now = time.time()
    steps = []
    for i in range(num_steps):
        steps.append(
            TraceStep(
                step_type=StepType.GENERATE if i % 2 == 0 else StepType.TOOL_CALL,
                timestamp=now + i * 0.1,
                duration_seconds=0.1 * (i + 1),
                input={"model": model} if i % 2 == 0 else {"tool": "calculator"},
                output={"tokens": 50} if i % 2 == 0 else {"success": True},
            )
        )
    trace = Trace(
        query=query,
        agent=agent,
        model=model,
        engine=engine,
        result="test result",
        outcome=outcome,
        feedback=feedback,
        started_at=now,
        ended_at=now + 1.0,
        total_tokens=100,
        total_latency_seconds=1.0,
        steps=steps,
    )
    return trace


class TestTraceStore:
    def test_creates_tables(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        assert store.count() == 0
        store.close()

    def test_save_and_get(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        trace = _make_trace()
        store.save(trace)

        retrieved = store.get(trace.trace_id)
        assert retrieved is not None
        assert retrieved.trace_id == trace.trace_id
        assert retrieved.query == "test query"
        assert retrieved.model == "qwen3:8b"
        assert retrieved.engine == "ollama"
        assert len(retrieved.steps) == 2
        assert retrieved.steps[0].step_type == StepType.GENERATE
        assert retrieved.steps[1].step_type == StepType.TOOL_CALL
        store.close()

    def test_get_nonexistent(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        assert store.get("nonexistent") is None
        store.close()

    def test_count(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        store.save(_make_trace(query="q1"))
        store.save(_make_trace(query="q2"))
        store.save(_make_trace(query="q3"))
        assert store.count() == 3
        store.close()

    def test_concurrent_saves_are_atomic_and_lossless(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        traces = [_make_trace(query=f"q{i}") for i in range(200)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(store.save, trace) for trace in traces]
            for future in futures:
                future.result()

        assert store.count() == len(traces)
        assert {trace.query for trace in store.list_traces(limit=len(traces))} == {
            trace.query for trace in traces
        }
        store.close()

    def test_list_traces_no_filter(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        store.save(_make_trace(query="q1"))
        store.save(_make_trace(query="q2"))
        traces = store.list_traces()
        assert len(traces) == 2
        store.close()

    def test_list_traces_filter_agent(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        store.save(_make_trace(agent="simple"))
        store.save(_make_trace(agent="orchestrator"))
        store.save(_make_trace(agent="simple"))
        traces = store.list_traces(agent="simple")
        assert len(traces) == 2
        assert all(t.agent == "simple" for t in traces)
        store.close()

    def test_list_traces_filter_model(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        store.save(_make_trace(model="qwen3:8b"))
        store.save(_make_trace(model="llama3:70b"))
        traces = store.list_traces(model="llama3:70b")
        assert len(traces) == 1
        assert traces[0].model == "llama3:70b"
        store.close()

    def test_list_traces_filter_outcome(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        store.save(_make_trace(outcome="success"))
        store.save(_make_trace(outcome="failure"))
        store.save(_make_trace(outcome="success"))
        traces = store.list_traces(outcome="success")
        assert len(traces) == 2
        store.close()

    def test_list_traces_filter_provenance_before_limit(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        owner = _make_trace(query="owner")
        owner.metadata = {"provenance": "principal:owner"}
        store.save(owner)
        for query in ("guest-1", "guest-2"):
            trace = _make_trace(query=query)
            trace.metadata = {"provenance": "principal:guest"}
            trace.started_at += 100
            store.save(trace)

        traces = store.list_traces(provenance="principal:owner", limit=1)

        assert [trace.query for trace in traces] == ["owner"]
        store.close()

    def test_list_traces_time_range(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        now = time.time()
        t1 = _make_trace(query="old")
        t1.started_at = now - 3600  # 1 hour ago
        t2 = _make_trace(query="recent")
        t2.started_at = now
        store.save(t1)
        store.save(t2)
        traces = store.list_traces(since=now - 60)
        assert len(traces) == 1
        assert traces[0].query == "recent"
        store.close()

    def test_list_traces_limit(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        for i in range(10):
            store.save(_make_trace(query=f"q{i}"))
        traces = store.list_traces(limit=3)
        assert len(traces) == 3
        store.close()

    def test_bus_subscription(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        bus = EventBus()
        store.subscribe_to_bus(bus)

        trace = _make_trace()
        bus.publish(EventType.TRACE_COMPLETE, {"trace": trace})

        assert store.count() == 1
        retrieved = store.get(trace.trace_id)
        assert retrieved is not None
        assert retrieved.query == trace.query
        store.close()

    def test_step_data_roundtrip(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        trace = _make_trace(num_steps=3)
        store.save(trace)

        retrieved = store.get(trace.trace_id)
        assert retrieved is not None
        assert len(retrieved.steps) == 3
        for orig, retr in zip(trace.steps, retrieved.steps):
            assert orig.step_type == retr.step_type
            assert orig.input == retr.input
            assert orig.output == retr.output
        store.close()

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = TraceStore(db_path)
        store.save(_make_trace())
        store.close()

        store2 = TraceStore(db_path)
        assert store2.count() == 1
        store2.close()

    def test_metadata_roundtrip(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "test.db")
        trace = _make_trace()
        trace.metadata = {"key": "value", "nested": [1, 2, 3]}
        store.save(trace)

        retrieved = store.get(trace.trace_id)
        assert retrieved is not None
        assert retrieved.metadata["key"] == "value"
        assert retrieved.metadata["nested"] == [1, 2, 3]
        store.close()


# ══ Une note humaine ne DÉTRUIT plus le verdict machine ═══════════════════════════


def _trace_notable(outcome: str | None):
    from openjarvis.core.types import Trace

    t = Trace(query="q", agent="a", model="m", engine="e", steps=[], result="r")
    t.outcome = outcome
    return t


def test_une_note_NE_DETRUIT_PAS_le_verdict_machine(tmp_path: Path) -> None:
    """⚠ LE DÉFAUT CORRIGÉ LE 2026-08-06. `update_feedback` écrivait
    `outcome = "success"/"failure"` dérivé de la note humaine. Or `outcome` porte un
    FAIT (`completed`, `recovered`, `tool_failure`) et la note un JUGEMENT DE QUALITÉ :
    deux questions orthogonales. Une réponse parfaitement aboutie peut être mauvaise ;
    une réponse coupée par un redémarrage n'est pas « mauvaise », elle est absente.
    ⚠ Mesure : **219 verdicts machine sur 220** auraient été écrasés par une seule note.
    Noter « 0,3 » une réponse aboutie aurait remplacé `recovered` par `failure` —
    perdant l'information « un outil a lâché mais elle s'en est sortie », justement
    celle qui sert
    à trouver les outils fragiles."""
    store = TraceStore(tmp_path / "t.db")
    trace = _trace_notable("recovered")
    store.save(trace)
    assert store.update_feedback(trace.trace_id, 0.3) is True
    relu = store.list_traces()[0]
    assert relu.outcome == "recovered", (
        "le fait machine doit survivre au jugement humain"
    )
    assert relu.feedback == 0.3
    store.close()


def test_une_trace_SANS_verdict_profite_encore_de_la_note(tmp_path: Path) -> None:
    """⚠ LE CONTRE-TEST. La dérivation existait pour une raison.

    Au 2026-08-05, `outcome` avait des lecteurs et aucun écrivain. La supprimer
    entièrement priverait les traces anciennes du seul verdict disponible. On ne
    dérive donc plus QUE si le champ est vide — `COALESCE` fait exactement cette
    lecture.
    """
    store = TraceStore(tmp_path / "t.db")
    trace = _trace_notable(None)
    store.save(trace)
    store.update_feedback(trace.trace_id, 0.9)
    assert store.list_traces()[0].outcome == "success"
    store.close()
