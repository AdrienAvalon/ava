"""Tests for ScanChunksTool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import ToolRegistry

_MISSING = object()


@pytest.fixture()
def store(tmp_path: Path) -> KnowledgeStore:
    ks = KnowledgeStore(str(tmp_path / "test.db"))
    ks.store("Met with Sequoia about Series A", source="granola", doc_type="document")
    ks.store("Fundraising discussion with a16z", source="granola", doc_type="document")
    ks.store("Weekly standup notes", source="granola", doc_type="document")
    ks.store("Trip to Spain with family", source="imessage", doc_type="message")
    return ks


def _fake_engine(
    content: str = "Found: Sequoia Series A discussion, a16z fundraising",
    finish_reason: Any = "stop",
) -> MagicMock:
    engine = MagicMock()
    response = {"content": content, "usage": {}}
    if finish_reason is not _MISSING:
        response["finish_reason"] = finish_reason
    engine.generate.return_value = response
    return engine


def test_scan_finds_semantic_matches(store: KnowledgeStore) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine()
    tool = ScanChunksTool(store=store, engine=engine, model="test", max_tokens=4096)
    result = tool.execute(question="Which VCs have I spoken with?")
    assert result.success
    assert "Sequoia" in result.content or "Found" in result.content
    assert engine.generate.called


def test_scan_respects_source_filter(store: KnowledgeStore) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine()
    tool = ScanChunksTool(store=store, engine=engine, model="test", max_tokens=4096)
    result = tool.execute(question="What trips?", source="imessage")
    assert result.success
    call_args = engine.generate.call_args
    messages = call_args[0][0] if call_args[0] else call_args[1].get("messages", [])
    all_content = str(messages)
    assert "Spain" in all_content


def test_scan_empty_store(tmp_path: Path) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    ks = KnowledgeStore(str(tmp_path / "empty.db"))
    engine = _fake_engine()
    tool = ScanChunksTool(store=ks, engine=engine, model="test", max_tokens=4096)
    result = tool.execute(question="Anything?")
    assert result.success
    assert "no chunks" in result.content.lower() or result.content == ""


def test_registered() -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    ToolRegistry.register_value("scan_chunks", ScanChunksTool)
    assert ToolRegistry.contains("scan_chunks")


def test_scan_inherits_runtime_completion_limit(
    store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    monkeypatch.setattr(
        "openjarvis.core.config.load_config",
        lambda: SimpleNamespace(
            intelligence=SimpleNamespace(max_tokens=16_384),
        ),
    )
    engine = _fake_engine()

    result = ScanChunksTool(store=store, engine=engine, model="test").execute(
        question="Which VCs have I spoken with?",
    )

    assert result.success is True
    assert engine.generate.call_args.kwargs["max_tokens"] == 16_384


def test_scan_explicit_completion_limit_wins_over_runtime(
    store: KnowledgeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    monkeypatch.setattr(
        "openjarvis.core.config.load_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("runtime config must not be read")
        ),
    )
    engine = _fake_engine()

    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=8192,
    ).execute(question="Which VCs have I spoken with?")

    assert result.success is True
    assert engine.generate.call_args.kwargs["max_tokens"] == 8192


@pytest.mark.parametrize("finish_reason", ["length", "", None, "future_reason"])
def test_scan_rejects_unproven_or_incomplete_completion(
    store: KnowledgeStore,
    finish_reason: str | None,
) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine(
        content="A partial finding that must not escape",
        finish_reason=finish_reason,
    )
    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=4096,
    ).execute(question="Which VCs have I spoken with?")

    assert result.success is False
    assert result.content == "Chunk scan could not complete."
    assert "partial finding" not in result.content


def test_scan_rejects_missing_finish_reason(store: KnowledgeStore) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine(
        content="A partial finding that must not escape",
        finish_reason=_MISSING,
    )
    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=4096,
    ).execute(question="Which VCs have I spoken with?")

    assert result.success is False
    assert result.content == "Chunk scan could not complete."


def test_scan_rejects_empty_content_even_after_stop(store: KnowledgeStore) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine(content="  ", finish_reason="stop")
    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=4096,
    ).execute(question="Which VCs have I spoken with?")

    assert result.success is False
    assert result.content == "Chunk scan could not complete."


def test_scan_does_not_expose_findings_from_an_incomplete_multi_batch(
    store: KnowledgeStore,
) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    for index in range(17):
        store.store(
            f"Additional chunk {index}",
            source="granola",
            doc_type="document",
        )
    engine = MagicMock()
    engine.generate.side_effect = [
        {"content": "Complete first-batch finding", "finish_reason": "stop"},
        {"content": "Truncated second-batch finding", "finish_reason": "length"},
    ]

    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=4096,
    ).execute(question="Which VCs have I spoken with?", max_chunks=21)

    assert result.success is False
    assert result.content == "Chunk scan could not complete."
    assert "first-batch" not in result.content


def test_scan_accepts_explicit_no_finding_after_stop(store: KnowledgeStore) -> None:
    from openjarvis.tools.scan_chunks import ScanChunksTool

    engine = _fake_engine(content="NOTHING_RELEVANT", finish_reason="stop")
    result = ScanChunksTool(
        store=store,
        engine=engine,
        model="test",
        max_tokens=4096,
    ).execute(question="Which VCs have I spoken with?")

    assert result.success is True
    assert "no relevant information" in result.content
