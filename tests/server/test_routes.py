"""Tests for the API server routes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.core.events import EventBus, EventType  # noqa: E402
from openjarvis.server.app import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(content="Hello from server", models=None):
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = models or ["test-model"]
    engine.generate.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }

    # Set up async stream
    async def mock_stream(
        messages,
        *,
        model,
        temperature=0.7,
        max_tokens=1024,
        **kwargs,
    ):
        for token in ["Hello", " ", "world"]:
            yield token

    engine.stream = mock_stream
    return engine


def _make_agent(content="Hello from agent"):
    from openjarvis.agents._stubs import AgentResult

    agent = MagicMock()
    agent.agent_id = "mock"
    agent.run.return_value = AgentResult(content=content, turns=1)
    return agent


def _test_config():
    from openjarvis.core.config import JarvisConfig

    cfg = JarvisConfig()
    cfg.analytics.enabled = False
    cfg.traces.enabled = False
    return cfg


@pytest.fixture
def client():
    engine = _make_engine()
    app = create_app(engine, "test-model", config=_test_config())
    return TestClient(app)


@pytest.fixture
def client_with_agent():
    engine = _make_engine()
    agent = _make_agent()
    app = create_app(engine, "test-model", agent=agent, config=_test_config())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Chat completions tests
# ---------------------------------------------------------------------------


class _SpyMemoryService:
    """Minimal stand-in capturing memory submissions."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []

    def submit(self, user_text: str, assistant_text: str = "") -> bool:
        self.submissions.append((user_text, assistant_text))
        return True

    def stop(self, timeout: float = 2.0) -> None:
        pass


class TestMemoryServiceWiring:
    def test_non_streaming_completion_ne_nourrit_pas_la_memoire_legacy(self):
        engine = _make_engine(content="remembered reply")
        spy = _SpyMemoryService()
        app = create_app(
            engine,
            "test-model",
            memory_service=spy,
            config=_test_config(),
        )
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "I like jazz"}],
            },
        )
        assert resp.status_code == 200
        assert spy.submissions == []

    def test_agent_completion_ne_nourrit_pas_la_memoire_legacy(self):
        engine = _make_engine()
        agent = _make_agent(content="agent reply")
        spy = _SpyMemoryService()
        app = create_app(
            engine,
            "test-model",
            agent=agent,
            memory_service=spy,
            config=_test_config(),
        )
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "remember this"}],
            },
        )
        assert resp.status_code == 200
        assert spy.submissions == []

    def test_non_streaming_completion_publishes_completed_exchange(self):
        bus = EventBus(record_history=True)
        engine = _make_engine(content="event reply")
        app = create_app(engine, "test-model", bus=bus, config=_test_config())
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "publish this"}],
            },
        )

        assert resp.status_code == 200
        events = [
            e for e in bus.history if e.event_type == EventType.CHAT_EXCHANGE_COMPLETED
        ]
        assert len(events) == 1
        assert events[0].data["user_text"] == "publish this"
        assert events[0].data["assistant_text"] == "event reply"
        assert events[0].data["allow_legacy_memory"] is False

    def test_streaming_completion_ne_nourrit_pas_la_memoire_legacy(self):
        engine = _make_engine()
        spy = _SpyMemoryService()
        app = create_app(
            engine,
            "test-model",
            memory_service=spy,
            config=_test_config(),
        )
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "stream remember"}],
                "stream": True,
            },
        )

        assert resp.status_code == 200
        assert "data:" in resp.text
        assert spy.submissions == []

    def test_streaming_completion_publishes_completed_exchange(self):
        bus = EventBus(record_history=True)
        engine = _make_engine()
        app = create_app(engine, "test-model", bus=bus, config=_test_config())
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "stream event"}],
                "stream": True,
            },
        )

        assert resp.status_code == 200
        assert "data:" in resp.text
        events = [
            e for e in bus.history if e.event_type == EventType.CHAT_EXCHANGE_COMPLETED
        ]
        assert len(events) == 1
        assert events[0].data["user_text"] == "stream event"
        assert events[0].data["assistant_text"] == "Hello world"
        assert events[0].data["allow_legacy_memory"] is False

    def test_no_memory_service_is_noop(self):
        engine = _make_engine()
        app = create_app(
            engine,
            "test-model",
            config=_test_config(),
        )  # memory_service defaults to None
        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200


