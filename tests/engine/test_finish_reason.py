"""Provider terminal reasons must fail closed when completion is unproven."""

from enum import Enum

import pytest

from openjarvis.engine._finish import conservative_finish_reason


class _GoogleReason(Enum):
    STOP = 1
    MAX_TOKENS = 2
    SAFETY = 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("future_reason", "future_reason"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("completed", "stop"),
        ("end_turn", "stop"),
        ("tool_use", "tool_calls"),
        ("pause_turn", "length"),
        ("max_output_tokens", "length"),
        (_GoogleReason.STOP, "stop"),
        (_GoogleReason.MAX_TOKENS, "length"),
        (_GoogleReason.SAFETY, "content_filter"),
    ],
)
def test_conservative_finish_reason(raw, expected: str | None) -> None:
    assert conservative_finish_reason(raw) == expected
