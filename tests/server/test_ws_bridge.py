"""Tests for the principal-scoped managed-agent WebSocket bridge."""

from __future__ import annotations

import base64
import time

import pytest
from ava_extensions.server.principal import OIDC_HEADER, Principal

from openjarvis.agents.manager import AgentManager
from openjarvis.core.events import EventBus, EventType

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from openjarvis.server.ws_bridge import create_ws_router  # noqa: E402

OWNER = Principal("oidc", "https://issuer.example.invalid", "managed-owner")
OTHER_OWNER = Principal("oidc", "https://issuer.example.invalid", "managed-other")
SERVICE_PRINCIPAL = Principal("service", "avalon-control-plane", "matrix:@ava:test")
WS_PROTOCOL = "ava-agent-events-v1"


def _credential_protocol(prefix: str, value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return f"{prefix}{encoded.rstrip('=')}"


def _protocols(token: str, api_key: str = "") -> list[str]:
    protocols = [WS_PROTOCOL, _credential_protocol("ava-oidc-v1.", token)]
    if api_key:
        protocols.append(_credential_protocol("ava-api-key-v1.", api_key))
    return protocols


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def manager(tmp_path) -> AgentManager:
    value = AgentManager(str(tmp_path / "managed-agents.db"))
    yield value
    value.close()


@pytest.fixture(autouse=True)
def _verified_test_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava_extensions.server import principal as principal_module

    principals = {
        "owner-token": OWNER,
        "other-token": OTHER_OWNER,
        "service-token": SERVICE_PRINCIPAL,
    }

    def resolve(headers):
        token = next(
            (
                value
                for key, value in headers.items()
                if str(key).lower() == OIDC_HEADER.lower()
            ),
            "",
        )
        return principals.get(token)

    monkeypatch.setattr(principal_module, "resolve_request_principal", resolve)


@pytest.fixture
def app(event_bus: EventBus, manager: AgentManager) -> FastAPI:
    value = FastAPI()
    value.state.api_key = ""
    value.state.agent_manager = manager
    value.include_router(create_ws_router(event_bus))
    return value


@pytest.fixture
def agents(manager: AgentManager) -> dict[str, dict]:
    return {
        "owner": manager.create_agent(
            name="owner-agent",
            owner_provenance=OWNER.provenance,
        ),
        "other": manager.create_agent(
            name="other-agent",
            owner_provenance=OTHER_OWNER.provenance,
        ),
        "legacy": manager.create_agent(
            name="legacy-ownerless",
            owner_provenance=None,
        ),
    }


def _assert_rejected(client: TestClient, path: str, **kwargs) -> None:
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(path, **kwargs):
            pass
    assert rejected.value.code == 1008
    assert rejected.value.reason == ""


class TestWSBridge:
    def test_owned_connection_receives_only_exactly_scoped_events(
        self,
        app: FastAPI,
        event_bus: EventBus,
        agents: dict[str, dict],
    ) -> None:
        client = TestClient(app)
        owner_id = agents["owner"]["id"]
        other_id = agents["other"]["id"]

        with client.websocket_connect(
            f"/v1/agents/events?agent_id={owner_id}",
            subprotocols=_protocols("owner-token"),
        ) as ws:
            assert ws.accepted_subprotocol == WS_PROTOCOL
            time.sleep(0.02)
            event_bus.publish(EventType.AGENT_TICK_START, {"agent_id": other_id})
            event_bus.publish(EventType.INFERENCE_END, {"content": "unscoped-private"})
            event_bus.publish(
                EventType.TOOL_CALL_END,
                {"agent_id": owner_id, "agent": other_id, "result": "ambiguous"},
            )
            event_bus.publish(
                EventType.TOOL_CALL_END,
                {"agent": owner_id, "result": "owner-result"},
            )

            data = ws.receive_json()
            assert data["type"] == "tool_call_end"
            assert data["data"] == {"agent": owner_id, "result": "owner-result"}

    @pytest.mark.parametrize("token", [None, "invalid-token", "service-token"])
    def test_verified_oidc_principal_is_mandatory(
        self,
        app: FastAPI,
        agents: dict[str, dict],
        token: str | None,
    ) -> None:
        protocols = [WS_PROTOCOL] if token is None else _protocols(token)
        _assert_rejected(
            TestClient(app),
            f"/v1/agents/events?agent_id={agents['owner']['id']}",
            subprotocols=protocols,
        )

    @pytest.mark.parametrize("suffix", ["", "?agent_id=", "?agent_id=a&agent_id=b"])
    def test_exactly_one_nonempty_agent_id_is_required(
        self,
        app: FastAPI,
        suffix: str,
    ) -> None:
        _assert_rejected(
            TestClient(app),
            f"/v1/agents/events{suffix}",
            subprotocols=_protocols("owner-token"),
        )

    @pytest.mark.parametrize("agent_key", ["other", "legacy"])
    def test_foreign_and_legacy_ownerless_agents_are_equally_invisible(
        self,
        app: FastAPI,
        agents: dict[str, dict],
        agent_key: str,
    ) -> None:
        _assert_rejected(
            TestClient(app),
            f"/v1/agents/events?agent_id={agents[agent_key]['id']}",
            subprotocols=_protocols("owner-token"),
        )

    def test_unknown_agent_is_not_distinguishable_from_foreign_agent(
        self,
        app: FastAPI,
    ) -> None:
        _assert_rejected(
            TestClient(app),
            "/v1/agents/events?agent_id=unknown-agent",
            subprotocols=_protocols("owner-token"),
        )

    def test_principal_and_ownership_are_rechecked_for_each_connection(
        self,
        app: FastAPI,
        event_bus: EventBus,
        agents: dict[str, dict],
    ) -> None:
        client = TestClient(app)
        owner_id = agents["owner"]["id"]
        path = f"/v1/agents/events?agent_id={owner_id}"

        with client.websocket_connect(
            path,
            subprotocols=_protocols("owner-token"),
        ) as ws:
            time.sleep(0.02)
            event_bus.publish(EventType.AGENT_TICK_END, {"agent_id": owner_id})
            assert ws.receive_json()["data"]["agent_id"] == owner_id

        _assert_rejected(client, path, subprotocols=_protocols("other-token"))

    def test_api_key_subprotocol_remains_a_second_barrier(
        self,
        app: FastAPI,
        event_bus: EventBus,
        agents: dict[str, dict],
    ) -> None:
        app.state.api_key = "daemon-secret"
        client = TestClient(app)
        owner_id = agents["owner"]["id"]
        path = f"/v1/agents/events?agent_id={owner_id}"

        _assert_rejected(client, path, subprotocols=_protocols("owner-token"))
        with client.websocket_connect(
            path,
            subprotocols=_protocols("owner-token", "daemon-secret"),
        ) as ws:
            assert ws.accepted_subprotocol == WS_PROTOCOL
            assert "daemon-secret" not in path
            assert "owner-token" not in path
            time.sleep(0.02)
            event_bus.publish(EventType.AGENT_TICK_START, {"agent_id": owner_id})
            assert ws.receive_json()["data"]["agent_id"] == owner_id

    def test_programmatic_headers_remain_supported(
        self,
        app: FastAPI,
        event_bus: EventBus,
        agents: dict[str, dict],
    ) -> None:
        app.state.api_key = "daemon-secret"
        owner_id = agents["owner"]["id"]
        with TestClient(app).websocket_connect(
            f"/v1/agents/events?agent_id={owner_id}",
            headers={
                OIDC_HEADER: "owner-token",
                "Authorization": "Bearer daemon-secret",
            },
        ) as ws:
            time.sleep(0.02)
            event_bus.publish(EventType.AGENT_TICK_START, {"agent_id": owner_id})
            assert ws.receive_json()["data"]["agent_id"] == owner_id

    def test_duplicate_identity_transports_fail_closed(
        self,
        app: FastAPI,
        agents: dict[str, dict],
    ) -> None:
        _assert_rejected(
            TestClient(app),
            f"/v1/agents/events?agent_id={agents['owner']['id']}",
            headers={OIDC_HEADER: "owner-token"},
            subprotocols=_protocols("owner-token"),
        )

    def test_ownership_lookup_failure_exposes_no_diagnostic(
        self,
        app: FastAPI,
        manager: AgentManager,
        agents: dict[str, dict],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_lookup(_agent_id: str, _owner_provenance: str):
            raise RuntimeError("private database diagnostic")

        monkeypatch.setattr(manager, "get_agent_for_owner", fail_lookup)
        with pytest.raises(WebSocketDisconnect) as rejected:
            with TestClient(app).websocket_connect(
                f"/v1/agents/events?agent_id={agents['owner']['id']}",
                subprotocols=_protocols("owner-token"),
            ):
                pass

        assert rejected.value.code == 1011
        assert rejected.value.reason == ""
        assert "private database diagnostic" not in str(rejected.value)
