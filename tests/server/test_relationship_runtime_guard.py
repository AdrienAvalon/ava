"""End-to-end boundaries for the opt-in relationship runtime guard."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
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
    RELATIONSHIP_REPAIR_REPLACEMENT_ID,
    relationship_repair_instruction,
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
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
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
    assert engine.generate.call_count == 2


def test_http_direct_repair_uses_only_authenticated_context_and_fixed_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-REPAIR-PROMPT Je suis jalouse."
    repaired = "Je peux t'aider à comparer calmement les options concrètes."
    engine = _engine()
    engine.generate.side_effect = (
        {
            "content": canary,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
                "opaque": 999,
            },
        },
        {
            "content": repaired,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 6,
                "total_tokens": 11,
                "untrusted": 999,
            },
        },
    )
    client = TestClient(create_app(engine, "test-model", config=_config()))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Instruction cliente banale."},
                {"role": "user", "content": "Première question sûre."},
                {"role": "assistant", "content": "Réponse antérieure sûre."},
                {"role": "user", "content": "Question actuelle sûre."},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search", "parameters": {}},
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == repaired
    assert response.json()["usage"] == {
        "prompt_tokens": 8,
        "completion_tokens": 10,
        "total_tokens": 18,
    }
    assert engine.generate.call_count == 2
    primary_call, repair_call = engine.generate.call_args_list
    primary_messages = primary_call.args[0]
    repair_messages = repair_call.args[0]
    expected_instruction = relationship_repair_instruction(("jealousy",))
    assert expected_instruction.prompt not in primary_messages[0].content
    assert repair_messages[0].content == (
        f"{primary_messages[0].content}\n\n{expected_instruction.prompt}"
    )
    assert repair_messages[1:] == primary_messages[1:]
    assert all(
        repair_message is not primary_message
        for repair_message, primary_message in zip(
            repair_messages,
            primary_messages,
            strict=True,
        )
    )
    assert canary not in repr(repair_messages)
    assert repair_call.kwargs == {
        "model": "test-model",
        "temperature": 0.0,
        "max_tokens": 1024,
    }
    assert "tools" in primary_call.kwargs
    assert "tools" not in repair_call.kwargs


def test_noninstrumented_direct_repair_emits_one_telemetry_record_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-REPAIR-TELEMETRY Je suis jalouse."
    engine = _engine()
    engine._publishes_events = False
    engine.generate.side_effect = (
        {
            "content": canary,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
        {
            "content": "Réponse réparée sûre.",
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 6,
                "total_tokens": 11,
            },
        },
    )
    bus = EventBus(record_history=True)

    response = _post(
        TestClient(
            create_app(
                engine,
                "test-model",
                bus=bus,
                config=_config(),
            )
        ),
        "Question sûre.",
    )

    assert response.status_code == 200
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    records = [
        event.data["record"]
        for event in generation_events
        if event.event_type == EventType.TELEMETRY_RECORD
    ]
    assert len(records) == 2
    assert [record.total_tokens for record in records] == [7, 11]
    assert canary not in repr(records)
    assert canary not in repr(bus.history)


def test_http_direct_safe_answer_does_not_call_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    engine = _engine("Réponse initiale sûre et utile.")

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question sûre.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "Réponse initiale sûre et utile."
    )
    assert engine.generate.call_count == 1


def test_prior_tool_taint_quarantines_lexically_safe_primary_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    primary = "Réponse primaire lexicalement sûre."
    tool_canary = "RAW-PRIOR-TOOL Je suis jalouse."
    repaired = "Je peux répondre directement avec une option concrète."
    engine = _engine()
    engine.generate.side_effect = (
        {
            "content": primary,
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call-tainted",
                    "name": "search",
                    "arguments": json.dumps({"query": tool_canary}),
                }
            ],
            "usage": {"total_tokens": 7},
        },
        {
            "content": repaired,
            "finish_reason": "stop",
            "usage": {"total_tokens": 11},
        },
    )
    bus = EventBus(record_history=True)

    response = _post(
        TestClient(create_app(engine, "test-model", bus=bus, config=_config())),
        "Question sûre.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == repaired
    inference_ends = [
        event for event in bus.history if event.event_type == EventType.INFERENCE_END
    ]
    assert [event.data["content"] for event in inference_ends] == ["", repaired]
    assert all(event.data["tool_calls"] == [] for event in inference_ends)
    assert primary not in repr(bus.history)
    assert tool_canary not in repr(bus.history)


def test_repair_uses_pre_primary_snapshot_if_engine_mutates_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-MUTATED-PRIMARY Je suis jalouse."
    repaired = "Je peux répondre sans pression avec une étape concrète."
    engine = _engine()
    call_count = 0

    def generate(messages, *, model, **kwargs):
        nonlocal call_count
        del model, kwargs
        call_count += 1
        if call_count == 1:
            messages[0].content = f"{messages[0].content}\n\n{canary}"
            messages.append(routes.Message(role=routes.Role.ASSISTANT, content=canary))
            return {"content": canary, "finish_reason": "stop", "usage": {}}
        return {"content": repaired, "finish_reason": "stop", "usage": {}}

    engine.generate.side_effect = generate

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question authentifiée et sûre.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == repaired
    assert engine.generate.call_count == 2
    repair_messages = engine.generate.call_args_list[1].args[0]
    assert [message.role for message in repair_messages] == [
        routes.Role.SYSTEM,
        routes.Role.USER,
    ]
    assert canary not in repr(repair_messages)


@pytest.mark.parametrize(
    "messages",
    (
        [
            routes.Message(role=routes.Role.SYSTEM, content="Persona."),
            routes.Message(role=routes.Role.SYSTEM, content="Second système."),
            routes.Message(role=routes.Role.USER, content="Question."),
        ],
        [
            routes.Message(role=routes.Role.SYSTEM, content="Persona."),
            routes.Message(
                role=routes.Role.TOOL,
                content="Résultat.",
                tool_call_id="1",
            ),
        ],
    ),
)
def test_repair_prompt_rejects_noncanonical_authenticated_conversation(
    messages,
) -> None:
    before = repr(messages)

    with pytest.raises(RuntimeError):
        routes._relationship_repair_messages(
            messages,
            relationship_repair_instruction(("jealousy",)),
        )

    assert repr(messages) == before


def test_repair_prompt_rejects_a_tampered_policy_instruction() -> None:
    import dataclasses

    canary = "RAW-TAMPERED-REPAIR-INSTRUCTION Je suis jalouse."
    messages = [
        routes.Message(role=routes.Role.SYSTEM, content="Persona serveur sûre."),
        routes.Message(role=routes.Role.USER, content="Question sûre."),
    ]
    instruction = dataclasses.replace(
        relationship_repair_instruction(("jealousy",)),
        prompt=canary,
    )

    with pytest.raises(RuntimeError, match="instruction is invalid"):
        routes._relationship_repair_messages(messages, instruction)

    assert canary not in repr(messages)


def test_http_direct_repair_exception_falls_back_without_raw_log_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _select(monkeypatch)
    canary = "RAW-REPAIR-EXCEPTION Je suis jalouse."
    engine = _engine()
    engine.generate.side_effect = (
        {
            "content": canary,
            "finish_reason": "stop",
            "usage": {},
        },
        RuntimeError(canary),
    )
    bus = EventBus(record_history=True)
    quarantines = []
    original_factory = routes._relationship_request_event_bus

    def capture_quarantine(parent_bus, guard):
        quarantine = original_factory(parent_bus, guard)
        quarantines.append(quarantine)
        return quarantine

    monkeypatch.setattr(
        routes,
        "_relationship_request_event_bus",
        capture_quarantine,
    )

    response = _post(
        TestClient(create_app(engine, "test-model", bus=bus, config=_config())),
        "Question sûre.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("jealousy",))
    )
    assert engine.generate.call_count == 2
    assert canary not in response.text
    assert canary not in caplog.text
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    inference_ends = [
        event
        for event in generation_events
        if event.event_type == EventType.INFERENCE_END
    ]
    assert [event.data["content"] for event in inference_ends] == ["", ""]
    assert all(
        event.data["finish_reason"] in {"stop", "length"} for event in inference_ends
    )
    telemetry_records = [
        event.data["record"]
        for event in generation_events
        if event.event_type == EventType.TELEMETRY_RECORD
    ]
    assert all(record.metadata == {} for record in telemetry_records)
    assert canary not in repr(generation_events)
    assert len(quarantines) == 1
    quarantine = quarantines[0]
    assert quarantine._attempts == []
    history_length = len(bus.history)
    quarantine.abort_generation_events()
    assert len(bus.history) == history_length
    assert canary not in repr(quarantine)


@pytest.mark.parametrize("failure_kind", ("provider", "guard"))
def test_http_direct_primary_failure_flushes_one_empty_canonical_attempt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
) -> None:
    _select(monkeypatch)
    canary = f"RAW-PRIMARY-{failure_kind.upper()} Je suis jalouse."
    engine = _engine()
    if failure_kind == "provider":
        engine.generate.side_effect = RuntimeError(canary)
    else:
        engine.generate.return_value = {
            "content": "Réponse primaire lexicalement sûre.",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call-invalid",
                    "name": "search",
                    "arguments": {"query": canary, "invalid": {canary}},
                }
            ],
            "usage": {"total_tokens": 7},
        }
    bus = EventBus(record_history=True)

    response = _post(
        TestClient(create_app(engine, "test-model", bus=bus, config=_config())),
        "Question sûre.",
    )

    assert response.status_code == 503
    assert engine.generate.call_count == 1
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    inference_end = generation_events[1]
    assert inference_end.data["content"] == ""
    assert inference_end.data["tool_calls"] == []
    assert generation_events[2].data["record"].metadata == {}
    assert canary not in response.text
    assert canary not in caplog.text
    assert canary not in repr(bus.history)


def test_overlay_sse_repair_runs_off_loop_and_stays_buffered() -> None:
    canary = "RAW-SSE-HEARTBEAT Je suis jalouse."
    repaired = "Réponse réparée, sûre et directement utile."
    engine = _engine()
    timestamps: dict[str, float] = {}

    async def unsafe_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=canary)
        yield StreamChunk(finish_reason="stop")

    def blocking_repair(messages, *, model, **kwargs):
        del messages, model, kwargs
        timestamps["repair_start"] = time.monotonic()
        time.sleep(0.15)
        timestamps["repair_end"] = time.monotonic()
        return {"content": repaired, "finish_reason": "stop", "usage": {}}

    engine.stream_full = unsafe_stream
    engine.generate.side_effect = blocking_repair

    async def scenario() -> str:
        guard = prepare_relationship_guard(OVERLAY)
        assert guard is not None
        response = await routes._handle_stream(
            engine,
            "test-model",
            ChatCompletionRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Question sûre.")],
                stream=True,
                max_tokens=128,
            ),
            base_identity_prompt="Persona serveur sûre.",
            relationship_overlay=OVERLAY,
            relationship_guard=guard,
        )

        async def heartbeat() -> None:
            await asyncio.sleep(0.02)
            timestamps["heartbeat"] = time.monotonic()

        heartbeat_task = asyncio.create_task(heartbeat())
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        await heartbeat_task
        return "".join(chunks)

    body = asyncio.run(scenario())

    assert timestamps["repair_start"] < timestamps["heartbeat"]
    assert timestamps["heartbeat"] < timestamps["repair_end"]
    assert canary not in body
    assert repaired in body
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


def test_no_overlay_keeps_unsafe_looking_output_and_never_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch, principal=None, overlay=None)
    merge_usage = MagicMock(side_effect=AssertionError("overlay-only helper called"))
    monkeypatch.setattr(routes, "_merge_relationship_usage", merge_usage)
    content = "Texte public inchangé : je suis jalouse."
    engine = _engine(content)

    response = _post(
        TestClient(create_app(engine, "test-model", config=_config())),
        "Question publique.",
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == content
    assert engine.generate.call_count == 1
    call = engine.generate.call_args
    assert call.kwargs == {
        "model": "test-model",
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    assert all(
        relationship_repair_instruction(("jealousy",)).prompt not in message.text
        for message in call.args[0]
    )
    merge_usage.assert_not_called()


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
    assert "Safe response." in response.text
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    trace = app.state.trace_store.list_traces()[0]
    assert raw_canary not in repr(trace)
    assert trace.result == "Safe response."
    assert trace.metadata["relationship_guard_action"] == "replace"
    assert trace.metadata["relationship_guard_repair_outcome"] == "accepted"
    assert trace.metadata["relationship_guard_replacement_id"] == (
        RELATIONSHIP_REPAIR_REPLACEMENT_ID
    )


def test_overlay_sse_incomplete_repair_never_reaches_events_trace_or_frames(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _select(monkeypatch)
    primary_canary = "RAW-SSE-FAILED-PRIMARY Je suis jalouse."
    repair_canary = "RAW-SSE-INCOMPLETE-REPAIR"
    engine = _engine()
    bus = EventBus(record_history=True)

    async def unsafe_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=primary_canary)
        yield StreamChunk(
            finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        )

    def incomplete_repair(messages, *, model, **kwargs):
        del messages, model, kwargs
        assert not any(
            event.event_type
            in {
                EventType.INFERENCE_START,
                EventType.INFERENCE_END,
                EventType.TELEMETRY_RECORD,
            }
            for event in bus.history
        )
        return {
            "content": f"{repair_canary} encore incomplète",
            "finish_reason": "length",
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        }

    engine.stream_full = unsafe_stream
    engine.generate.side_effect = incomplete_repair
    app = create_app(
        engine,
        "test-model",
        bus=bus,
        config=_config(tmp_path / "sse-incomplete-repair-traces.db"),
    )

    response = _post(TestClient(app), "Bonjour.", stream=True)

    replacement = safe_relationship_replacement_for(("jealousy",))
    assert response.status_code == 200
    assert replacement in response.text
    assert primary_canary not in response.text
    assert repair_canary not in response.text
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    assert [
        event.data["content"]
        for event in generation_events
        if event.event_type == EventType.INFERENCE_END
    ] == ["", ""]
    assert all(
        event.data["record"].metadata == {}
        for event in generation_events
        if event.event_type == EventType.TELEMETRY_RECORD
    )
    trace = app.state.trace_store.list_traces()[0]
    assert trace.result == replacement
    assert trace.metadata["relationship_guard_repair_outcome"] == "incomplete"
    assert primary_canary not in repr(bus.history)
    assert repair_canary not in repr(bus.history)
    assert primary_canary not in repr(trace)
    assert repair_canary not in repr(trace)
    assert primary_canary not in caplog.text
    assert repair_canary not in caplog.text


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
    assert "Safe response." in response.text
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
    assert "Safe response." in response.text
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

    replacement = "Safe response."
    assert first == {"type": "chunk", "content": replacement}
    assert second == {"type": "done", "content": replacement}
    assert raw_canary not in repr((first, second))
    trace = app.state.trace_store.save.call_args.args[0]
    assert raw_canary not in repr(trace)
    assert trace.result == replacement
    assert trace.metadata["relationship_guard_action"] == "replace"


def test_overlay_websocket_structured_repair_never_reaches_events_or_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _select(monkeypatch)
    primary_canary = "RAW-WS-FAILED-PRIMARY Je suis jalouse."
    repair_canary = "RAW-WS-STRUCTURED-REPAIR"
    engine = _engine()
    bus = EventBus(record_history=True)

    async def unsafe_stream(messages, *, model, **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content=primary_canary)
        yield StreamChunk(finish_reason="stop", usage={"total_tokens": 7})

    def structured_repair(messages, *, model, **kwargs):
        del messages, model, kwargs
        assert not any(
            event.event_type
            in {
                EventType.INFERENCE_START,
                EventType.INFERENCE_END,
                EventType.TELEMETRY_RECORD,
            }
            for event in bus.history
        )
        return {
            "content": "Réponse de réparation lexicalement sûre.",
            "finish_reason": "stop",
            "tool_calls": [
                {
                    "id": "repair-structured-call",
                    "name": "search",
                    "arguments": json.dumps({"query": repair_canary}),
                }
            ],
            "usage": {"total_tokens": 11},
        }

    engine.stream_full = unsafe_stream
    engine.generate.side_effect = structured_repair
    app = create_app(
        engine,
        "test-model",
        bus=bus,
        config=_config(tmp_path / "ws-structured-repair-traces.db"),
    )

    with TestClient(app).websocket_connect("/v1/chat/stream") as websocket:
        websocket.send_text(json.dumps({"message": "Bonjour."}))
        first = websocket.receive_json()
        second = websocket.receive_json()

    replacement = safe_relationship_replacement_for(("jealousy",))
    assert first == {"type": "chunk", "content": replacement}
    assert second == {"type": "done", "content": replacement}
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    inference_ends = [
        event
        for event in generation_events
        if event.event_type == EventType.INFERENCE_END
    ]
    assert [event.data["content"] for event in inference_ends] == ["", ""]
    assert all(event.data["tool_calls"] == [] for event in inference_ends)
    trace = app.state.trace_store.list_traces()[0]
    assert trace.result == replacement
    assert trace.metadata["relationship_guard_repair_outcome"] == "structured_output"
    assert primary_canary not in repr((first, second, bus.history, trace))
    assert repair_canary not in repr((first, second, bus.history, trace))
    assert primary_canary not in caplog.text
    assert repair_canary not in caplog.text


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
def test_native_inference_event_is_quarantined_before_terminal_verdict(
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

    request_bus.publish(EventType.INFERENCE_END, payload)

    assert observed == []
    assert not any(event.event_type == EventType.INFERENCE_END for event in bus.history)
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)


def test_native_structured_event_never_bypasses_generation_quarantine() -> None:
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

    assert observed == []
    assert previous not in repr(observed)
    inference_history = [
        event for event in bus.history if event.event_type == EventType.INFERENCE_END
    ]
    assert inference_history == []
    assert previous not in repr(request_bus)


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

    engine = MagicMock()
    engine.engine_id = "native-test-engine"
    engine.generate.return_value = payload
    request_engine = routes._copy_engine_for_relationship_events(engine, request_bus)

    result = request_engine.generate([], model="test-model")
    assert observed == []
    assert not any(
        event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
        for event in bus.history
    )
    decision = guard.apply(
        result["content"],
        tool_argument_json=routes._tool_call_json(result["tool_calls"]),
    )
    request_bus.finalize_generation_events(decision)
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


def test_agent_has_one_staged_terminal_decision_and_no_raw_parent_event_or_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch)
    canary = "RAW-APPLY-COUNT Je suis jalouse."
    guard_module = importlib.import_module("ava_extensions.identity.relationship_guard")
    original_prepare = guard_module.prepare_relationship_guard
    original_begin = guard_module.RelationshipOutputGuard._begin_bounded_repair
    original_finish = guard_module.RelationshipOutputGuard._finish_bounded_repair
    counts = {"prepare": 0, "begin": 0, "finish": 0}

    def counted_prepare(*args, **kwargs):
        counts["prepare"] += 1
        return original_prepare(*args, **kwargs)

    def counted_begin(self, *args, **kwargs):
        counts["begin"] += 1
        return original_begin(self, *args, **kwargs)

    def counted_finish(self, *args, **kwargs):
        counts["finish"] += 1
        return original_finish(self, *args, **kwargs)

    monkeypatch.setattr(
        guard_module,
        "prepare_relationship_guard",
        counted_prepare,
    )
    monkeypatch.setattr(
        guard_module.RelationshipOutputGuard,
        "_begin_bounded_repair",
        counted_begin,
    )
    monkeypatch.setattr(
        guard_module.RelationshipOutputGuard,
        "_finish_bounded_repair",
        counted_finish,
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
    assert counts == {"prepare": 1, "begin": 1, "finish": 1}
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


def test_agent_repair_is_observed_scrubbed_and_counted_in_usage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _select(monkeypatch)
    canary = "RAW-AGENT-REPAIR Je suis jalouse."
    repaired = "Je peux t'aider à avancer avec une étape concrète et libre."
    engine = _engine()
    engine._publishes_events = False
    engine.generate.side_effect = (
        {
            "content": canary,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        },
        {
            "content": repaired,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 6,
                "total_tokens": 11,
            },
        },
    )
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.INFERENCE_END, observed.append)
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
        config=_config(tmp_path / "accepted-repair-traces.db"),
    )

    response = _post(TestClient(app), "Question sûre.")

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == repaired
    assert response.json()["usage"] == {
        "prompt_tokens": 8,
        "completion_tokens": 10,
        "total_tokens": 18,
    }
    assert engine.generate.call_count == 2
    assert len(observed) == 2
    assert canary not in repr(observed)
    assert canary not in repr(bus.history)
    assert canary not in caplog.text
    traces = app.state.trace_store.list_traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.result == repaired
    assert canary not in repr(trace)
    assert trace.metadata["relationship_guard_repair_outcome"] == "accepted"
    assert trace.metadata["relationship_guard_replacement_id"] == (
        RELATIONSHIP_REPAIR_REPLACEMENT_ID
    )
    generate_steps = [
        step for step in trace.steps if step.step_type == StepType.GENERATE
    ]
    assert len(generate_steps) == 2
    assert [step.output["content"] for step in generate_steps] == ["", repaired]


@pytest.mark.parametrize(
    ("failure_kind", "expected_outcome"),
    (
        ("incomplete", "incomplete"),
        ("structured", "structured_output"),
        ("unsafe", "unsafe"),
        ("provider_error", "provider_error"),
    ),
)
def test_failed_agent_repair_never_leaves_generation_text_in_events_or_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    expected_outcome: str,
) -> None:
    _select(monkeypatch)
    primary_canary = "RAW-FAILED-PRIMARY Je suis jalouse."
    repair_canary = f"RAW-FAILED-REPAIR-{failure_kind}"
    engine = _engine()
    engine._publishes_events = False
    primary_result = {
        "content": primary_canary,
        "finish_reason": "stop",
        "usage": {"total_tokens": 7},
    }
    if failure_kind == "incomplete":
        repair_result = {
            "content": f"{repair_canary} réponse encore incomplète",
            "finish_reason": "length",
            "usage": {"total_tokens": 11},
        }
    elif failure_kind == "structured":
        repair_result = {
            "content": f"{repair_canary} réponse structurée",
            "finish_reason": "stop",
            "tool_calls": [
                {
                    "id": "repair-call",
                    "name": "search",
                    "arguments": '{"query":"valeur sûre"}',
                }
            ],
            "usage": {"total_tokens": 11},
        }
    elif failure_kind == "unsafe":
        repair_result = {
            "content": f"{repair_canary} Tu n'as besoin que de moi.",
            "finish_reason": "stop",
            "usage": {"total_tokens": 11},
        }
    else:
        repair_result = RuntimeError(f"{repair_canary} provider detail")
    engine.generate.side_effect = (primary_result, repair_result)
    bus = EventBus(record_history=True)
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
        config=_config(tmp_path / f"failed-{failure_kind}-trace.db"),
    )

    response = _post(TestClient(app), "Question sûre.")

    assert response.status_code == 200
    fallback_gate_ids = (
        ("jealousy", "exclusivity") if failure_kind == "unsafe" else ("jealousy",)
    )
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(fallback_gate_ids)
    )
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    inference_ends = [
        event
        for event in generation_events
        if event.event_type == EventType.INFERENCE_END
    ]
    assert [event.data["content"] for event in inference_ends] == ["", ""]
    assert all(event.data["tool_calls"] == [] for event in inference_ends)
    trace_complete_index = max(
        index
        for index, event in enumerate(bus.history)
        if event.event_type == EventType.TRACE_COMPLETE
    )
    last_generation_index = max(
        index
        for index, event in enumerate(bus.history)
        if event.event_type == EventType.TELEMETRY_RECORD
    )
    assert trace_complete_index > last_generation_index
    traces = app.state.trace_store.list_traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.metadata["relationship_guard_repair_outcome"] == expected_outcome
    assert all(
        step.output.get("content", "") == ""
        for step in trace.steps
        if step.step_type == StepType.GENERATE
    )
    assert primary_canary not in repr(bus.history)
    assert repair_canary not in repr(bus.history)
    assert primary_canary not in repr(trace)
    assert repair_canary not in repr(trace)
    assert repair_canary not in caplog.text


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
    assert observed[0].data["engine"] == "relationship-test"
    assert observed[0].data["content"] == ""
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * 2
    telemetry_events = [
        event
        for event in generation_events
        if event.event_type == EventType.TELEMETRY_RECORD
    ]
    assert len(telemetry_events) == 2
    assert inner.generate.call_count == 2
    assert all(
        event.data["record"].model_id == "test-model"
        and event.data["record"].engine == "relationship-test"
        and event.data["record"].metadata == {}
        for event in telemetry_events
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
    assert len(inference_events) == 4
    assert len(trace_events) == 2
    assert len({event.correlation_id for event in inference_events}) == 2
    trace_correlations = {
        event.data["trace"].query: event.correlation_id for event in trace_events
    }
    assert set(trace_correlations.values()) == {
        event.correlation_id for event in inference_events
    }
    assert all(
        sum(event.correlation_id == correlation for event in inference_events) == 2
        for correlation in trace_correlations.values()
    )


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


async def _wait_for_thread_event(event: threading.Event) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3)


async def _wait_until(predicate) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=3)


def _assert_cancelled_generation_quarantine(
    bus: EventBus,
    quarantine,
    *,
    calls: int,
    canaries: tuple[str, ...],
) -> None:
    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ] * calls
    assert all(
        event.data["content"] == ""
        and event.data["tool_calls"] == []
        and event.data["content_blocks"] == []
        for event in generation_events
        if event.event_type == EventType.INFERENCE_END
    )
    assert all(
        event.data["record"].metadata == {}
        for event in generation_events
        if event.event_type == EventType.TELEMETRY_RECORD
    )
    assert quarantine._finalized is True
    assert quarantine._drop_late_events is True
    assert quarantine._attempts == []
    assert all(canary not in repr(bus.history) for canary in canaries)
    assert all(canary not in repr(quarantine) for canary in canaries)


@pytest.mark.parametrize("with_tools", (False, True), ids=("sse", "sse-tools"))
@pytest.mark.parametrize("cancel_phase", ("primary", "repair"))
def test_overlay_sse_cancellation_purges_generation_and_late_worker(
    monkeypatch: pytest.MonkeyPatch,
    with_tools: bool,
    cancel_phase: str,
) -> None:
    primary_canary = f"RAW-CANCEL-{cancel_phase.upper()}-PRIMARY Je suis jalouse."
    repair_canary = f"RAW-CANCEL-{cancel_phase.upper()}-REPAIR"
    bus = EventBus(record_history=True)
    quarantines = []
    original_factory = routes._relationship_request_event_bus

    def capture_quarantine(parent_bus, guard):
        quarantine = original_factory(parent_bus, guard)
        quarantines.append(quarantine)
        return quarantine

    monkeypatch.setattr(
        routes,
        "_relationship_request_event_bus",
        capture_quarantine,
    )
    engine = _engine()
    phase_started = threading.Event()
    release_repair = threading.Event()
    repair_finished = threading.Event()
    repair_results: list[dict] = []
    repair_message_reprs: list[str] = []

    if cancel_phase == "primary":

        async def slow_primary(messages, *, model, **kwargs):
            del messages, model, kwargs
            yield StreamChunk(content=primary_canary)
            phase_started.set()
            await asyncio.Event().wait()

        engine.stream_full = slow_primary
    else:

        async def completed_primary(messages, *, model, **kwargs):
            del messages, model, kwargs
            yield StreamChunk(content=primary_canary)
            yield StreamChunk(finish_reason="stop", usage={"total_tokens": 7})

        def blocking_repair(messages, *, model, **kwargs):
            del model, kwargs
            repair_message_reprs.append(repr(messages))
            phase_started.set()
            release_repair.wait(timeout=3)
            result = {
                "content": repair_canary,
                "finish_reason": "stop",
                "usage": {"total_tokens": 11},
            }
            repair_results.append(result)
            repair_finished.set()
            return result

        engine.stream_full = completed_primary
        engine.generate.side_effect = blocking_repair

    trace_store = MagicMock()

    async def scenario() -> list[str]:
        guard = prepare_relationship_guard(OVERLAY)
        assert guard is not None
        request = ChatCompletionRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Question sûre.")],
            stream=True,
            max_tokens=128,
            tools=(
                [
                    {
                        "type": "function",
                        "function": {"name": "search", "parameters": {}},
                    }
                ]
                if with_tools
                else None
            ),
        )
        if with_tools:
            response = await routes._handle_stream_tools(
                engine,
                "test-model",
                request,
                base_identity_prompt="Persona serveur sûre.",
                bus=bus,
                relationship_overlay=OVERLAY,
                relationship_guard=guard,
            )
        else:
            response = await routes._handle_stream(
                engine,
                "test-model",
                request,
                trace_store=trace_store,
                base_identity_prompt="Persona serveur sûre.",
                bus=bus,
                relationship_overlay=OVERLAY,
                relationship_guard=guard,
            )

        emitted: list[str] = []

        async def consume() -> None:
            async for frame in response.body_iterator:
                emitted.append(frame.decode() if isinstance(frame, bytes) else frame)

        task = asyncio.create_task(consume())
        await _wait_for_thread_event(phase_started)
        if cancel_phase == "repair":
            assert all(
                attempt.content == "" and attempt.tool_calls == []
                for attempt in quarantines[0]._attempts
            )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(quarantines) == 1
        _assert_cancelled_generation_quarantine(
            bus,
            quarantines[0],
            calls=1 if cancel_phase == "primary" else 2,
            canaries=(primary_canary, repair_canary),
        )
        release_repair.set()
        if cancel_phase == "repair":
            await _wait_for_thread_event(repair_finished)
            await _wait_until(lambda: repair_results == [{}])
        return emitted

    try:
        emitted = asyncio.run(scenario())
    finally:
        release_repair.set()

    assert emitted == []
    assert trace_store.save.call_count == 0
    assert not any(
        event.event_type == EventType.TRACE_COMPLETE for event in bus.history
    )
    if cancel_phase == "repair":
        assert engine.generate.call_count == 1
        assert primary_canary not in repair_message_reprs[0]


@pytest.mark.parametrize("cancel_phase", ("primary", "repair"))
def test_overlay_websocket_cancellation_purges_generation_trace_and_late_worker(
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    from openjarvis.server import api_routes

    primary_canary = f"RAW-WS-CANCEL-{cancel_phase.upper()}-PRIMARY Je suis jalouse."
    repair_canary = f"RAW-WS-CANCEL-{cancel_phase.upper()}-REPAIR"
    bus = EventBus(record_history=True)
    quarantines = []
    original_factory = routes._relationship_request_event_bus

    def capture_quarantine(parent_bus, guard):
        quarantine = original_factory(parent_bus, guard)
        quarantines.append(quarantine)
        return quarantine

    monkeypatch.setattr(
        routes,
        "_relationship_request_event_bus",
        capture_quarantine,
    )

    async def trust_context(_websocket):
        guard = prepare_relationship_guard(OVERLAY)
        assert guard is not None
        return OWNER, OVERLAY, "Persona serveur sûre.", guard

    monkeypatch.setattr(api_routes, "_websocket_trust_context", trust_context)
    engine = _engine()
    phase_started = threading.Event()
    release_repair = threading.Event()
    repair_finished = threading.Event()
    repair_results: list[dict] = []

    if cancel_phase == "primary":

        async def slow_primary(messages, *, model, **kwargs):
            del messages, model, kwargs
            yield StreamChunk(content=primary_canary)
            phase_started.set()
            await asyncio.Event().wait()

        engine.stream_full = slow_primary
    else:

        async def completed_primary(messages, *, model, **kwargs):
            del messages, model, kwargs
            yield StreamChunk(content=primary_canary)
            yield StreamChunk(finish_reason="stop", usage={"total_tokens": 7})

        def blocking_repair(messages, *, model, **kwargs):
            del messages, model, kwargs
            phase_started.set()
            release_repair.wait(timeout=3)
            result = {
                "content": repair_canary,
                "finish_reason": "stop",
                "usage": {"total_tokens": 11},
            }
            repair_results.append(result)
            repair_finished.set()
            return result

        engine.stream_full = completed_primary
        engine.generate.side_effect = blocking_repair

    trace_store = MagicMock()
    app = create_app(engine, "test-model", bus=bus, config=_config())
    app.state.trace_store = trace_store

    class FakeWebSocket:
        def __init__(self) -> None:
            self.app = app
            self.headers = {}
            self.sent: list[dict] = []
            self._received = False

        async def accept(self) -> None:
            return None

        async def close(self, code: int) -> None:
            del code

        async def receive_text(self) -> str:
            if not self._received:
                self._received = True
                return json.dumps({"message": "Question sûre."})
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

    websocket = FakeWebSocket()

    async def scenario() -> None:
        task = asyncio.create_task(api_routes.websocket_chat_stream(websocket))
        await _wait_for_thread_event(phase_started)
        if cancel_phase == "repair":
            assert all(
                attempt.content == "" and attempt.tool_calls == []
                for attempt in quarantines[0]._attempts
            )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(quarantines) == 1
        _assert_cancelled_generation_quarantine(
            bus,
            quarantines[0],
            calls=1 if cancel_phase == "primary" else 2,
            canaries=(primary_canary, repair_canary),
        )
        release_repair.set()
        if cancel_phase == "repair":
            await _wait_for_thread_event(repair_finished)
            await _wait_until(lambda: repair_results == [{}])

    try:
        asyncio.run(scenario())
    finally:
        release_repair.set()

    assert websocket.sent == []
    trace_store.save.assert_not_called()
    assert not any(
        event.event_type == EventType.TRACE_COMPLETE for event in bus.history
    )


@pytest.mark.parametrize("use_agent", (False, True), ids=("direct", "agent"))
@pytest.mark.parametrize("cancel_phase", ("primary", "repair"))
def test_overlay_nonstream_cancellation_drops_late_result_and_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    use_agent: bool,
    cancel_phase: str,
) -> None:
    from starlette.requests import Request

    _select(monkeypatch)
    primary_canary = (
        f"RAW-NONSTREAM-CANCEL-{'AGENT' if use_agent else 'DIRECT'}-PRIMARY "
        "Je suis jalouse."
    )
    repair_canary = f"RAW-NONSTREAM-CANCEL-{'AGENT' if use_agent else 'DIRECT'}-REPAIR"
    phase_started = threading.Event()
    release_provider = threading.Event()
    provider_returned = threading.Event()
    primary_result = {
        "content": primary_canary,
        "finish_reason": "stop",
        "usage": {"total_tokens": 7},
    }
    repair_results: list[dict] = []
    engine = _engine()
    engine._publishes_events = False
    call_count = 0

    def slow_generate(messages, *, model, **kwargs):
        nonlocal call_count

        del messages, model, kwargs
        call_count += 1
        if call_count == 1 and cancel_phase == "repair":
            return primary_result
        phase_started.set()
        release_provider.wait(timeout=3)
        if call_count == 1:
            result = primary_result
        else:
            result = {
                "content": repair_canary,
                "finish_reason": "stop",
                "usage": {"total_tokens": 11},
            }
            repair_results.append(result)
        provider_returned.set()
        return result

    engine.generate.side_effect = slow_generate
    bus = EventBus(record_history=True)
    quarantines = []
    original_factory = routes._relationship_request_event_bus

    def capture_quarantine(parent_bus, guard):
        quarantine = original_factory(parent_bus, guard)
        quarantines.append(quarantine)
        return quarantine

    monkeypatch.setattr(
        routes,
        "_relationship_request_event_bus",
        capture_quarantine,
    )
    agent = (
        OrchestratorAgent(
            engine,
            "test-model",
            bus=bus,
            max_turns=1,
            parallel_tools=False,
        )
        if use_agent
        else None
    )
    app = create_app(
        engine,
        "test-model",
        agent=agent,
        bus=bus,
        config=_config(
            tmp_path / f"cancel-{'agent' if use_agent else 'direct'}-{cancel_phase}.db"
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
            "app": app,
        }
    )
    body = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question sûre.")],
    )

    async def scenario() -> None:
        task = asyncio.create_task(routes.chat_completions(body, request))
        await _wait_for_thread_event(phase_started)
        if cancel_phase == "repair":
            assert all(
                attempt.content == "" and attempt.tool_calls == []
                for attempt in quarantines[0]._attempts
            )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(quarantines) == 1
        _assert_cancelled_generation_quarantine(
            bus,
            quarantines[0],
            calls=1 if cancel_phase == "primary" else 2,
            canaries=(primary_canary, repair_canary),
        )
        release_provider.set()
        await _wait_for_thread_event(provider_returned)
        if cancel_phase == "primary":
            await _wait_until(lambda: primary_result == {})
        else:
            await _wait_until(lambda: repair_results == [{}])

    try:
        asyncio.run(scenario())
    finally:
        release_provider.set()

    assert engine.generate.call_count == (1 if cancel_phase == "primary" else 2)
    assert app.state.trace_store.list_traces() == []
    assert not any(
        event.event_type == EventType.TRACE_COMPLETE for event in bus.history
    )


def test_cancel_barrier_drops_non_generation_event_after_sanitizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.AGENT_TURN_START, observed.append)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    sanitizer_entered = threading.Event()
    release_sanitizer = threading.Event()
    original_sanitizer = routes._sanitize_relationship_event_data

    def blocking_sanitizer(*args, **kwargs):
        sanitizer_entered.set()
        release_sanitizer.wait(timeout=3)
        return original_sanitizer(*args, **kwargs)

    monkeypatch.setattr(
        routes,
        "_sanitize_relationship_event_data",
        blocking_sanitizer,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            quarantine.publish,
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )
        assert sanitizer_entered.wait(timeout=2)
        quarantine.cancel_generation_events()
        release_sanitizer.set()
        future.result(timeout=2)

    assert observed == []
    assert not any(
        event.event_type == EventType.AGENT_TURN_START for event in bus.history
    )
    assert quarantine._finalized is True
    assert quarantine._drop_late_events is True
    assert quarantine._attempts == []


def test_cancel_barrier_revokes_publication_admitted_before_parent_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    bus = EventBus(record_history=True)
    observed = []
    bus.subscribe(EventType.AGENT_TURN_START, observed.append)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    publication_reserved = threading.Event()
    release_publication = threading.Event()
    cancellation_returned = threading.Event()
    original_reserve = routes._RelationshipRequestEventBus._reserve_parent_publication

    def reserve_then_pause(self, event_type):
        reserved = original_reserve(self, event_type)
        if reserved:
            publication_reserved.set()
            release_publication.wait(timeout=3)
        return reserved

    monkeypatch.setattr(
        routes._RelationshipRequestEventBus,
        "_reserve_parent_publication",
        reserve_then_pause,
    )

    def cancel() -> None:
        quarantine.cancel_generation_events()
        cancellation_returned.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(
            quarantine.publish,
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )
        assert publication_reserved.wait(timeout=2)
        cancellation = pool.submit(cancel)
        assert not cancellation_returned.wait(timeout=0.1)
        release_publication.set()
        publication.result(timeout=2)
        cancellation.result(timeout=2)

    assert cancellation_returned.is_set()
    assert observed == []
    assert (
        len(
            [
                event
                for event in bus.history
                if event.event_type == EventType.AGENT_TURN_START
            ]
        )
        == 0
    )
    quarantine.publish(
        EventType.AGENT_TURN_START,
        {"agent": "late", "input": "Question tardive."},
    )
    assert observed == []


def test_reentrant_cancel_revokes_foreign_reservation_before_parent_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    quarantine.begin_generation(model="test-model", engine="test-engine")
    foreign_reserved = threading.Event()
    release_foreign = threading.Event()
    cancellation_returned = threading.Event()
    observed_threads: list[str] = []
    original_reserve = routes._RelationshipRequestEventBus._reserve_parent_publication

    def reserve_then_pause_foreign(self, event_type):
        reserved = original_reserve(self, event_type)
        if reserved and threading.current_thread().name == "relationship-foreign":
            foreign_reserved.set()
            assert release_foreign.wait(timeout=3)
        return reserved

    def cancel_from_parent_listener(_event) -> None:
        observed_threads.append(threading.current_thread().name)
        if threading.current_thread().name == "relationship-canceller":
            quarantine.cancel_generation_events()
            cancellation_returned.set()

    monkeypatch.setattr(
        routes._RelationshipRequestEventBus,
        "_reserve_parent_publication",
        reserve_then_pause_foreign,
    )
    bus.subscribe(EventType.AGENT_TURN_START, cancel_from_parent_listener)

    def publish_with_name(name: str) -> None:
        threading.current_thread().name = name
        quarantine.publish(
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        foreign = pool.submit(publish_with_name, "relationship-foreign")
        assert foreign_reserved.wait(timeout=2)
        canceller = pool.submit(publish_with_name, "relationship-canceller")
        assert cancellation_returned.wait(timeout=2)
        history_size_at_cancel = len(bus.history)
        assert history_size_at_cancel == 4
        assert [
            event.event_type
            for event in bus.history
            if event.event_type
            in {
                EventType.INFERENCE_START,
                EventType.INFERENCE_END,
                EventType.TELEMETRY_RECORD,
            }
        ] == [
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        ]
        release_foreign.set()
        canceller.result(timeout=2)
        foreign.result(timeout=2)

    assert len(bus.history) == history_size_at_cancel
    assert observed_threads == ["relationship-canceller"]
    assert quarantine._parent_publications == 0


def test_cancel_barrier_drains_indivisible_canonical_flush() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    bus = EventBus(record_history=True)
    scoped = bus.scoped("canonical-cancel-race")
    parent_entered = threading.Event()
    release_parent = threading.Event()
    cancellation_returned = threading.Event()

    class BlockingDelegate:
        @property
        def history(self):
            return scoped.history

        def clear_history(self) -> None:
            scoped.clear_history()

        def subscribe(self, event_type, callback) -> None:
            scoped.subscribe(event_type, callback)

        def unsubscribe(self, event_type, callback) -> None:
            scoped.unsubscribe(event_type, callback)

        def publish(self, event_type, data=None):
            parent_entered.set()
            assert release_parent.wait(timeout=3)
            return scoped.publish(event_type, data)

    quarantine = routes._RelationshipRequestEventBus(BlockingDelegate(), guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={
            "content": "Réponse sûre.",
            "finish_reason": "stop",
            "usage": {"total_tokens": 7},
        },
        failed=False,
    )
    decision = guard.apply("Réponse sûre.")

    def cancel() -> None:
        quarantine.cancel_generation_events()
        cancellation_returned.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        finalization = pool.submit(quarantine.finalize_generation_events, decision)
        assert parent_entered.wait(timeout=2)
        cancellation = pool.submit(cancel)
        assert not cancellation_returned.wait(timeout=0.1)
        release_parent.set()
        finalization.result(timeout=2)
        cancellation.result(timeout=2)

    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    history_size = len(bus.history)
    quarantine.publish(
        EventType.AGENT_TURN_START,
        {"agent": "late", "input": "Question tardive."},
    )
    assert len(bus.history) == history_size


def test_reentrant_canonical_cancel_keeps_only_one_complete_safe_batch() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    raw_canary = "RAW-CANONICAL-CANCEL Tu n'as besoin que de moi."
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={"content": raw_canary, "finish_reason": "stop"},
        failed=False,
    )
    decision = guard.apply(raw_canary)
    history_sizes_at_cancel_return: list[int] = []

    def cancel_from_start(_event) -> None:
        quarantine.cancel_generation_events()
        history_sizes_at_cancel_return.append(len(bus.history))

    bus.subscribe(EventType.INFERENCE_START, cancel_from_start)
    quarantine.finalize_generation_events(decision)

    generation_events = [
        event
        for event in bus.history
        if event.event_type
        in {
            EventType.INFERENCE_START,
            EventType.INFERENCE_END,
            EventType.TELEMETRY_RECORD,
        }
    ]
    assert history_sizes_at_cancel_return == [1]
    assert [event.event_type for event in generation_events] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    inference_end = generation_events[1]
    assert inference_end.data["content"] == ""
    assert inference_end.data["tool_calls"] == []
    assert raw_canary not in repr(bus.history)

    history_size = len(bus.history)
    quarantine.finalize_generation_events(decision)
    quarantine.publish(EventType.TRACE_COMPLETE, {"trace": object()})
    assert len(bus.history) == history_size


def test_reentrant_trace_is_flushed_after_exact_canonical_triplet() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={"content": "Réponse sûre.", "finish_reason": "stop"},
        failed=False,
    )
    decision = guard.apply("Réponse sûre.")
    trace = Trace(result=decision.output_text, metadata=guard.metadata())

    def publish_trace_from_start(_event) -> None:
        quarantine.publish(EventType.TRACE_COMPLETE, {"trace": trace})

    bus.subscribe(EventType.INFERENCE_START, publish_trace_from_start)
    quarantine.finalize_generation_events(decision)

    assert [event.event_type for event in bus.history] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
        EventType.TRACE_COMPLETE,
    ]
    assert bus.history[-1].data["trace"] is not trace
    assert quarantine._canonical_flush_complete is True
    assert quarantine._canonical_flush_failed is False
    assert quarantine._pending_trace_complete is None

    quarantine.publish(EventType.TRACE_COMPLETE, {"trace": trace})
    assert len(bus.history) == 4


def test_reentrant_cancel_purges_trace_queued_during_canonical_batch() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={"content": "Réponse sûre.", "finish_reason": "stop"},
        failed=False,
    )
    decision = guard.apply("Réponse sûre.")
    trace = Trace(result=decision.output_text, metadata=guard.metadata())
    pending_after_cancel: list[dict | None] = []

    def queue_trace_then_cancel(_event) -> None:
        quarantine.publish(EventType.TRACE_COMPLETE, {"trace": trace})
        assert quarantine._pending_trace_complete is not None
        quarantine.cancel_generation_events()
        pending_after_cancel.append(quarantine._pending_trace_complete)

    bus.subscribe(EventType.INFERENCE_START, queue_trace_then_cancel)
    quarantine.finalize_generation_events(decision)

    assert [event.event_type for event in bus.history] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    assert pending_after_cancel == [None]
    assert quarantine._canonical_flush_complete is True
    assert quarantine._pending_trace_complete is None
    assert quarantine._drop_late_events is True


def test_canonical_exception_suppresses_reentrant_deferred_trace() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={"content": "Réponse sûre.", "finish_reason": "stop"},
        failed=False,
    )
    decision = guard.apply("Réponse sûre.")
    trace = Trace(result=decision.output_text, metadata=guard.metadata())
    bus.subscribe(
        EventType.INFERENCE_START,
        lambda _event: quarantine.publish(
            EventType.TRACE_COMPLETE,
            {"trace": trace},
        ),
    )
    bus.subscribe(
        EventType.INFERENCE_END,
        lambda _event: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )

    with pytest.raises(RuntimeError, match="generation event flush failed"):
        quarantine.finalize_generation_events(decision)

    assert [event.event_type for event in bus.history] == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    assert quarantine._canonical_flush_complete is True
    assert quarantine._canonical_flush_failed is True
    assert quarantine._pending_trace_complete is None
    assert quarantine._parent_publications == 0


def test_cancel_barrier_is_reentrant_from_parent_listener() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    bus = EventBus(record_history=True)
    quarantine = routes._relationship_request_event_bus(bus, guard)
    cancellation_returned = threading.Event()

    def cancel_from_listener(_event) -> None:
        quarantine.cancel_generation_events()
        cancellation_returned.set()

    bus.subscribe(EventType.AGENT_TURN_START, cancel_from_listener)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(
            quarantine.publish,
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )
        publication.result(timeout=2)

    assert cancellation_returned.is_set()
    assert quarantine._finalized is True
    assert quarantine._drop_late_events is True
    assert (
        len(
            [
                event
                for event in bus.history
                if event.event_type == EventType.AGENT_TURN_START
            ]
        )
        == 1
    )


def test_parent_publication_exception_releases_cancel_barrier() -> None:
    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    calls: list[EventType] = []

    class RaisingDelegate:
        @property
        def history(self):
            return []

        def clear_history(self) -> None:
            return None

        def subscribe(self, event_type, callback) -> None:
            del event_type, callback

        def unsubscribe(self, event_type, callback) -> None:
            del event_type, callback

        def publish(self, event_type, data=None):
            del data
            calls.append(event_type)
            raise RuntimeError("synthetic parent failure")

    quarantine = routes._RelationshipRequestEventBus(RaisingDelegate(), guard)
    with pytest.raises(RuntimeError, match="synthetic parent failure"):
        quarantine.publish(
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )

    quarantine.cancel_generation_events()
    assert calls == [EventType.AGENT_TURN_START]
    assert quarantine._parent_publications == 0
    assert quarantine._parent_publication_threads == {}


def test_canonical_parent_exceptions_release_barrier_after_exact_attempt() -> None:
    guard = prepare_relationship_guard(
        OVERLAY,
        (("user", "Question sûre."),),
    )
    assert guard is not None
    calls: list[EventType] = []

    class RaisingDelegate:
        @property
        def history(self):
            return []

        def clear_history(self) -> None:
            return None

        def subscribe(self, event_type, callback) -> None:
            del event_type, callback

        def unsubscribe(self, event_type, callback) -> None:
            del event_type, callback

        def publish(self, event_type, data=None):
            del data
            calls.append(event_type)
            raise RuntimeError("synthetic canonical failure")

    quarantine = routes._RelationshipRequestEventBus(RaisingDelegate(), guard)
    attempt = quarantine.begin_generation(model="test-model", engine="test-engine")
    quarantine.finish_generation(
        attempt,
        result={"content": "Réponse sûre.", "finish_reason": "stop"},
        failed=False,
    )
    decision = guard.apply("Réponse sûre.")

    with pytest.raises(RuntimeError, match="generation event flush failed"):
        quarantine.finalize_generation_events(decision)

    assert calls == [
        EventType.INFERENCE_START,
        EventType.INFERENCE_END,
        EventType.TELEMETRY_RECORD,
    ]
    assert quarantine._parent_publications == 0
    assert quarantine._parent_publication_threads == {}


def test_deferred_trace_commit_allows_reentrant_eventbus_cancellation() -> None:
    scope = routes._RelationshipCancellationScope()
    bus = EventBus(record_history=True)
    store = MagicMock()
    trace = object()
    scope.register_bus(bus)
    scope.register_pending_trace(trace, store, bus)
    bus.subscribe(EventType.TRACE_COMPLETE, lambda _event: scope.cancel())

    scope.commit_pending_trace()

    store.save.assert_called_once_with(trace)
    assert [event.event_type for event in bus.history] == [EventType.TRACE_COMPLETE]
    assert scope.cancelled() is True

    late_trace = object()
    scope.register_pending_trace(late_trace, store, bus)
    scope.commit_pending_trace()
    store.save.assert_called_once_with(trace)
    assert len(bus.history) == 1


def test_deferred_trace_commit_rechecks_cancel_after_store_callback() -> None:
    scope = routes._RelationshipCancellationScope()
    bus = EventBus(record_history=True)
    store = MagicMock()
    trace = object()
    scope.register_bus(bus)
    scope.register_pending_trace(trace, store, bus)
    store.save.side_effect = lambda _trace: scope.cancel()

    scope.commit_pending_trace()

    store.save.assert_called_once_with(trace)
    assert scope.cancelled() is True
    assert bus.history == []


def test_cross_quarantine_parent_callbacks_follow_one_lock_order() -> None:
    guard_one = prepare_relationship_guard(OVERLAY)
    guard_two = prepare_relationship_guard(OVERLAY)
    assert guard_one is not None
    assert guard_two is not None
    bus = EventBus(record_history=True)
    quarantine_one = routes._RelationshipRequestEventBus(
        bus.scoped("relationship-cross-one"),
        guard_one,
    )
    quarantine_two = routes._RelationshipRequestEventBus(
        bus.scoped("relationship-cross-two"),
        guard_two,
    )

    def cross_publish_then_cancel(event) -> None:
        if event.correlation_id == "relationship-cross-one":
            quarantine_two.publish(
                EventType.AGENT_TURN_START,
                {"agent": "orchestrator", "input": "Question sûre deux."},
            )
        elif event.correlation_id == "relationship-cross-two":
            quarantine_one.cancel_generation_events()

    bus.subscribe(EventType.AGENT_TURN_START, cross_publish_then_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(
            quarantine_one.publish,
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre une."},
        )
        publication.result(timeout=2)

    assert [event.correlation_id for event in bus.history] == [
        "relationship-cross-one",
        "relationship-cross-two",
    ]
    assert quarantine_one._drop_late_events is True
    assert quarantine_one._parent_publications == 0
    assert quarantine_two._parent_publications == 0


def test_cross_quarantine_cancel_revokes_foreign_waiter_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_one = prepare_relationship_guard(OVERLAY)
    guard_two = prepare_relationship_guard(OVERLAY)
    assert guard_one is not None
    assert guard_two is not None
    bus = EventBus(record_history=True)
    quarantine_one = routes._RelationshipRequestEventBus(
        bus.scoped("relationship-cross-canceller"),
        guard_one,
    )
    quarantine_two = routes._RelationshipRequestEventBus(
        bus.scoped("relationship-cross-foreign"),
        guard_two,
    )
    foreign_reserved = threading.Event()
    release_foreign = threading.Event()
    cancellation_returned = threading.Event()
    original_reserve = routes._RelationshipRequestEventBus._reserve_parent_publication

    def reserve_then_pause_foreign(self, event_type):
        reserved = original_reserve(self, event_type)
        if reserved and threading.current_thread().name == "relationship-cross-waiter":
            foreign_reserved.set()
            assert release_foreign.wait(timeout=3)
        return reserved

    def cancel_foreign_quarantine(event) -> None:
        if event.correlation_id == "relationship-cross-canceller":
            quarantine_two.cancel_generation_events()
            cancellation_returned.set()

    monkeypatch.setattr(
        routes._RelationshipRequestEventBus,
        "_reserve_parent_publication",
        reserve_then_pause_foreign,
    )
    bus.subscribe(EventType.AGENT_TURN_START, cancel_foreign_quarantine)

    def publish_foreign() -> None:
        threading.current_thread().name = "relationship-cross-waiter"
        quarantine_two.publish(
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question étrangère sûre."},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        foreign = pool.submit(publish_foreign)
        assert foreign_reserved.wait(timeout=2)
        canceller = pool.submit(
            quarantine_one.publish,
            EventType.AGENT_TURN_START,
            {"agent": "orchestrator", "input": "Question sûre."},
        )
        assert cancellation_returned.wait(timeout=2)
        history_size_at_cancel = len(bus.history)
        release_foreign.set()
        canceller.result(timeout=2)
        foreign.result(timeout=2)

    assert len(bus.history) == history_size_at_cancel == 1
    assert bus.history[0].correlation_id == "relationship-cross-canceller"
    assert quarantine_two._drop_late_events is True
    assert quarantine_two._parent_publications == 0


@pytest.mark.parametrize("cancel_on_publication", [1, 2])
def test_cancel_barrier_serializes_cross_reentrant_publications(
    monkeypatch: pytest.MonkeyPatch,
    cancel_on_publication: int,
) -> None:
    from openjarvis.core.events import Event

    guard = prepare_relationship_guard(OVERLAY)
    assert guard is not None
    publications_reserved = threading.Barrier(2)
    cancellation_returned = threading.Event()
    delegate_calls: list[str] = []
    quarantine = None

    original_reserve = routes._RelationshipRequestEventBus._reserve_parent_publication

    def reserve_together(self, event_type):
        reserved = original_reserve(self, event_type)
        if reserved:
            publications_reserved.wait(timeout=2)
        return reserved

    monkeypatch.setattr(
        routes._RelationshipRequestEventBus,
        "_reserve_parent_publication",
        reserve_together,
    )

    class CrossCancellingDelegate:
        @property
        def history(self):
            return []

        def clear_history(self) -> None:
            return None

        def subscribe(self, event_type, callback) -> None:
            del event_type, callback

        def unsubscribe(self, event_type, callback) -> None:
            del event_type, callback

        def publish(self, event_type, data=None):
            del data
            delegate_calls.append(threading.current_thread().name)
            assert quarantine is not None
            if len(delegate_calls) == cancel_on_publication:
                quarantine.cancel_generation_events()
                cancellation_returned.set()
            return Event(
                event_type=event_type,
                timestamp=time.time(),
                data={},
                correlation_id="cross-reentrant",
            )

    quarantine = routes._RelationshipRequestEventBus(
        CrossCancellingDelegate(),
        guard,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        publications = [
            pool.submit(
                quarantine.publish,
                EventType.AGENT_TURN_START,
                {"agent": "orchestrator", "input": f"Question sûre {index}."},
            )
            for index in range(2)
        ]
        for publication in publications:
            publication.result(timeout=3)

    assert cancellation_returned.is_set()
    assert len(delegate_calls) == cancel_on_publication
    assert quarantine._finalized is True
    assert quarantine._drop_late_events is True
    assert quarantine._parent_publications == 0
