"""Tests for extended API routes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from ava_extensions.server.principal import Principal  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.core.types import StepType, Trace, TraceStep  # noqa: E402
from openjarvis.server.api_routes import include_all_routes  # noqa: E402
from openjarvis.traces.store import TraceStore  # noqa: E402


@pytest.fixture(autouse=True)
def _legacy_memory_http_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("OPENJARVIS_ENABLE_LEGACY_MEMORY_HTTP", "1")


def _make_app():
    app = FastAPI()
    include_all_routes(app)
    return app


class TestAgentRoutes:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/v1/agents", None),
            ("POST", "/v1/agents", {"agent_type": "simple"}),
            ("POST", "/v1/agents", {}),
            ("DELETE", "/v1/agents/private-canary", None),
            (
                "POST",
                "/v1/agents/private-canary/message",
                {"message": "overwrite-private-canary"},
            ),
            ("POST", "/v1/agents/private-canary/message", {}),
        ],
    )
    def test_legacy_http_is_quarantined_without_read_or_mutation(
        self, method, path, payload
    ):
        from openjarvis.tools.agent_tools import _SPAWNED_AGENTS

        previous = deepcopy(_SPAWNED_AGENTS)
        canary = {
            "agent_type": "simple",
            "status": "running",
            "private_state": "PRIVATE-AGENT-CANARY",
        }
        _SPAWNED_AGENTS.clear()
        _SPAWNED_AGENTS["private-canary"] = deepcopy(canary)
        before = deepcopy(_SPAWNED_AGENTS)
        client = TestClient(_make_app())
        try:
            response = client.request(method, path, json=payload)

            assert response.status_code == 410
            assert response.json()["detail"].startswith(
                "Legacy shared agent HTTP API is quarantined"
            )
            assert "PRIVATE-AGENT-CANARY" not in response.text
            assert _SPAWNED_AGENTS == before
        finally:
            _SPAWNED_AGENTS.clear()
            _SPAWNED_AGENTS.update(previous)


class TestMemoryRoutes:
    # 503 is the documented response when the native ``openjarvis_rust``
    # extension is absent from the venv (see TestMemoryRustMissing below).
    # These tests are only asserting "the route is wired up", so a backend
    # that cannot be built is tolerated the same way a 500 is.
    _BACKEND_OPTIONAL = (200, 500, 503)

    def test_search(self):
        client = TestClient(_make_app())
        resp = client.post("/v1/memory/search", json={"query": "test"})
        # May fail if SQLite not set up, that's ok
        assert resp.status_code in self._BACKEND_OPTIONAL

    def test_stats(self):
        client = TestClient(_make_app())
        resp = client.get("/v1/memory/stats")
        assert resp.status_code in self._BACKEND_OPTIONAL

    def test_default_without_opt_in_is_quarantined(self, monkeypatch):
        monkeypatch.delenv("OPENJARVIS_ENABLE_LEGACY_MEMORY_HTTP", raising=False)
        client = TestClient(_make_app())
        assert client.post("/v1/memory/search", json={"query": "x"}).status_code == 410
        assert client.post("/v1/memory/store", json={"content": "x"}).status_code == 410

    def test_memory_facts_remains_quarantined_even_with_opt_in(
        self, monkeypatch, tmp_path
    ):
        facts = tmp_path / "memory_facts.jsonl"
        facts.write_text('{"fact":"canary"}\n')
        monkeypatch.setenv("OPENJARVIS_WORKSPACE", str(tmp_path))
        response = TestClient(_make_app()).post(
            "/v1/memory/index",
            json={"path": str(facts)},
        )
        assert response.status_code == 403

    def test_memory_facts_hard_link_remains_quarantined(self, monkeypatch, tmp_path):
        facts = tmp_path / "memory_facts.jsonl"
        facts.write_text('{"fact":"canary"}\n')
        alias = tmp_path / "innocent-note.txt"
        alias.hardlink_to(facts)
        monkeypatch.setenv("OPENJARVIS_WORKSPACE", str(tmp_path))
        app = _make_app()
        app.state.config = SimpleNamespace(
            memory=SimpleNamespace(facts_path=str(facts))
        )

        response = TestClient(app).post(
            "/v1/memory/index",
            json={"path": str(alias)},
        )

        assert response.status_code == 403

    def test_configured_fact_store_path_remains_quarantined(
        self, monkeypatch, tmp_path
    ):
        facts = tmp_path / "facts-live.jsonl"
        facts.write_text('{"fact":"canary"}\n')
        monkeypatch.setenv("OPENJARVIS_WORKSPACE", str(tmp_path))
        app = _make_app()
        app.state.config = SimpleNamespace(
            memory=SimpleNamespace(facts_path=str(facts))
        )

        response = TestClient(app).post(
            "/v1/memory/index",
            json={"path": str(tmp_path)},
        )

        assert response.status_code == 403


class TestMemoryRustMissing:
    """Regression for #502: when the native ``openjarvis_rust`` extension is
    missing from the serving venv, memory ops must surface a CLEAR, ACTIONABLE
    error — never the misleading "Failed to index path" or a 200 silent no-op.
    """

    @staticmethod
    def _client(monkeypatch):
        # Force the same failure mode as a venv without the compiled extension.
        def _boom():
            raise ImportError("No module named 'openjarvis_rust'")

        import openjarvis._rust_bridge as bridge

        monkeypatch.setattr(bridge, "get_rust_module", _boom)
        return TestClient(_make_app())

    def test_store_is_not_a_silent_noop(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post("/v1/memory/store", json={"content": "hi"})
        # Must NOT return the old 200 {"status":"stored","note":"no backend..."}.
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "openjarvis_rust" in detail
        assert "maturin develop" in detail

    def test_index_surfaces_actionable_detail(self, monkeypatch, tmp_path):
        (tmp_path / "note.txt").write_text("hello world some content here")
        monkeypatch.setenv("OPENJARVIS_WORKSPACE", str(tmp_path))
        client = self._client(monkeypatch)
        resp = client.post("/v1/memory/index", json={"path": str(tmp_path)})
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        # The frontend reads this `detail`; it must point at the real cause,
        # not blame the indexed path.
        assert "openjarvis_rust" in detail
        assert detail != "Failed to index path"
        assert detail != "No memory backend available"

    def test_config_reports_unavailable(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/v1/memory/config")
        assert resp.status_code == 200
        data = resp.json()
        # Must not falsely report a healthy backend when none could be built.
        assert data["available"] is False
        assert "openjarvis_rust" in (data["detail"] or "")


class TestBudgetRoutes:
    def test_get_budget(self):
        client = TestClient(_make_app())
        resp = client.get("/v1/budget")
        assert resp.status_code == 200
        data = resp.json()
        assert "limits" in data
        assert "usage" in data

    def test_set_limits(self):
        client = TestClient(_make_app())
        resp = client.put("/v1/budget/limits", json={"max_tokens_per_day": 100000})
        assert resp.status_code == 200
        assert resp.json()["limits"]["max_tokens_per_day"] == 100000


class TestMetricsRoute:
    def test_metrics_endpoint(self):
        client = TestClient(_make_app())
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "openjarvis" in resp.text or "No metrics" in resp.text


class TestSkillRoutes:
    def test_list_skills(self):
        client = TestClient(_make_app())
        resp = client.get("/v1/skills")
        assert resp.status_code == 200
        assert "skills" in resp.json()


class TestSessionRoutes:
    def test_legacy_session_routes_are_quarantined(self):
        client = TestClient(_make_app())
        assert client.get("/v1/sessions").status_code == 410
        assert client.get("/v1/sessions/example").status_code == 410


class TestTraceRoutes:
    OWNER = Principal(provider="oidc", issuer="https://issuer.invalid", subject="owner")
    GUEST = Principal(provider="oidc", issuer="https://issuer.invalid", subject="guest")

    @staticmethod
    def _client(monkeypatch, tmp_path):
        from ava_extensions.server import principal as principal_module

        owner = TestTraceRoutes.OWNER
        guest = TestTraceRoutes.GUEST

        def _resolve(headers):
            return {"owner": owner, "guest": guest}.get(headers.get("X-Test-Principal"))

        monkeypatch.setattr(principal_module, "resolve_request_principal", _resolve)
        app = _make_app()
        app.state.trace_store = TraceStore(tmp_path / "traces.db")
        app.state.trace_store.save(
            Trace(
                trace_id="trace-owner",
                query="OWNER_PRIVATE_QUERY",
                result="OWNER_PRIVATE_REPLY",
                feedback=1.0,
                metadata={"provenance": owner.provenance},
            )
        )
        app.state.trace_store.save(
            Trace(
                trace_id="trace-guest",
                query="GUEST_PRIVATE_QUERY",
                result="GUEST_PRIVATE_REPLY",
                feedback=0.0,
                metadata={"provenance": guest.provenance},
            )
        )
        app.state.trace_store.save(
            Trace(
                trace_id="trace-legacy",
                query="UNATTRIBUTED_PRIVATE_QUERY",
                result="UNATTRIBUTED_PRIVATE_REPLY",
            )
        )
        return TestClient(app), app.state.trace_store

    def test_trace_routes_require_a_verified_principal(self, monkeypatch, tmp_path):
        client, store = self._client(monkeypatch, tmp_path)

        assert client.get("/v1/traces").status_code == 401
        assert client.get("/v1/traces/trace-owner").status_code == 401
        assert (
            client.post(
                "/v1/feedback",
                json={"trace_id": "trace-owner", "score": 0.5},
            ).status_code
            == 401
        )
        store.close()

    def test_tool_execution_proof_is_scoped_bounded_and_content_free(
        self, monkeypatch, tmp_path
    ):
        client, store = self._client(monkeypatch, tmp_path)
        canaries = {
            "argument": "PRIVATE_ARGUMENT_CANARY",
            "result": "PRIVATE_RESULT_CANARY",
            "metadata": "PRIVATE_METADATA_CANARY",
        }
        store.save(
            Trace(
                trace_id="trace-proof",
                query="PRIVATE_QUERY_CANARY",
                result="PRIVATE_REPLY_CANARY",
                outcome="completed",
                metadata={"provenance": self.OWNER.provenance},
                steps=[
                    TraceStep(
                        step_type=StepType.TOOL_CALL,
                        timestamp=1.0,
                        input={"tool": "lire_doc", "arguments": canaries},
                        output={"success": True, "result": canaries["result"]},
                        metadata=canaries,
                    ),
                    TraceStep(
                        step_type=StepType.TOOL_CALL,
                        timestamp=2.0,
                        input={"tool": "proposer_plan", "arguments": canaries},
                        output={"success": True, "result": canaries["result"]},
                        metadata=canaries,
                    ),
                    TraceStep(
                        step_type=StepType.TOOL_CALL,
                        timestamp=3.0,
                        input={"tool": "lire_doc", "arguments": canaries},
                        output={"success": False, "result": canaries["result"]},
                        metadata=canaries,
                    ),
                ],
            )
        )
        path = "/v1/traces/trace-proof/tool-execution-proof"

        response = client.get(path, headers={"X-Test-Principal": "owner"})

        assert response.status_code == 200
        assert response.json() == {
            "schema": "ava.tool-execution-proof/v1",
            "trace_id": "trace-proof",
            "complete": False,
            "call_count": 3,
            "calls": [
                {
                    "tool": "lire_doc",
                    "count": 2,
                    "successes": 1,
                    "failures": 1,
                },
                {
                    "tool": "proposer_plan",
                    "count": 1,
                    "successes": 1,
                    "failures": 0,
                },
            ],
        }
        assert not any(value in response.text for value in canaries.values())
        assert "PRIVATE_QUERY_CANARY" not in response.text
        assert "PRIVATE_REPLY_CANARY" not in response.text
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={"X-Test-Principal": "guest"}).status_code == 404
        )
        store.close()

    @pytest.mark.parametrize(
        ("tool", "success"),
        [
            ("bad tool", True),
            ("safe_tool", "yes"),
        ],
    )
    def test_tool_execution_proof_rejects_malformed_steps_without_leakage(
        self, monkeypatch, tmp_path, tool, success
    ):
        client, store = self._client(monkeypatch, tmp_path)
        store.save(
            Trace(
                trace_id="trace-corrupt",
                result="PRIVATE_REPLY_CANARY",
                outcome="completed",
                metadata={"provenance": self.OWNER.provenance},
                steps=[
                    TraceStep(
                        step_type=StepType.TOOL_CALL,
                        timestamp=1.0,
                        input={"tool": tool, "arguments": "PRIVATE_ARGUMENT_CANARY"},
                        output={"success": success, "result": "PRIVATE_RESULT_CANARY"},
                    )
                ],
            )
        )

        response = client.get(
            "/v1/traces/trace-corrupt/tool-execution-proof",
            headers={"X-Test-Principal": "owner"},
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "Trace proof unavailable"}
        assert "PRIVATE" not in response.text
        store.close()

    @pytest.mark.parametrize(
        ("names", "expected_status"),
        [
            (["tool"] * 64, 200),
            (["tool"] * 65, 409),
            ([f"tool_{index}" for index in range(16)], 200),
            ([f"tool_{index}" for index in range(17)], 409),
        ],
    )
    def test_tool_execution_proof_fails_closed_at_call_and_name_bounds(
        self, monkeypatch, tmp_path, names, expected_status
    ):
        client, store = self._client(monkeypatch, tmp_path)
        store.save(
            Trace(
                trace_id="trace-bounds",
                result="complete",
                outcome="completed",
                metadata={"provenance": self.OWNER.provenance},
                steps=[
                    TraceStep(
                        step_type=StepType.TOOL_CALL,
                        timestamp=float(index),
                        input={"tool": name},
                        output={"success": True},
                    )
                    for index, name in enumerate(names)
                ],
            )
        )

        response = client.get(
            "/v1/traces/trace-bounds/tool-execution-proof",
            headers={"X-Test-Principal": "owner"},
        )

        assert response.status_code == expected_status
        if expected_status == 200:
            assert response.json()["call_count"] == len(names)
        else:
            assert response.json() == {"detail": "Trace proof unavailable"}
        store.close()

    def test_tool_execution_proof_rejects_an_oversized_non_tool_trace(
        self, monkeypatch, tmp_path
    ):
        client, store = self._client(monkeypatch, tmp_path)
        store.save(
            Trace(
                trace_id="trace-too-many-steps",
                result="complete",
                outcome="completed",
                metadata={"provenance": self.OWNER.provenance},
                steps=[
                    TraceStep(step_type=StepType.GENERATE, timestamp=float(index))
                    for index in range(257)
                ],
            )
        )

        response = client.get(
            "/v1/traces/trace-too-many-steps/tool-execution-proof",
            headers={"X-Test-Principal": "owner"},
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "Trace proof unavailable"}
        store.close()

    def test_list_and_stats_are_scoped_to_principal(self, monkeypatch, tmp_path):
        client, store = self._client(monkeypatch, tmp_path)
        headers = {"X-Test-Principal": "owner"}

        response = client.get("/v1/traces", headers=headers)
        stats = client.get("/v1/feedback/stats", headers=headers)

        assert response.status_code == 200
        assert [trace["id"] for trace in response.json()["traces"]] == ["trace-owner"]
        assert stats.status_code == 200
        assert stats.json()["total"] == 1
        assert stats.json()["mean_score"] == 1.0
        assert stats.json()["traces_totales"] == 1
        store.close()

    def test_detail_and_feedback_hide_other_principals(self, monkeypatch, tmp_path):
        client, store = self._client(monkeypatch, tmp_path)
        owner_headers = {"X-Test-Principal": "owner"}
        guest_headers = {"X-Test-Principal": "guest"}

        assert (
            client.get("/v1/traces/trace-owner", headers=owner_headers).status_code
            == 200
        )
        assert (
            client.get("/v1/traces/trace-owner", headers=guest_headers).status_code
            == 404
        )
        assert (
            client.get("/v1/traces/trace-legacy", headers=owner_headers).status_code
            == 404
        )
        assert (
            client.post(
                "/v1/feedback",
                headers=guest_headers,
                json={"trace_id": "trace-owner", "score": 0.25},
            ).status_code
            == 404
        )
        assert store.get("trace-owner").feedback == 1.0
        assert (
            client.post(
                "/v1/feedback",
                headers=owner_headers,
                json={"trace_id": "trace-owner", "score": 0.25},
            ).status_code
            == 200
        )
        assert store.get("trace-owner").feedback == 0.25
        store.close()
