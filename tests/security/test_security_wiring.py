"""Verify security wiring reaches agents and ToolExecutor."""

from __future__ import annotations

from unittest.mock import MagicMock

from openjarvis.agents._stubs import (
    AgentResult,
    ToolUsingAgent,
    configure_tool_execution_security,
)
from openjarvis.core.config import (
    CapabilitiesConfig,
    JarvisConfig,
    SecurityConfig,
)
from openjarvis.core.events import EventBus
from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security import setup_security
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _ConcreteAgent(ToolUsingAgent):
    """Minimal concrete subclass — ToolUsingAgent is abstract."""

    agent_id = "test"

    def run(self, input, context=None, **kwargs):
        return AgentResult(content="ok")


class _ProtectedTool(BaseTool):
    tool_id = "protected_wiring"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description="Protected wiring probe.",
            required_capabilities=["ava:test:read"],
            requires_capability_policy=True,
        )

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name=self.tool_id, content="allowed", success=True)


class _OwnerPolicy:
    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return (agent_id, capability, resource) == (
            "verified:owner",
            "ava:test:read",
            "protected_wiring",
        )


def _make_mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.generate.return_value = {
        "content": "ok",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "model": "m",
        "finish_reason": "stop",
    }
    engine.list_models.return_value = ["m"]
    engine.health.return_value = True
    return engine


def _has_rust() -> bool:
    try:
        import openjarvis_rust  # noqa: F401

        return True
    except ImportError:
        return False


class TestCapabilityPolicyReachesExecutor:
    def test_post_wiring_separates_trace_id_from_verified_principal(self) -> None:
        tool = _ProtectedTool()
        agent = _ConcreteAgent(
            _make_mock_engine(),
            "m",
            tools=[tool],
            agent_id="managed-agent-uuid",
        )
        guard = object()

        configure_tool_execution_security(
            agent,
            capability_policy=_OwnerPolicy(),
            boundary_guard=guard,
            principal_provenance="verified:owner",
        )

        result = agent._executor.execute(
            ToolCall(id="probe", name=tool.tool_id, arguments="{}")
        )
        assert result.success is True
        assert agent._executor._agent_id == "managed-agent-uuid"
        assert agent._executor._principal_provenance == "verified:owner"
        assert agent._executor._boundary_guard is guard

    def test_security_disabled_still_denies_policy_required_tool(self) -> None:
        sec = setup_security(
            JarvisConfig(security=SecurityConfig(enabled=False)),
            _make_mock_engine(),
        )
        tool = _ProtectedTool()
        agent = _ConcreteAgent(sec.engine, "m", tools=[tool])
        configure_tool_execution_security(
            agent,
            capability_policy=sec.capability_policy,
            boundary_guard=sec.boundary_guard,
            principal_provenance="verified:owner",
        )

        result = agent._executor.execute(
            ToolCall(id="probe", name=tool.tool_id, arguments="{}")
        )

        assert result.success is False
        assert "Capability" in result.content

    def test_no_policy_when_caps_disabled(self) -> None:
        cfg = JarvisConfig()
        cfg.security = SecurityConfig(
            enabled=True,
            capabilities=CapabilitiesConfig(enabled=False),
        )
        bus = EventBus()
        engine = _make_mock_engine()
        sec = setup_security(cfg, engine, bus)

        agent = _ConcreteAgent(
            sec.engine,
            "m",
            tools=[],
            capability_policy=sec.capability_policy,
        )
        assert agent._executor._capability_policy is None

    def test_no_policy_when_security_disabled(self) -> None:
        cfg = JarvisConfig()
        cfg.security = SecurityConfig(enabled=False)
        engine = _make_mock_engine()
        sec = setup_security(cfg, engine)

        agent = _ConcreteAgent(
            sec.engine,
            "m",
            tools=[],
            capability_policy=sec.capability_policy,
        )
        assert agent._executor._capability_policy is None
        # Engine should be the original, unwrapped
        assert sec.engine is engine
