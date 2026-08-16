"""Direct cloud/local streams must preserve provider terminal metadata."""

from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.server import cloud_router


class _FakeResponse:
    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = list(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines: Iterable[str], captured: dict) -> None:
        self._lines = lines
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs):
        self._captured.update(method=method, url=url, **kwargs)
        return _FakeResponse(self._lines)


def _install_client(monkeypatch, lines: Iterable[str]) -> dict:
    captured: dict = {}
    monkeypatch.setattr(
        cloud_router.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(lines, captured),
    )
    return captured


async def _collect(stream):
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [("stop", "stop"), ("length", "length"), (None, None)],
)
async def test_openai_direct_stream_preserves_terminal(
    monkeypatch: pytest.MonkeyPatch,
    reason,
    expected,
) -> None:
    final_choice = {"delta": {}}
    if reason is not None:
        final_choice["finish_reason"] = reason
    lines = [
        'data: {"choices":[{"delta":{"content":"part"}}]}',
        f"data: {json.dumps({'choices': [final_choice]})}",
        "data: [DONE]",
    ]
    captured = _install_client(monkeypatch, lines)
    monkeypatch.setattr(cloud_router, "_load_keys", lambda: {"OPENAI_API_KEY": "x"})

    chunks = await _collect(
        cloud_router._stream_openai(
            "gpt-test",
            [Message(role=Role.USER, content="hi")],
            0.2,
            16_384,
        )
    )

    assert chunks[0].content == "part"
    terminals = [chunk.finish_reason for chunk in chunks if chunk.finish_reason]
    assert terminals == ([expected] if expected is not None else [])
    assert captured["json"]["max_tokens"] == 16_384


@pytest.mark.asyncio
async def test_anthropic_and_google_direct_stream_map_provider_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cloud_router,
        "_load_keys",
        lambda: {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": "y"},
    )
    _install_client(
        monkeypatch,
        [
            'data: {"type":"content_block_delta","delta":{"text":"part"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}',
        ],
    )
    anthropic = await _collect(
        cloud_router._stream_anthropic(
            "claude-test",
            [Message(role=Role.USER, content="hi")],
            0.2,
            100,
        )
    )
    assert anthropic[-1].finish_reason == "length"

    _install_client(
        monkeypatch,
        [
            'data: {"candidates":[{"content":{"parts":[{"text":"part"}]},'
            '"finishReason":"SAFETY"}]}'
        ],
    )
    google = await _collect(
        cloud_router._stream_google(
            "gemini-test",
            [Message(role=Role.USER, content="hi")],
            0.2,
            100,
        )
    )
    assert google[-1].finish_reason == "content_filter"


@pytest.mark.asyncio
async def test_ollama_direct_stream_missing_reason_is_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        [
            '{"message":{"content":"part"}}',
            '{"message":{"content":""},"done":true}',
        ],
    )

    chunks = await _collect(
        cloud_router.stream_local_full(
            "local-test",
            [Message(role=Role.USER, content="hi")],
            max_tokens=100,
        )
    )

    assert chunks[0].content == "part"
    assert chunks[-1].finish_reason is None


@pytest.mark.asyncio
async def test_direct_stream_rejects_malformed_frame_before_later_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        [
            'data: {"choices":[{"delta":{"content":"lost"}}]',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(cloud_router, "_load_keys", lambda: {"OPENAI_API_KEY": "x"})

    with pytest.raises(json.JSONDecodeError):
        await _collect(
            cloud_router._stream_openai(
                "gpt-test",
                [Message(role=Role.USER, content="hi")],
                0.2,
                100,
            )
        )
