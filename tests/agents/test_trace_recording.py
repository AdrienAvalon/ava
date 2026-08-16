"""Tests for trace recording in AgentExecutor."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.executor import AgentExecutor
from openjarvis.agents.manager import AgentManager
from openjarvis.core.events import EventBus, EventType
from openjarvis.traces.store import TraceStore


def test_executor_records_trace(tmp_path):
    """execute_tick records a trace with steps to TraceStore."""
    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus, trace_store=trace_store)

    agent = mgr.create_agent("trace-test")

    def fake_invoke(agent_dict):
        bus.publish(
            EventType.TOOL_CALL_START,
            {
                "agent": agent_dict["id"],
                "tool": "web_search",
                "args": {"query": "test"},
            },
        )
        bus.publish(
            EventType.TOOL_CALL_END,
            {
                "agent": agent_dict["id"],
                "tool": "web_search",
                "result": "search results...",
                "duration": 0.5,
            },
        )
        return AgentResult(
            content="found it",
            metadata={"finish_reason": "stop", "tokens_used": 100},
        )

    with patch.object(executor, "_invoke_agent", side_effect=fake_invoke):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    assert traces[0].agent == agent["id"]
    assert traces[0].outcome == "success"
    assert len(traces[0].steps) == 1
    assert traces[0].steps[0].step_type.value == "tool_call"
    assert traces[0].steps[0].input["tool"] == "web_search"
    assert traces[0].total_latency_seconds > 0

    mgr.close()
    trace_store.close()


def test_executor_attributes_owned_agent_trace_to_verified_provenance(tmp_path):
    """Managed HTTP ownership follows execution into the shared trace store."""

    owner = "principal:oidc:sha256:" + ("b" * 64)
    mgr = AgentManager(str(tmp_path / "owned-agents.db"))
    trace_store = TraceStore(str(tmp_path / "owned-traces.db"))
    executor = AgentExecutor(mgr, EventBus(), trace_store=trace_store)
    agent = mgr.create_agent("owned", owner_provenance=owner)

    with patch.object(
        executor,
        "_invoke_agent",
        return_value=AgentResult(content="done", metadata={"finish_reason": "stop"}),
    ):
        executor.execute_tick(agent["id"])

    owned_traces = trace_store.list_traces(
        agent=agent["id"],
        provenance=owner,
    )
    assert len(owned_traces) == 1
    assert owned_traces[0].metadata == {"provenance": owner}

    mgr.close()
    trace_store.close()


def test_executor_records_error_trace(tmp_path):
    """execute_tick records an error trace on failure."""
    from openjarvis.agents.errors import FatalError

    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus, trace_store=trace_store)

    agent = mgr.create_agent("error-trace")

    with patch.object(
        executor,
        "_invoke_agent",
        side_effect=FatalError("boom"),
    ):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    assert traces[0].outcome == "error"

    mgr.close()
    trace_store.close()


@pytest.mark.parametrize("metadata", [{"finish_reason": "length"}, {}])
def test_executor_records_incomplete_result_as_error_trace(tmp_path, metadata):
    """Partial model output must never become a successful training trace."""

    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    executor = AgentExecutor(mgr, EventBus(), trace_store=trace_store)
    agent = mgr.create_agent("incomplete-trace")

    with patch.object(
        executor,
        "_invoke_agent",
        return_value=AgentResult(content="PARTIAL_OUTPUT", metadata=metadata),
    ):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    assert traces[0].outcome == "error"
    assert traces[0].metadata["error_detail"]["error_type"] == "fatal"
    assert traces[0].metadata["error_detail"]["error_message"] == (
        "managed agent produced an incomplete response"
    )

    mgr.close()
    trace_store.close()


def test_executor_no_trace_without_store(tmp_path):
    """Without trace_store, no error is raised."""
    mgr = AgentManager(str(tmp_path / "agents.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus)  # No trace_store

    agent = mgr.create_agent("no-trace")

    def fake_invoke(agent_dict):
        return AgentResult(content="done", metadata={"finish_reason": "stop"})

    with patch.object(executor, "_invoke_agent", side_effect=fake_invoke):
        executor.execute_tick(agent["id"])

    updated = mgr.get_agent(agent["id"])
    assert updated["status"] == "idle"
    mgr.close()
