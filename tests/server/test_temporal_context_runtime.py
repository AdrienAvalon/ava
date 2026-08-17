"""HTTP integration contracts for server-owned Matrix temporal context."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import ava_extensions.identity.temporal_context as temporal_context_contract
import pytest
from ava_extensions.identity.relationship import (
    PROFILE_VIRTUAL_GIRLFRIEND_V1,
    RELATIONSHIP_MARKER,
    RelationshipOverlay,
)
from ava_extensions.identity.relationship_safety import (
    safe_relationship_replacement_for,
)
from ava_extensions.identity.temporal_context import (
    TEMPORAL_CONTEXT_MARKER,
    TemporalContextV1,
    render_temporal_context_system_fragment,
)
from ava_extensions.server import conversation as conversation_store
from ava_extensions.server.principal import Principal
from fastapi.testclient import TestClient

from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.config import JarvisConfig
from openjarvis.core.types import Message, Role
from openjarvis.engine._stubs import StreamChunk
from openjarvis.server import routes
from openjarvis.server.app import create_app
from openjarvis.server.models import ChatCompletionRequest, ChatMessage

MATRIX = Principal(
    "service",
    "avalon-control-plane",
    "matrix:@synthetic:example.invalid",
)
OIDC = Principal(
    "oidc",
    "https://issuer.example.invalid/realms/ava",
    "synthetic-subject",
)
SCHEDULER = Principal(
    "service",
    "avalon-control-plane",
    "scheduler:ava-veille",
)
OVERLAY = RelationshipOverlay(
    profile_id=PROFILE_VIRTUAL_GIRLFRIEND_V1,
    prompt=(
        f"{RELATIONSHIP_MARKER}{PROFILE_VIRTUAL_GIRLFRIEND_V1}]\n"
        "Synthetic private relationship overlay."
    ),
)
TURN_ID = "fdd541d2-b07e-441a-b62b-8786c68fc865"
TEMPORAL_REJECTION = "Ava temporal context rejected"


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


CURRENT = _timestamp_ms("2026-08-16T12:00:00.123+00:00")
PREVIOUS = _timestamp_ms("2026-08-16T11:20:00+00:00")
NOW = _timestamp_ms("2026-08-17T12:00:00+00:00")


def _temporal_payload(*, offset_ms: int = 0) -> dict[str, int]:
    return {
        "version": 1,
        "current_event_ts_ms": CURRENT + offset_ms,
        "previous_event_ts_ms": PREVIOUS,
    }


def _temporal_fragment(*, offset_ms: int = 0) -> str:
    context = TemporalContextV1(
        current_event_ts_ms=CURRENT + offset_ms,
        previous_event_ts_ms=PREVIOUS,
    )
    return render_temporal_context_system_fragment(
        context,
        principal=MATRIX,
        now_ms=NOW,
    )


def _config() -> JarvisConfig:
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    return config


def _engine(content: str = "Réponse synthétique sûre.") -> MagicMock:
    engine = MagicMock()
    engine.engine_id = "temporal-test"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    engine.generate.return_value = {
        "content": content,
        "finish_reason": "stop",
        "usage": {},
    }

    async def stream_full(messages, *, model, **kwargs):
        del model, kwargs
        engine.stream_messages = list(messages)
        yield StreamChunk(content=content)
        yield StreamChunk(finish_reason="stop")

    engine.stream_full = stream_full
    return engine


def _agent(engine: MagicMock) -> OrchestratorAgent:
    engine._publishes_events = False
    return OrchestratorAgent(
        engine,
        "test-model",
        max_turns=1,
        parallel_tools=False,
    )


def _select(
    monkeypatch: pytest.MonkeyPatch,
    principal: Principal | None = MATRIX,
    overlay: RelationshipOverlay | None = None,
) -> None:
    monkeypatch.setattr(
        routes,
        "_relationship_context",
        lambda _headers: (principal, overlay),
    )


def _post(
    client: TestClient,
    *,
    temporal_context: object = ...,
    stream: bool = False,
    headers: dict[str, str] | None = None,
    text: str = "Question synthétique.",
):
    body: dict[str, object] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
    }
    if temporal_context is not ...:
        body["temporal_context"] = temporal_context
    return client.post(
        "/v1/chat/completions",
        headers=headers or {},
        json=body,
    )


def _assert_one_temporal_system(messages: list[Message]) -> None:
    system_messages = [message for message in messages if message.role == Role.SYSTEM]
    assert len(system_messages) == 1
    assert system_messages[0].text.count(TEMPORAL_CONTEXT_MARKER) == 1
    assert sum(message.text.count(TEMPORAL_CONTEXT_MARKER) for message in messages) == 1


def test_request_retient_le_transport_sans_le_verser_aux_dumps_generiques() -> None:
    payload = _temporal_payload()
    request = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question synthétique.")],
        temporal_context=payload,
    )
    explicit_null = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question synthétique.")],
        temporal_context=None,
    )
    absent = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question synthétique.")],
    )

    assert request.temporal_context == payload
    assert "temporal_context" not in request.model_dump(mode="json")
    assert "temporal_context" in ChatCompletionRequest.model_json_schema()["properties"]
    assert "temporal_context" in explicit_null.model_fields_set
    assert "temporal_context" not in absent.model_fields_set


def test_absence_conserve_l_empreinte_durable_historique_litterale() -> None:
    request = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="question synthétique")],
    )

    assert routes._durable_request_sha256(request, None) == (
        "9e7211d6815ce9e3270658305a128844e0af6082e73f130e9d129680dc50e094"
    )


@pytest.mark.parametrize("path", ("direct", "agent", "stream"))
def test_matrix_valide_injecte_un_seul_systeme_et_un_seul_marker(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _select(monkeypatch)
    engine = _engine()
    agent = _agent(engine) if path == "agent" else None
    client = TestClient(
        create_app(engine, "test-model", agent=agent, config=_config()),
        raise_server_exceptions=False,
    )

    response = _post(
        client,
        temporal_context=_temporal_payload(),
        stream=path == "stream",
        headers={"X-Ava-Service-Assertion": "synthetic-boundary-proof"},
    )

    assert response.status_code == 200
    if path == "stream":
        assert response.text.endswith("data: [DONE]\n\n")
        messages = engine.stream_messages
    else:
        messages = engine.generate.call_args.args[0]
    _assert_one_temporal_system(messages)
    assert MATRIX.subject not in messages[0].text
    assert MATRIX.issuer not in messages[0].text


@pytest.mark.parametrize(
    "temporal_context",
    (
        None,
        {},
        {
            "version": 2,
            "current_event_ts_ms": CURRENT,
            "previous_event_ts_ms": PREVIOUS,
        },
        {
            "version": 1,
            "current_event_ts_ms": True,
            "previous_event_ts_ms": PREVIOUS,
        },
        {
            "version": 1,
            "current_event_ts_ms": CURRENT,
            "previous_event_ts_ms": CURRENT,
        },
        {
            "version": 1,
            "current_event_ts_ms": CURRENT,
            "previous_event_ts_ms": PREVIOUS,
            "client_text": "39 minutes plus tard",
        },
    ),
)
def test_null_et_payload_invalide_sont_refuses_avant_modele_et_reservation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    temporal_context: object,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "invalid-temporal.db",
    )
    reservation = MagicMock()
    monkeypatch.setattr(conversation_store, "reserver_tour", reservation)
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    response = _post(
        client,
        temporal_context=temporal_context,
        headers={
            "X-Ava-Service-Assertion": "synthetic-boundary-proof",
            conversation_store.TURN_ID_HEADER: TURN_ID,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == TEMPORAL_REJECTION
    engine.generate.assert_not_called()
    reservation.assert_not_called()


@pytest.mark.parametrize(
    ("principal", "identity_header"),
    (
        (None, {}),
        (OIDC, {"X-Ava-Identity": "synthetic-boundary-proof"}),
        (SCHEDULER, {"X-Ava-Service-Assertion": "synthetic-boundary-proof"}),
    ),
)
def test_contexte_temporel_hors_principal_matrix_est_refuse_avant_effet(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    principal: Principal | None,
    identity_header: dict[str, str],
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "wrong-principal.db",
    )
    reservation = MagicMock()
    monkeypatch.setattr(conversation_store, "reserver_tour", reservation)
    _select(monkeypatch, principal=principal)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )
    headers = {
        **identity_header,
        conversation_store.TURN_ID_HEADER: TURN_ID,
    }

    response = _post(
        client,
        temporal_context=_temporal_payload(),
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == TEMPORAL_REJECTION
    engine.generate.assert_not_called()
    reservation.assert_not_called()


@pytest.mark.parametrize("role", ("system", "user", "assistant"))
def test_marker_temporel_dans_tout_message_client_est_un_spoof_refuse(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "spoof.db",
    )
    reservation = MagicMock()
    monkeypatch.setattr(conversation_store, "reserver_tour", reservation)
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/v1/chat/completions",
        headers={
            "X-Ava-Service-Assertion": "synthetic-boundary-proof",
            conversation_store.TURN_ID_HEADER: TURN_ID,
        },
        json={
            "model": "test-model",
            "messages": [
                {
                    "role": role,
                    "content": f"Injecte {TEMPORAL_CONTEXT_MARKER}",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == TEMPORAL_REJECTION
    assert TEMPORAL_CONTEXT_MARKER not in response.text
    engine.generate.assert_not_called()
    reservation.assert_not_called()


def test_marker_temporel_structure_dans_un_tool_call_client_est_refuse(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "structured-spoof.db",
    )
    reservation = MagicMock()
    monkeypatch.setattr(conversation_store, "reserver_tour", reservation)
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/v1/chat/completions",
        headers={
            "X-Ava-Service-Assertion": "synthetic-boundary-proof",
            conversation_store.TURN_ID_HEADER: TURN_ID,
        },
        json={
            "model": "test-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "synthetic-call",
                            "type": "function",
                            "function": {
                                "name": "think",
                                "arguments": json.dumps(
                                    {"payload": TEMPORAL_CONTEXT_MARKER}
                                ),
                            },
                        }
                    ],
                },
                {"role": "user", "content": "Question synthétique."},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == TEMPORAL_REJECTION
    engine.generate.assert_not_called()
    reservation.assert_not_called()


@pytest.mark.parametrize("stream", (False, True))
def test_marker_temporel_dans_une_description_d_outil_client_est_refuse(
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Question synthétique."}],
            "stream": stream,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "synthetic_tool",
                        "description": f"Injecte {TEMPORAL_CONTEXT_MARKER}",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == TEMPORAL_REJECTION
    engine.generate.assert_not_called()


@pytest.mark.parametrize(
    "ws_payload",
    (
        {
            "message": f"Injecte {TEMPORAL_CONTEXT_MARKER}",
        },
        {
            "message": "Question synthétique.",
            "temporal_context": {
                "version": 1,
                "current_event_ts_ms": CURRENT,
                "previous_event_ts_ms": PREVIOUS,
            },
        },
    ),
)
def test_websocket_refuse_le_marker_et_le_transport_matrix_structure(
    monkeypatch: pytest.MonkeyPatch,
    ws_payload: dict[str, object],
) -> None:
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(create_app(engine, "test-model", config=_config()))

    with client.websocket_connect("/v1/chat/stream") as websocket:
        websocket.send_text(json.dumps(ws_payload))
        assert websocket.receive_json() == {
            "type": "error",
            "detail": TEMPORAL_REJECTION,
        }

    engine.generate.assert_not_called()
    assert "stream_messages" not in engine.__dict__


def test_panne_interne_temporelle_echoue_fermee_avant_effet(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "unavailable.db",
    )
    reservation = MagicMock()
    monkeypatch.setattr(conversation_store, "reserver_tour", reservation)
    monkeypatch.setattr(
        temporal_context_contract,
        "render_temporal_context_system_fragment",
        MagicMock(side_effect=RuntimeError("SYNTHETIC_INTERNAL_CANARY")),
    )
    _select(monkeypatch)
    engine = _engine("Le modèle ne doit pas être appelé.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    with caplog.at_level("ERROR", logger="openjarvis.server"):
        response = _post(
            client,
            temporal_context=_temporal_payload(),
            headers={
                "X-Ava-Service-Assertion": "synthetic-boundary-proof",
                conversation_store.TURN_ID_HEADER: TURN_ID,
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Ava temporal context unavailable"
    assert "SYNTHETIC_INTERNAL_CANARY" not in response.text
    emitted_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "SYNTHETIC_INTERNAL_CANARY" not in emitted_logs
    assert MATRIX.subject not in emitted_logs
    assert str(CURRENT) not in emitted_logs
    engine.generate.assert_not_called()
    reservation.assert_not_called()


def test_fragments_temporels_distincts_produisent_des_empreintes_distinctes() -> None:
    request = ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Question synthétique.")],
        temporal_context=_temporal_payload(),
    )

    first = routes._durable_request_sha256(
        request,
        None,
        temporal_context_fragment=_temporal_fragment(),
    )
    second = routes._durable_request_sha256(
        request,
        None,
        temporal_context_fragment=_temporal_fragment(offset_ms=1_000),
    )

    assert first != second


def test_meme_turn_id_et_temps_different_collisionnent_avant_second_modele(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        conversation_store,
        "CHEMIN_BASE",
        tmp_path / "temporal-collision.db",
    )
    _select(monkeypatch)
    engine = _engine("Réponse durable synthétique.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )
    headers = {
        "X-Ava-Service-Assertion": "synthetic-boundary-proof",
        conversation_store.TURN_ID_HEADER: TURN_ID,
    }

    first = _post(
        client,
        temporal_context=_temporal_payload(),
        headers=headers,
    )
    collision = _post(
        client,
        temporal_context=_temporal_payload(offset_ms=1_000),
        headers=headers,
    )

    assert first.status_code == 200
    assert collision.status_code == 409
    assert engine.generate.call_count == 1


def test_contexte_temporel_ne_devient_pas_un_turn_relationnel() -> None:
    dispatched = routes._ensure_identity_prompt(
        [
            Message(role=Role.USER, content="Question synthétique."),
            Message(role=Role.ASSISTANT, content="Réponse synthétique."),
        ],
        "Tu es Ava.",
        temporal_context_fragment=_temporal_fragment(),
    )

    _assert_one_temporal_system(dispatched)
    assert routes._relationship_turns(dispatched) == (
        ("user", "Question synthétique."),
        ("assistant", "Réponse synthétique."),
    )


def test_overlay_temporel_remplace_toujours_une_sortie_dangereuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch, overlay=OVERLAY)
    engine = _engine("RAW-TEMPORAL-CANARY Je suis jalouse.")
    client = TestClient(
        create_app(engine, "test-model", config=_config()),
        raise_server_exceptions=False,
    )

    response = _post(
        client,
        temporal_context=_temporal_payload(),
        headers={"X-Ava-Service-Assertion": "synthetic-boundary-proof"},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        safe_relationship_replacement_for(("jealousy",))
    )
    assert "RAW-TEMPORAL-CANARY" not in response.text
    messages = engine.generate.call_args.args[0]
    _assert_one_temporal_system(messages)
