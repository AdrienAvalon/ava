"""Tests for web Deep Research planner engine selection."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from openjarvis.agents.research_loop import DEFAULT_PLANNER_MODEL
from openjarvis.core.config import JarvisConfig
from openjarvis.server import research_router


class _DummyEngine:
    def __init__(self, servable: bool = True) -> None:
        self.servable = servable

    def can_serve(self, model: str) -> bool:
        return self.servable


def test_resolve_planner_config_uses_chat_defaults() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "local-model"

    assert research_router._resolve_planner_config(cfg) == (
        "lmstudio",
        "local-model",
    )


def test_resolve_planner_config_prefers_active_chat_runtime() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "ollama"
    cfg.intelligence.default_model = ""

    assert research_router._resolve_planner_config(
        cfg,
        active_engine_key="lmstudio",
        active_model="server-model",
        request_model="selected-model",
    ) == (
        "lmstudio",
        "selected-model",
    )


def test_resolve_planner_config_uses_server_model_before_legacy_default() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "ollama"
    cfg.intelligence.default_model = ""
    cfg.server.model = "serve-model"

    assert research_router._resolve_planner_config(cfg) == (
        "ollama",
        "serve-model",
    )


def test_resolve_planner_config_allows_deep_research_override() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "chat-model"
    cfg.deep_research.engine = "vllm"
    cfg.deep_research.model = "planner-model"

    assert research_router._resolve_planner_config(cfg) == (
        "vllm",
        "planner-model",
    )


def test_resolve_planner_config_allows_partial_model_override() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "chat-model"
    cfg.deep_research.model = "planner-model"

    assert research_router._resolve_planner_config(cfg) == (
        "lmstudio",
        "planner-model",
    )


def test_resolve_planner_config_allows_partial_engine_override() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "chat-model"
    cfg.deep_research.engine = "vllm"

    assert research_router._resolve_planner_config(cfg) == (
        "vllm",
        "chat-model",
    )


def test_resolve_planner_config_keeps_legacy_fallback_when_unconfigured() -> None:
    cfg = JarvisConfig()
    cfg.engine.default = ""
    cfg.intelligence.default_model = ""

    assert research_router._resolve_planner_config(cfg) == (
        "ollama",
        DEFAULT_PLANNER_MODEL,
    )


def test_research_completion_limit_uses_server_runtime_config() -> None:
    cfg = JarvisConfig()
    cfg.intelligence.max_tokens = 16_384

    assert research_router._resolve_research_completion_limit(cfg) == 16_384


@pytest.mark.parametrize("value", [True, 0, -1, 32_769])
def test_research_completion_limit_rejects_invalid_runtime_config(
    value: object,
) -> None:
    cfg = JarvisConfig()
    cfg.intelligence.max_tokens = value  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="intelligence.max_tokens"):
        research_router._resolve_research_completion_limit(cfg)


def test_build_planner_engine_uses_configured_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "local-model"
    engine = _DummyEngine()
    calls: list[tuple[str | None, str | None]] = []

    def fake_get_engine(
        config: JarvisConfig,
        engine_key: str | None = None,
        model: str | None = None,
    ) -> tuple[str, _DummyEngine]:
        calls.append((engine_key, model))
        return "lmstudio", engine

    monkeypatch.setattr(research_router, "get_engine", fake_get_engine)

    engine_key, resolved_engine, model = research_router._build_planner_engine(cfg)

    assert calls == [("lmstudio", "local-model")]
    assert engine_key == "lmstudio"
    assert resolved_engine is engine
    assert model == "local-model"


def test_build_planner_engine_uses_active_engine_without_config_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "ollama"
    cfg.intelligence.default_model = ""
    active_engine = _DummyEngine()

    def fail_get_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("should use the live app engine")

    monkeypatch.setattr(research_router, "get_engine", fail_get_engine)

    engine_key, resolved_engine, model = research_router._build_planner_engine(
        cfg,
        active_engine=active_engine,
        active_engine_key="lmstudio",
        active_model="server-model",
        request_model="selected-model",
    )

    assert engine_key == "lmstudio"
    assert resolved_engine is active_engine
    assert model == "selected-model"


def test_build_planner_engine_rejects_active_engine_that_cannot_serve_model() -> None:
    cfg = JarvisConfig()

    with pytest.raises(RuntimeError, match="selected-model"):
        research_router._build_planner_engine(
            cfg,
            active_engine=_DummyEngine(servable=False),
            active_engine_key="cloud",
            request_model="selected-model",
        )


def test_build_planner_engine_honors_explicit_deep_research_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = JarvisConfig()
    cfg.deep_research.engine = "vllm"
    cfg.deep_research.model = "planner-model"
    active_engine = _DummyEngine()
    planner_engine = _DummyEngine()

    def fake_get_engine(
        config: JarvisConfig,
        engine_key: str | None = None,
        model: str | None = None,
    ) -> tuple[str, _DummyEngine]:
        assert engine_key == "vllm"
        assert model == "planner-model"
        return "vllm", planner_engine

    monkeypatch.setattr(research_router, "get_engine", fake_get_engine)

    engine_key, resolved_engine, model = research_router._build_planner_engine(
        cfg,
        active_engine=active_engine,
        active_engine_key="lmstudio",
        active_model="chat-model",
        request_model="selected-model",
    )

    assert engine_key == "vllm"
    assert resolved_engine is planner_engine
    assert model == "planner-model"


def test_research_route_passes_live_engine_and_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    active_engine = _DummyEngine()
    runtime_config = JarvisConfig()

    def fake_stream(query: str, **kwargs: object):
        captured["query"] = query
        captured.update(kwargs)

        async def gen():
            yield 'data: {"type":"done","usage":{}}\n\n'

        return gen()

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                engine=active_engine,
                engine_name="lmstudio",
                model="server-model",
                config=runtime_config,
            )
        )
    )

    monkeypatch.setattr(research_router, "_stream_research", fake_stream)

    response = asyncio.run(
        research_router.research(
            research_router.ResearchRequest(
                query="find notes",
                model="selected-model",
            ),
            request,  # type: ignore[arg-type]
        )
    )

    assert response.media_type == "text/event-stream"
    assert captured == {
        "query": "find notes",
        "active_engine": active_engine,
        "active_engine_key": "lmstudio",
        "active_model": "server-model",
        "request_model": "selected-model",
        "runtime_config": runtime_config,
    }


def _patch_stream_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    agent_type: type,
) -> None:
    class _Sampler:
        available = False

        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> dict[str, float]:
            return {
                "energy_j": 0.0,
                "mean_power_w": 0.0,
                "peak_power_w": 0.0,
                "duration_s": 0.0,
            }

    monkeypatch.setattr(
        research_router,
        "_build_planner_engine",
        lambda *args, **kwargs: ("cloud", _DummyEngine(), "server-model"),
    )
    monkeypatch.setattr(research_router, "KnowledgeStore", lambda: object())
    monkeypatch.setattr(
        research_router,
        "OllamaEmbedder",
        lambda: SimpleNamespace(is_available=lambda: True),
    )
    monkeypatch.setattr(research_router, "HybridSearch", lambda *args: object())
    monkeypatch.setattr(research_router, "ResearchAgent", agent_type)
    monkeypatch.setattr(research_router, "_LiveGPUSampler", _Sampler)
    monkeypatch.setattr(
        research_router,
        "_record_research_telemetry",
        lambda **kwargs: None,
    )


async def _collect_research_events(**kwargs: object) -> list[dict[str, object]]:
    frames = [
        frame
        async for frame in research_router._stream_research("find notes", **kwargs)
    ]
    return [json.loads(frame.removeprefix("data: ")) for frame in frames]


def test_research_stream_passes_runtime_limit_and_marks_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _SuccessfulAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self._on_event = kwargs["on_event"]

        def run(self, query: str) -> SimpleNamespace:
            self._on_event(
                {"type": "final_answer", "text": "Complete answer.", "sources": []}
            )
            return SimpleNamespace(
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
            )

    _patch_stream_dependencies(monkeypatch, _SuccessfulAgent)
    cfg = JarvisConfig()
    cfg.intelligence.max_tokens = 16_384

    events = asyncio.run(_collect_research_events(runtime_config=cfg))

    assert captured["max_tokens"] == 16_384
    assert "synthesis" in [event["type"] for event in events]
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "success"


def test_research_stream_rejects_incomplete_answer_without_leaking_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _IncompleteAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, query: str) -> None:
            raise research_router.IncompleteResearchResponse(
                "max_tokens at /private/provider/path"
            )

    _patch_stream_dependencies(monkeypatch, _IncompleteAgent)
    cfg = JarvisConfig()
    cfg.intelligence.max_tokens = 16_384

    events = asyncio.run(_collect_research_events(runtime_config=cfg))

    assert captured["max_tokens"] == 16_384
    assert [event["type"] for event in events] == ["error", "done"]
    assert events[0]["message"] == research_router._INCOMPLETE_RESPONSE_MESSAGE
    assert "/private/provider/path" not in json.dumps(events)
    assert events[-1]["status"] == "error"


def test_research_stream_does_not_mark_empty_final_answer_successful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EmptyAgent:
        def __init__(self, **kwargs: object) -> None:
            self._on_event = kwargs["on_event"]

        def run(self, query: str) -> SimpleNamespace:
            self._on_event({"type": "final_answer", "text": "  ", "sources": []})
            return SimpleNamespace(usage={})

    _patch_stream_dependencies(monkeypatch, _EmptyAgent)

    events = asyncio.run(_collect_research_events(runtime_config=JarvisConfig()))

    assert [event["type"] for event in events] == ["error", "done"]
    assert events[-1]["status"] == "error"


def test_build_planner_engine_rejects_fallback_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "local-model"

    def fake_get_engine(
        config: JarvisConfig,
        engine_key: str | None = None,
        model: str | None = None,
    ) -> tuple[str, _DummyEngine]:
        return "ollama", _DummyEngine()

    monkeypatch.setattr(research_router, "get_engine", fake_get_engine)

    with pytest.raises(RuntimeError, match="lmstudio"):
        research_router._build_planner_engine(cfg)


def test_build_planner_engine_rejects_unavailable_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = JarvisConfig()
    cfg.engine.default = "lmstudio"
    cfg.intelligence.default_model = "local-model"

    monkeypatch.setattr(research_router, "get_engine", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="local-model"):
        research_router._build_planner_engine(cfg)
