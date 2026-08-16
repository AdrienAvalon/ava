"""Tests for webhook routes."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.server.webhook_routes import create_webhook_router


@pytest.fixture
def mock_bridge():
    bridge = MagicMock()
    bridge.handle_incoming.return_value = "Got it!"
    return bridge


class TestTwilioWebhook:
    @pytest.fixture
    def twilio_app(self, mock_bridge):
        app = FastAPI()
        router = create_webhook_router(
            bridge=mock_bridge,
            twilio_auth_token="test_token",
        )
        app.include_router(router)
        return app

    @pytest.fixture
    def twilio_client(self, twilio_app):
        return TestClient(twilio_app)

    def test_twilio_fails_closed_before_global_owner_selection(
        self,
        twilio_app,
        twilio_client,
        mock_bridge,
    ):
        manager = MagicMock()
        manager.list_agents.return_value = [
            {"id": "owner-a-agent"},
            {"id": "owner-b-agent"},
        ]
        state_bridge = MagicMock()
        engine = MagicMock()
        twilio_app.state.agent_manager = manager
        twilio_app.state.channel_bridge = state_bridge
        twilio_app.state.engine = engine

        with (
            patch(
                "openjarvis.server.webhook_routes._validate_twilio_signature",
                return_value=True,
            ) as validate_signature,
            patch("openjarvis.server.webhook_routes.asyncio.to_thread") as to_thread,
        ):
            resp = twilio_client.post(
                "/webhooks/twilio",
                data={
                    "From": "+15551234567",
                    "Body": "hello jarvis",
                    "MessageSid": "SM123",
                },
            )

        assert resp.status_code == 503
        assert resp.text == "Channel unavailable"
        validate_signature.assert_not_called()
        to_thread.assert_not_called()
        manager.list_agents.assert_not_called()
        manager.list_channel_bindings.assert_not_called()
        mock_bridge.handle_incoming.assert_not_called()
        state_bridge.handle_incoming.assert_not_called()
        assert engine.mock_calls == []

    def test_twilio_rejection_does_not_expose_signature_state(self, twilio_client):
        with patch(
            "openjarvis.server.webhook_routes._validate_twilio_signature",
            return_value=False,
        ) as validate_signature:
            resp = twilio_client.post(
                "/webhooks/twilio",
                data={
                    "From": "+15551234567",
                    "Body": "hello",
                    "MessageSid": "SM123",
                },
            )
        assert resp.status_code == 503
        assert resp.text == "Channel unavailable"
        validate_signature.assert_not_called()


class TestBlueBubblesWebhook:
    @pytest.fixture
    def bb_app(self, mock_bridge):
        app = FastAPI()
        router = create_webhook_router(
            bridge=mock_bridge,
            bluebubbles_password="bb_secret",
        )
        app.include_router(router)
        return app

    @pytest.fixture
    def bb_client(self, bb_app):
        return TestClient(bb_app)

    def test_valid_bluebubbles_webhook(self, bb_client, mock_bridge):
        resp = bb_client.post(
            "/webhooks/bluebubbles",
            json={
                "type": "new-message",
                "data": {
                    "handle": {"address": "user@icloud.com"},
                    "text": "hello from imessage",
                    "guid": "msg-123",
                },
            },
            headers={"Authorization": "bb_secret"},
        )
        assert resp.status_code == 200

    def test_wrong_password_rejected(self, bb_client):
        resp = bb_client.post(
            "/webhooks/bluebubbles",
            json={"type": "new-message", "data": {}},
            headers={"Authorization": "wrong_password"},
        )
        assert resp.status_code == 403


class TestWhatsAppWebhook:
    @pytest.fixture
    def wa_app(self, mock_bridge):
        app = FastAPI()
        router = create_webhook_router(
            bridge=mock_bridge,
            whatsapp_verify_token="wa_verify_123",
            whatsapp_app_secret="wa_secret",
        )
        app.include_router(router)
        return app

    @pytest.fixture
    def wa_client(self, wa_app):
        return TestClient(wa_app)

    def test_verification_challenge(self, wa_client):
        resp = wa_client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wa_verify_123",
                "hub.challenge": "challenge_string_42",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "challenge_string_42"

    def test_verification_wrong_token(self, wa_client):
        resp = wa_client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "challenge_string_42",
            },
        )
        assert resp.status_code == 403

    def test_invalid_signature_rejected(self, wa_client):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "123",
                                        "text": {"body": "hi"},
                                        "id": "x",
                                        "type": "text",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        body_bytes = json.dumps(payload).encode()
        resp = wa_client.post(
            "/webhooks/whatsapp",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )
        assert resp.status_code == 403

    def test_valid_message_webhook(self, wa_client, mock_bridge):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "15551234567",
                                        "text": {"body": "hello wa"},
                                        "id": "wamid.abc123",
                                        "type": "text",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        body_bytes = json.dumps(payload).encode()
        sig = hmac.new(b"wa_secret", body_bytes, hashlib.sha256).hexdigest()
        resp = wa_client.post(
            "/webhooks/whatsapp",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert resp.status_code == 200


class TestWebhooksFailClosed:
    """Webhooks reject when channel authentication or owner mapping is absent."""

    def _client(self, mock_bridge, **kwargs):
        app = FastAPI()
        app.include_router(create_webhook_router(bridge=mock_bridge, **kwargs))
        return TestClient(app)

    def test_twilio_without_token_rejected(self, mock_bridge):
        c = self._client(mock_bridge)  # no twilio_auth_token
        resp = c.post(
            "/webhooks/twilio",
            data={"From": "+15551234567", "Body": "hi", "MessageSid": "SM1"},
        )
        assert resp.status_code == 503
        assert resp.text == "Channel unavailable"
        mock_bridge.handle_incoming.assert_not_called()

    def test_bluebubbles_without_password_rejected(self, mock_bridge):
        c = self._client(mock_bridge)  # no bluebubbles_password
        resp = c.post(
            "/webhooks/bluebubbles",
            json={"type": "new-message", "data": {}},
            headers={"Authorization": "anything"},
        )
        assert resp.status_code == 403

    def test_whatsapp_without_secret_rejected(self, mock_bridge):
        c = self._client(mock_bridge)  # no whatsapp_app_secret
        resp = c.post(
            "/webhooks/whatsapp",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_whatsapp_verify_without_token_rejected(self, mock_bridge):
        c = self._client(mock_bridge)  # no whatsapp_verify_token
        resp = c.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "",
                "hub.challenge": "x",
            },
        )
        assert resp.status_code == 403
