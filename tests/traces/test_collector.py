"""Tests for the TraceCollector."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import pytest

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, ToolResult
from openjarvis.traces.collector import TraceCollector, TraceContentFilterResult
from openjarvis.traces.store import TraceStore


class _FakeAgent(BaseAgent):
    """Minimal agent that returns a fixed response."""

    agent_id = "fake"

    def __init__(
        self,
        response: str = "test response",
        bus: Optional[EventBus] = None,
    ) -> None:
        self._response = response
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        # Simulate an inference step via event bus
        if self._bus:
            self._bus.publish(
                EventType.INFERENCE_START,
                {
                    "model": "qwen3:8b",
                    "engine": "ollama",
                },
            )
            self._bus.publish(
                EventType.INFERENCE_END,
                {
                    "total_tokens": 50,
                },
            )
        return AgentResult(
            content=self._response,
            turns=1,
            metadata={"finish_reason": "stop"},
        )


class _ToolAgent(BaseAgent):
    """Agent that simulates a tool call during execution."""

    agent_id = "tool_agent"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        # Simulate inference + tool call + inference
        inf = {"model": "qwen3:8b", "engine": "ollama"}
        self._bus.publish(EventType.INFERENCE_START, inf)
        self._bus.publish(EventType.INFERENCE_END, {"total_tokens": 30})
        self._bus.publish(
            EventType.TOOL_CALL_START,
            {
                "tool": "calculator",
                "arguments": {"expr": "2+2"},
            },
        )
        self._bus.publish(
            EventType.TOOL_CALL_END,
            {
                "tool": "calculator",
                "success": True,
                "latency": 0.01,
            },
        )
        self._bus.publish(EventType.INFERENCE_START, inf)
        self._bus.publish(EventType.INFERENCE_END, {"total_tokens": 20})
        return AgentResult(content="4", turns=2, metadata={"finish_reason": "stop"})


class TestTraceCollector:
    def test_basic_collection(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="hello", bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        result = collector.run("say hello")

        assert result.content == "hello"
        assert store.count() == 1

        traces = store.list_traces()
        trace = traces[0]
        assert trace.query == "say hello"
        assert trace.agent == "fake"
        assert trace.model == "qwen3:8b"
        assert trace.engine == "ollama"
        assert trace.result == "hello"
        assert trace.outcome == "completed"
        store.close()

    def test_missing_or_truncated_terminal_is_never_completed(
        self, tmp_path: Path
    ) -> None:
        for suffix, metadata in (
            ("missing", {}),
            ("length", {"finish_reason": "length"}),
        ):
            store = TraceStore(tmp_path / f"{suffix}.db")
            agent = _FakeAgent(response="fragment")

            def run_truncated(*args, _metadata=metadata, **kwargs):
                return AgentResult(
                    content="fragment",
                    turns=1,
                    metadata=_metadata,
                )

            agent.run = run_truncated

            TraceCollector(agent, store=store).run("question")

            trace = store.list_traces()[0]
            assert trace.outcome == "incomplete"
            store.close()

    def test_pre_start_tool_refusal_cannot_be_erased_by_later_successes(
        self, tmp_path: Path
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "pre-start-refusal.db")

        class _RefusedThenSuccessfulAgent(BaseAgent):
            agent_id = "refused_then_successful"

            def __init__(self) -> None:
                self._bus = bus

            def run(
                self,
                input: str,
                context: Optional[AgentContext] = None,
                **kwargs: Any,
            ) -> AgentResult:
                del input, context, kwargs
                successful: list[ToolResult] = []
                for name in ("lire_doc", "avalon_status", "proposer_plan"):
                    self._bus.publish(
                        EventType.TOOL_CALL_START,
                        {"tool": name, "arguments": {}},
                    )
                    self._bus.publish(
                        EventType.TOOL_CALL_END,
                        {"tool": name, "success": True, "result": "ok"},
                    )
                    successful.append(
                        ToolResult(tool_name=name, content="ok", success=True)
                    )
                return AgentResult(
                    content="Plan calculé",
                    tool_results=[
                        ToolResult(
                            tool_name="proposer",
                            content="Capability denied",
                            success=False,
                        ),
                        *successful,
                    ],
                    turns=2,
                    metadata={"finish_reason": "stop"},
                )

        collector = TraceCollector(
            _RefusedThenSuccessfulAgent(),
            store=store,
            bus=bus,
        )

        collector.run("inspect")

        trace = store.list_traces()[0]
        assert trace.outcome == "recovered"
        assert [
            step.input["tool"]
            for step in trace.steps
            if step.step_type == StepType.TOOL_CALL
        ] == ["lire_doc", "avalon_status", "proposer_plan"]
        assert "Capability denied" not in repr(trace)
        store.close()

    def test_records_generate_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace = store.list_traces()[0]
        generate_steps = [s for s in trace.steps if s.step_type == StepType.GENERATE]
        assert len(generate_steps) == 1
        assert generate_steps[0].output.get("tokens") == 50
        store.close()

    @pytest.mark.parametrize("content", [None, {"provider": "rich"}])
    def test_no_content_filter_preserves_inference_content(
        self,
        tmp_path: Path,
        content: Any,
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "unfiltered-content.db")

        class _UnfilteredContentAgent(BaseAgent):
            agent_id = "unfiltered_content"

            def __init__(self) -> None:
                self._bus = bus

            def run(
                self,
                input: str,
                context: Optional[AgentContext] = None,
                **kwargs: Any,
            ) -> AgentResult:
                del input, context, kwargs
                bus.publish(
                    EventType.INFERENCE_START,
                    {"model": "test-model", "engine": "test"},
                )
                bus.publish(
                    EventType.INFERENCE_END,
                    {"content": content, "total_tokens": 1},
                )
                return AgentResult(
                    content="safe",
                    turns=1,
                    metadata={"finish_reason": "stop"},
                )

        TraceCollector(
            _UnfilteredContentAgent(),
            store=store,
            bus=bus,
        ).run("question")

        trace = store.list_traces()[0]
        generate_step = next(
            step for step in trace.steps if step.step_type == StepType.GENERATE
        )
        assert generate_step.output["content"] == content
        store.close()

    def test_records_tool_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _ToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].input["tool"] == "calculator"
        assert tool_steps[0].output["success"] is True
        store.close()

    def test_records_respond_step(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="final answer", bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace = store.list_traces()[0]
        respond_steps = [s for s in trace.steps if s.step_type == StepType.RESPOND]
        assert len(respond_steps) == 1
        assert respond_steps[0].output["content"] == "final answer"
        store.close()

    def test_records_memory_retrieve(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        # Monkey-patch agent to emit memory event
        original_run = agent.run

        def run_with_memory(input, context=None, **kwargs):
            bus.publish(
                EventType.MEMORY_RETRIEVE,
                {
                    "query": "meeting notes",
                    "num_results": 3,
                    "latency": 0.2,
                },
            )
            return original_run(input, context=context, **kwargs)

        agent.run = run_with_memory
        collector.run("find my meeting notes")

        trace = store.list_traces()[0]
        retrieve_steps = [s for s in trace.steps if s.step_type == StepType.RETRIEVE]
        assert len(retrieve_steps) == 1
        assert retrieve_steps[0].input["query"] == "meeting notes"
        store.close()

    def test_publishes_trace_complete(self, tmp_path: Path) -> None:
        bus = EventBus(record_history=True)
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace_events = [
            e for e in bus.history if e.event_type == EventType.TRACE_COMPLETE
        ]
        assert len(trace_events) == 1
        assert trace_events[0].data["trace"].query == "test"
        store.close()

    def test_no_store(self) -> None:
        """Collector works without a store (just collects, doesn't persist)."""
        bus = EventBus()
        agent = _FakeAgent(response="ok", bus=bus)
        collector = TraceCollector(agent, bus=bus)  # no store

        result = collector.run("test")
        assert result.content == "ok"

    def test_no_bus(self, tmp_path: Path) -> None:
        """Collector works without a bus (no event-based step collection)."""
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="ok")
        collector = TraceCollector(agent, store=store)  # no bus

        result = collector.run("test")
        assert result.content == "ok"
        assert store.count() == 1
        # Only the RESPOND step (no events to capture)
        trace = store.list_traces()[0]
        assert len(trace.steps) == 1
        assert trace.steps[0].step_type == StepType.RESPOND
        store.close()

    def test_timing(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        before = time.time()
        collector.run("test")
        after = time.time()

        trace = store.list_traces()[0]
        assert trace.started_at >= before
        assert trace.ended_at <= after
        assert trace.ended_at >= trace.started_at
        store.close()

    def test_unsubscribes_after_run(self, tmp_path: Path) -> None:
        """Events after run() completes should NOT affect the next trace."""
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("first")

        # Emit events after run — should not affect stored trace
        bus.publish(EventType.INFERENCE_START, {"model": "stray"})
        bus.publish(EventType.INFERENCE_END, {"total_tokens": 999})

        assert store.count() == 1
        trace = store.list_traces()[0]
        # No step with model="stray"
        for s in trace.steps:
            assert s.input.get("model") != "stray"
        store.close()


class _RichToolAgent(BaseAgent):
    """Agent that emits content-enriched events for testing."""

    agent_id = "rich_tool_agent"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        from openjarvis.core.types import ToolResult

        # Turn 1: inference with tool call request
        self._bus.publish(
            EventType.INFERENCE_START,
            {
                "model": "test-model",
                "engine": "test",
            },
        )
        self._bus.publish(
            EventType.INFERENCE_END,
            {
                "total_tokens": 30,
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                "content": "I'll calculate that for you.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "calculator",
                        "arguments": '{"expr": "2+2"}',
                    },
                ],
                "finish_reason": "tool_calls",
            },
        )
        # Tool execution
        self._bus.publish(
            EventType.TOOL_CALL_START,
            {
                "tool": "calculator",
                "arguments": {"expr": "2+2"},
            },
        )
        self._bus.publish(
            EventType.TOOL_CALL_END,
            {
                "tool": "calculator",
                "success": True,
                "latency": 0.01,
                "result": "4",
            },
        )
        # Turn 2: final answer
        self._bus.publish(
            EventType.INFERENCE_START,
            {
                "model": "test-model",
                "engine": "test",
            },
        )
        self._bus.publish(
            EventType.INFERENCE_END,
            {
                "total_tokens": 15,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "content": "The answer is 4.",
                "tool_calls": [],
                "finish_reason": "stop",
            },
        )

        # Return result with messages in metadata
        messages = [
            {"role": "user", "content": input},
            {"role": "assistant", "content": "I'll calculate that for you."},
            {"role": "tool", "content": "4", "name": "calculator"},
            {"role": "assistant", "content": "The answer is 4."},
        ]
        return AgentResult(
            content="The answer is 4.",
            tool_results=[
                ToolResult(tool_name="calculator", content="4", success=True),
            ],
            turns=2,
            metadata={"messages": messages, "finish_reason": "stop"},
        )


