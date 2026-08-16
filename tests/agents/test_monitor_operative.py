"""Tests for MonitorOperativeAgent."""

from unittest.mock import MagicMock

import pytest

from openjarvis.agents.monitor_operative import MonitorOperativeAgent
from openjarvis.core.registry import AgentRegistry


def _make_engine(content: str = "Hello") -> MagicMock:
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.generate.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "model": "test-model",
        "finish_reason": "stop",
    }
    return engine


class TestMonitorOperativeAgent:
    def test_registration(self) -> None:
        # Import triggers registration; re-register after autouse fixture
        # clears the registry (same pattern as test_monitor.py)
        import openjarvis.agents.monitor_operative  # noqa: F401

        if not AgentRegistry.contains("monitor_operative"):
            AgentRegistry.register_value("monitor_operative", MonitorOperativeAgent)
        assert AgentRegistry.contains("monitor_operative")
        cls = AgentRegistry.get("monitor_operative")
        assert cls is MonitorOperativeAgent

    def test_instantiation(self) -> None:
        engine = _make_engine()
        agent = MonitorOperativeAgent(engine, "test-model")
        assert agent.agent_id == "monitor_operative"
        assert agent.accepts_tools is True

    def test_default_strategies(self) -> None:
        engine = _make_engine()
        agent = MonitorOperativeAgent(engine, "test-model")
        assert agent._memory_extraction == "causality_graph"
        assert agent._observation_compression == "summarize"
        assert agent._retrieval_strategy == "hybrid_with_self_eval"
        assert agent._task_decomposition == "phased"

    def test_custom_strategies(self) -> None:
        engine = _make_engine()
        agent = MonitorOperativeAgent(
            engine,
            "test-model",
            memory_extraction="scratchpad",
            observation_compression="none",
            retrieval_strategy="keyword",
            task_decomposition="monolithic",
        )
        assert agent._memory_extraction == "scratchpad"
        assert agent._observation_compression == "none"

    def test_simple_run(self) -> None:
        engine = _make_engine("The answer is 42.")
        agent = MonitorOperativeAgent(engine, "test-model")
        result = agent.run("What is the answer?")
        assert result.content == "The answer is 42."
        assert result.turns >= 1
        assert result.metadata["finish_reason"] == "stop"

    @pytest.mark.parametrize("finish_reason", ["length", "max_tokens", None])
    def test_incomplete_native_tool_call_is_not_executed(self, finish_reason) -> None:
        engine = _make_engine("partial")
        engine.generate.return_value.update(
            {
                "finish_reason": finish_reason,
                "tool_calls": [
                    {
                        "id": "partial",
                        "name": "think",
                        "arguments": '{"thought":"partial"}',
                    }
                ],
            }
        )
        agent = MonitorOperativeAgent(engine, "test-model")
        agent._executor.execute = MagicMock()

        result = agent.run("Do not execute")

        agent._executor.execute.assert_not_called()
        assert result.metadata["finish_reason"] == (
            "length" if finish_reason == "max_tokens" else finish_reason
        )
