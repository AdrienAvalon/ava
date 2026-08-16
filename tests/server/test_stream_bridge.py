"""Terminal-evidence tests for the synchronous-agent SSE bridge."""

from __future__ import annotations

import json

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.events import EventBus
from openjarvis.engine._stubs import StreamChunk
from openjarvis.server.models import ChatCompletionRequest
from openjarvis.server.stream_bridge import AgentStreamBridge


class _Agent:
    def __init__(self, result: AgentResult, *, engine=None) -> None:
        self._result = result
        self._engine = engine
        self._model = "original-model"

    def run(self, _input: str, context=None) -> AgentResult:
        del context
        return self._result


class _FailingAgent(_Agent):
    def run(self, _input: str, context=None) -> AgentResult:
        del context
        raise RuntimeError("backend disconnected")


class _StreamingEngine:
    def __init__(
        self,
        terminal: object = None,
        *,
        content: str = "visible",
        fail_after_content: bool = False,
    ) -> None:
        self._terminal = terminal
        self._content = content
        self._fail_after_content = fail_after_content

    async def stream_full(self, _messages, **_kwargs):
        if self._content:
            yield StreamChunk(content=self._content)
        if self._fail_after_content:
            raise RuntimeError("stream disconnected")
        if self._terminal is not None:
            yield StreamChunk(finish_reason=self._terminal)


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[{"role": "user", "content": "question"}],
        stream=True,
    )


async def _payloads(agent: _Agent) -> list[dict]:
    bridge = AgentStreamBridge(agent, EventBus(), "test-model", _request())
    frames = [frame async for frame in bridge.stream()]
    return [
        json.loads(line[len("data:") :].strip())
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]


def _terminal(payloads: list[dict]) -> str | None:
    reasons = [
        payload["choices"][0].get("finish_reason")
        for payload in payloads
        if payload.get("choices")
        and payload["choices"][0].get("finish_reason") is not None
    ]
    return reasons[-1] if reasons else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"finish_reason": None},
        {"finish_reason": "future_reason"},
        {"finish_reason": "stop", "max_turns_exceeded": True},
        {"finish_reason": "stop", "incomplete_tool_call": True},
    ],
)
async def test_fallback_replay_never_turns_missing_or_incomplete_into_stop(metadata):
    payloads = await _payloads(
        _Agent(AgentResult(content="partial", metadata=metadata))
    )

    assert _terminal(payloads) == "length"


@pytest.mark.asyncio
async def test_fallback_replay_preserves_proven_stop():
    payloads = await _payloads(
        _Agent(AgentResult(content="complete", metadata={"finish_reason": "end_turn"}))
    )

    assert _terminal(payloads) == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "future_reason"])
async def test_real_stream_eof_or_unknown_terminal_is_incomplete(terminal):
    payloads = await _payloads(
        _Agent(
            AgentResult(content="agent content", metadata={"finish_reason": "stop"}),
            engine=_StreamingEngine(terminal),
        )
    )

    assert _terminal(payloads) == "length"


@pytest.mark.asyncio
async def test_real_stream_preserves_proven_stop():
    payloads = await _payloads(
        _Agent(
            AgentResult(content="agent content", metadata={"finish_reason": "stop"}),
            engine=_StreamingEngine("stop"),
        )
    )

    assert _terminal(payloads) == "stop"


@pytest.mark.asyncio
async def test_real_stream_disconnect_after_visible_content_is_incomplete():
    payloads = await _payloads(
        _Agent(
            AgentResult(content="agent content", metadata={"finish_reason": "stop"}),
            engine=_StreamingEngine("stop", fail_after_content=True),
        )
    )

    assert _terminal(payloads) == "length"


@pytest.mark.asyncio
async def test_agent_exception_is_explicit_error_not_stop():
    payloads = await _payloads(
        _FailingAgent(AgentResult(content="unused", metadata={"finish_reason": "stop"}))
    )

    assert _terminal(payloads) == "error"
