"""Capability and egress contracts for Ava's private tools.

All tests are offline.  Fixed Control Plane destinations are intercepted
before I/O, and synthetic principals contain no installation identity.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ava_extensions.server.principal import Principal
from ava_extensions.skills import (
    avalon_status,
    camera,
    evolutions,
    home_assistant,
    introspection,
    journal,
    logs,
    proposer,
)
from ava_extensions.tool_capabilities import (
    DOCS_PLAN,
    DOCS_PROPOSE,
    DOCS_READ,
    EVOLUTIONS_READ,
    FILE_READ,
    HOME_OBSERVE,
    INFRA_LOGS_READ,
    INFRA_OBSERVE,
    INTROSPECTION_READ,
    JOURNAL_READ,
    NETWORK_FETCH,
)
from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security.boundary import BoundaryGuard
from openjarvis.server.routes import _copy_agent_for_request
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

OWNER = Principal("oidc", "https://issuer.example.invalid", "synthetic-owner")
GUEST = Principal("oidc", "https://issuer.example.invalid", "synthetic-guest")
MATRIX_OWNER = Principal(
    "service",
    "synthetic-control-plane",
    "matrix:@synthetic:matrix.example.invalid",
)


def _tool_specs() -> dict[str, ToolSpec]:
    tools = (
        avalon_status.AvalonStatusTool(),
        camera.CameraTool(),
        evolutions.EvolutionsTool(),
        home_assistant.HomeAssistantTool(),
        introspection.IntrospectionTool(),
        journal.JournalTool(),
        logs.LogsTool(),
        proposer.LireDocTool(),
        proposer.ProposerPlanTool(),
        proposer.ProposerTool(),
    )
    return {tool.spec.name: tool.spec for tool in tools}


def test_private_tool_capability_matrix_is_explicit() -> None:
    expected = {
        "avalon_status": [NETWORK_FETCH, INFRA_OBSERVE],
        "camera": [NETWORK_FETCH, HOME_OBSERVE],
        "evolutions": [FILE_READ, NETWORK_FETCH, EVOLUTIONS_READ],
        "home_assistant": [NETWORK_FETCH, HOME_OBSERVE],
        "introspection": [FILE_READ, INTROSPECTION_READ],
        "journal": [FILE_READ, JOURNAL_READ],
        "logs": [NETWORK_FETCH, INFRA_LOGS_READ],
        "lire_doc": [NETWORK_FETCH, DOCS_READ],
        "proposer_plan": [NETWORK_FETCH, DOCS_PLAN],
        "proposer": [NETWORK_FETCH, DOCS_PROPOSE],
    }

    specs = _tool_specs()

    assert set(specs) == set(expected)
    for name, capabilities in expected.items():
        assert specs[name].required_capabilities == capabilities
        assert specs[name].requires_capability_policy is True


def test_every_cp_tool_is_external_with_a_fixed_destination_contract() -> None:
    tools = (
        avalon_status.AvalonStatusTool(),
        camera.CameraTool(),
        evolutions.EvolutionsTool(),
        home_assistant.HomeAssistantTool(),
        logs.LogsTool(),
        proposer.LireDocTool(),
        proposer.ProposerPlanTool(),
        proposer.ProposerTool(),
    )

    for tool in tools:
        assert tool.is_local is False
        assert tool.spec.metadata["fixed_destination"] == "avalon-control-plane"


class _ExactPolicy:
    def __init__(self, allowed_subjects: set[str]) -> None:
        self._allowed_subjects = allowed_subjects

    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return (
            agent_id in self._allowed_subjects
            and resource == "probe"
            and capability in {NETWORK_FETCH, INFRA_OBSERVE}
        )


class _ProbeTool(BaseTool):
    tool_id = "probe"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="probe",
            description="Synthetic protected observation.",
            required_capabilities=[NETWORK_FETCH, INFRA_OBSERVE],
            requires_capability_policy=True,
        )

    def execute(self, **params: object) -> ToolResult:
        return ToolResult(tool_name="probe", content="allowed", success=True)


def _request_agent_copy(principal: Principal | None) -> SimpleNamespace:
    tool = _ProbeTool()
    policy = _ExactPolicy({OWNER.provenance, MATRIX_OWNER.provenance})
    agent = SimpleNamespace(
        _model="configured",
        _temperature=0.7,
        _max_tokens=1024,
        _bus=None,
        _tools=[tool],
        _executor=ToolExecutor(
            [tool],
            capability_policy=policy,
            # This class-level identity must be overwritten at the HTTP boundary.
            agent_id="orchestrator",
        ),
        _loop_guard=None,
    )
    return _copy_agent_for_request(
        agent,
        "model",
        frozenset(),
        temperature=0.4,
        max_tokens=1024,
        principal_provenance=principal.provenance if principal is not None else None,
    )


@pytest.mark.parametrize(
    ("principal", "allowed"),
    [
        (OWNER, True),
        (MATRIX_OWNER, True),
        (GUEST, False),
        (None, False),
    ],
)
def test_http_and_matrix_capabilities_use_only_verified_provenance(
    principal: Principal | None,
    allowed: bool,
) -> None:
    request_agent = _request_agent_copy(principal)

    result = request_agent._executor.execute(
        ToolCall(id="probe", name="probe", arguments="{}")
    )

    assert result.success is allowed
    assert request_agent._executor._principal_provenance == (
        principal.provenance if principal is not None else ""
    )
    assert request_agent._executor._agent_id == "orchestrator"
    if principal is not None:
        assert principal.subject not in request_agent._executor._principal_provenance


class _RecordingGuard:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def check_outbound(self, tool_call: ToolCall) -> ToolCall:
        self.calls.append(tool_call)
        return replace(
            tool_call,
            arguments=tool_call.arguments.replace(
                "evil.invalid",
                "redacted.invalid",
            ),
        )


class _LogsPolicy:
    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return (
            agent_id == OWNER.provenance
            and resource == "logs"
            and capability in {NETWORK_FETCH, INFRA_LOGS_READ}
        )


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "fenetre": "24h",
                "libelle": "redemarrages",
                "occurrences": 0,
            }
        ).encode("utf-8")


def test_boundary_guard_scans_args_without_letting_params_change_cp_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        del timeout
        captured_requests.append(request)
        return _Response()

    monkeypatch.setattr(logs, "_jeton", lambda: "synthetic-token")
    monkeypatch.setattr(logs.urllib.request, "urlopen", fake_urlopen)
    guard = _RecordingGuard()
    executor = ToolExecutor(
        [logs.LogsTool()],
        capability_policy=_LogsPolicy(),
        boundary_guard=guard,
        principal_provenance=OWNER.provenance,
    )

    result = executor.execute(
        ToolCall(
            id="logs",
            name="logs",
            arguments=json.dumps(
                {
                    "question": "redemarrages",
                    "hote": "https://evil.invalid/redirect?x=1",
                }
            ),
        )
    )

    assert result.success is True
    assert len(guard.calls) == 1
    assert len(captured_requests) == 1
    target = urlsplit(captured_requests[0].full_url)
    configured = urlsplit(logs.CP_BASE)
    assert (target.scheme, target.netloc) == (configured.scheme, configured.netloc)
    assert target.path == "/api/v1/logs/redemarrages"
    assert "evil.invalid" not in target.netloc
    assert parse_qs(target.query)["hote"] == ["https://redacted.invalid/redirect?x=1"]


class _ProposalScanner:
    marker = "synthetic-secret-value"

    def scan(self, text: str) -> SimpleNamespace:
        findings = [object()] if self.marker in text else []
        return SimpleNamespace(findings=findings)

    def redact(self, text: str) -> str:
        return text.replace(self.marker, "[REDACTED:synthetic]")


class _DocsPlanPolicy:
    def check(self, agent_id: str, capability: str, resource: str) -> bool:
        return (
            agent_id == OWNER.provenance
            and resource == "proposer_plan"
            and capability in {NETWORK_FETCH, DOCS_PLAN}
        )


class _PlanResponse:
    def __enter__(self) -> _PlanResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "ok": True,
                "mode": "plan",
                "effets_externes": False,
                "fichiers": [],
                "resume": {},
            }
        ).encode("utf-8")


def _proposal_call() -> ToolCall:
    return ToolCall(
        id="proposer_plan",
        name="proposer_plan",
        arguments=json.dumps(
            {
                "titre": "docs(test): verifier le garde",
                "description": "preuve hors ligne",
                "chemin": "docs/test.md",
                "contenu": "# Test\n\nsynthetic-secret-value\n",
                # A client-controlled lookalike is ordinary input.  It neither
                # authorizes an egress destination nor overrides ToolSpec metadata.
                "fixed_destination": "https://evil.invalid/redirect",
            }
        ),
    )


def test_boundary_redaction_keeps_proposal_json_valid_and_destination_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> _PlanResponse:
        del timeout
        captured_requests.append(request)
        return _PlanResponse()

    monkeypatch.setattr(proposer, "_jeton", lambda: "synthetic-token")
    monkeypatch.setattr(proposer.urllib.request, "urlopen", fake_urlopen)
    scanner = _ProposalScanner()
    executor = ToolExecutor(
        [proposer.ProposerPlanTool()],
        capability_policy=_DocsPlanPolicy(),
        boundary_guard=BoundaryGuard(mode="redact", scanners=[scanner]),
        principal_provenance=OWNER.provenance,
    )

    result = executor.execute(_proposal_call())

    assert result.success is True
    assert len(captured_requests) == 1
    request = captured_requests[0]
    target = urlsplit(request.full_url)
    configured = urlsplit(proposer.CP_BASE)
    assert (target.scheme, target.netloc) == (configured.scheme, configured.netloc)
    assert target.path == "/api/v1/propositions/plan"
    body = json.loads(request.data)
    assert body["fichiers"][0]["contenu"].endswith("[REDACTED:synthetic]\n")
    assert "fixed_destination" not in body
    assert "evil.invalid" not in request.full_url


def test_boundary_block_stops_proposal_before_egress_despite_fixed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proposer, "_jeton", lambda: "synthetic-token")

    def forbidden_urlopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("a blocked proposal must not reach the Control Plane transport")

    monkeypatch.setattr(proposer.urllib.request, "urlopen", forbidden_urlopen)
    tool = proposer.ProposerPlanTool()
    assert tool.spec.metadata["fixed_destination"] == "avalon-control-plane"
    executor = ToolExecutor(
        [tool],
        capability_policy=_DocsPlanPolicy(),
        boundary_guard=BoundaryGuard(mode="block", scanners=[_ProposalScanner()]),
        principal_provenance=OWNER.provenance,
    )

    result = executor.execute(_proposal_call())

    assert result.success is False
    assert result.content.startswith("Security block:")