class TestRichTraceCollector:
    def test_captures_content_in_generate_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        gen_steps = [s for s in trace.steps if s.step_type == StepType.GENERATE]
        assert len(gen_steps) == 2
        assert gen_steps[0].output["content"] == "I'll calculate that for you."
        expected_tc = [
            {"id": "call_1", "name": "calculator", "arguments": '{"expr": "2+2"}'},
        ]
        assert gen_steps[0].output["tool_calls"] == expected_tc
        assert gen_steps[0].output["finish_reason"] == "tool_calls"
        assert gen_steps[1].output["content"] == "The answer is 4."
        assert gen_steps[1].output["finish_reason"] == "stop"
        store.close()

    def test_request_filter_scrubs_before_store_and_trace_complete(
        self, tmp_path: Path
    ) -> None:
        canary = "CANARY-UNSAFE-OUTPUT"
        replacement = "Safe replacement."
        bus = EventBus()
        store = TraceStore(tmp_path / "filtered.db")
        published: list[object] = []
        bus.subscribe(
            EventType.TRACE_COMPLETE,
            lambda event: published.append(event.data["trace"]),
        )

        class _UnsafeAgent(BaseAgent):
            agent_id = "unsafe"

            def __init__(self) -> None:
                pass

            def run(
                self,
                input: str,
                context: Optional[AgentContext] = None,
                **kwargs: Any,
            ) -> AgentResult:
                del input, context, kwargs
                bus.publish(
                    EventType.INFERENCE_START,
                    {"model": "test-model", "engine": "test"},
                )
                bus.publish(
                    EventType.INFERENCE_END,
                    {
                        "content": canary,
                        "tool_calls": [
                            {
                                "id": "call-unsafe",
                                "name": "probe",
                                "arguments": f'{{"query":"{canary}"}}',
                            }
                        ],
                        "tool_results": [{"content": canary}],
                        "content_blocks": [{"text": canary}],
                        "finish_reason": "tool_calls",
                    },
                )
                bus.publish(
                    EventType.TOOL_CALL_START,
                    {"tool": "probe", "arguments": {"query": canary}},
                )
                bus.publish(
                    EventType.TOOL_CALL_END,
                    {"tool": "probe", "success": True, "result": canary},
                )
                return AgentResult(
                    content=canary,
                    tool_results=[
                        ToolResult(tool_name="probe", content=canary, success=True)
                    ],
                    turns=1,
                    metadata={
                        "finish_reason": "tool_calls",
                        "audio_path": f"/tmp/{canary}.wav",
                        "content_blocks": [{"text": canary}],
                        "messages": [
                            {"role": "user", "content": "question"},
                            {
                                "role": "assistant",
                                "content": canary,
                                "tool_calls": [{"arguments": canary}],
                                "content_blocks": [{"text": canary}],
                            },
                        ],
                    },
                )

        def content_filter(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del allow_conversation_echo, final
            blocked = canary in content or any(
                canary in item for item in structured_output_json
            )
            return TraceContentFilterResult(
                content=replacement if blocked else content,
                suppress_structured_output=blocked,
                force_stop=blocked,
            )

        collector = TraceCollector(_UnsafeAgent(), store=store, bus=bus)
        result = collector.run(
            "question",
            content_filter=content_filter,
            trace_metadata_provider=lambda: {"filter_policy": "test-v1"},
        )

        assert result.content == replacement
        assert result.tool_results == []
        assert result.metadata["finish_reason"] == "stop"
        assert "audio_path" not in result.metadata
        assert "content_blocks" not in result.metadata
        assert canary not in repr(result)
        trace = store.list_traces()[0]
        assert trace.result == replacement
        assert trace.metadata["filter_policy"] == "test-v1"
        assert all(step.step_type != StepType.TOOL_CALL for step in trace.steps)
        assert canary not in repr(trace)
        assert len(published) == 1
        assert canary not in repr(published[0])
        store.close()

    def test_active_filter_always_drops_rich_content_blocks(
        self, tmp_path: Path
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "content-blocks.db")

        class _ContentBlockAgent(_FakeAgent):
            def run(self, *args: Any, **kwargs: Any) -> AgentResult:
                bus.publish(
                    EventType.INFERENCE_START,
                    {"model": "test-model", "engine": "test"},
                )
                bus.publish(
                    EventType.INFERENCE_END,
                    {
                        "content": "safe",
                        "content_blocks": [{"text": "safe"}],
                        "finish_reason": "stop",
                    },
                )
                return AgentResult(
                    content="safe",
                    turns=1,
                    metadata={"finish_reason": "stop"},
                )

        def allow(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del structured_output_json, allow_conversation_echo, final
            return TraceContentFilterResult(content=content)

        TraceCollector(_ContentBlockAgent(), store=store, bus=bus).run(
            "question",
            content_filter=allow,
        )

        generate = next(
            step
            for step in store.list_traces()[0].steps
            if step.step_type == StepType.GENERATE
        )
        assert "content_blocks" not in generate.output
        store.close()

    def test_active_filter_canonicalizes_divergent_messages_and_drops_system(
        self, tmp_path: Path
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "canonical-messages.db")
        canary = "MESSAGE-CANARY Je suis jalouse."
        private_prompt = "PRIVATE-RELATIONSHIP-OVERLAY-CANARY"
        replacement = "Message assistant retire."

        class _DivergentMessagesAgent(_FakeAgent):
            def run(self, *args: Any, **kwargs: Any) -> AgentResult:
                return AgentResult(
                    content="Synthese finale sure.",
                    turns=1,
                    metadata={
                        "finish_reason": "stop",
                        "messages": [
                            {"role": "system", "content": private_prompt},
                            {
                                "role": "user",
                                "content": "question",
                                "provider_field": "not persisted",
                            },
                            {
                                "role": "assistant",
                                "content": canary,
                                "tool_calls": [{"arguments": "{}"}],
                                "metadata": {"opaque": canary},
                                "images": [canary],
                            },
                        ],
                    },
                )

        def content_filter(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del allow_conversation_echo, final
            blocked = canary in content or any(
                canary in item for item in structured_output_json
            )
            return TraceContentFilterResult(
                content=replacement if blocked else content,
                suppress_structured_output=blocked,
                force_stop=blocked,
            )

        result = TraceCollector(_DivergentMessagesAgent(), store=store, bus=bus).run(
            "question", content_filter=content_filter
        )

        assert result.content == "Synthese finale sure."
        trace = store.list_traces()[0]
        assert trace.result == "Synthese finale sure."
        assert trace.messages == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": replacement},
        ]
        assert canary not in repr(trace)
        assert private_prompt not in repr(trace)
        store.close()

    def test_active_filter_rejects_unknown_message_role_without_raw_persistence(
        self, tmp_path: Path
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "unknown-message-role.db")
        canary = "UNKNOWN-ROLE-CANARY Je suis jalouse."

        class _UnknownRoleAgent(_FakeAgent):
            def run(self, *args: Any, **kwargs: Any) -> AgentResult:
                return AgentResult(
                    content="Synthese finale sure.",
                    turns=1,
                    metadata={
                        "finish_reason": "stop",
                        "messages": [{"role": "model", "content": canary}],
                    },
                )

        def allow(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del structured_output_json, allow_conversation_echo, final
            return TraceContentFilterResult(content=content)

        with pytest.raises(RuntimeError, match="trace content filtering failed"):
            TraceCollector(_UnknownRoleAgent(), store=store, bus=bus).run(
                "question",
                content_filter=allow,
            )

        traces = store.list_traces()
        assert traces == []
        assert canary not in repr(traces)
        store.close()

    def test_tool_start_arguments_never_use_conversation_echo(
        self, tmp_path: Path
    ) -> None:
        copied_user_turn = "Une question substantielle avec plus de quatre mots."
        bus = EventBus()
        store = TraceStore(tmp_path / "tool-echo.db")

        class _EchoingToolAgent(BaseAgent):
            agent_id = "tool-echo"

            def __init__(self) -> None:
                pass

            def run(self, *args: Any, **kwargs: Any) -> AgentResult:
                bus.publish(
                    EventType.TOOL_CALL_START,
                    {"tool": "search", "arguments": {"query": copied_user_turn}},
                )
                bus.publish(
                    EventType.TOOL_CALL_END,
                    {"tool": "search", "success": True, "result": "safe page"},
                )
                return AgentResult(
                    content="Synthese sure.",
                    turns=1,
                    metadata={"finish_reason": "stop"},
                )

        def echo_sensitive_filter(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del final
            echoed = allow_conversation_echo and any(
                copied_user_turn in item for item in structured_output_json
            )
            return TraceContentFilterResult(
                content="Blocked echo." if echoed else content,
                suppress_structured_output=echoed,
                force_stop=echoed,
            )

        result = TraceCollector(_EchoingToolAgent(), store=store, bus=bus).run(
            "question", content_filter=echo_sensitive_filter
        )

        trace = store.list_traces()[0]
        assert result.content == "Synthese sure."
        assert trace.result == "Synthese sure."
        assert trace.outcome == "completed"
        assert any(step.step_type == StepType.TOOL_CALL for step in trace.steps)
        store.close()

    def test_unsafe_external_tool_result_is_scrubbed_without_sticky_replace(
        self, tmp_path: Path
    ) -> None:
        canary = "EXTERNAL-CANARY Je suis jalouse."
        replacement = "External content removed."
        bus = EventBus()
        store = TraceStore(tmp_path / "tool-result.db")

        class _ExternalToolAgent(BaseAgent):
            agent_id = "external-tool"

            def __init__(self) -> None:
                pass

            def run(self, *args: Any, **kwargs: Any) -> AgentResult:
                bus.publish(
                    EventType.TOOL_CALL_START,
                    {"tool": "search", "arguments": {"query": "safe"}},
                )
                bus.publish(
                    EventType.TOOL_CALL_END,
                    {"tool": "search", "success": True, "result": canary},
                )
                return AgentResult(
                    content="Synthese finale sure.",
                    tool_results=[
                        ToolResult(tool_name="search", content=canary, success=True)
                    ],
                    turns=1,
                    metadata={"finish_reason": "stop"},
                )

        def text_gate_filter(
            content: str,
            structured_output_json: tuple[str, ...],
            *,
            allow_conversation_echo: bool,
            final: bool,
        ) -> TraceContentFilterResult:
            del allow_conversation_echo, final
            blocked = canary in content or any(
                canary in item for item in structured_output_json
            )
            return TraceContentFilterResult(
                content=replacement if blocked else content,
                suppress_structured_output=blocked,
                force_stop=blocked,
            )

        result = TraceCollector(_ExternalToolAgent(), store=store, bus=bus).run(
            "question", content_filter=text_gate_filter
        )

        trace = store.list_traces()[0]
        assert result.content == "Synthese finale sure."
        assert result.tool_results[0].content == replacement
        assert result.metadata["finish_reason"] == "stop"
        assert trace.result == "Synthese finale sure."
        assert trace.outcome == "completed"
        assert canary not in repr(trace)
        tool_step = next(
            step for step in trace.steps if step.step_type == StepType.TOOL_CALL
        )
        assert tool_step.output["result"] == replacement
        store.close()

    def test_captures_tool_arguments_and_result(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].input["tool"] == "calculator"
        assert tool_steps[0].input["arguments"] == {"expr": "2+2"}
        assert tool_steps[0].output["result"] == "4"
        assert tool_steps[0].output["success"] is True
        store.close()

    def test_captures_messages_in_trace(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        assert len(trace.messages) == 4
        assert trace.messages[0]["role"] == "user"
        assert trace.messages[3]["role"] == "assistant"
        store.close()

    def test_last_trace_property(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = collector.last_trace
        assert trace is not None
        assert trace.query == "What is 2+2?"
        assert len(trace.messages) == 4
        store.close()


# ══ `recovered` — un outil qui échoue n'est pas un tour qui échoue ════════════════


class _AgentOutilCasse:
    """Un agent dont l'outil échoue ; le contenu rendu est paramétrable."""

    agent_id = "agent_casse"

    def __init__(self, bus: EventBus, contenu: str) -> None:
        self._bus, self._contenu = bus, contenu

    def run(self, input: str, context: Any = None, **kwargs: Any) -> AgentResult:  # noqa: A002
        inf = {"model": "qwen3:8b", "engine": "ollama"}
        self._bus.publish(EventType.INFERENCE_START, inf)
        self._bus.publish(EventType.INFERENCE_END, {"total_tokens": 30})
        self._bus.publish(EventType.TOOL_CALL_START, {"tool": "logs", "arguments": {}})
        self._bus.publish(
            EventType.TOOL_CALL_END, {"tool": "logs", "success": False, "latency": 0.01}
        )
        return AgentResult(
            content=self._contenu,
            turns=1,
            metadata={"finish_reason": "stop"},
        )


def _trace_avec_outil_casse(tmp_path: Path, contenu: str):
    bus = EventBus()
    store = TraceStore(tmp_path / "t.db")
    collector = TraceCollector(_AgentOutilCasse(bus, contenu), store=store, bus=bus)
    collector.run("question")
    trace = store.list_traces()[0]
    store.close()
    return trace


def test_un_ECHEC_D_OUTIL_SUIVI_D_UNE_REPONSE_est_RECOVERED(tmp_path: Path) -> None:
    """⚠ MON DÉFAUT, écrit le matin même du 2026-08-06. La règle disait « s'il y a un
    échec d'outil, c'est `tool_failure` », SANS regarder si la réponse avait été livrée.
    Un agent qui se heurte à un outil, se reprend et rend une réponse complète était
    noté comme un échec, avec `feedback = 0.0`.
    ⚠ Mesure sur 210 traces : **19 des 21 `tool_failure` avaient livré une réponse**
    (médiane 978 caractères, jusqu'à 7537). Le taux publié tombait à 89 % quand le réel
    est 98,1 % — et c'est Ava qui lit ce chiffre sur elle-même via `introspection`. Un
    système qui note ses propres réussites comme des échecs n'apprend pas : il apprend à
    se croire mauvais."""
    trace = _trace_avec_outil_casse(tmp_path, "Une réponse complète et utile.")
    assert trace.outcome == "recovered"
    assert trace.feedback is None, (
        "une note nulle sur une réussite est le défaut corrigé"
    )


def test_un_ECHEC_D_OUTIL_SANS_REPONSE_reste_TOOL_FAILURE(tmp_path: Path) -> None:
    """⚠ LE CONTRE-TEST. Élargir `recovered` à tous les cas masquerait les vrais
    échecs — on remplacerait un chiffre pessimiste par un chiffre flatteur, ce qui est
    pire : le premier fait chercher, le second fait dormir."""
    trace = _trace_avec_outil_casse(tmp_path, "")
    assert trace.outcome == "tool_failure"
    # ⚠ `feedback` reste NULL : la machine ne juge pas la QUALITE. Defaut trouve en lui
    #   parlant le 2026-08-06 — elle lisait ses propres verdicts automatiques comme des
    #   notes de l'admin, et se croyait jugee negativement par quelqu'un qui ne l'avait
    #   jamais notee. `feedback is not None` signifie desormais « un humain a juge ».
    assert trace.feedback is None
