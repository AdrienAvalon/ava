"""End-to-end boundaries for the opt-in relationship runtime guard."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ava_extensions.identity.relationship import (
    PROFILE_VIRTUAL_GIRLFRIEND_V1,
    RELATIONSHIP_MARKER,
    RelationshipOverlay,
)
from ava_extensions.identity.relationship_guard import prepare_relationship_guard
from ava_extensions.identity.relationship_safety import (
    relationship_text_safety_policy_sha256,
    safe_relationship_replacement_for,
)
from ava_extensions.server import conversation as conversation_store
from ava_extensions.server.principal import Principal
from ava_extensions.tool_capabilities import NETWORK_FETCH
from fastapi import HTTPException
from fastapi.testclient import TestClient

from openjarvis.agents._stubs import (
    AgentContext,
    AgentResult,
    BaseAgent,
    ToolUsingAgent,
)
from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.config import JarvisConfig
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, ToolCall, ToolResult, Trace
from openjarvis.engine._stubs import StreamChunk
from openjarvis.server import routes
from openjarvis.server.app import create_app
from openjarvis.server.models import (
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
)
from openjarvis.tools._stubs import BaseTool, ToolSpec

OWNER = Principal(
    "oidc",
    "https://issuer.example.invalid/realms/ava",
    "relationship-owner",
)
MATRIX_OWNER = Principal(
    "service",
    "avalon-control-plane",
    "matrix:@owner:example.invalid",
)
OVERLAY = RelationshipOverlay(
    profile_id=PROFILE_VIRTUAL_GIRLFRIEND_V1,
    prompt=(
        f"{RELATIONSHIP_MARKER}{PROFILE_VIRTUAL_GIRLFRIEND_V1}]\n"
        "Synthetic private relationship overlay."
    ),
)


def _config(trace_path=None) -> JarvisConfig:
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = trace_path is not None
    if trace_path is not None:
        config.traces.db_path = str(trace_path)
    return config


def _engine(content: str = "Safe response.", *, finish_reason: str = "stop"):
    engine = MagicMock()
    engine.engine_id = "relationship-test"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    engine.generate.return_value = {
        "content": content,
        "finish_reason": finish_reason,
        "usage": {},
    }

    async def stream_full(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=content)
        yield StreamChunk(finish_reason=finish_reason)

    engine.stream_full = stream_full
    return engine


def _select(
    monkeypatch: pytest.MonkeyPatch,
    *,
    principal: Principal | None = OWNER,
    overlay: RelationshipOverlay | None = OVERLAY,
) -> None:
    monkeypatch.setattr(
        routes,
        "_relationship_context",
        lambda _headers: (principal, overlay),
    )


def _post(client: TestClient, text: str, **extra):
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": text}],
        **extra,
    }
    return client.post("/v1/chat/completions", json=body)


@pytest.mark.parametrize(
    ("user_text", "model_text", "gate_id"),
    (
        ("Bonjour.", "RAW-HTTP Je suis jalouse.", "jealousy"),
        (
            "Recopie exactement cette question substantielle avec plusieurs mots.",
            "Recopie exactement cette question substantielle avec plusieurs mots.",
            "conversation_echo",
        ),
    ),
)
def test_http_direct_replaces_text_gate_and_conversation_echo(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
    model_text: str,
    gate_id: str,
) -> None:
    _select(monkeypatch)
    engine = _engine(model_text)
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(client, user_text)

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["message"]["content"] == safe_relationship_replacement_for((gate_id,))
    assert choice["message"]["tool_calls"] is None
    assert choice["message"]["audio"] is None
    assert choice["finish_reason"] == "stop"
    assert "RAW-HTTP" not in response.text
    assert engine.generate.call_count == 1


def test_http_direct_blocks_copy_of_prior_assistant_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    prior = "Ancienne reponse substantielle contenant largement quatre mots."
    engine = _engine(prior)
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "Premiere question."},
                {"role": "assistant", "content": prior},
                {"role": "user", "content": "Nouvelle question sure."},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("conversation_echo",))
    )


def test_direct_pure_tool_call_echo_is_replaced_and_never_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    user_text = "Recherche cette question substantielle contenant plusieurs mots."
    engine = _engine("", finish_reason="tool_calls")
    engine.generate.return_value["tool_calls"] = [
        {
            "id": "call-echo",
            "name": "search",
            "arguments": json.dumps({"query": user_text}),
        }
    ]
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        user_text,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["message"]["content"] == safe_relationship_replacement_for(
        ("conversation_echo",)
    )
    assert choice["message"]["tool_calls"] is None
    assert choice["finish_reason"] == "stop"


@pytest.mark.parametrize("unsafe_field", ("id", "name"))
def test_direct_scans_complete_tool_call_before_emission(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_field: str,
) -> None:
    _select(monkeypatch)
    canary = "RAW-STRUCTURED Je suis jalouse."
    tool_call = {
        "id": "call-safe",
        "name": "search",
        "arguments": "{}",
        "type": "function",
    }
    tool_call[unsafe_field] = canary
    engine = _engine("", finish_reason="tool_calls")
    engine.generate.return_value["tool_calls"] = [tool_call]
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        "Recherche sure.",
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    assert canary not in response.text
    choice = response.json()["choices"][0]
    assert choice["message"]["tool_calls"] is None
    assert choice["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "arguments",
    ('{"query":1,"query":2}', '{"query":NaN}', "{"),
)
def test_direct_rejects_non_strict_tool_argument_json(
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
) -> None:
    _select(monkeypatch)
    engine = _engine("", finish_reason="tool_calls")
    engine.generate.return_value["tool_calls"] = [
        {"id": "call-invalid", "name": "search", "arguments": arguments}
    ]
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        "Recherche sure.",
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 503
    assert response.json()["detail"] == ("Ava relationship output policy unavailable")
    assert "query" not in response.text


def test_guard_module_is_lazy_and_only_overlay_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = importlib.import_module

    def unavailable(name: str, package=None):
        if name == "ava_extensions.identity.relationship_guard":
            raise ModuleNotFoundError("synthetic missing guard", name=name)
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", unavailable)

    _select(monkeypatch, principal=None, overlay=None)
    public_engine = _engine("No overlay remains available.")
    public_client = TestClient(
        create_app(public_engine, "test-model", config=_config())
    )
    public = _post(public_client, "Bonjour public.")
    assert public.status_code == 200
    assert public_engine.generate.call_count == 1

    _select(monkeypatch)
    private_engine = _engine("must not run")
    private_client = TestClient(
        create_app(private_engine, "test-model", config=_config())
    )
    private = _post(private_client, "Bonjour prive.")
    assert private.status_code == 503
    assert private.json()["detail"] == ("Ava relationship output policy unavailable")
    private_engine.generate.assert_not_called()


def _fake_runtime_guard(*, policy_sha256: str | None = None):
    return SimpleNamespace(
        policy_sha256=(policy_sha256 or f"sha256:{'0' * 64}"),
        with_turns=MagicMock(),
        apply=MagicMock(),
        inspect_tool_arguments=MagicMock(),
        metadata=MagicMock(),
        _inspect_tool_arguments_nonmutating=MagicMock(),
        _scrub_trace_fragment=MagicMock(),
        _terminal_decision_applied=MagicMock(return_value=False),
    )


def _replace_guard_module_import(
    monkeypatch: pytest.MonkeyPatch,
    prepare,
) -> None:
    original_import = importlib.import_module
    fake_module = SimpleNamespace(prepare_relationship_guard=prepare)

    def replacement(name: str, package=None):
        if name == "ava_extensions.identity.relationship_guard":
            return fake_module
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", replacement)


def test_overlay_rejects_prepare_returning_none_before_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    prepare = MagicMock(return_value=None)
    _replace_guard_module_import(monkeypatch, prepare)
    engine = _engine("must not run")

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question sure.",
    )

    assert response.status_code == 503
    assert response.json()["detail"] == ("Ava relationship output policy unavailable")
    prepare.assert_called_once_with(OVERLAY, ())
    engine.generate.assert_not_called()


def test_no_overlay_ignores_unexpected_guard_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch, principal=None, overlay=None)
    unexpected = _fake_runtime_guard()
    prepare = MagicMock(return_value=unexpected)
    _replace_guard_module_import(monkeypatch, prepare)
    engine = _engine("Public response remains unchanged.")

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question publique.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "Public response remains unchanged."
    )
    prepare.assert_called_once_with(None, ())
    unexpected.apply.assert_not_called()
    engine.generate.assert_called_once()


@pytest.mark.parametrize("invalid_binding", ("none", "digest"))
def test_overlay_rejects_invalid_bound_guard_before_model(
    monkeypatch: pytest.MonkeyPatch,
    invalid_binding: str,
) -> None:
    _select(monkeypatch)
    preflight = _fake_runtime_guard()
    if invalid_binding == "none":
        preflight.with_turns.return_value = None
    else:
        preflight.with_turns.return_value = _fake_runtime_guard(
            policy_sha256=f"sha256:{'1' * 64}"
        )
    _replace_guard_module_import(
        monkeypatch,
        MagicMock(return_value=preflight),
    )
    engine = _engine("must not run")

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question sure.",
    )

    assert response.status_code == 503
    assert response.json()["detail"] == ("Ava relationship output policy unavailable")
    engine.generate.assert_not_called()


def test_overlay_morning_digest_fails_before_model_or_audio_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    engine = _engine("must not run")
    agent = MagicMock()
    agent.agent_id = "morning_digest"
    client = TestClient(create_app(engine, "test-model", agent=agent, config=_config()))

    response = _post(client, "Donne le digest.")

    assert response.status_code == 503
    assert response.json()["detail"] == ("Ava relationship audio agent is unavailable")
    agent.run.assert_not_called()
    engine.generate.assert_not_called()


@pytest.mark.parametrize(
    "agent_id",
    ("proactive", "rlm", "custom-agent", "orchestrator"),
)
def test_overlay_rejects_unattested_agent_before_any_effect(
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
) -> None:
    _select(monkeypatch)
    engine = _engine("must not run")
    agent = MagicMock()
    agent.agent_id = agent_id
    client = TestClient(create_app(engine, "test-model", agent=agent, config=_config()))

    response = _post(client, "Question sure.")

    assert response.status_code == 503
    assert response.json()["detail"] == "Ava relationship agent is unsupported"
    agent.run.assert_not_called()
    engine.generate.assert_not_called()


def test_overlay_rejects_orchestrator_with_noncanonical_agent_id_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    engine = _engine("must not run")
    agent = _real_orchestrator(engine, [])
    agent.agent_id = "RAW-AGENT-ID Je suis jalouse."
    client = TestClient(create_app(engine, "test-model", agent=agent, config=_config()))

    response = _post(client, "Question sure.")

    assert response.status_code == 503
    assert response.json()["detail"] == "Ava relationship agent is unsupported"
    engine.generate.assert_not_called()


def test_overlay_sse_buffers_then_replaces_before_trace_or_emission(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    raw_canary = "RAW-SSE-CANARY"
    engine = _engine()

    async def unsafe_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=f"{raw_canary} Je suis ")
        yield StreamChunk(content="jalouse.")
        yield StreamChunk(finish_reason="stop")

    engine.stream_full = unsafe_stream
    app = create_app(
        engine,
        "test-model",
        config=_config(tmp_path / "sse-traces.db"),
    )
    client = TestClient(app)

    response = _post(client, "Bonjour.", stream=True)

    assert response.status_code == 200
    assert raw_canary not in response.text
    assert "Je suis jalouse" not in response.text
    assert safe_relationship_replacement_for(("jealousy",)) in response.text
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    trace = app.state.trace_store.list_traces()[0]
    assert raw_canary not in repr(trace)
    assert trace.result == safe_relationship_replacement_for(("jealousy",))
    assert trace.metadata["relationship_guard_action"] == "replace"


def test_overlay_sse_pure_tool_echo_becomes_text_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    user_text = "Recherche cette question substantielle contenant plusieurs mots."
    encoded = json.dumps({"query": user_text})
    split = len(encoded) // 2
    engine = _engine()

    async def tool_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call-echo",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": encoded[:split],
                    },
                }
            ]
        )
        yield StreamChunk(
            tool_calls=[
                {
                    "index": 0,
                    "function": {"arguments": encoded[split:]},
                }
            ]
        )
        yield StreamChunk(finish_reason="tool_calls")

    engine.stream_full = tool_stream
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        user_text,
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    assert "call-echo" not in response.text
    assert safe_relationship_replacement_for(("conversation_echo",)) in (response.text)
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert all(
        choice["delta"]["tool_calls"] is None
        for frame in frames
        for choice in frame["choices"]
    )
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_overlay_sse_reemits_only_the_canonical_inspected_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    arguments = '{"query":"meteo sure"}'
    split = len(arguments) // 2
    engine = _engine()

    async def tool_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        for fragment in (arguments[:split], arguments[split:]):
            yield StreamChunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call-canonical",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": fragment,
                        },
                    }
                ]
            )
        yield StreamChunk(finish_reason="tool_calls")

    engine.stream_full = tool_stream
    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Recherche la meteo.",
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    emitted_calls = [
        tool_call
        for frame in frames
        for choice in frame["choices"]
        for tool_call in (choice["delta"]["tool_calls"] or [])
    ]
    assert emitted_calls == [
        {
            "index": 0,
            "id": "call-canonical",
            "type": "function",
            "function": {"name": "search", "arguments": arguments},
        }
    ]
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("unsafe_field", ("name", "type"))
def test_overlay_sse_scans_tool_name_and_type_before_any_delta(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_field: str,
) -> None:
    _select(monkeypatch)
    canary = "RAW-SSE-FIELD Je suis jalouse."
    engine = _engine()

    async def tool_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        call_type = canary if unsafe_field == "type" else "function"
        call_name = canary if unsafe_field == "name" else "search"
        yield StreamChunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call-name",
                    "type": call_type,
                    "function": {"name": call_name, "arguments": "{}"},
                }
            ]
        )
        yield StreamChunk(finish_reason="tool_calls")

    engine.stream_full = tool_stream
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        "Recherche sure.",
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    assert canary not in response.text
    if unsafe_field == "type":
        assert '"type":"relationship_policy_error"' in response.text
        assert '"role":"assistant"' not in response.text
        return
    assert safe_relationship_replacement_for(("jealousy",)) in response.text
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert all(
        choice["delta"]["tool_calls"] is None
        for frame in frames
        for choice in frame["choices"]
    )
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_overlay_sse_rejects_unknown_provider_field_before_any_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-SSE-UNKNOWN Je suis jalouse."
    engine = _engine()

    async def tool_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call-unknown",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                    "provider_field": canary,
                }
            ]
        )
        yield StreamChunk(finish_reason="tool_calls")

    engine.stream_full = tool_stream
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = _post(
        client,
        "Recherche sure.",
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    assert canary not in response.text
    assert '"type":"relationship_policy_error"' in response.text
    assert '"role":"assistant"' not in response.text
    assert "call-unknown" not in response.text


@pytest.mark.parametrize("missing_field", ("id", "name"))
def test_overlay_sse_rejects_incomplete_tool_call_before_any_delta(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    _select(monkeypatch)
    engine = _engine()

    async def tool_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        call = {
            "index": 0,
            "id": "call-complete",
            "type": "function",
            "function": {"name": "search", "arguments": "{}"},
        }
        if missing_field == "id":
            call["id"] = ""
        else:
            call["function"]["name"] = ""
        yield StreamChunk(tool_calls=[call])
        yield StreamChunk(finish_reason="tool_calls")

    engine.stream_full = tool_stream
    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Recherche sure.",
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    )

    assert response.status_code == 200
    assert '"type":"relationship_policy_error"' in response.text
    assert '"role":"assistant"' not in response.text


def test_overlay_websocket_buffers_unsafe_chunks_and_traces_only_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    raw_canary = "RAW-WS-CANARY"
    engine = _engine()

    async def unsafe_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=f"{raw_canary} Je suis ")
        yield StreamChunk(content="jalouse.")
        yield StreamChunk(finish_reason="stop")

    engine.stream_full = unsafe_stream
    app = create_app(engine, "test-model", config=_config())
    app.state.trace_store = MagicMock()
    client = TestClient(app)

    with client.websocket_connect("/v1/chat/stream") as websocket:
        websocket.send_text(json.dumps({"message": "Bonjour."}))
        first = websocket.receive_json()
        second = websocket.receive_json()

    replacement = safe_relationship_replacement_for(("jealousy",))
    assert first == {"type": "chunk", "content": replacement}
    assert second == {"type": "done", "content": replacement}
    assert raw_canary not in repr((first, second))
    trace = app.state.trace_store.save.call_args.args[0]
    assert raw_canary not in repr(trace)
    assert trace.result == replacement
    assert trace.metadata["relationship_guard_action"] == "replace"


class _AllowPolicy:
    def check(self, principal: str, capability: str, resource: str) -> bool:
        return (
            principal == OWNER.provenance
            and capability == NETWORK_FETCH
            and resource == "web_search"
        )


class _CountingTool(BaseTool):
    is_local = False

    def __init__(
        self,
        name: str,
        *,
        capabilities: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"Synthetic {self.name}",
            required_capabilities=list(self.capabilities),
            requires_capability_policy=bool(self.capabilities),
        )

    def execute(self, **params) -> ToolResult:
        del params
        self.calls += 1
        return ToolResult(tool_name=self.name, content="executed", success=True)


class _BoundaryProbeAgent(ToolUsingAgent):
    agent_id = "orchestrator"

    def __init__(self, engine, tool: _CountingTool) -> None:
        super().__init__(
            engine,
            "test-model",
            tools=[tool],
            capability_policy=_AllowPolicy(),
        )

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        tool_result = self._executor.execute(
            ToolCall(
                id="unsafe-call",
                name="web_search",
                arguments='{"query":"RAW-TOOL-CANARY Je suis jalouse."}',
            )
        )
        return AgentResult(
            content="Final model text is otherwise safe.",
            tool_results=[tool_result],
            turns=1,
            metadata={"finish_reason": "stop"},
        )


class _LocalCountingTool(_CountingTool):
    is_local = True


class _LocalToolProbeAgent(ToolUsingAgent):
    agent_id = "orchestrator"

    def __init__(self, engine, tool: _LocalCountingTool, tool_call: ToolCall) -> None:
        super().__init__(
            engine,
            "test-model",
            tools=[tool],
            capability_policy=_AllowPolicy(),
        )
        self.tool_call = tool_call
        self.observed_results: list[ToolResult] = []

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        result = self._executor.execute(self.tool_call)
        self.observed_results.append(result)
        return AgentResult(
            content="Final model text is safe.",
            tool_results=[result],
            turns=1,
            metadata={"finish_reason": "stop"},
        )


class _ResultProbeTool(_CountingTool):
    is_local = False

    def __init__(self, content: str, *, metadata: dict | None = None) -> None:
        super().__init__("web_search", capabilities=(NETWORK_FETCH,))
        self.content = content
        self.result_metadata = metadata or {}

    def execute(self, **params) -> ToolResult:
        del params
        self.calls += 1
        return ToolResult(
            tool_name=self.name,
            content=self.content,
            success=True,
            metadata=dict(self.result_metadata),
        )


def _tool_sequence_engine(
    tool_call: ToolCall,
    *,
    final_content: str = "Synthese finale sure.",
):
    engine = _engine()
    engine._publishes_events = False
    engine.generate.side_effect = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
            ],
            "finish_reason": "tool_calls",
            "usage": {},
        },
        {
            "content": final_content,
            "finish_reason": "stop",
            "usage": {},
        },
    ]
    return engine


def _real_orchestrator(engine, tools: list[BaseTool]) -> OrchestratorAgent:
    agent = OrchestratorAgent(
        engine,
        "test-model",
        tools=tools,
        max_turns=2,
        parallel_tools=False,
    )
    agent._executor._capability_policy = _AllowPolicy()
    return agent


class _ExecutingProbeAgent(ToolUsingAgent):
    agent_id = "orchestrator"

    def __init__(self, engine, tool: _ResultProbeTool, arguments: str) -> None:
        super().__init__(
            engine,
            "test-model",
            tools=[tool],
            capability_policy=_AllowPolicy(),
        )
        self.arguments = arguments

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        result = self._executor.execute(
            ToolCall(
                id="result-probe",
                name="web_search",
                arguments=self.arguments,
            )
        )
        return AgentResult(
            content="Synthese finale sure.",
            tool_results=[result],
            turns=1,
            metadata={"finish_reason": "stop"},
        )


def test_agent_tool_arguments_block_before_execution_event_trace_and_response(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    tool = _CountingTool("web_search", capabilities=(NETWORK_FETCH,))
    tool_call = ToolCall(
        id="unsafe-call",
        name="web_search",
        arguments='{"query":"RAW-TOOL-CANARY Je suis jalouse."}',
    )
    engine = _tool_sequence_engine(
        tool_call,
        final_content="Final model text is otherwise safe.",
    )
    agent = _real_orchestrator(engine, [tool])
    original_boundary = agent._executor._boundary_guard
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        config=_config(tmp_path / "agent-traces.db"),
    )
    client = TestClient(app)

    response = _post(client, "Fais une recherche sure.")

    assert response.status_code == 200
    assert tool.calls == 0
    assert agent._executor._boundary_guard is original_boundary
    assert "RAW-TOOL-CANARY" not in response.text
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("jealousy",))
    )
    trace = app.state.trace_store.list_traces()[0]
    assert "RAW-TOOL-CANARY" not in repr(trace)
    assert all(step.step_type != StepType.TOOL_CALL for step in trace.steps)
    assert trace.metadata["relationship_guard_tool_arguments_blocked"] is True


def test_local_tool_key_gate_is_blocked_before_execute_or_parent_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-LOCAL-KEY Je suis jalouse."
    tool = _LocalCountingTool("think")
    tool_call = ToolCall(
        id="local-key",
        name="think",
        arguments=json.dumps({canary: True}),
    )
    engine = _tool_sequence_engine(
        tool_call,
        final_content="Final model text is safe.",
    )
    agent = _real_orchestrator(engine, [tool])
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.TOOL_CALL_START, observed.append)
    bus.subscribe(EventType.TOOL_CALL_END, observed.append)
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "local-tool-traces.db"),
    )

    response = _post(TestClient(app), "Utilise le raisonnement local.")

    assert response.status_code == 200
    assert tool.calls == 0
    assert observed == []
    assert canary not in response.text
    assert canary not in repr(bus.history)
    assert canary not in repr(app.state.trace_store.list_traces())
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("jealousy",))
    )


def test_unknown_tool_name_is_rejected_without_echo_or_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-UNKNOWN-NAME Je suis jalouse."
    tool = _LocalCountingTool("think")
    tool_call = ToolCall(id="unknown", name=canary, arguments="{}")
    engine = _tool_sequence_engine(
        tool_call,
        final_content="Final model text is safe.",
    )
    agent = _real_orchestrator(engine, [tool])
    bus = EventBus(record_history=True)
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "unknown-tool-traces.db"),
    )

    response = _post(TestClient(app), "Appelle seulement un outil connu.")

    assert response.status_code == 200
    assert tool.calls == 0
    assert canary not in response.text
    assert canary not in repr(bus.history)
    assert canary not in repr(app.state.trace_store.list_traces())


def test_benign_tool_argument_may_echo_user_without_blocking_execution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    user_text = "Recherche cette question substantielle avec plusieurs mots."
    tool = _ResultProbeTool("Page externe sure.")
    engine = _tool_sequence_engine(
        ToolCall(
            id="result-probe",
            name="web_search",
            arguments=json.dumps({"query": user_text}),
        )
    )
    agent = _real_orchestrator(engine, [tool])
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=EventBus(record_history=True),
        config=_config(tmp_path / "benign-echo-tool.db"),
    )

    response = _post(TestClient(app), user_text)

    assert response.status_code == 200
    assert tool.calls == 1
    assert response.json()["choices"][0]["message"]["content"] == (
        "Synthese finale sure."
    )
    trace = app.state.trace_store.list_traces()[0]
    assert trace.result == "Synthese finale sure."
    assert trace.outcome == "completed"
    assert any(step.step_type == StepType.TOOL_CALL for step in trace.steps)


def test_unsafe_external_tool_result_is_scrubbed_without_replacing_safe_final(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-EXTERNAL-RESULT Je suis jalouse."
    tool = _ResultProbeTool(
        canary,
        metadata={"snippet": canary},
    )
    engine = _tool_sequence_engine(
        ToolCall(
            id="result-probe",
            name="web_search",
            arguments='{"query":"meteo sure"}',
        )
    )
    agent = _real_orchestrator(engine, [tool])
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.TOOL_CALL_END, observed.append)
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "external-result.db"),
    )

    response = _post(TestClient(app), "Fais une recherche sure.")

    assert response.status_code == 200
    assert tool.calls == 1
    assert response.json()["choices"][0]["message"]["content"] == (
        "Synthese finale sure."
    )
    assert canary not in response.text
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)
    trace = app.state.trace_store.list_traces()[0]
    assert canary not in repr(trace)
    assert trace.result == "Synthese finale sure."
    assert trace.outcome == "completed"
    tool_step = next(
        step for step in trace.steps if step.step_type == StepType.TOOL_CALL
    )
    assert tool_step.output["result"] == safe_relationship_replacement_for(
        ("jealousy",)
    )
    assert tool_step.metadata == {}
    assert trace.metadata["relationship_guard_action"] == "allow"


def test_existing_boundary_mutation_is_rechecked_before_sink(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-BOUNDARY-MUTATION Je suis jalouse."
    tool = _ResultProbeTool("must not execute")
    engine = _tool_sequence_engine(
        ToolCall(
            id="result-probe",
            name="web_search",
            arguments='{"query":"requete sure"}',
        )
    )
    agent = _real_orchestrator(engine, [tool])

    class _MutatingBoundary:
        def check_outbound(self, tool_call: ToolCall) -> ToolCall:
            tool_call.arguments = json.dumps({"query": canary})
            return tool_call

    agent._executor._boundary_guard = _MutatingBoundary()
    bus = EventBus(record_history=True)
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "mutating-boundary.db"),
    )

    response = _post(TestClient(app), "Recherche sure.")

    assert response.status_code == 200
    assert tool.calls == 0
    assert not any(
        event.event_type in {EventType.TOOL_CALL_START, EventType.TOOL_CALL_END}
        for event in bus.history
    )
    assert canary not in response.text
    assert canary not in repr(bus.history)
    assert canary not in repr(app.state.trace_store.list_traces())
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("jealousy",))
    )


class _SurfaceProbeAgent(ToolUsingAgent):
    agent_id = "orchestrator"

    def __init__(self, engine, tools: list[_CountingTool]) -> None:
        super().__init__(
            engine,
            "test-model",
            tools=tools,
            capability_policy=_AllowPolicy(),
        )
        self.observed: list[tuple[str, ...]] = []
        self.unknown_results: list[ToolResult] = []

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        self.observed.append(tuple(self._executor._tools))
        self.unknown_results.append(
            self._executor.execute(
                ToolCall(id="unknown", name="queue_action", arguments="{}")
            )
        )
        return AgentResult(
            content="Surface inspected safely.",
            turns=1,
            metadata={"finish_reason": "stop"},
        )


def test_http_zero_capability_surface_is_profile_independent_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    engine = _engine("Surface inspected safely.")
    engine._publishes_events = False
    tools = [
        _CountingTool("calculator"),
        _CountingTool("think"),
        _CountingTool("queue_action"),
        _CountingTool("skill_manage"),
        _CountingTool("unknown_zero_cap"),
        _CountingTool("channel_send"),
        _CountingTool("text_to_speech"),
        _CountingTool("web_search", capabilities=(NETWORK_FETCH,)),
    ]
    agent = _real_orchestrator(engine, tools)
    observed: list[tuple[str, ...]] = []
    original_copy = routes._copy_agent_for_request

    def observing_copy(*args, **kwargs):
        request_agent = original_copy(*args, **kwargs)
        observed.append(tuple(request_agent._executor._tools))
        return request_agent

    monkeypatch.setattr(routes, "_copy_agent_for_request", observing_copy)
    client = TestClient(create_app(engine, "test-model", agent=agent, config=_config()))

    response = _post(client, "Inspecte la surface.")

    assert response.status_code == 200
    _select(monkeypatch, overlay=None)
    public_response = _post(client, "Inspecte aussi la surface commune.")

    assert public_response.status_code == 200
    assert observed == [
        ("calculator", "think", "web_search"),
        ("calculator", "think", "web_search"),
    ]
    assert all(tool.calls == 0 for tool in tools)


class _EventProbeAgent(BaseAgent):
    agent_id = "orchestrator"

    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.calls = 0
        self._bus = None

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        self.calls += 1
        self._bus.publish(
            EventType.INFERENCE_START,
            {"model": "test-model", "engine": "probe"},
        )
        self._bus.publish(
            EventType.INFERENCE_END,
            {
                "model": "test-model",
                "engine": "probe",
                "content": self.canary,
                "usage": {"total_tokens": 7},
                "finish_reason": "stop",
            },
        )
        return AgentResult(
            content=self.canary,
            turns=1,
            metadata={
                "finish_reason": "stop",
                "messages": [
                    {"role": "user", "content": "Question sure."},
                    {"role": "assistant", "content": self.canary},
                ],
            },
        )


class _SpoofedTraceAgent(BaseAgent):
    agent_id = "orchestrator"

    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.calls = 0
        self._bus = None

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del input, context, kwargs
        self.calls += 1
        self._bus.publish(
            EventType.TRACE_COMPLETE,
            {"trace": Trace(result=self.canary)},
        )
        return AgentResult(
            content="must not return",
            turns=1,
            metadata={"finish_reason": "stop"},
        )


def test_agent_cannot_spoof_trace_complete_before_terminal_guard(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-SPOOFED-TRACE Je suis jalouse."
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.TRACE_COMPLETE, observed.append)
    agent = _SpoofedTraceAgent(canary)
    app = create_app(
        _engine(),
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "spoofed-trace.db"),
    )

    response = _post(
        TestClient(app, raise_server_exceptions=False),
        "Question sure.",
    )

    assert response.status_code >= 500
    assert canary not in response.text
    assert observed == []
    assert agent.calls == 0
    assert canary not in repr(bus.history)
    assert app.state.trace_store.list_traces() == []


@pytest.mark.parametrize("unsafe_field", ("usage", "finish_reason"))
def test_inference_event_rejects_opaque_usage_and_finish_before_parent(
    unsafe_field: str,
) -> None:
    previous = "Question substantielle contenant bien plus de quatre mots."
    canary = (
        previous if unsafe_field == "usage" else "RAW-FINISH-REASON Je suis jalouse."
    )
    guard = prepare_relationship_guard(OVERLAY, (("user", previous),))
    assert guard is not None
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)
    payload = {
        "model": "test-model",
        "content": "Reponse finale sure.",
        "usage": {"total_tokens": 7},
        "finish_reason": "stop",
    }
    if unsafe_field == "usage":
        payload["usage"] = {"opaque": canary}
    else:
        payload["finish_reason"] = canary

    with pytest.raises(RuntimeError):
        request_bus.publish(EventType.INFERENCE_END, payload)

    assert observed == []
    assert not any(event.event_type == EventType.INFERENCE_END for event in bus.history)
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)


def test_inference_event_scrubs_structured_conversation_echo_before_parent() -> None:
    previous = "Tour utilisateur substantiel contenant largement quatre mots."
    guard = prepare_relationship_guard(OVERLAY, (("user", previous),))
    assert guard is not None
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)

    request_bus.publish(
        EventType.INFERENCE_END,
        {
            "model": "test-model",
            "content": "Reponse textuelle sure.",
            "content_blocks": [{"type": "text", "text": previous}],
            "usage": {"total_tokens": 7},
            "finish_reason": "stop",
        },
    )

    assert len(observed) == 1
    assert previous not in repr(observed)
    inference_history = [
        event for event in bus.history if event.event_type == EventType.INFERENCE_END
    ]
    assert previous not in repr(inference_history)
    assert observed[0].data["content_blocks"] == []
    assert observed[0].data["finish_reason"] == "stop"


def test_relationship_event_payload_is_detached_before_parent_publish() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)
    payload = {
        "model": "test-model",
        "content": "Reponse textuelle sure.",
        "tool_calls": [
            {
                "id": "call-safe",
                "name": "search",
                "arguments": '{"query":"etat du service"}',
            }
        ],
        "usage": {"total_tokens": 7},
        "finish_reason": "tool_calls",
    }

    request_bus.publish(EventType.INFERENCE_END, payload)
    payload["usage"]["total_tokens"] = 999
    payload["tool_calls"][0]["name"] = "RAW-LATE-MUTATION Je suis jalouse."

    assert len(observed) == 1
    assert observed[0].data["usage"] == {"total_tokens": 7}
    assert observed[0].data["tool_calls"][0]["name"] == "search"
    assert "RAW-LATE-MUTATION" not in repr(bus.history)


def test_non_inference_model_event_is_scanned_before_parent_publish() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    canary = "RAW-TURN-END Je suis jalouse."
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.AGENT_TURN_END, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)

    with pytest.raises(RuntimeError):
        request_bus.publish(
            EventType.AGENT_TURN_END,
            {"agent": "orchestrator", "summary": canary},
        )

    assert observed == []
    assert canary not in repr(bus.history)


def test_agent_turn_start_never_forwards_free_form_input_under_overlay() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    canary = "RAW-SPOOFED-TURN-INPUT Je suis jalouse."
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.AGENT_TURN_START, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)

    request_bus.publish(
        EventType.AGENT_TURN_START,
        {"agent": "orchestrator", "input": canary},
    )

    assert len(observed) == 1
    assert observed[0].data == {"agent": "orchestrator"}
    assert canary not in repr(bus.history)


def test_agent_turn_start_rejects_noncanonical_agent_id() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    canary = "RAW-AGENT-ID Je suis jalouse."
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.AGENT_TURN_START, observed.append)
    request_bus = routes._relationship_request_event_bus(bus, guard)

    with pytest.raises(RuntimeError):
        request_bus.publish(
            EventType.AGENT_TURN_START,
            {"agent": canary, "input": "Question sure."},
        )

    assert observed == []
    assert canary not in repr(bus.history)


def test_agent_has_one_terminal_apply_and_no_raw_parent_event_or_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-APPLY-COUNT Je suis jalouse."
    guard_module = importlib.import_module("ava_extensions.identity.relationship_guard")
    original_prepare = guard_module.prepare_relationship_guard
    original_apply = guard_module.RelationshipOutputGuard.apply
    counts = {"prepare": 0, "apply": 0}

    def counted_prepare(*args, **kwargs):
        counts["prepare"] += 1
        return original_prepare(*args, **kwargs)

    def counted_apply(self, *args, **kwargs):
        counts["apply"] += 1
        return original_apply(self, *args, **kwargs)

    monkeypatch.setattr(
        guard_module,
        "prepare_relationship_guard",
        counted_prepare,
    )
    monkeypatch.setattr(
        guard_module.RelationshipOutputGuard,
        "apply",
        counted_apply,
    )
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
    engine = _engine(canary)
    engine._publishes_events = False
    engine.generate.return_value["usage"] = {"total_tokens": 7}
    agent = OrchestratorAgent(
        engine,
        "test-model",
        bus=bus,
        max_turns=1,
        parallel_tools=False,
    )
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "apply-count-traces.db"),
    )

    response = _post(TestClient(app), "Question sure.")

    assert response.status_code == 200
    assert counts == {"prepare": 1, "apply": 1}
    assert canary not in response.text
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)
    traces = app.state.trace_store.list_traces()
    assert canary not in repr(traces)
    assert traces[0].metadata["relationship_guard_action"] == "replace"
    assert any(event.event_type == EventType.TRACE_COMPLETE for event in bus.history)
    inference = next(
        event for event in observed if event.event_type == EventType.INFERENCE_END
    )
    assert inference.data["usage"]["total_tokens"] == 7


def test_instrumented_direct_engine_sanitizes_before_parent_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjarvis.telemetry.instrumented_engine import InstrumentedEngine

    _select(monkeypatch)
    canary = "RAW-INSTRUMENTED Je suis jalouse."
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
    inner = _engine(canary)
    engine = InstrumentedEngine(inner, bus)
    client = TestClient(create_app(engine, "test-model", bus=bus, config=_config()))

    response = _post(client, "Question instrumentee sure.")

    assert response.status_code == 200
    assert canary not in response.text
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)
    assert observed
    assert observed[0].data["model"] == "test-model"
    assert observed[0].data["content"] == safe_relationship_replacement_for(
        ("jealousy",)
    )


class _ConcurrentInnerEngine:
    engine_id = "concurrent-inner"

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.barrier = threading.Barrier(len(responses))
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def generate(self, messages, *, model: str, **kwargs):
        del model, kwargs
        user_text = messages[-1].text
        with self.lock:
            self.calls.append(user_text)
        self.barrier.wait(timeout=5)
        return {
            "content": self.responses[user_text],
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }

    def list_models(self) -> list[str]:
        return ["test-model"]

    def health(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _ConcurrentProbeAgent(BaseAgent):
    agent_id = "orchestrator"

    def run(
        self,
        input: str,
        context: AgentContext | None = None,
        **kwargs,
    ) -> AgentResult:
        del kwargs
        self._emit_turn_start(input)
        result = self._generate(self._build_messages(input, context))
        self._emit_turn_end(turns=1)
        return AgentResult(
            content=result["content"],
            turns=1,
            metadata={"finish_reason": result["finish_reason"]},
        )


def test_concurrent_overlay_requests_isolate_engine_bus_guard_and_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjarvis.telemetry.instrumented_engine import InstrumentedEngine

    _select(monkeypatch)
    first_input = "Premiere question concurrente sure."
    second_input = "Seconde question concurrente sure."
    first_canary = "RAW-CONCURRENT-FIRST Je suis jalouse."
    second_canary = "RAW-CONCURRENT-SECOND Tu n'as besoin que de moi."
    inner = _ConcurrentInnerEngine(
        {first_input: first_canary, second_input: second_canary}
    )
    bus = EventBus(record_history=True)
    engine = InstrumentedEngine(inner, bus)
    agent = OrchestratorAgent(
        engine,
        "test-model",
        bus=bus,
        max_turns=1,
        parallel_tools=False,
    )
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(tmp_path / "concurrent-traces.db"),
    )
    client = TestClient(app)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            text: pool.submit(_post, client, text)
            for text in (first_input, second_input)
        }
        responses = {
            text: future.result(timeout=10) for text, future in futures.items()
        }

    expected = {
        first_input: safe_relationship_replacement_for(("jealousy",)),
        second_input: safe_relationship_replacement_for(("exclusivity",)),
    }
    assert all(response.status_code == 200 for response in responses.values())
    assert {
        text: response.json()["choices"][0]["message"]["content"]
        for text, response in responses.items()
    } == expected
    assert set(inner.calls) == {first_input, second_input}
    assert agent._bus is bus
    assert agent._engine is engine
    assert engine._bus is bus
    assert first_canary not in repr(bus.history)
    assert second_canary not in repr(bus.history)

    traces = app.state.trace_store.list_traces()
    assert {trace.query: trace.result for trace in traces} == expected
    inference_events = [
        event for event in bus.history if event.event_type == EventType.INFERENCE_END
    ]
    trace_events = [
        event for event in bus.history if event.event_type == EventType.TRACE_COMPLETE
    ]
    assert len(inference_events) == len(trace_events) == 2
    assert len({event.correlation_id for event in inference_events}) == 2
    assert {
        event.data["trace"].query: event.correlation_id for event in trace_events
    } == {
        next(
            query
            for query, replacement in expected.items()
            if replacement == event.data["content"]
        ): event.correlation_id
        for event in inference_events
    }


def test_matrix_durable_http_commits_and_replays_only_filtered_response(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "matrix-conversations.db",
    )
    _select(monkeypatch, principal=MATRIX_OWNER)
    turn_id = "96f845f4-099e-4f80-8999-89f6a4626c9d"
    headers = {conversation_store.TURN_ID_HEADER: turn_id}
    first_engine = _engine("RAW-MATRIX-CANARY Je suis jalouse.")
    first_client = TestClient(create_app(first_engine, "test-model", config=_config()))

    first = first_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Bonjour Matrix."}],
        },
    )
    assert first.status_code == 200
    assert "RAW-MATRIX-CANARY" not in first.text

    second_engine = _engine("must never run")
    second_client = TestClient(
        create_app(second_engine, "test-model", config=_config())
    )
    replay = second_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Bonjour Matrix."}],
        },
    )

    assert replay.status_code == 200
    assert replay.json() == first.json()
    second_engine.generate.assert_not_called()
    texts = [
        row["texte"] for row in conversation_store.lire(MATRIX_OWNER.conversation_key)
    ]
    assert texts == [
        "Bonjour Matrix.",
        safe_relationship_replacement_for(("jealousy",)),
    ]


def test_unsafe_overlay_replay_is_refused_not_transformed() -> None:
    unsafe = "RAW-REPLAY-CANARY Je suis jalouse."
    response = ChatCompletionResponse(
        model="test-model",
        choices=[
            Choice(
                message=ChoiceMessage(content=unsafe),
                finish_reason="stop",
            )
        ],
    )
    entry = SimpleNamespace(
        state="completed",
        user_text="Question sure.",
        request_sha256="a" * 64,
        assistant_text=unsafe,
        response_json=response.model_dump_json(),
        timestamp=0.0,
    )
    store = MagicMock()
    store.lire_statut_tour.return_value = entry
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sure."),),
    )
    assert guard is not None

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            routes._resolve_existing_durable_turn(
                store,
                OWNER.conversation_key,
                "31bff169-e7ec-4e28-ab7f-7f64eeb15730",
                "Question sure.",
                "a" * 64,
                relationship_guard=guard,
            )
        )

    assert raised.value.status_code == 410
    assert raised.value.detail == ("Ava durable turn is unsafe and cannot be replayed")
    assert unsafe not in str(raised.value)


def test_overlay_durable_fingerprint_binds_dynamic_guard_digest() -> None:
    from openjarvis.server.models import ChatCompletionRequest, ChatMessage

    request = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question sure.")],
    )
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sure."),),
    )
    assert guard is not None

    without_guard = routes._durable_request_sha256(request, OVERLAY)
    with_guard = routes._durable_request_sha256(
        request,
        OVERLAY,
        relationship_guard=guard,
    )

    assert with_guard != without_guard
    assert guard.policy_sha256 == relationship_text_safety_policy_sha256()
