"""Tests for DeepResearchAgent -- multi-hop retrieval with citations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.deep_research import DeepResearchAgent
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import AgentRegistry
from openjarvis.tools.knowledge_search import KnowledgeSearchTool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.health.return_value = True
    return engine


@pytest.fixture
def store(tmp_path):
    s = KnowledgeStore(db_path=str(tmp_path / "dr_test.db"))
    s.store(
        content="Kubernetes migration proposed by Sarah",
        source="slack",
        doc_type="message",
        author="sarah",
    )
    s.store(
        content="Cost analysis shows 40% increase",
        source="gdrive",
        doc_type="document",
        author="sarah",
    )
    s.store(
        content="Migration approved March 8",
        source="gmail",
        doc_type="email",
        author="mike",
    )
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_response(content, tool_calls=None):
    result = {
        "content": content,
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 50,
            "total_tokens": 100,
        },
        "model": "test-model",
        "finish_reason": "stop",
    }
    if tool_calls:
        result["tool_calls"] = tool_calls
        result["finish_reason"] = "tool_calls"
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_agent_registration():
    """DeepResearchAgent registers under 'deep_research'."""
    AgentRegistry.register_value("deep_research", DeepResearchAgent)
    assert AgentRegistry.contains("deep_research")


def test_agent_produces_result(mock_engine, store):
    """Engine returns final answer directly -- verify AgentResult has content."""
    mock_engine.generate.return_value = _make_engine_response(
        "Based on my research, the Kubernetes migration was approved."
    )
    ks_tool = KnowledgeSearchTool(store=store)
    agent = DeepResearchAgent(mock_engine, "test-model", tools=[ks_tool])
    result = agent.run("What happened with the Kubernetes migration?")
    assert isinstance(result, AgentResult)
    assert "migration" in result.content.lower() or "Kubernetes" in result.content
    assert result.turns == 1
    assert result.tool_results == []


def test_agent_continues_anthropic_max_tokens_and_accumulates_usage(mock_engine, store):
    first = _make_engine_response("Rapport partiel")
    first["finish_reason"] = "max_tokens"
    last = _make_engine_response(" puis conclusion.")
    last["finish_reason"] = "end_turn"
    mock_engine.generate.side_effect = [first, last]
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )

    result = agent.run("Fais un rapport complet")

    assert result.content == "Rapport partiel puis conclusion."
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["prompt_tokens"] == 100
    assert result.metadata["completion_tokens"] == 100
    assert result.metadata["total_tokens"] == 200


def test_agent_propagates_exhausted_continuation_as_length(mock_engine, store):
    responses = []
    for text in ("A", "B", "C"):
        response = _make_engine_response(text)
        response["finish_reason"] = "max_tokens"
        responses.append(response)
    mock_engine.generate.side_effect = responses
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )

    result = agent.run("Réponse très longue")

    assert result.content == "ABC"
    assert result.metadata["finish_reason"] == "length"
    assert result.metadata["total_tokens"] == 300


def test_agent_uses_knowledge_search(mock_engine, store):
    """Engine returns tool_call first, then final answer; verify tool was called."""
    tool_call_response = _make_engine_response(
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": json.dumps({"query": "Kubernetes migration"}),
                },
            }
        ],
    )
    final_response = _make_engine_response(
        "The Kubernetes migration was proposed by Sarah and approved by Mike."
    )
    mock_engine.generate.side_effect = [tool_call_response, final_response]

    ks_tool = KnowledgeSearchTool(store=store)
    agent = DeepResearchAgent(mock_engine, "test-model", tools=[ks_tool])
    result = agent.run("Tell me about the Kubernetes migration")

    assert result.turns == 2
    assert len(result.tool_results) == 1
    assert result.tool_results[0].tool_name == "knowledge_search"
    assert result.tool_results[0].success is True
    assert "migration" in result.content.lower() or "Kubernetes" in result.content


def test_stop_terminal_with_structured_tool_call_executes(mock_engine, store):
    tool_call_response = _make_engine_response(
        "",
        tool_calls=[
            {
                "id": "call_stop",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": json.dumps({"query": "Kubernetes migration"}),
                },
            }
        ],
    )
    tool_call_response["finish_reason"] = "stop"
    mock_engine.generate.side_effect = [
        tool_call_response,
        _make_engine_response("Recherche terminée."),
    ]
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )

    result = agent.run("Recherche")

    assert len(result.tool_results) == 1
    assert result.metadata["finish_reason"] == "stop"


def test_stop_terminal_with_invalid_tool_json_never_executes(mock_engine, store):
    response = _make_engine_response(
        "partial",
        tool_calls=[
            {
                "id": "partial-call",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": '{"query":',
                },
            }
        ],
    )
    response["finish_reason"] = "stop"
    mock_engine.generate.return_value = response
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )
    agent._executor.execute = MagicMock()

    result = agent.run("Do not execute malformed JSON")

    agent._executor.execute.assert_not_called()
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["incomplete_tool_call"] is True


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", None])
def test_incomplete_tool_call_terminal_never_executes_tool(
    mock_engine, store, finish_reason
):
    response = _make_engine_response(
        "partial",
        tool_calls=[
            {
                "id": "partial-call",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": json.dumps({"query": "partial"}),
                },
            }
        ],
    )
    response["finish_reason"] = finish_reason
    mock_engine.generate.return_value = response
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )
    agent._executor.execute = MagicMock()

    result = agent.run("Do not run partial args")

    agent._executor.execute.assert_not_called()
    assert result.tool_results == []
    assert result.metadata["finish_reason"] == (
        "length" if finish_reason == "max_tokens" else finish_reason
    )


def test_forced_synthesis_continues_and_reports_its_terminal_reason(mock_engine, store):
    tool_call_response = _make_engine_response(
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": json.dumps({"query": "Kubernetes migration"}),
                },
            }
        ],
    )
    empty_final = _make_engine_response("")
    synth_start = _make_engine_response("Synthèse")
    synth_start["finish_reason"] = "max_tokens"
    synth_end = _make_engine_response(" complète.")
    synth_end["finish_reason"] = "end_turn"
    mock_engine.generate.side_effect = [
        tool_call_response,
        empty_final,
        synth_start,
        synth_end,
    ]
    agent = DeepResearchAgent(
        mock_engine,
        "test-model",
        tools=[KnowledgeSearchTool(store=store)],
    )

    result = agent.run("Synthétise la migration")

    assert result.content == "Synthèse complète."
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["total_tokens"] == 400


def test_agent_respects_max_turns(mock_engine, store):
    """Engine always returns tool_calls; verify turns <= max_turns."""
    always_search = _make_engine_response(
        "",
        tool_calls=[
            {
                "id": "call_loop",
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "arguments": json.dumps({"query": "migration"}),
                },
            }
        ],
    )
    mock_engine.generate.return_value = always_search

    ks_tool = KnowledgeSearchTool(store=store)
    agent = DeepResearchAgent(mock_engine, "test-model", tools=[ks_tool], max_turns=3)
    result = agent.run("Keep searching forever")

    assert result.turns == 3
    assert result.metadata.get("max_turns_exceeded") is True
    assert len(result.tool_results) == 3


def test_agent_system_prompt_mentions_research(mock_engine, store):
    """System message contains 'research' and 'source'."""
    mock_engine.generate.return_value = _make_engine_response("Done.")

    ks_tool = KnowledgeSearchTool(store=store)
    agent = DeepResearchAgent(mock_engine, "test-model", tools=[ks_tool])
    agent.run("test")

    call_args = mock_engine.generate.call_args
    messages = call_args[0][0]
    system_msg = messages[0]
    assert system_msg.role.value == "system"
    assert "research" in system_msg.content.lower()
    assert "source" in system_msg.content.lower()


def test_agent_defaults():
    """Verify agent_id, default max_turns, temperature, max_tokens."""
    assert DeepResearchAgent.agent_id == "deep_research"
    assert DeepResearchAgent._default_max_turns == 8
    assert DeepResearchAgent._default_temperature == 0.3
    assert DeepResearchAgent._default_max_tokens == 4096
