"""Tests for AgentExecutor single-tick execution."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.errors import FatalError, RetryableError
from openjarvis.core.events import EventBus, EventType


@pytest.fixture
def manager():
    from openjarvis.agents.manager import AgentManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = AgentManager(db_path=str(Path(tmpdir) / "agents.db"))
        yield mgr
        mgr.close()


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def executor(manager, event_bus):
    from openjarvis.agents.executor import AgentExecutor

    mock_system = MagicMock()
    ex = AgentExecutor(manager=manager, event_bus=event_bus)
    ex.set_system(mock_system)
    return ex


class TestExecutorBasic:
    def test_execute_tick_publishes_start_end_events(
        self, executor, manager, event_bus
    ):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")
        events = []
        event_bus.subscribe(EventType.AGENT_TICK_START, lambda e: events.append(e))
        event_bus.subscribe(EventType.AGENT_TICK_END, lambda e: events.append(e))

        rv = AgentResult(content="result text", metadata={"finish_reason": "stop"})
        with patch.object(executor, "_invoke_agent", return_value=rv):
            executor.execute_tick(agent["id"])

        assert len(events) == 2
        assert events[0].event_type == EventType.AGENT_TICK_START
        assert events[1].event_type == EventType.AGENT_TICK_END

    def test_execute_tick_updates_run_stats(self, executor, manager):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")

        rv = AgentResult(content="result text", metadata={"finish_reason": "stop"})
        with patch.object(executor, "_invoke_agent", return_value=rv):
            executor.execute_tick(agent["id"])

        updated = manager.get_agent(agent["id"])
        assert updated["total_runs"] == 1
        assert updated["status"] == "idle"

    def test_execute_tick_sets_running_then_idle(self, executor, manager):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")
        statuses = []

        original_start = manager.start_tick

        def track_start(aid):
            token = original_start(aid)
            statuses.append(manager.get_agent(aid)["status"])
            return token

        manager.start_tick = track_start

        rv = AgentResult(content="result", metadata={"finish_reason": "stop"})
        with patch.object(executor, "_invoke_agent", return_value=rv):
            executor.execute_tick(agent["id"])

        assert statuses == ["running"]
        assert manager.get_agent(agent["id"])["status"] == "idle"

    def test_execute_tick_handles_fatal_error(self, executor, manager, event_bus):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")
        errors = []
        event_bus.subscribe(EventType.AGENT_TICK_ERROR, lambda e: errors.append(e))

        with patch.object(
            executor, "_invoke_agent", side_effect=FatalError("bad config")
        ):
            executor.execute_tick(agent["id"])

        assert manager.get_agent(agent["id"])["status"] == "error"
        assert len(errors) == 1

    def test_execute_tick_retries_retryable_error(self, executor, manager):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")
        call_count = 0

        def flaky_invoke(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableError("rate limit")
            return AgentResult(content="success", metadata={"finish_reason": "stop"})

        with patch.object(executor, "_invoke_agent", side_effect=flaky_invoke):
            with patch("openjarvis.agents.executor.retry_delay", return_value=0):
                executor.execute_tick(agent["id"])

        assert call_count == 3
        assert manager.get_agent(agent["id"])["status"] == "idle"

    def test_execute_tick_gives_up_after_max_retries(self, executor, manager):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")

        with patch.object(
            executor, "_invoke_agent", side_effect=RetryableError("always fails")
        ):
            with patch("openjarvis.agents.executor.retry_delay", return_value=0):
                executor.execute_tick(agent["id"])

        assert manager.get_agent(agent["id"])["status"] == "error"

    def test_tick_claims_and_completes_exactly_one_queued_message(
        self, executor, manager
    ):
        agent = manager.create_agent(name="paired", agent_type="monitor_operative")
        first = manager.send_message(agent["id"], "first")
        second = manager.send_message(agent["id"], "second")

        with patch.object(
            executor,
            "_invoke_agent",
            return_value=AgentResult(
                content="first answer",
                metadata={"finish_reason": "stop"},
            ),
        ):
            executor.execute_tick(agent["id"])

        messages = manager.list_messages(agent["id"])
        by_id = {message["id"]: message for message in messages}
        response = next(
            message for message in messages if message["direction"] == "agent_to_user"
        )
        assert by_id[first["id"]]["status"] == "delivered"
        assert by_id[second["id"]]["status"] == "pending"
        assert response["reply_to_id"] == first["id"]

    def test_failed_tick_terminally_fails_only_its_claim(self, executor, manager):
        agent = manager.create_agent(name="failed", agent_type="monitor_operative")
        first = manager.send_message(agent["id"], "first")
        second = manager.send_message(agent["id"], "second")

        with patch.object(executor, "_invoke_agent", side_effect=FatalError("boom")):
            executor.execute_tick(agent["id"])

        by_id = {
            message["id"]: message for message in manager.list_messages(agent["id"])
        }
        assert by_id[first["id"]]["status"] == "failed"
        assert by_id[second["id"]]["status"] == "pending"

    @pytest.mark.parametrize(
        "metadata",
        [
            {"finish_reason": "length"},
            {"finish_reason": "max_tokens"},
            {},
            {"finish_reason": "stop", "max_turns_exceeded": True},
            {"finish_reason": "stop", "incomplete_tool_call": True},
        ],
    )
    def test_incomplete_result_fails_claim_without_assistant_reply(
        self, executor, manager, metadata
    ):
        agent = manager.create_agent(name="incomplete", agent_type="orchestrator")
        source = manager.send_message(agent["id"], "question")

        with patch.object(
            executor,
            "_invoke_agent",
            return_value=AgentResult(content="PARTIAL_OUTPUT", metadata=metadata),
        ):
            executor.execute_tick(agent["id"])

        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["id"] == source["id"]
        assert messages[0]["status"] == "failed"
        updated = manager.get_agent(agent["id"])
        assert updated["status"] == "error"
        assert updated["total_runs"] == 0

    def test_anthropic_end_turn_is_normalized_before_claim_commit(
        self, executor, manager
    ):
        agent = manager.create_agent(name="complete", agent_type="orchestrator")
        source = manager.send_message(agent["id"], "question")

        with patch.object(
            executor,
            "_invoke_agent",
            return_value=AgentResult(
                content="COMPLETE_OUTPUT",
                metadata={"finish_reason": "end_turn"},
            ),
        ):
            executor.execute_tick(agent["id"])

        messages = manager.list_messages(agent["id"])
        assert len(messages) == 2
        stored_source = next(
            message for message in messages if message["id"] == source["id"]
        )
        answer = next(
            message for message in messages if message["direction"] == "agent_to_user"
        )
        assert stored_source["status"] == "delivered"
        assert answer["content"] == "COMPLETE_OUTPUT"
        assert manager.get_agent(agent["id"])["status"] == "idle"

    def test_execute_tick_concurrency_guard(self, executor, manager):
        agent = manager.create_agent(name="test", agent_type="monitor_operative")
        manager.start_tick(agent["id"])  # Simulate already running

        # Second tick should handle the ValueError from start_tick
        rv = AgentResult(content="result", metadata={"finish_reason": "stop"})
        with patch.object(executor, "_invoke_agent", return_value=rv):
            executor.execute_tick(agent["id"])

        # Agent should still be running (first tick owns it)
        assert manager.get_agent(agent["id"])["status"] == "running"


def test_finalize_tick_reads_agent_result_metadata(tmp_path):
    """_finalize_tick() accumulates cost/tokens from AgentResult.metadata."""
    from openjarvis.agents.executor import AgentExecutor
    from openjarvis.agents.manager import AgentManager

    mgr = AgentManager(str(tmp_path / "test.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus)

    agent = mgr.create_agent("budget-agent")
    tick_token = mgr.start_tick(agent["id"])

    result = AgentResult(
        content="done",
        metadata={"finish_reason": "stop", "tokens_used": 500, "cost": 0.05},
    )
    executor._finalize_tick(
        agent["id"], result, error=None, duration=1.0, tick_token=tick_token
    )

    updated = mgr.get_agent(agent["id"])
    assert updated["total_tokens"] == 500
    assert updated["total_cost"] == 0.05
    assert updated["stall_retries"] == 0
    mgr.close()


def test_http_tick_uses_common_identity_without_private_agent_state(
    manager, event_bus, monkeypatch
):
    """Managed HTTP turns must not ingest or overwrite installation persona state."""
    from openjarvis.agents import AgentRegistry
    from openjarvis.agents.executor import AgentExecutor

    captured = {}

    class CapturingAgent:
        accepts_tools = False

        def __init__(self, engine, model, **kwargs):
            captured["engine"] = engine
            captured["model"] = model
            captured["kwargs"] = kwargs

        def run(self, input_text, context=None):
            captured["input"] = input_text
            captured["context"] = context
            return AgentResult(
                content="public answer",
                metadata={"finish_reason": "stop"},
            )

    monkeypatch.setattr(AgentRegistry, "get", lambda _key: CapturingAgent)
    engine = object()
    trace_store = MagicMock()
    executor = AgentExecutor(
        manager=manager,
        event_bus=event_bus,
        trace_store=trace_store,
    )
    executor.set_system(
        SimpleNamespace(
            engine=engine,
            model="fallback-model",
            config=None,
            http_boundary=True,
            memory_backend=None,
        )
    )
    agent = manager.create_agent(
        name="http",
        agent_type="capture",
        config={
            "temperature": 0.25,
            "max_tokens": 321,
            "max_turns": 4,
        },
    )
    manager.update_summary_memory(agent["id"], "PRIVATE_LEGACY_CANARY")
    claimed = manager.send_claimed_message(agent["id"], "question")

    tick_token = manager.start_tick(agent["id"])
    executor.execute_tick(
        agent["id"],
        lock_already_held=True,
        tick_token=tick_token,
        claimed_message=claimed,
    )

    assert captured["engine"] is engine
    assert captured["kwargs"] == {
        "bus": event_bus,
        "temperature": 0.25,
        "max_tokens": 321,
        "max_turns": 4,
    }
    assert "PRIVATE_LEGACY_CANARY" not in captured["input"]
    identity = captured["context"].metadata["server_identity_prompt"]
    assert "Ava" in identity
    assert manager.get_agent(agent["id"])["summary_memory"] == ("PRIVATE_LEGACY_CANARY")
    trace = trace_store.save.call_args.args[0]
    assert trace.query == "question"
    assert "PRIVATE_LEGACY_CANARY" not in trace.query


def test_background_tick_inherits_server_max_tokens_without_agent_override(
    manager, event_bus, monkeypatch
):
    from openjarvis.agents import AgentRegistry
    from openjarvis.agents.executor import AgentExecutor

    captured = {}

    class CapturingAgent:
        accepts_tools = False

        def __init__(self, _engine, _model, **kwargs):
            captured["kwargs"] = kwargs

        def run(self, _input_text, context=None):
            del context
            return AgentResult(
                content="complete",
                metadata={"finish_reason": "stop"},
            )

    monkeypatch.setattr(AgentRegistry, "get", lambda _key: CapturingAgent)
    server_config = SimpleNamespace(
        intelligence=SimpleNamespace(max_tokens=16_384),
        agent=SimpleNamespace(context_from_memory=False),
    )
    executor = AgentExecutor(manager=manager, event_bus=event_bus)
    executor.set_system(
        SimpleNamespace(
            engine=object(),
            model="fallback-model",
            config=server_config,
            http_boundary=True,
            memory_backend=None,
        )
    )
    agent = manager.create_agent(
        name="inherits-server-limit",
        agent_type="capture",
        config={"temperature": 0.25, "max_turns": 4},
    )

    result = executor._invoke_agent(agent)

    assert result.content == "complete"
    assert captured["kwargs"]["max_tokens"] == 16_384
    assert "max_tokens" not in manager.get_agent(agent["id"])["config"]


def test_claim_failure_releases_tick_as_error(manager, event_bus, monkeypatch):
    from openjarvis.agents.executor import AgentExecutor

    executor = AgentExecutor(manager=manager, event_bus=event_bus)
    agent = manager.create_agent(name="claim-failure", agent_type="simple")
    monkeypatch.setattr(
        manager,
        "claim_next_message",
        lambda _agent_id: (_ for _ in ()).throw(RuntimeError("claim failed")),
    )

    with pytest.raises(RuntimeError, match="claim failed"):
        executor.execute_tick(agent["id"])

    assert manager.get_agent(agent["id"])["status"] == "error"
