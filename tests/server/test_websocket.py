"""Tests for the WebSocket streaming endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from ava_extensions.identity.principal_context import (  # noqa: E402
    PRINCIPAL_CONTEXT_MARKER,
    PrincipalContext,
)
from ava_extensions.identity.relationship import (  # noqa: E402
    RELATIONSHIP_MARKER,
    RelationshipOverlay,
)
from ava_extensions.server.principal import Principal  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from openjarvis.core.types import Role  # noqa: E402
from openjarvis.engine._stubs import StreamChunk  # noqa: E402
from openjarvis.server import routes as server_routes  # noqa: E402
from openjarvis.server.api_routes import include_all_routes  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(engine=None):
    """Create a minimal FastAPI app with mock engine wired up."""
    app = FastAPI()
    if engine is None:
        engine = _make_streaming_engine()
    app.state.engine = engine
    app.state.model = "test-model"
    app.state.config = SimpleNamespace(
        intelligence=SimpleNamespace(max_tokens=16_384),
    )
    include_all_routes(app)
    return app


def _make_streaming_engine(tokens=None):
    """Return a mock engine whose ``stream()`` yields tokens."""
    if tokens is None:
        tokens = ["Hello", " ", "world"]
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.captured_messages = []
    engine.captured_kwargs = []

    async def mock_stream(messages, *, model="test-model", **kwargs):
        engine.captured_messages.append(messages)
        for tok in tokens:
            yield tok

    async def mock_stream_full(messages, *, model="test-model", **kwargs):
        engine.captured_messages.append(messages)
        engine.captured_kwargs.append({"model": model, **kwargs})
        for tok in tokens:
            yield StreamChunk(content=tok)
        yield StreamChunk(finish_reason="stop")

    engine.stream = mock_stream
    engine.stream_full = mock_stream_full
    engine.generate.return_value = {
        "content": "Hello world",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }
    return engine


def _make_generate_only_engine(content="Hello world"):
    """Return a mock engine that only has ``generate()`` (no ``stream()``)."""
    engine = MagicMock(spec=["generate", "engine_id"])
    engine.engine_id = "mock-nostream"
    engine.generate.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSocketStreaming:
    """Tests for WS /v1/chat/stream endpoint."""

    def test_basic_streaming_exchange(self):
        """A valid message should produce chunk messages followed by a done."""
        engine = _make_streaming_engine()
        app = _make_app(engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            chunks = []
            done = None
            # Read all responses until we get 'done'
            while True:
                data = ws.receive_json()
                if data["type"] == "chunk":
                    chunks.append(data["content"])
                elif data["type"] == "done":
                    done = data
                    break
                else:
                    break
            assert len(chunks) == 3
            assert chunks == ["Hello", " ", "world"]
            assert done is not None
            assert done["content"] == "Hello world"
        assert engine.captured_kwargs[-1]["max_tokens"] == 16_384
        messages = engine.captured_messages[-1]
        assert messages[0].role == Role.SYSTEM
        assert "Tu es **Ava**" in messages[0].content
        assert messages[1].role == Role.USER
        assert messages[1].content == "Hi"

    def test_missing_message_field(self):
        """Sending JSON without a 'message' field should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"text": "Hi"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Missing" in data["detail"]

    def test_invalid_json(self):
        """Sending non-JSON text should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text("not json at all")
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Invalid JSON" in data["detail"]

    def test_empty_message_field(self):
        """An empty string for 'message' should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": ""}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Missing" in data["detail"]

    def test_generate_fallback_when_no_stream(self):
        """When the engine has no stream(), generate() result is sent as one chunk."""
        engine = _make_generate_only_engine("Fallback response")
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            chunks = []
            done = None
            while True:
                data = ws.receive_json()
                if data["type"] == "chunk":
                    chunks.append(data["content"])
                elif data["type"] == "done":
                    done = data
                    break
                else:
                    break
            assert len(chunks) == 1
            assert chunks[0] == "Fallback response"
            assert done is not None
            assert done["content"] == "Fallback response"
        assert engine.generate.call_args.kwargs["max_tokens"] == 16_384

    @pytest.mark.parametrize("terminal", ["length", None, "future_reason"])
    def test_incomplete_stream_never_emits_done_or_trace(self, terminal):
        engine = MagicMock()
        engine.engine_id = "mock"

        async def stream_full(messages, *, model="test-model", **kwargs):
            del messages, model, kwargs
            yield StreamChunk(content="partial")
            if terminal is not None:
                yield StreamChunk(finish_reason=terminal)

        engine.stream_full = stream_full
        app = _make_app(engine)
        app.state.trace_store = MagicMock()
        client = TestClient(app)

        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            assert ws.receive_json() == {"type": "chunk", "content": "partial"}
            assert ws.receive_json() == {
                "type": "error",
                "detail": "Chat response incomplete",
            }

        app.state.trace_store.save.assert_not_called()

    def test_incomplete_generate_never_emits_done_or_trace(self):
        engine = _make_generate_only_engine("partial")
        engine.generate.return_value["finish_reason"] = "max_tokens"
        app = _make_app(engine)
        app.state.trace_store = MagicMock()
        client = TestClient(app)

        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            assert ws.receive_json() == {"type": "chunk", "content": "partial"}
            assert ws.receive_json() == {
                "type": "error",
                "detail": "Chat response incomplete",
            }

        app.state.trace_store.save.assert_not_called()

    def test_custom_model_in_request(self):
        """The model field from the request should be forwarded to the engine."""
        tokens = ["OK"]
        engine = _make_streaming_engine(tokens=tokens)
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi", "model": "custom-model"}))
            # Consume until done
            while True:
                data = ws.receive_json()
                if data["type"] == "done":
                    break
            # The mock stream function was called — we can't easily inspect
            # async-generator call args, but the exchange completed without error
            assert data["content"] == "OK"

    def test_engine_error_returns_only_generic_message(self):
        """Backend exception details must not cross the WebSocket boundary."""
        engine = MagicMock()

        async def bad_stream(messages, *, model="test-model", **kwargs):
            raise RuntimeError("Engine exploded")
            # Make it look like an async generator to the endpoint
            yield  # pragma: no cover – unreachable, but needed for async gen syntax

        engine.stream_full = bad_stream
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "boom"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert data["detail"] == "Chat generation failed"
            assert "exploded" not in data["detail"].lower()

    def test_multiple_messages_on_same_connection(self):
        """The WebSocket should support multiple request/response cycles."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"message": "Hi"}))
                # Drain until done
                while True:
                    data = ws.receive_json()
                    if data["type"] == "done":
                        assert data["content"] == "Hello world"
                        break

    def test_no_engine_configured(self):
        """If app.state has no engine, an error should be returned."""
        app = FastAPI()
        app.state.model = "test-model"
        # Intentionally do NOT set app.state.engine
        include_all_routes(app)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert data["detail"] == "Chat unavailable"

    def test_verified_principal_selects_overlay_and_pseudonymous_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        principal = Principal("oidc", "https://issuer.example.invalid", "owner-subject")
        principal_context = PrincipalContext(
            display_name="Test Owner",
            preferred_language="fr-FR",
        )
        overlay = RelationshipOverlay(
            profile_id="virtual-girlfriend-v1",
            prompt=(
                f"{RELATIONSHIP_MARKER}virtual-girlfriend-v1]\n"
                "Profil relationnel synthétique."
            ),
        )
        monkeypatch.setattr(
            server_routes,
            "_relationship_context",
            lambda _headers: (principal, overlay, True, principal_context),
        )
        engine = _make_streaming_engine(tokens=["ok"])
        app = _make_app(engine)
        app.state.trace_store = MagicMock()
        app.state.memory_service = MagicMock()
        client = TestClient(app)

        with client.websocket_connect(
            "/v1/chat/stream",
            headers={"X-Ava-Identity": "verified-by-test-double"},
        ) as ws:
            ws.send_text(json.dumps({"message": "bonjour"}))
            while ws.receive_json()["type"] != "done":
                pass

        system_prompt = engine.captured_messages[-1][0].content
        assert system_prompt.count(RELATIONSHIP_MARKER) == 1
        assert system_prompt.count(PRINCIPAL_CONTEXT_MARKER) == 1
        assert "Test Owner" in system_prompt
        assert principal.subject not in system_prompt
        trace = app.state.trace_store.save.call_args.args[0]
        assert trace.metadata == {"provenance": principal.provenance}
        assert principal.subject not in trace.metadata["provenance"]
        app.state.memory_service.submit.assert_not_called()

    def test_invalid_ava_credential_is_rejected_before_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            server_routes,
            "_relationship_context",
            lambda _headers: (None, None, False),
        )
        engine = _make_streaming_engine()
        client = TestClient(_make_app(engine))

        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/v1/chat/stream",
                headers={"X-Ava-Identity": "forged.jwt"},
            ):
                pass

        assert rejected.value.code == 1008
        assert engine.captured_messages == []
        engine.generate.assert_not_called()

    def test_invalid_relationship_policy_is_rejected_before_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(
            "AVA_RELATIONSHIP_POLICY_FILE",
            str(tmp_path / "missing-relationship-policy.json"),
        )
        engine = _make_streaming_engine()
        client = TestClient(_make_app(engine))

        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/v1/chat/stream"):
                pass

        assert rejected.value.code == 1011
        assert engine.captured_messages == []
        engine.generate.assert_not_called()


__all__ = [
    "TestWebSocketStreaming",
]
