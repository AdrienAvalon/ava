"""Tests for tools/_stubs.py — ToolSpec, BaseTool, ToolExecutor."""

from __future__ import annotations

import json

from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoTool(BaseTool):
    """Minimal tool that echoes its input."""

    tool_id = "echo"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="Echoes input back.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            category="testing",
        )

    def execute(self, **params) -> ToolResult:
        return ToolResult(
            tool_name="echo",
            content=params.get("text", ""),
            success=True,
        )


class _ErrorTool(BaseTool):
    """Tool that always raises."""

    tool_id = "error"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="error", description="Always errors.")

    def execute(self, **params) -> ToolResult:
        raise RuntimeError("boom")


class _ProtectedTool(BaseTool):
    """Tool whose effect is forbidden without an explicit policy grant."""

    tool_id = "protected"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="protected",
            description="Protected test tool.",
            required_capabilities=["network:fetch", "ava:test:read"],
            requires_capability_policy=True,
        )

    def execute(self, **params) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name="protected", content="executed", success=True)


class _AllowExactPolicy:
    def __init__(self, subject: str) -> None:
        self.subject = subject

    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return (
            agent_id == self.subject
            and capability in {"network:fetch", "ava:test:read"}
            and resource == "protected"
        )


class _BrokenPolicy:
    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        raise RuntimeError("policy backend unavailable")


class _ResourcePolicy:
    def __init__(self, grants: set[tuple[str, str]]) -> None:
        self.grants = grants

    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return agent_id == "owner" and (capability, resource) in self.grants


# ---------------------------------------------------------------------------
# ToolSpec tests
# ---------------------------------------------------------------------------


class TestToolSpec:
    def test_defaults(self):
        s = ToolSpec(name="test", description="A test tool.")
        assert s.name == "test"
        assert s.description == "A test tool."
        assert s.parameters == {}
        assert s.category == ""
        assert s.cost_estimate == 0.0
        assert s.requires_confirmation is False
        assert s.requires_capability_policy is False

    def test_full_spec(self):
        s = ToolSpec(
            name="calc",
            description="Calculate things.",
            parameters={"type": "object"},
            category="math",
            cost_estimate=0.01,
            latency_estimate=0.5,
            requires_confirmation=True,
            metadata={"version": "1.0"},
        )
        assert s.category == "math"
        assert s.metadata["version"] == "1.0"


# ---------------------------------------------------------------------------
# BaseTool tests
# ---------------------------------------------------------------------------


class TestBaseTool:
    def test_echo_tool_spec(self):
        tool = _EchoTool()
        assert tool.spec.name == "echo"
        assert tool.tool_id == "echo"

    def test_echo_tool_execute(self):
        tool = _EchoTool()
        result = tool.execute(text="hello")
        assert result.content == "hello"
        assert result.success is True

    def test_to_openai_function(self):
        tool = _EchoTool()
        fn = tool.to_openai_function()
        assert fn["type"] == "function"
        assert fn["function"]["name"] == "echo"
        assert fn["function"]["description"] == "Echoes input back."
        assert "properties" in fn["function"]["parameters"]


# ---------------------------------------------------------------------------
# ToolExecutor tests
# ---------------------------------------------------------------------------


