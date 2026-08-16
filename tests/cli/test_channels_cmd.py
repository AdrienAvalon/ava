"""Completion contract for the native iMessage channel command."""

from types import SimpleNamespace

import pytest

from openjarvis.cli.channels_cmd import _completed_agent_text


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("private fragment", "length"),
        ("private fragment", None),
        ("", "stop"),
    ],
)
def test_incomplete_agent_result_never_reaches_imessage(
    content: str, finish_reason: str | None
) -> None:
    result = SimpleNamespace(
        content=content,
        metadata={"finish_reason": finish_reason},
    )

    reply = _completed_agent_text(result)

    assert "private fragment" not in reply
    assert "complete" in reply.lower()


def test_complete_agent_result_reaches_imessage() -> None:
    result = SimpleNamespace(
        content="Complete answer",
        metadata={"finish_reason": "end_turn"},
    )

    assert _completed_agent_text(result) == "Complete answer"
