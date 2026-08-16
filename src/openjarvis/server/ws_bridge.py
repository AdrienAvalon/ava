"""WebSocket bridge: EventBus → connected WebSocket clients."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from openjarvis.core.events import Event, EventBus, EventType

try:
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover
    pass  # FastAPI is optional; create_ws_router will fail at call time

logger = logging.getLogger(__name__)

_AGENT_EVENTS_SUBPROTOCOL = "ava-agent-events-v1"
_OIDC_SUBPROTOCOL_PREFIX = "ava-oidc-v1."
_API_KEY_SUBPROTOCOL_PREFIX = "ava-api-key-v1."
_MAX_PROTOCOL_CREDENTIAL_BYTES = 16 * 1024

# Agent-related event types to forward
_AGENT_EVENTS = {
    EventType.AGENT_TICK_START,
    EventType.AGENT_TICK_END,
    EventType.AGENT_TICK_ERROR,
    EventType.AGENT_BUDGET_EXCEEDED,
    EventType.AGENT_STALL_DETECTED,
    EventType.AGENT_MESSAGE_RECEIVED,
    EventType.AGENT_CHECKPOINT_SAVED,
    EventType.TOOL_CALL_START,
    EventType.TOOL_CALL_END,
    EventType.INFERENCE_START,
    EventType.INFERENCE_END,
}


@dataclass(frozen=True, slots=True)
class _ProtocolCredentials:
    oidc_token: str | None
    api_key: str | None
    negotiated_subprotocol: str | None
    valid: bool


def _offered_subprotocols(websocket: WebSocket) -> tuple[str, ...]:
    """Return the client's protocol tokens without logging credential values."""

    protocols = websocket.scope.get("subprotocols", ())
    if isinstance(protocols, (list, tuple)):
        return tuple(value for value in protocols if isinstance(value, str) and value)
    return ()


def _decode_protocol_credential(protocol: str, prefix: str) -> str | None:
    encoded = protocol.removeprefix(prefix)
    if not encoded or len(encoded) > (_MAX_PROTOCOL_CREDENTIAL_BYTES * 2):
        return None
    try:
        raw = base64.b64decode(
            encoded + ("=" * (-len(encoded) % 4)),
            altchars=b"-_",
            validate=True,
        )
        value = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        not value
        or len(raw) > _MAX_PROTOCOL_CREDENTIAL_BYTES
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    return value


def _extract_protocol_credential(
    protocols: tuple[str, ...],
    prefix: str,
) -> tuple[str | None, bool]:
    matches = [value for value in protocols if value.startswith(prefix)]
    if not matches:
        return None, True
    if len(matches) != 1:
        return None, False
    decoded = _decode_protocol_credential(matches[0], prefix)
    return decoded, decoded is not None


def _protocol_credentials(websocket: WebSocket) -> _ProtocolCredentials:
    protocols = _offered_subprotocols(websocket)
    oidc_token, oidc_valid = _extract_protocol_credential(
        protocols,
        _OIDC_SUBPROTOCOL_PREFIX,
    )
    api_key, api_key_valid = _extract_protocol_credential(
        protocols,
        _API_KEY_SUBPROTOCOL_PREFIX,
    )
    transport_count = protocols.count(_AGENT_EVENTS_SUBPROTOCOL)
    has_protocol_credential = oidc_token is not None or api_key is not None
    valid = (
        oidc_valid
        and api_key_valid
        and transport_count <= 1
        and (not has_protocol_credential or transport_count == 1)
    )
    return _ProtocolCredentials(
        oidc_token=oidc_token,
        api_key=api_key,
        negotiated_subprotocol=(
            _AGENT_EVENTS_SUBPROTOCOL if transport_count == 1 else None
        ),
        valid=valid,
    )


def _header_present(headers: Any, name: str) -> bool:
    expected = name.lower()
    try:
        return any(str(key).lower() == expected for key in headers.keys())
    except Exception:  # noqa: BLE001 - foreign ASGI header mapping
        return False