class TestToolExecutor:
    def test_execute_success(self):
        executor = ToolExecutor([_EchoTool()])
        call = ToolCall(id="1", name="echo", arguments='{"text":"hi"}')
        result = executor.execute(call)
        assert result.success is True
        assert result.content == "hi"
        assert result.latency_seconds > 0

    def test_execute_unknown_tool(self):
        executor = ToolExecutor([_EchoTool()])
        call = ToolCall(id="1", name="nonexistent", arguments="{}")
        result = executor.execute(call)
        assert result.success is False
        assert "Unknown tool" in result.content

    def test_execute_invalid_json(self):
        executor = ToolExecutor([_EchoTool()])
        call = ToolCall(id="1", name="echo", arguments="not json")
        result = executor.execute(call)
        assert result.success is False
        assert "Invalid arguments JSON" in result.content

    def test_execute_empty_arguments(self):
        executor = ToolExecutor([_EchoTool()])
        call = ToolCall(id="1", name="echo", arguments="")
        result = executor.execute(call)
        assert result.success is True
        assert result.content == ""

    def test_execute_tool_error(self):
        executor = ToolExecutor([_ErrorTool()])
        call = ToolCall(id="1", name="error", arguments="{}")
        result = executor.execute(call)
        assert result.success is False
        assert result.content == "Tool 'error' failed."
        assert "boom" not in result.content

    def test_available_tools(self):
        executor = ToolExecutor([_EchoTool(), _ErrorTool()])
        specs = executor.available_tools()
        assert len(specs) == 2
        names = {s.name for s in specs}
        assert names == {"echo", "error"}

    def test_get_openai_tools(self):
        executor = ToolExecutor([_EchoTool()])
        tools = executor.get_openai_tools()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "echo"

    def test_event_bus_integration(self):
        bus = EventBus(record_history=True)
        executor = ToolExecutor([_EchoTool()], bus=bus)
        call = ToolCall(id="1", name="echo", arguments='{"text":"ping"}')
        executor.execute(call)
        events = bus.history
        types = [e.event_type for e in events]
        assert EventType.TOOL_CALL_START in types
        assert EventType.TOOL_CALL_END in types
        # Check start event data
        start = [e for e in events if e.event_type == EventType.TOOL_CALL_START][0]
        assert start.data["tool"] == "echo"
        # Check end event data
        end = [e for e in events if e.event_type == EventType.TOOL_CALL_END][0]
        assert end.data["success"] is True

    def test_event_bus_on_error(self):
        bus = EventBus(record_history=True)
        executor = ToolExecutor([_ErrorTool()], bus=bus)
        call = ToolCall(id="1", name="error", arguments="{}")
        executor.execute(call)
        end = [e for e in bus.history if e.event_type == EventType.TOOL_CALL_END][0]
        assert end.data["success"] is False

    def test_no_bus_works(self):
        executor = ToolExecutor([_EchoTool()])
        call = ToolCall(id="1", name="echo", arguments='{"text":"ok"}')
        result = executor.execute(call)
        assert result.success is True

    def test_empty_executor(self):
        executor = ToolExecutor([])
        assert executor.available_tools() == []
        assert executor.get_openai_tools() == []

    def test_policy_required_tool_refuse_quand_policy_absente(self):
        tool = _ProtectedTool()
        executor = ToolExecutor([tool], agent_id="owner")

        result = executor.execute(ToolCall(id="1", name="protected", arguments="{}"))

        assert result.success is False
        assert "Capability" in result.content
        assert tool.calls == 0

    def test_execution_agent_id_ne_peut_pas_servir_de_principal(self):
        tool = _ProtectedTool()
        executor = ToolExecutor(
            [tool],
            capability_policy=_AllowExactPolicy("owner"),
            # This identifier is only for traces; it is not authenticated.
            agent_id="owner",
        )

        result = executor.execute(ToolCall(id="1", name="protected", arguments="{}"))

        assert result.success is False
        assert tool.calls == 0

    def test_policy_required_tool_accepte_les_grants_exacts(self):
        tool = _ProtectedTool()
        executor = ToolExecutor(
            [tool],
            capability_policy=_AllowExactPolicy("owner"),
            principal_provenance="owner",
        )

        result = executor.execute(ToolCall(id="1", name="protected", arguments="{}"))

        assert result.success is True
        assert tool.calls == 1

    def test_erreur_policy_refuse_et_emet_un_evenement(self):
        tool = _ProtectedTool()
        bus = EventBus(record_history=True)
        executor = ToolExecutor(
            [tool],
            bus=bus,
            capability_policy=_BrokenPolicy(),
            principal_provenance="owner",
        )

        result = executor.execute(ToolCall(id="1", name="protected", arguments="{}"))

        assert result.success is False
        assert tool.calls == 0
        denied = [
            event
            for event in bus.history
            if event.event_type == EventType.CAPABILITY_DENIED
        ]
        assert len(denied) == 1
        assert denied[0].data["reason"] == "policy_error"

    def test_calculator_et_think_restent_locaux_sans_policy(self):
        from openjarvis.tools.calculator import CalculatorTool
        from openjarvis.tools.think import ThinkTool

        calculator = ToolExecutor([CalculatorTool()])
        think = ToolExecutor([ThinkTool()])

        calculated = calculator.execute(
            ToolCall(id="1", name="calculator", arguments='{"expression":"2+2"}')
        )
        reflected = think.execute(
            ToolCall(id="2", name="think", arguments='{"thought":"local"}')
        )

        assert calculated.success is True
        assert calculated.content == "4.0"
        assert reflected.success is True
        assert reflected.content == "local"

    def test_file_read_et_web_search_exigent_une_policy(self):
        from openjarvis.tools.file_read import FileReadTool
        from openjarvis.tools.web_search import WebSearchTool

        file_spec = FileReadTool().spec
        web_spec = WebSearchTool().spec

        assert file_spec.required_capabilities == ["file:read"]
        assert file_spec.requires_capability_policy is True
        assert web_spec.required_capabilities == ["network:fetch"]
        assert web_spec.requires_capability_policy is True

    def test_file_read_et_web_search_refusent_policy_absente(self, tmp_path):
        from openjarvis.tools.file_read import FileReadTool
        from openjarvis.tools.web_search import WebSearchTool

        path = tmp_path / "readable.txt"
        path.write_text("safe", encoding="utf-8")
        file_result = ToolExecutor(
            [FileReadTool([str(tmp_path)])], agent_id="owner"
        ).execute(
            ToolCall(
                id="file",
                name="file_read",
                arguments=f'{{"path":{json.dumps(str(path))}}}',
            )
        )
        web_result = ToolExecutor([WebSearchTool()], agent_id="owner").execute(
            ToolCall(
                id="web",
                name="web_search",
                arguments='{"query":"offline"}',
            )
        )

        assert file_result.success is False
        assert web_result.success is False

    def test_file_read_et_web_search_acceptent_seulement_leurs_grants_exacts(
        self,
        tmp_path,
        monkeypatch,
    ):
        from openjarvis.tools.file_read import FileReadTool
        from openjarvis.tools.web_search import WebSearchTool

        path = tmp_path / "readable.txt"
        path.write_text("safe", encoding="utf-8")
        file_tool = FileReadTool([str(tmp_path)])
        web_tool = WebSearchTool()
        web_calls: list[dict] = []

        def fake_web_execute(**params):
            web_calls.append(params)
            return ToolResult(tool_name="web_search", content="offline", success=True)

        monkeypatch.setattr(web_tool, "execute", fake_web_execute)
        policy = _ResourcePolicy(
            {
                ("file:read", "file_read"),
                ("network:fetch", "web_search"),
            }
        )
        executor = ToolExecutor(
            [file_tool, web_tool],
            capability_policy=policy,
            principal_provenance="owner",
        )

        file_result = executor.execute(
            ToolCall(
                id="file",
                name="file_read",
                arguments=f'{{"path":{json.dumps(str(path))}}}',
            )
        )
        web_result = executor.execute(
            ToolCall(
                id="web",
                name="web_search",
                arguments='{"query":"offline"}',
            )
        )

        assert file_result.success is True
        assert file_result.content == "safe"
        assert web_result.success is True
        assert web_calls == [{"query": "offline"}]
