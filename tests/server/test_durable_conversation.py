"""Durability and idempotency contracts for authenticated Ava chat turns."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import time
from unittest.mock import MagicMock

import pytest
from ava_extensions.server import conversation as conversation_store
from ava_extensions.server.principal import Principal
from fastapi import HTTPException
from fastapi.testclient import TestClient

from openjarvis.core.config import JarvisConfig
from openjarvis.server import routes
from openjarvis.server.app import create_app
from openjarvis.server.models import ChatCompletionRequest, ChatMessage

TURN_ID = "4d593ddf-cf92-4d85-9d5d-68a961f5827b"
PRINCIPAL_A = Principal("oidc", "https://issuer.example.invalid", "subject-a")
PRINCIPAL_B = Principal("oidc", "https://issuer.example.invalid", "subject-b")


def _config() -> JarvisConfig:
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    return config


def _engine(content: str = "synthetic reply") -> MagicMock:
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]
    engine.generate.return_value = {
        "content": content,
        "finish_reason": "stop",
        "usage": {},
    }
    return engine


def _client(engine: MagicMock, *, memory_service=None) -> TestClient:
    return TestClient(
        create_app(
            engine,
            "test-model",
            config=_config(),
            memory_service=memory_service,
        ),
        raise_server_exceptions=False,
    )


def _post(
    client: TestClient,
    text: str = "synthetic question",
    *,
    turn_id: str = TURN_ID,
):
    return client.post(
        "/v1/chat/completions",
        headers={conversation_store.TURN_ID_HEADER: turn_id},
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        },
    )


def _select(monkeypatch, principal=PRINCIPAL_A) -> None:
    monkeypatch.setattr(
        routes,
        "_relationship_context",
        lambda _headers: (principal, None),
    )


def _request_sha256(text: str = "synthetic question") -> str:
    return routes._durable_request_sha256(
        ChatCompletionRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content=text)],
        ),
        None,
    )


def test_waiter_observes_pending_to_abandoned_as_terminal() -> None:
    request_sha256 = "a" * 64
    pending = conversation_store.TurnJournalEntry(
        turn_id=TURN_ID,
        user_text="synthetic question",
        assistant_text=None,
        timestamp=time.time(),
        state="pending",
        request_sha256=request_sha256,
    )
    abandoned = conversation_store.TurnJournalEntry(
        turn_id=TURN_ID,
        user_text="",
        assistant_text=None,
        timestamp=pending.timestamp,
        state="abandoned",
        abandon_reason="assistant_response_too_large",
        request_sha256=request_sha256,
    )
    store = MagicMock()
    store.lire_statut_tour.side_effect = [pending, abandoned]

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            routes._resolve_existing_durable_turn(
                store,
                PRINCIPAL_A.conversation_key,
                TURN_ID,
                "synthetic question",
                request_sha256,
            )
        )

    assert getattr(caught.value, "status_code", None) == 410


def test_retry_apres_restart_rejoue_le_tour_sans_rappeler_le_modele(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    first_engine = _engine("first committed reply")
    first_engine.generate.return_value["finish_reason"] = "length"
    first_engine.generate.return_value["usage"] = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    with _client(first_engine) as first_client:
        first = _post(first_client)
    assert first.status_code == 200
    assert first_engine.generate.call_count == 1

    # A new app/client simulates the daemon reopening the same SQLite file.
    second_engine = _engine("must never be generated")
    with _client(second_engine) as second_client:
        replay = _post(second_client)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["choices"][0]["finish_reason"] == "length"
    assert replay.json()["usage"]["total_tokens"] == 18
    assert second_engine.generate.call_count == 0
    assert [
        row["texte"] for row in conversation_store.lire(PRINCIPAL_A.conversation_key)
    ] == ["synthetic question", "first committed reply"]


def test_collision_de_question_est_refusee_avant_generation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine()
    with _client(engine) as client:
        assert _post(client, "first question").status_code == 200
        collision = _post(client, "different question")

    assert collision.status_code == 409
    assert engine.generate.call_count == 1
    assert [
        row["texte"] for row in conversation_store.lire(PRINCIPAL_A.conversation_key)
    ] == ["first question", "synthetic reply"]


def test_collision_de_contexte_ou_modele_est_refusee_avant_generation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine("reply bound to context a")
    with _client(engine) as client:
        first = client.post(
            "/v1/chat/completions",
            headers={conversation_store.TURN_ID_HEADER: TURN_ID},
            json={
                "model": "model-a",
                "messages": [
                    {"role": "assistant", "content": "context a"},
                    {"role": "user", "content": "same question"},
                ],
                "temperature": 0.1,
            },
        )
        collision = client.post(
            "/v1/chat/completions",
            headers={conversation_store.TURN_ID_HEADER: TURN_ID},
            json={
                "model": "model-b",
                "messages": [
                    {"role": "assistant", "content": "context b"},
                    {"role": "user", "content": "same question"},
                ],
                "temperature": 0.9,
            },
        )

    assert first.status_code == 200
    assert collision.status_code == 409
    assert engine.generate.call_count == 1


def test_generation_en_echec_ne_persiste_aucune_question(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine()
    engine.generate.side_effect = RuntimeError("synthetic model failure")

    with _client(engine) as client:
        response = _post(client)

    assert response.status_code == 500
    assert conversation_store.lire_tour(PRINCIPAL_A.conversation_key, TURN_ID) is None
    entry = conversation_store.lire_statut_tour(PRINCIPAL_A.conversation_key, TURN_ID)
    assert entry is not None
    assert entry.state == "pending"


def test_panne_de_commit_ne_publie_pas_la_reponse_en_memoire_legacy(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)

    class SpyMemory:
        def __init__(self) -> None:
            self.submissions: list[tuple[str, str]] = []

        def submit(self, user: str, assistant: str) -> bool:
            self.submissions.append((user, assistant))
            return True

        def stop(self, timeout: float = 2.0) -> None:
            del timeout

    memory = SpyMemory()

    def fail_commit(*_args, **_kwargs):
        raise conversation_store.ConversationStorageError("synthetic disk failure")

    monkeypatch.setattr(conversation_store, "finaliser_tour", fail_commit)
    with _client(_engine(), memory_service=memory) as client:
        response = _post(client)

    assert response.status_code == 503
    assert memory.submissions == []
    entry = conversation_store.lire_statut_tour(PRINCIPAL_A.conversation_key, TURN_ID)
    assert entry is not None
    assert entry.state == "pending"


def test_le_meme_uuid_ne_melange_jamais_deux_principals(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    selected = {"principal": PRINCIPAL_A}
    monkeypatch.setattr(
        routes,
        "_relationship_context",
        lambda _headers: (selected["principal"], None),
    )
    engine = _engine("reply a")
    with _client(engine) as client:
        assert _post(client, "question a").status_code == 200
        selected["principal"] = PRINCIPAL_B
        engine.generate.return_value["content"] = "reply b"
        assert _post(client, "question b").status_code == 200
        selected["principal"] = PRINCIPAL_A
        replay_a = _post(client, "question a")

    assert replay_a.json()["choices"][0]["message"]["content"] == "reply a"
    assert [
        row["texte"] for row in conversation_store.lire(PRINCIPAL_A.conversation_key)
    ] == [
        "question a",
        "reply a",
    ]
    assert [
        row["texte"] for row in conversation_store.lire(PRINCIPAL_B.conversation_key)
    ] == [
        "question b",
        "reply b",
    ]


def test_requetes_concurrentes_ne_dupliquent_pas_la_paire(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine("same deterministic reply")
    with _client(engine) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _index: _post(client), range(12)))

    assert all(response.status_code == 200 for response in responses)
    assert {
        response.json()["choices"][0]["message"]["content"] for response in responses
    } == {"same deterministic reply"}
    assert engine.generate.call_count == 1
    assert [
        row["texte"] for row in conversation_store.lire(PRINCIPAL_A.conversation_key)
    ] == ["synthetic question", "same deterministic reply"]


def test_header_invalide_et_stream_sont_refuses_avant_generation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine()
    with _client(engine) as client:
        invalid = _post(client, turn_id="not-a-uuid")
        streaming = client.post(
            "/v1/chat/completions",
            headers={conversation_store.TURN_ID_HEADER: TURN_ID},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "question"}],
                "stream": True,
            },
        )

    assert invalid.status_code == 422
    assert streaming.status_code == 422
    assert engine.generate.call_count == 0


def test_tour_durable_exige_que_le_dernier_message_soit_la_question_executee(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    engine = _engine("must not run")

    with _client(engine) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={conversation_store.TURN_ID_HEADER: TURN_ID},
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "durably stored question"},
                    {"role": "assistant", "content": "different actual input"},
                ],
            },
        )

    assert response.status_code == 422
    assert engine.generate.call_count == 0
    assert (
        conversation_store.lire_statut_tour(PRINCIPAL_A.conversation_key, TURN_ID)
        is None
    )


def test_pending_stale_echoue_ferme_sans_reexecuter_le_modele(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    conversation_store.reserver_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
        "synthetic question",
        request_sha256=_request_sha256(),
        horodatage=1.0,
    )
    monkeypatch.setattr(routes, "_DURABLE_PENDING_WAIT_SECONDS", 0.0)
    engine = _engine("must not run")

    with _client(engine) as client:
        response = _post(client)

    assert response.status_code == 425
    assert response.headers["Retry-After"] == "5"
    assert engine.generate.call_count == 0
    assert conversation_store.lire(PRINCIPAL_A.conversation_key) == []


def test_plafond_pending_refuse_avant_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    monkeypatch.setattr(conversation_store, "MAX_TOURS_PENDING_PAR_UTILISATEUR", 1)
    _select(monkeypatch)
    conversation_store.reserver_tour(
        PRINCIPAL_A.conversation_key,
        "6576aa72-92fd-45c0-bf38-4cb89316f31a",
        "previous ambiguous question",
        request_sha256="a" * 64,
    )
    engine = _engine("must not run")

    with _client(engine) as client:
        response = _post(client)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert engine.generate.call_count == 0


def test_reconciliation_abandonne_un_seul_pending_et_interdit_son_rejeu(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CHEMIN_BASE", database)
    conversation_store.reserver_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
        "ambiguous effect",
        request_sha256="a" * 64,
    )
    other_turn = "6576aa72-92fd-45c0-bf38-4cb89316f31a"
    conversation_store.reserver_tour(
        PRINCIPAL_B.conversation_key,
        other_turn,
        "other principal",
        request_sha256="b" * 64,
    )

    result = conversation_store.abandonner_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
    )
    assert result.abandoned is True
    assert (
        conversation_store.lire_statut_tour(PRINCIPAL_A.conversation_key, TURN_ID)
        is None
    )
    assert (
        conversation_store.lire_statut_tour(PRINCIPAL_B.conversation_key, other_turn)
        is not None
    )
    assert (
        conversation_store.abandonner_tour(
            PRINCIPAL_A.conversation_key,
            TURN_ID,
        ).abandoned
        is False
    )
    with pytest.raises(conversation_store.TurnCollisionError, match="abandoned"):
        conversation_store.reserver_tour(
            PRINCIPAL_A.conversation_key,
            TURN_ID,
            "ambiguous effect",
            request_sha256="a" * 64,
        )
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT action, length(user_text_sha256), reason FROM turn_reconciliations"
        ).fetchone()
    assert audit == ("abandoned", 64, "manual_reconciliation")


def test_longue_reponse_legitime_est_commitee_sans_pending_residuel(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    long_reply = "x" * 32_000
    engine = _engine(long_reply)

    with _client(engine) as client:
        response = _post(client)

    assert response.status_code == 200
    entry = conversation_store.lire_statut_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
    )
    assert entry is not None and entry.state == "completed"
    assert entry.assistant_text == long_reply


def test_reponse_a_la_limite_du_store_est_commitee(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    exact_reply = "x" * conversation_store.MAX_CAR_TEXTE

    with _client(_engine(exact_reply)) as client:
        response = _post(client)

    assert response.status_code == 200
    entry = conversation_store.lire_statut_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
    )
    assert entry is not None and entry.state == "completed"
    assert entry.assistant_text == exact_reply


def test_reponse_store_plus_un_abandonne_et_audite_le_tour(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CHEMIN_BASE", database)
    _select(monkeypatch)
    oversized_reply = "x" * (conversation_store.MAX_CAR_TEXTE + 1)
    engine = _engine(oversized_reply)

    with _client(engine) as client:
        response = _post(client)
        replay = _post(client)

    assert response.status_code == 502
    assert replay.status_code == 410
    assert engine.generate.call_count == 1
    entry = conversation_store.lire_statut_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
    )
    assert entry is not None and entry.state == "abandoned"
    assert entry.abandon_reason == "assistant_response_too_large"
    assert conversation_store.lire(PRINCIPAL_A.conversation_key) == []
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT reason, length(user_text_sha256), "
            "length(assistant_text_sha256) FROM turn_reconciliations"
        ).fetchone()
    assert audit == ("assistant_response_too_large", 64, 64)


def test_enveloppe_utf8_trop_grande_abandonne_et_audite_le_tour(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        conversation_store, "CHEMIN_BASE", tmp_path / "conversations.db"
    )
    _select(monkeypatch)
    # The character count is valid, but the UTF-8 JSON envelope exceeds the
    # durable byte limit.  This must become terminal instead of leaving a
    # permanently pending turn after the model has already answered.
    reply = "é" * conversation_store.MAX_CAR_TEXTE
    engine = _engine(reply)

    with _client(engine) as client:
        response = _post(client)
        replay = _post(client)

    assert response.status_code == 502
    assert replay.status_code == 410
    assert engine.generate.call_count == 1
    entry = conversation_store.lire_statut_tour(
        PRINCIPAL_A.conversation_key,
        TURN_ID,
    )
    assert entry is not None and entry.state == "abandoned"
    assert entry.abandon_reason == "response_envelope_too_large"
    assert conversation_store.lire(PRINCIPAL_A.conversation_key) == []