def _single_agent_id(websocket: WebSocket) -> str | None:
    try:
        values = websocket.query_params.getlist("agent_id")
    except Exception:  # noqa: BLE001 - foreign ASGI query mapping
        return None
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    agent_id = values[0].strip()
    if (
        not agent_id
        or agent_id != values[0]
        or len(agent_id) > 256
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in agent_id)
    ):
        return None
    return agent_id


def _api_key_authorized(
    websocket: WebSocket,
    expected_key: str,
    protocol_key: str | None,
) -> bool:
    """Reuse the shared WebSocket API-key verifier with a header transport view."""

    from openjarvis.server.auth_middleware import websocket_authorized

    headers = dict(websocket.headers.items())
    if protocol_key is not None:
        if (
            _header_present(headers, "authorization")
            or "token" in websocket.query_params
        ):
            return False
        headers["authorization"] = f"Bearer {protocol_key}"
    auth_view = SimpleNamespace(
        headers=headers,
        query_params=websocket.query_params,
    )
    return websocket_authorized(auth_view, expected_key)


def _verified_oidc_principal(
    websocket: WebSocket,
    protocol_token: str | None,
) -> Any | None:
    """Resolve only a cryptographically verified OIDC principal."""

    from ava_extensions.server.principal import OIDC_HEADER, resolve_request_principal

    headers = dict(websocket.headers.items())
    if protocol_token is not None:
        if _header_present(headers, OIDC_HEADER):
            return None
        headers[OIDC_HEADER] = protocol_token
    principal = resolve_request_principal(headers)
    if principal is None or getattr(principal, "provider", None) != "oidc":
        return None
    return principal


def create_ws_router(event_bus: EventBus) -> Any:
    """Create a FastAPI router with a WebSocket endpoint for agent events."""
    router = APIRouter()
    # Each client is permanently bound to one already-authorized agent.
    clients: dict[
        WebSocket,
        tuple[asyncio.Queue, asyncio.AbstractEventLoop, str],
    ] = {}

    def _on_event(event: Event) -> None:
        """Forward event to all connected WebSocket client queues (thread-safe)."""
        data = event.data if isinstance(event.data, dict) else {}
        event_agents = {
            value
            for value in (data.get("agent_id"), data.get("agent"))
            if isinstance(value, str) and value
        }
        payload = {
            "type": event.event_type.value,
            "timestamp": event.timestamp,
            "data": data,
        }
        for _ws, (queue, loop, agent_id) in list(clients.items()):
            # Unscoped and contradictory events are never broadcast. Tick events
            # carry ``agent_id`` while tool-call events carry ``agent``.
            if event_agents != {agent_id}:
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except (RuntimeError, asyncio.QueueFull):
                pass  # Loop closed or client is slow

    # Subscribe to all agent events
    for event_type in _AGENT_EVENTS:
        event_bus.subscribe(event_type, _on_event)

    @router.websocket("/v1/agents/events")
    async def agent_events(websocket: WebSocket) -> None:
        credentials = _protocol_credentials(websocket)
        agent_id = _single_agent_id(websocket)
        expected_key = getattr(websocket.app.state, "api_key", "")
        if (
            not credentials.valid
            or agent_id is None
            or not _api_key_authorized(websocket, expected_key, credentials.api_key)
        ):
            await websocket.close(code=1008)
            return

        principal = _verified_oidc_principal(websocket, credentials.oidc_token)
        manager = getattr(websocket.app.state, "agent_manager", None)
        if principal is None or manager is None:
            await websocket.close(code=1008)
            return
        try:
            owned_agent = manager.get_agent_for_owner(agent_id, principal.provenance)
        except Exception:  # noqa: BLE001 - fail closed without exposing DB diagnostics
            logger.warning("Managed-agent WebSocket ownership lookup failed")
            await websocket.close(code=1011)
            return
        if owned_agent is None:
            # Deliberately identical for an unknown, foreign, or legacy NULL owner.
            await websocket.close(code=1008)
            return

        await websocket.accept(subprotocol=credentials.negotiated_subprotocol)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        loop = asyncio.get_running_loop()
        clients[websocket] = (queue, loop, agent_id)
        try:
            while True:
                payload = await queue.get()
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            pass
        finally:
            clients.pop(websocket, None)

    return router


__all__ = ["create_ws_router"]