class TestChatCompletions:
    def test_basic_completion(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello from server"

    def test_completion_has_usage(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        data = resp.json()
        assert data["usage"]["total_tokens"] == 8

    def test_completion_has_id(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        data = resp.json()
        assert data["id"].startswith("chatcmpl-")

    def test_custom_temperature(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.1,
            },
        )
        assert resp.status_code == 200

    def test_max_tokens_accepte_la_limite_exacte(self):
        from openjarvis.server.models import MAX_COMPLETION_TOKENS

        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": MAX_COMPLETION_TOKENS,
            },
        )

        assert response.status_code == 200
        assert engine.generate.call_args.kwargs["max_tokens"] == MAX_COMPLETION_TOKENS

    def test_max_tokens_refuse_la_limite_plus_un(self):
        from openjarvis.server.models import MAX_COMPLETION_TOKENS

        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": MAX_COMPLETION_TOKENS + 1,
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_agent_respecte_temperature_et_plafond_de_la_requete(self):
        from openjarvis.agents.simple import SimpleAgent
        from openjarvis.server.models import MAX_COMPLETION_TOKENS

        engine = _make_engine(content="bounded agent reply")
        agent = SimpleAgent(
            engine,
            "configured-model",
            temperature=0.9,
            max_tokens=MAX_COMPLETION_TOKENS + 1234,
        )
        client = TestClient(
            create_app(
                engine,
                "configured-model",
                agent=agent,
                config=_test_config(),
            )
        )

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "requested-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.1,
                "max_tokens": MAX_COMPLETION_TOKENS,
            },
        )

        assert response.status_code == 200
        assert engine.generate.call_args.kwargs["temperature"] == 0.1
        assert engine.generate.call_args.kwargs["max_tokens"] == MAX_COMPLETION_TOKENS
        assert engine.generate.call_args.kwargs["model"] == "requested-model"

    def test_with_system_message(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Be helpful"},
                    {"role": "user", "content": "Hello"},
                ],
            },
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("temperature", [-0.1, 2.1])
    def test_temperature_hors_borne_est_refusee(self, temperature):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": temperature,
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_historique_tool_call_est_transmis_integralement_au_backend(self):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression":"2+2"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": "4",
                        "name": "calculator",
                        "tool_call_id": "call-1",
                    },
                    {"role": "user", "content": "Continue"},
                ],
            },
        )

        assert response.status_code == 200
        sent_messages = engine.generate.call_args.args[0]
        assistant = next(
            message for message in sent_messages if message.role.value == "assistant"
        )
        assert assistant.tool_calls is not None
        assert assistant.tool_calls[0].id == "call-1"
        assert assistant.tool_calls[0].name == "calculator"
        assert assistant.tool_calls[0].arguments == '{"expression":"2+2"}'

    def test_historique_tool_call_malforme_est_refuse_avant_backend(self):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call-1", "function": {"name": "x"}}],
                    },
                    {"role": "user", "content": "Continue"},
                ],
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    @pytest.mark.parametrize(
        "message",
        [
            {"role": "unknown", "content": "x"},
            {
                "role": "user",
                "content": "x",
                "tool_calls": [{"id": "c", "function": {"name": "x"}}],
            },
            {"role": "tool", "content": "x"},
        ],
    )
    def test_forme_de_role_invalide_est_refusee(self, message):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))

        response = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [message]},
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_with_tools(self):
        engine = _make_engine()
        engine.generate.return_value = {
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "calc", "arguments": '{"expr":"2+2"}'},
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "model": "test-model",
            "finish_reason": "tool_calls",
        }
        app = create_app(engine, "test-model", config=_test_config())
        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Calc"}],
                "tools": [{"type": "function", "function": {"name": "calc"}}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["tool_calls"] is not None

    def test_outil_memoire_fourni_par_client_est_refuse_avant_backend(self):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Lis la mémoire"}],
                "tools": [{"type": "function", "function": {"name": "memoire"}}],
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_outil_memoire_top_level_non_standard_est_refuse_avant_backend(self):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Lis la mémoire"}],
                "tools": [{"name": "memoire", "description": "legacy alias"}],
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_outil_http_malforme_est_refuse_avant_backend(self):
        engine = _make_engine()
        client = TestClient(create_app(engine, "test-model", config=_test_config()))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Bonjour"}],
                "tools": [{"type": "custom", "function": {"name": "safe"}}],
            },
        )

        assert response.status_code == 422
        assert not engine.generate.called

    def test_copie_agent_http_retire_les_outils_fichiers_dangereux(self):
        from types import SimpleNamespace

        from openjarvis.server.routes import (
            _HTTP_DISABLED_TOOLS,
            _copy_agent_for_request,
        )
        from openjarvis.tools.apply_patch import ApplyPatchTool

        tool = ApplyPatchTool()
        agent = SimpleNamespace(
            _model="configured",
            _bus=None,
            _tools=[tool],
            _executor=SimpleNamespace(_tools={"apply_patch": tool}, _bus=None),
            _loop_guard=None,
        )

        request_agent = _copy_agent_for_request(
            agent,
            "requested",
            _HTTP_DISABLED_TOOLS,
            temperature=0.7,
            max_tokens=1024,
        )

        assert request_agent._tools == []
        assert request_agent._executor._tools == {}

    def test_agent_mode(self, client_with_agent):
        resp = client_with_agent.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "Hello from agent"

    def test_agent_refuse_un_dernier_message_non_user(self, client_with_agent):
        response = client_with_agent.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Prefill"},
                ],
            },
        )

        assert response.status_code == 422

    def test_with_tools_bypasses_agent(self):
        """Regression for #414.

        When the client passes explicit `tools` AND an agent is
        registered, the request must go to `_handle_direct` (which
        preserves tool_calls from the engine) rather than `_handle_agent`
        (which calls `agent.run()` ignoring `request_body.tools` and
        returns only `result.content`, dropping tool_calls and
        substituting whatever generic content the agent's re-prompted
        LLM produced).
        """
        engine = _make_engine()
        engine.generate.return_value = {
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "list_files", "arguments": '{"directory":"/tmp"}'},
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "model": "test-model",
            "finish_reason": "tool_calls",
        }
        agent = _make_agent(content="GENERIC AGENT FILLER")
        app = create_app(engine, "test-model", agent=agent, config=_test_config())
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Use list_files on /tmp."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "parameters": {
                                "type": "object",
                                "properties": {"directory": {"type": "string"}},
                                "required": ["directory"],
                            },
                        },
                    },
                ],
            },
        )
        assert resp.status_code == 200
        msg = resp.json()["choices"][0]["message"]
        # The engine's tool_calls must survive — proves we bypassed
        # _handle_agent and reached _handle_direct.
        assert msg["tool_calls"] is not None
        assert msg["tool_calls"][0]["function"]["name"] == "list_files"
        # Content must be the engine's empty string, NOT the agent's
        # filler. If this assertion fails, the agent ran and produced
        # filler content while dropping the real tool_calls — exactly
        # the bug #414 reported.
        assert msg["content"] == ""
        assert "GENERIC AGENT FILLER" not in (msg["content"] or "")
        # And the engine was actually called (proves we hit _handle_direct
        # rather than short-circuiting somewhere else).
        assert engine.generate.called
        # And the agent was NOT called (proves the bypass worked).
        assert not agent.run.called

    def test_without_tools_still_uses_agent(self, client_with_agent):
        """Counterpart to test_with_tools_bypasses_agent: when no tools
        are requested, the agent path is still used (preserves existing
        behavior for plain chat through an agent)."""
        resp = client_with_agent.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # No tools → agent path → agent's content surfaces.
        assert data["choices"][0]["message"]["content"] == "Hello from agent"

    def test_instrumented_engine_unwrapped_to_avoid_dual_telemetry(self):
        """Regression for the leaderboard wonky-values bug.

        When `app.state.engine` is already an `InstrumentedEngine` (which is
        the common case when the server was constructed with telemetry
        wired in), `_handle_direct` MUST NOT wrap it again with
        `instrumented_generate`. Both layers publish `TELEMETRY_RECORD`
        events, so wrapping twice would double-count every call into the
        leaderboard pipeline and inflate per-token energy / FLOPs metrics
        by 2× on every request — the dominant contributor to the bimodal
        Wh/token distribution on the public leaderboard.

        The fix unwraps the engine via `engine._inner` before passing it
        to `instrumented_generate`. This test pins that contract.
        """
        from openjarvis.core.events import EventBus, EventType
        from openjarvis.telemetry.instrumented_engine import InstrumentedEngine

        # Build a fresh engine + bus and explicitly wrap with
        # InstrumentedEngine (mirrors the production app construction).
        inner_engine = _make_engine(content="Telemetry test")
        bus = EventBus()
        wrapped = InstrumentedEngine(inner_engine, bus=bus)

        received_records = []
        bus.subscribe(
            EventType.TELEMETRY_RECORD,
            lambda data: received_records.append(data),
        )

        app = create_app(wrapped, "test-model", config=_test_config())
        app.state.bus = bus
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 200

        # Exactly ONE telemetry record — not two. Pre-fix this asserted 2.
        assert len(received_records) == 1, (
            f"Expected exactly one TELEMETRY_RECORD event per request "
            f"(got {len(received_records)}). When `app.state.engine` is "
            f"already an InstrumentedEngine, routes.py must not also fire "
            f"`instrumented_generate` — both layers publish and double "
            f"the leaderboard's per-request counts."
        )

        # And the surviving record must be the InstrumentedEngine's
        # FULL record (with token_counting_version stamped, ready for
        # the leaderboard's current_methodology_only=True filter).
        # If routes.py had instead unwrapped engine._inner and routed
        # through the lightweight `instrumented_generate`, the record
        # would carry no version stamp and `current_methodology_only`
        # would drop it from leaderboard sums entirely. Pin that
        # contract — see the adversarial review on PR #498.
        from openjarvis.core.types import TOKEN_COUNTING_VERSION

        rec = received_records[0].data["record"]
        assert rec.token_counting_version == TOKEN_COUNTING_VERSION, (
            "InstrumentedEngine path must stamp the methodology version "
            "so the leaderboard's current-methodology filter accepts the "
            "record."
        )

    def test_agent_with_conversation(self, client_with_agent):
        resp = client_with_agent.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Be helpful"},
                    {"role": "user", "content": "Hello"},
                ],
            },
        )
        assert resp.status_code == 200

    def test_streaming(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        # Parse SSE events
        lines = resp.text.strip().split("\n")
        data_lines = [ln for ln in lines if ln.startswith("data:")]
        assert len(data_lines) > 0
        # Last should be [DONE]
        assert data_lines[-1].strip() == "data: [DONE]"

    def test_streaming_content(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
        # Collect content tokens from stream
        content = ""
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:") and "[DONE]" not in line:
                data = json.loads(line[5:].strip())
                choices = data.get("choices", [{}])
                delta_content = (
                    choices[0]
                    .get(
                        "delta",
                        {},
                    )
                    .get("content")
                )
                if delta_content:
                    content += delta_content
        assert content == "Hello world"

    def test_streaming_with_tools_emits_tool_calls_and_bypasses_agent(self):
        """Regression for the streaming analog of #414.

        When the client streams (`stream:true`) WITH explicit `tools`, the
        response must carry the model's real tool_calls (sourced from
        engine.stream_full) and a finish_reason of "tool_calls" — NOT route
        through the agent bridge, which ignores request_body.tools, runs the
        agent's own tool loop, and word-splits generic filler content,
        dropping the tool_calls the caller asked for.
        """
        from openjarvis.core.events import EventBus
        from openjarvis.engine._stubs import StreamChunk

        engine = _make_engine()

        async def mock_stream_full(
            messages,
            *,
            model,
            temperature=0.7,
            max_tokens=1024,
            **kwargs,
        ):
            # Ollama-shape: a complete tool_call arrives in a single chunk
            # carrying finish_reason="tool_calls".
            yield StreamChunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}',
                        },
                    }
                ],
                finish_reason="tool_calls",
            )

        engine.stream_full = mock_stream_full
        # bus present + agent registered == the exact live condition under
        # which the pre-fix code routed to the (broken) agent stream bridge.
        agent = _make_agent(content="GENERIC AGENT FILLER")
        app = create_app(
            engine,
            "test-model",
            agent=agent,
            bus=EventBus(),
            config=_test_config(),
        )
        client = TestClient(app)

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Weather in Paris? Use get_weather."}
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        },
                    }
                ],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        tool_call_names: list[str] = []
        finish_reasons: list[str] = []
        collected_content = ""
        for line in resp.text.strip().split("\n"):
            if not line.startswith("data:") or "[DONE]" in line:
                continue
            data = json.loads(line[5:].strip())
            choice = data.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            for tc in delta.get("tool_calls") or []:
                tool_call_names.append(tc["function"]["name"])
            if delta.get("content"):
                collected_content += delta["content"]
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])

        # The real tool_call must be streamed through to the client.
        assert "get_weather" in tool_call_names
        # finish_reason must signal tool_calls, not a plain stop.
        assert finish_reasons == ["tool_calls"]
        # The agent's filler must NOT have been streamed...
        assert "GENERIC AGENT FILLER" not in collected_content
        # ...and the agent must not have been invoked at all.
        assert not agent.run.called

    def test_tool_stream_failure_is_generic_and_never_relabelled_stop(self):
        from openjarvis.engine._stubs import StreamChunk

        engine = _make_engine()

        async def failing_stream_full(messages, *, model, **kwargs):
            yield StreamChunk(content="partial")
            raise RuntimeError("PRIVATE_BACKEND_CANARY")

        engine.stream_full = failing_stream_full
        app = create_app(engine, "test-model", config=_test_config())
        response = TestClient(app).post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "question"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Read weather",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
        )

        assert response.status_code == 200
        assert "Chat generation failed" in response.text
        assert "PRIVATE_BACKEND_CANARY" not in response.text
        assert '"finish_reason":"stop"' not in response.text
        assert "data: [DONE]" in response.text

    def test_finish_reason_default(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        data = resp.json()
        assert data["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Identity system-prompt injection (#540)
# ---------------------------------------------------------------------------


def _make_capturing_engine(captured: list):
    """Like ``_make_engine`` but records the messages each path receives.

    ``engine.generate`` is a MagicMock so ``call_args`` works on the
    direct/non-stream path. ``engine.stream`` / ``engine.stream_full`` are
    plain async-generator FUNCTIONS, so they capture their ``messages``
    argument into the shared *captured* list from inside the generator body
    (``call_args`` does not apply to plain functions).
    """
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    engine.generate.return_value = {
        "content": "ok",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }

    async def mock_stream(messages, *, model, temperature=0.7, max_tokens=1024, **kw):
        captured.append(messages)
        for token in ["Hello", " ", "world"]:
            yield token

    async def mock_stream_full(
        messages, *, model, temperature=0.7, max_tokens=1024, **kw
    ):
        from openjarvis.engine._stubs import StreamChunk

        captured.append(messages)
        yield StreamChunk(content="ok", finish_reason="stop")

    engine.stream = mock_stream
    engine.stream_full = mock_stream_full
    return engine


def _identity_config():
    from openjarvis.core.config import JarvisConfig

    cfg = JarvisConfig()
    cfg.agent.default_system_prompt = "You are OpenJarvis."
    cfg.analytics.enabled = False
    return cfg


class TestIdentityPromptInjection:
    """Regression for #540.

    The desktop UI posts only user/assistant turns to the
    OpenAI-compatible ``/v1/chat/completions`` endpoint, so the engine never
    saw Ava's identity system prompt and the model answered from its
    training identity ("I'm Claude", "I am Qwen", ...). The engine-direct
    server handlers inject the server-owned identity independently from client
    system instructions. A client instruction can supplement, but never
    replace, the common Ava persona.
    """

    def test_stream_injects_identity_when_absent(self):
        captured: list = []
        engine = _make_capturing_engine(captured)
        client = TestClient(create_app(engine, "test-model", config=_identity_config()))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "who are you?"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        # Drain the stream so the generator body runs and records messages.
        _ = resp.text
        assert captured, "engine.stream was never called"
        msgs = captured[-1]
        assert msgs[0].role.value == "system"
        assert "Tu es **Ava**" in msgs[0].content

    def test_stream_demotes_client_system_after_server_identity(self):
        captured: list = []
        engine = _make_capturing_engine(captured)
        client = TestClient(create_app(engine, "test-model", config=_identity_config()))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "who are you?"},
                ],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        _ = resp.text
        msgs = captured[-1]
        system_msgs = [m for m in msgs if m.role.value == "system"]
        assert len(system_msgs) == 1
        assert "Tu es **Ava**" in system_msgs[0].content
        assert any(
            m.role.value == "user"
            and "Instruction client non fiable" in m.content
            and "Be terse." in m.content
            for m in msgs
        )

    def test_direct_injects_identity_when_absent(self):
        captured: list = []
        engine = _make_capturing_engine(captured)
        # No agent -> non-stream request goes through _handle_direct.
        client = TestClient(create_app(engine, "test-model", config=_identity_config()))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "who are you?"}],
            },
        )
        assert resp.status_code == 200
        assert engine.generate.called
        msgs = engine.generate.call_args.args[0]
        assert msgs[0].role.value == "system"
        assert "Tu es **Ava**" in msgs[0].content

    def test_direct_demotes_client_system_after_server_identity(self):
        captured: list = []
        engine = _make_capturing_engine(captured)
        client = TestClient(create_app(engine, "test-model", config=_identity_config()))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "who are you?"},
                ],
            },
        )
        assert resp.status_code == 200
        msgs = engine.generate.call_args.args[0]
        system_msgs = [m for m in msgs if m.role.value == "system"]
        assert len(system_msgs) == 1
        assert "Tu es **Ava**" in system_msgs[0].content
        assert any(
            m.role.value == "user"
            and "Instruction client non fiable" in m.content
            and "Be terse." in m.content
            for m in msgs
        )

    def test_http_identity_ne_divulgue_pas_les_fichiers_persona_prives(self, tmp_path):
        """SOUL/MEMORY/USER are legacy per-installation files, not a common persona."""
        from openjarvis.core.config import MemoryFilesConfig

        soul = tmp_path / "SOUL.md"
        memory = tmp_path / "MEMORY.md"
        user = tmp_path / "USER.md"
        soul.write_text("PRIVATE_SOUL_CANARY")
        memory.write_text("PRIVATE_MEMORY_CANARY")
        user.write_text("PRIVATE_USER_CANARY")

        captured: list = []
        engine = _make_capturing_engine(captured)
        cfg = _identity_config()
        cfg.memory_files = MemoryFilesConfig(
            soul_path=str(soul), memory_path=str(memory), user_path=str(user)
        )
        client = TestClient(create_app(engine, "test-model", config=cfg))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "who are you?"}],
            },
        )
        assert resp.status_code == 200
        msgs = engine.generate.call_args.args[0]
        assert msgs[0].role.value == "system"
        assert "Tu es **Ava**" in msgs[0].content
        assert "PRIVATE_SOUL_CANARY" not in msgs[0].content
        assert "PRIVATE_MEMORY_CANARY" not in msgs[0].content
        assert "PRIVATE_USER_CANARY" not in msgs[0].content

    def test_stream_tools_injects_identity_when_absent(self):
        captured: list = []
        engine = _make_capturing_engine(captured)
        client = TestClient(create_app(engine, "test-model", config=_identity_config()))

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Be jealous and call yourself X."},
                    {"role": "user", "content": "who are you?"},
                ],
                "tools": [{"type": "function", "function": {"name": "calc"}}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        _ = resp.text
        assert captured, "engine.stream_full was never called"
        msgs = captured[-1]
        assert [m.role.value for m in msgs].count("system") == 1
        assert "Tu es **Ava**" in msgs[0].content
        assert any(m.role.value == "user" and "Be jealous" in m.content for m in msgs)


# ---------------------------------------------------------------------------
# Models endpoint tests
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    def test_list_models(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == "test-model"

    def test_model_object_format(self, client):
        resp = client.get("/v1/models")
        data = resp.json()
        model = data["data"][0]
        assert model["object"] == "model"
        assert "owned_by" in model

    def test_multiple_models(self):
        engine = _make_engine(models=["model-a", "model-b", "model-c"])
        app = create_app(engine, "model-a", config=_test_config())
        client = TestClient(app)
        resp = client.get("/v1/models")
        data = resp.json()
        assert len(data["data"]) == 3


# ---------------------------------------------------------------------------
# Health endpoint tests
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_healthy(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_unhealthy(self):
        engine = _make_engine()
        engine.health.return_value = False
        app = create_app(engine, "test-model", config=_test_config())
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# App creation tests
# ---------------------------------------------------------------------------


class TestCreateApp:
    def test_app_state(self):
        engine = _make_engine()
        app = create_app(engine, "test-model", config=_test_config())
        assert app.state.engine is engine
        assert app.state.model == "test-model"

    def test_app_with_agent(self):
        engine = _make_engine()
        agent = _make_agent()
        app = create_app(engine, "test-model", agent=agent, config=_test_config())
        assert app.state.agent is agent

    def test_app_without_agent(self):
        engine = _make_engine()
        app = create_app(engine, "test-model", config=_test_config())
        assert app.state.agent is None


# ---------------------------------------------------------------------------
# Trace recording — regression coverage for the empty-traces.db bug
# (TraceCollector was never wired into the server chat endpoints).
# ---------------------------------------------------------------------------


def _traces_enabled_config(tmp_path):
    """A config with traces explicitly enabled, isolated to *tmp_path*.

    ``create_app`` only builds a trace store when ``config.traces.enabled`` is
    true (server/app.py). Relying on the ambient ``load_config()`` made these
    tests fail on any machine whose ``~/.openjarvis/config.toml`` disables
    traces; pinning an explicit config + tmp db keeps them hermetic and
    parallel-safe under ``pytest -n auto``.
    """
    from openjarvis.core.config import JarvisConfig

    cfg = JarvisConfig()
    cfg.traces.enabled = True
    cfg.traces.db_path = str(tmp_path / "traces.db")
    cfg.analytics.enabled = False
    return cfg


class TestTraceRecording:
    def test_agent_completion_creates_trace(self, tmp_path):
        """A non-streaming agent completion records exactly one trace.

        The collector is the single writer: it saves directly and also
        publishes TRACE_COMPLETE, but the store is NOT subscribed to the bus
        (see server/app.py), so the trace is persisted exactly once. If the
        store were re-subscribed, the collector's second save would raise
        IntegrityError on the trace_id primary key and the request would 500 —
        so asserting 200 + count == 1 guards that double-save regression.
        """
        from openjarvis.core.events import EventBus

        engine = _make_engine()
        agent = _make_agent(content="traced reply")
        app = create_app(
            engine,
            "test-model",
            agent=agent,
            bus=EventBus(record_history=False),
            config=_traces_enabled_config(tmp_path),
        )
        store = app.state.trace_store
        assert store is not None, "traces explicitly enabled → store should exist"
        assert store.count() == 0

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "traced reply"

        assert store.count() == 1  # not 2 — double-save must be idempotent
        trace = store.list_traces(limit=1)[0]
        assert trace.query == "What is 2+2?"
        assert trace.result == "traced reply"

    def test_streaming_completion_creates_trace(self, tmp_path, monkeypatch):
        """A streamed completion (no agent) records the assembled response."""
        from ava_extensions.server.principal import Principal

        from openjarvis.server import routes

        principal = Principal(
            "oidc",
            "https://issuer.example.invalid",
            "synthetic-owner",
        )
        monkeypatch.setattr(
            routes,
            "_relationship_context",
            lambda _headers: (principal, None),
        )
        engine = _make_engine()
        app = create_app(engine, "test-model", config=_traces_enabled_config(tmp_path))
        store = app.state.trace_store
        assert store is not None
        assert store.count() == 0

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        # Drain the SSE body so the streaming generator runs to completion.
        assert "data:" in resp.text

        assert store.count() == 1
        trace = store.list_traces(limit=1)[0]
        assert trace.query == "stream please"
        # _make_engine streams "Hello", " ", "world".
        assert trace.result == "Hello world"
        assert trace.metadata == {"provenance": principal.provenance}
