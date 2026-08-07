"""Tests for server Pydantic models."""

from __future__ import annotations

import pytest

pydantic = pytest.importorskip("pydantic")

from openjarvis.server.models import (  # noqa: E402
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    DeltaMessage,
    ModelListResponse,
    ModelObject,
    StreamChoice,
    UsageInfo,
)


class TestChatCompletionRequest:
    def test_minimal(self):
        req = ChatCompletionRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Hello")],
        )
        assert req.model == "test-model"
        assert len(req.messages) == 1
        assert req.temperature == 0.7
        assert req.stream is False

    def test_with_options(self):
        req = ChatCompletionRequest(
            model="test",
            messages=[ChatMessage(role="user", content="Hi")],
            temperature=0.1,
            max_tokens=256,
            stream=True,
        )
        assert req.temperature == 0.1
        assert req.max_tokens == 256
        assert req.stream is True


class TestChatCompletionResponse:
    def test_defaults(self):
        resp = ChatCompletionResponse(
            model="test",
            choices=[Choice(message=ChoiceMessage(content="Hi"))],
        )
        assert resp.object == "chat.completion"
        assert resp.id.startswith("chatcmpl-")
        assert resp.created > 0
        assert len(resp.choices) == 1

    def test_usage(self):
        resp = ChatCompletionResponse(
            model="test",
            choices=[Choice(message=ChoiceMessage(content="Hi"))],
            usage=UsageInfo(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )
        assert resp.usage.total_tokens == 8


class TestChatCompletionChunk:
    def test_defaults(self):
        chunk = ChatCompletionChunk(
            id="test-id",
            model="test",
            choices=[StreamChoice(delta=DeltaMessage(content="Hi"))],
        )
        assert chunk.object == "chat.completion.chunk"
        assert chunk.choices[0].delta.content == "Hi"


class TestModelListResponse:
    def test_empty(self):
        resp = ModelListResponse()
        assert resp.object == "list"
        assert resp.data == []

    def test_with_models(self):
        resp = ModelListResponse(
            data=[
                ModelObject(id="model-a"),
                ModelObject(id="model-b"),
            ]
        )
        assert len(resp.data) == 2
        assert resp.data[0].id == "model-a"
        assert resp.data[0].object == "model"


class TestChatMessage:
    def test_user_message(self):
        msg = ChatMessage(role="user", content="Hello")
        assert msg.role == "user"

    def test_with_tool_call_id(self):
        msg = ChatMessage(role="tool", content="result", tool_call_id="abc")
        assert msg.tool_call_id == "abc"


def test_la_reponse_porte_un_TRACE_ID_notable():
    """⚠ LE CHAÎNON QUI MANQUAIT À TOUTE LA BOUCLE D'APPRENTISSAGE.

    Mesure du 2026-08-07 : **1 trace notée sur 327**. Ce n'était ni un manque de
    stockage, ni un manque de route (`POST /v1/feedback` existe et fonctionne), ni un
    désintérêt — le verdict machine est riche (289 `completed`, 33 `recovered`). C'est
    que l'identifiant de trace ne SORTAIT JAMAIS du processus : personne ne pouvait
    noter, faute de savoir quoi nommer.

    ⚠ Le défaut par défaut est `None`, JAMAIS la chaîne vide : une chaîne vide se
      passerait pour un identifiant valide et produirait des 404 inexplicables côté
      notation.
    ⚠ `ava-ci` (la CI bloquante de cette branche) ne couvre PAS `tests/server/` — elle
      ne lance que `ava_extensions/tests/` et `tests/speech`. Ce test est donc exécuté
      par `ci.yml`, qui ne tourne pas sur `ava-main`. À lancer à la main tant que ce
      périmètre n'est pas élargi : ne pas le croire couvert.
    """
    from openjarvis.server.models import ChatCompletionResponse

    vide = ChatCompletionResponse(model="x").model_dump()
    assert "trace_id" in vide
    assert vide["trace_id"] is None

    porte = ChatCompletionResponse(model="x", trace_id="78e6955f6c864012").model_dump()
    assert porte["trace_id"] == "78e6955f6c864012"
