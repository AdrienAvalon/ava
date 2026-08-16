"""Test ChannelBridge routes to DeepResearchAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_handle_chat_uses_deep_research_agent() -> None:
    """When a DeepResearch agent is configured, route through it."""
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore

    mock_agent = MagicMock()
    mock_agent.run.return_value = MagicMock(
        content="Found 3 results about Spain.",
        metadata={"finish_reason": "stop"},
    )

    bridge = ChannelBridge(
        channels={},
        session_store=SessionStore(db_path=":memory:"),
        bus=MagicMock(),
        deep_research_agent=mock_agent,
    )

    result = bridge.handle_incoming(
        sender_id="+15551234567",
        content="When was my last trip to Spain?",
        channel_type="twilio",
    )

    assert "Spain" in result
    mock_agent.run.assert_called_once()


def test_handle_chat_falls_back_to_system() -> None:
    """When no DeepResearch agent, fall back to system.ask()."""
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore

    mock_system = MagicMock()
    mock_system.ask.return_value = {
        "content": "Generic response",
        "finish_reason": "stop",
    }

    bridge = ChannelBridge(
        channels={},
        session_store=SessionStore(db_path=":memory:"),
        bus=MagicMock(),
        system=mock_system,
    )

    result = bridge.handle_incoming(
        sender_id="+15551234567",
        content="Hello",
        channel_type="twilio",
    )

    assert result == "Generic response"
    mock_system.ask.assert_called_once()


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("private fragment", "length"),
        ("private fragment", None),
        ("", "stop"),
    ],
)
def test_deep_research_incomplete_result_is_not_persisted(
    content: str, finish_reason: str | None
) -> None:
    from openjarvis.server.channel_bridge import ChannelBridge
    from openjarvis.server.session_store import SessionStore

    store = SessionStore(db_path=":memory:")
    mock_agent = MagicMock()
    mock_agent.run.return_value = MagicMock(
        content=content,
        metadata={"finish_reason": finish_reason},
    )
    bridge = ChannelBridge(
        channels={},
        session_store=store,
        bus=MagicMock(),
        deep_research_agent=mock_agent,
    )

    reply = bridge.handle_incoming("sender", "question", "twilio")

    assert "private fragment" not in reply
    assert "complete" in reply.lower()
    assert store.get_or_create("sender", "twilio")["conversation_history"] == []
