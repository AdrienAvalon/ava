"""WebSocket authentication tests (issue #217).

The HTTP ``AuthMiddleware`` never sees WebSocket upgrade requests, so the
streaming endpoints (`/v1/chat/stream`, `/v1/agents/events`) must validate the
token themselves in the handshake before accepting the connection.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from ava_extensions.server.principal import OIDC_HEADER, Principal  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from openjarvis.core.events import EventBus, EventType  # noqa: E402
from openjarvis.engine._stubs import StreamChunk  # noqa: E402
from openjarvis.server.api_routes import include_all_routes  # noqa: E402
from openjarvis.server.auth_middleware import websocket_authorized  # noqa: E402
from openjarvis.server.ws_bridge import create_ws_router  # noqa: E402

EVENT_OWNER = Principal("oidc", "https://issuer.example.invalid", "event-owner")


def _ws(query=None, headers=None):
    stub = MagicMock()
    stub.query_params = query or {}
    stub.headers = headers or {}
    return stub


class TestWebsocketAuthorizedHelper:
    def test_no_key_allows_all(self):
        assert websocket_authorized(_ws(), "") is True

    def test_token_via_query(self):
        assert websocket_authorized(_ws(query={"token": "sek"}), "sek") is True

    def test_token_via_bearer_header(self):
        ws = _ws(headers={"authorization": "Bearer sek"})
        assert websocket_authorized(ws, "sek") is True

    def test_wrong_token_rejected(self):
        assert websocket_authorized(_ws(query={"token": "nope"}), "sek") is False

    def test_missing_token_rejected_when_required(self):
        assert websocket_authorized(_ws(), "sek") is False


def _make_app(api_key=""):
    app = FastAPI()
    engine = MagicMock()
    engine.engine_id = "mock"

    async def mock_stream(messages, *, model="test-model", **kwargs):
        for tok in ["hi"]:
            yield tok

    async def mock_stream_full(messages, *, model="test-model", **kwargs):
        del messages, model, kwargs
        yield StreamChunk(content="hi")
        yield StreamChunk(finish_reason="stop")

    engine.stream = mock_stream
    engine.stream_full = mock_stream_full
    app.state.engine = engine
    app.state.model = "test-model"
    app.state.config = SimpleNamespace(
        intelligence=SimpleNamespace(max_tokens=16_384),
    )
    app.state.api_key = api_key
    include_all_routes(app)
    return app


class TestChatStreamAuth:
    def test_rejected_without_token_when_key_set(self):
        client = TestClient(_make_app(api_key="secret"))
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/chat/stream") as ws:
                ws.receive_text()

    def test_rejected_with_wrong_token(self):
        client = TestClient(_make_app(api_key="secret"))
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/chat/stream?token=wrong") as ws:
                ws.receive_text()

    def test_accepted_with_correct_token(self):
        client = TestClient(_make_app(api_key="secret"))
        with client.websocket_connect("/v1/chat/stream?token=secret") as ws:
            ws.send_text(json.dumps({"message": "hi"}))
            assert ws.receive_json()["type"] in ("chunk", "done", "error")

    def test_allowed_when_no_key_configured(self):
        client = TestClient(_make_app(api_key=""))
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "hi"}))
            assert ws.receive_json()["type"] in ("chunk", "done", "error")


class TestAgentEventsAuth:
    def _app(self, api_key=""):
        app = FastAPI()
        app.state.api_key = api_key
        app.state.agent_manager = MagicMock()
        app.state.agent_manager.get_agent_for_owner.return_value = {"id": "a"}
        app.include_router(create_ws_router(EventBus()))
        return app

    def test_rejected_without_token_when_key_set(self, monkeypatch):
        from ava_extensions.server import principal as principal_module

        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: EVENT_OWNER,
        )
        client = TestClient(self._app(api_key="secret"))
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/v1/agents/events?agent_id=a",
                headers={OIDC_HEADER: "verified-by-test-double"},
            ) as ws:
                ws.receive_text()

    def test_accepted_with_correct_token(self, monkeypatch):
        from ava_extensions.server import principal as principal_module

        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: EVENT_OWNER,
        )
        bus = EventBus()
        app = FastAPI()
        app.state.api_key = "secret"
        app.state.agent_manager = MagicMock()
        app.state.agent_manager.get_agent_for_owner.return_value = {"id": "a"}
        app.include_router(create_ws_router(bus))
        client = TestClient(app)
        with client.websocket_connect(
            "/v1/agents/events?agent_id=a&token=secret",
            headers={OIDC_HEADER: "verified-by-test-double"},
        ) as ws:
            time.sleep(0.02)
            bus.publish(EventType.AGENT_TICK_START, {"agent_id": "a"})
            assert ws.receive_json()["data"]["agent_id"] == "a"
