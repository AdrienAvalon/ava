"""Conservative provider finish-reason handling."""

from __future__ import annotations

from typing import Any

_ALIASES = {
    "completed": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "max_output_tokens": "length",
    "pause_turn": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
    "incomplete": "length",
    "safety": "content_filter",
    "recitation": "content_filter",
    "blocklist": "content_filter",
    "prohibited_content": "content_filter",
    "spii": "content_filter",
    "image_safety": "content_filter",
}


def conservative_finish_reason(value: Any) -> str | None:
    """Normalize a provider terminal without ever inventing success."""

    if value is None or isinstance(value, bool):
        return None
    enum_name = getattr(value, "name", None)
    raw = enum_name if isinstance(enum_name, str) else str(value)
    normalized = raw.strip().lower().rsplit(".", 1)[-1]
    if not normalized:
        return None
    return _ALIASES.get(normalized, normalized)


__all__ = ["conservative_finish_reason"]
