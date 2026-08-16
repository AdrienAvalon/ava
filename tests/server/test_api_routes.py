"""Tests for extended API routes."""

from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from ava_extensions.server.principal import Principal  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.core.types import Trace  # noqa: E402
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
    def test_list_agents(self):
        client = TestClient(_make_app())
        resp = client.get("/v1/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert "registered" in data
        assert "running" in data

    def test_create_agent(self):
        client = TestClient(_make_app())
        resp = client.post("/v1/agents", json={"agent_type": "simple"})
        # May succeed or fail depending on agent_tools availability
        assert resp.status_code in (200, 501)

    def test_kill_nonexistent(self):
        client = TestClient(_make_app())
        resp = client.delete("/v1/agents/nonexistent")
        assert resp.status_code in (404, 501)


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
