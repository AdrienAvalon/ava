"""Completion contract for the native Slack daemon."""

from types import SimpleNamespace

import pytest

from openjarvis.channels.slack_daemon import _completed_slack_reply


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("private fragment", "length"),
        ("private fragment", None),
        ("", "stop"),
    ],
)
def test_incomplete_agent_result_never_reaches_slack(
    content: str, finish_reason: str | None
) -> None:
    result = SimpleNamespace(
        content=content,
        metadata={"finish_reason": finish_reason},
    )

    reply = _completed_slack_reply(result)

    assert "private fragment" not in reply
    assert "complete" in reply.lower()


def test_complete_agent_result_is_formatted_for_slack() -> None:
    result = SimpleNamespace(
        content="**Complete** answer",
        metadata={"finish_reason": "stop_sequence"},
    )

    assert _completed_slack_reply(result) == "*Complete* answer"
