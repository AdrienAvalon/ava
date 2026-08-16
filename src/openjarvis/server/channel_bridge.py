"""ChannelBridge — unified orchestrator for multi-channel messaging."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from openjarvis.channels._stubs import BaseChannel, ChannelStatus
from openjarvis.core.events import Event, EventBus, EventType
from openjarvis.engine._finish import conservative_finish_reason
from openjarvis.server.session_store import SessionStore

logger = logging.getLogger(__name__)

_DEFAULT_MAX_LENGTH = 4000
_SMS_MAX_LENGTH = 1600
_MANAGED_AGENT_COMMANDS_UNAVAILABLE = (
    "Managed-agent commands are unavailable on messaging channels until "
    "a verified principal mapping is configured."
)
_CHAT_FAILURE_MESSAGE = (
    "Sorry, I couldn't complete that response. Please try again in a moment."
)

_HELP_TEXT = """\
Available commands:
/agents — list running agents
/agent <id> status — agent state and current task
/agent <id> <message> — send a message to an agent
/agent <id> pause — pause an agent
/agent <id> resume — resume an agent
/notify <channel> — set where to receive notifications
/sessions — list your active sessions
/more — get the rest of a truncated response
/help — show this message\
"""

# Managed-agent and scheduler events carry private prompts, summaries, results
# and errors, but channel sessions do not yet have a verified channel-to-owner
# mapping. Keep both the subscription list and the callback itself fail-closed
# so a future accidental subscription cannot create a cross-principal broadcast.
_UNSCOPED_NOTIFICATION_EVENTS = frozenset(
    event_type for event_type in EventType if event_type.name.startswith("AGENT_")
) | {EventType.SCHEDULER_TASK_END}

# No event is eligible until notifications can be routed to a verified owner.
_NOTIFICATION_EVENTS: tuple[EventType, ...] = ()


class ChannelBridge:
    """Orchestrates incoming messages across multiple channel adapters.

    Provides backward-compatible ``send()``/``status()``/``list_channels()``
    so it can replace the old single-channel bridge in ``app.state``.
    """

    def __init__(
        self,
        channels: Dict[str, BaseChannel],
        session_store: SessionStore,
        bus: EventBus,
        system: Any = None,
        agent_manager: Any = None,
        deep_research_agent: Any = None,
    ) -> None:
        self._channels = channels
        self._session_store = session_store
        self._bus = bus
        self._system = system
        self._agent_manager = agent_manager
        self._deep_research_agent = deep_research_agent
        self._subscribe_notifications()

    # --------------------------------------------------------------
    # Backward-compatible BaseChannel interface
    # --------------------------------------------------------------

    def connect(self) -> None:
        for ch in self._channels.values():
            ch.connect()

    def disconnect(self) -> None:
        for ch in self._channels.values():
            ch.disconnect()

    def list_channels(self) -> List[str]:
        result: List[str] = []
        for ch in self._channels.values():
            result.extend(ch.list_channels())
        return result

    def status(self) -> ChannelStatus:
        statuses = [ch.status() for ch in self._channels.values()]
        if not statuses:
            return ChannelStatus.DISCONNECTED
        if any(s == ChannelStatus.CONNECTED for s in statuses):
            return ChannelStatus.CONNECTED
        if all(s == ChannelStatus.ERROR for s in statuses):
            return ChannelStatus.ERROR
        return ChannelStatus.DISCONNECTED

    def send(
        self,
        channel: str,
        content: str,
        *,
        conversation_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        for ch in self._channels.values():
            if channel in ch.list_channels():
                return ch.send(
                    channel,
                    content,
                    conversation_id=conversation_id,
                    metadata=metadata,
                )
        logger.warning("No adapter found for channel %s", channel)
        return False

    # --------------------------------------------------------------
    # Incoming message handling
    # --------------------------------------------------------------

    def handle_incoming(
        self,
        sender_id: str,
        content: str,
        channel_type: str,
        metadata: Optional[Dict[str, Any]] = None,
        max_length: int = _DEFAULT_MAX_LENGTH,
    ) -> str:
        self._session_store.get_or_create(sender_id, channel_type)

        # Command routing
        stripped = content.strip()
        if stripped.startswith("/"):
            result = self._handle_command(sender_id, stripped, channel_type)
            if result is not None:
                return result

        # Regular chat — route to JarvisSystem.ask()
        return self._handle_chat(sender_id, stripped, channel_type, max_length)

    # --------------------------------------------------------------
    # Command parsing
    # --------------------------------------------------------------

    def _handle_command(
        self,
        sender_id: str,
        content: str,
        channel_type: str,
    ) -> Optional[str]:
        parts = content.split(None, 2)
        cmd = parts[0].lower()

        if cmd == "/help":
            return _HELP_TEXT

        if cmd == "/more":
            return self._handle_more(sender_id, channel_type)

        if cmd == "/notify" and len(parts) >= 2:
            pref = parts[1]
            self._session_store.set_notification_preference(
                sender_id, channel_type, pref
            )
            return f"Notifications will be sent to {pref}."

        if cmd == "/sessions":
            return self._handle_sessions(sender_id)

        if cmd == "/agents":
            return _MANAGED_AGENT_COMMANDS_UNAVAILABLE

        if cmd == "/agent":
            return _MANAGED_AGENT_COMMANDS_UNAVAILABLE

        # Unknown command — fall through to chat
        return None

    def _handle_more(self, sender_id: str, channel_type: str) -> str:
        session = self._session_store.get_or_create(sender_id, channel_type)
        pending = session.get("pending_response")
        if pending:
            self._session_store.clear_pending_response(sender_id, channel_type)
            return pending
        return "No pending response."

    # --------------------------------------------------------------
    # Chat handling
    # --------------------------------------------------------------

    def _handle_sessions(self, sender_id: str) -> str:
        targets = self._session_store.get_notification_targets()
        user_sessions = [t for t in targets if t["sender_id"] == sender_id]
        if not user_sessions:
            return "No active sessions with notification preferences."
        lines = []
        for s in user_sessions:
            lines.append(
                f"  {s['channel_type']} -> "
                f"notifications: {s['preferred_notification_channel']}"
            )
        return "Your sessions:\n" + "\n".join(lines)

    def _handle_chat(
        self,
        sender_id: str,
        content: str,
        channel_type: str,
        max_length: int,
    ) -> str:
        # Build context from conversation history
        session = self._session_store.get_or_create(sender_id, channel_type)
        history = session.get("conversation_history", [])
        context_lines = []
        for msg in history:
            context_lines.append(f"{msg['role']}: {msg['content']}")
        context_str = "\n".join(context_lines)

        query = content
        if context_str:
            query = (
                f"Previous conversation:\n{context_str}\n\nCurrent message: {content}"
            )

        # Try DeepResearchAgent first
        if self._deep_research_agent is not None:
            try:
                result = self._deep_research_agent.run(content)
                response_text = getattr(result, "content", "")
                metadata = getattr(result, "metadata", {}) or {}
                finish_reason = conservative_finish_reason(
                    metadata.get("finish_reason")
                )
            except Exception:
                logger.exception("DeepResearch agent failed")
                return _CHAT_FAILURE_MESSAGE
        elif self._system is not None:
            try:
                result = self._system.ask(query)
                response_text = result.get("content", "")
                finish_reason = conservative_finish_reason(result.get("finish_reason"))
            except Exception:
                logger.exception("Error in JarvisSystem.ask()")
                return _CHAT_FAILURE_MESSAGE
        else:
            return _CHAT_FAILURE_MESSAGE

        if (
            finish_reason != "stop"
            or not isinstance(response_text, str)
            or not response_text.strip()
        ):
            return _CHAT_FAILURE_MESSAGE

        # Format and possibly truncate
        formatted = self._format_response(
            sender_id, channel_type, response_text, max_length
        )
        self._session_store.append_message(sender_id, channel_type, "user", content)
        self._session_store.append_message(
            sender_id, channel_type, "assistant", response_text
        )
        return formatted

    def _format_response(
        self,
        sender_id: str,
        channel_type: str,
        response: str,
        max_length: int,
    ) -> str:
        if len(response) <= max_length:
            return response
        # Truncate and store full response for /more retrieval
        truncation_notice = "\n\n... (reply /more for full response)"
        cut_at = max_length - len(truncation_notice)
        truncated = response[:cut_at] + truncation_notice
        self._session_store.set_pending_response(sender_id, channel_type, response)
        return truncated

    # --------------------------------------------------------------
    # Notifications
    # --------------------------------------------------------------

    def _subscribe_notifications(self) -> None:
        for event_type in _NOTIFICATION_EVENTS:
            self._bus.subscribe(event_type, self._on_notification_event)

    def _on_notification_event(self, event: Event) -> None:
        if event.event_type in _UNSCOPED_NOTIFICATION_EVENTS:
            return
        logger.debug(
            "Ignoring notification event %s without a verified owner mapping",
            event.event_type,
        )
