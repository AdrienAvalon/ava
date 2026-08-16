"""Tests for Agent Manager API routes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ava_extensions.server.principal import Principal

from openjarvis.agents.manager import AgentManager

OWNER = Principal("oidc", "https://issuer.example.invalid", "managed-owner")
OTHER_OWNER = Principal("oidc", "https://issuer.example.invalid", "managed-other")


@pytest.fixture(autouse=True)
def _verified_managed_owner(monkeypatch):
    """Keep pre-existing route tests behind a verified synthetic principal."""

    from ava_extensions.server import principal as principal_module

    monkeypatch.setattr(
        principal_module,
        "resolve_request_principal",
        lambda _headers: OWNER,
    )


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = AgentManager(db_path=str(Path(tmpdir) / "agents.db"))
        original_create = mgr.create_agent

        def create_owned_agent(*args, **kwargs):
            kwargs.setdefault("owner_provenance", OWNER.provenance)
            return original_create(*args, **kwargs)

        # Route tests that seed state directly must model records previously
        # created by the same verified HTTP principal.
        mgr.create_agent = create_owned_agent
        yield mgr
        mgr.close()


try:
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
class TestAgentManagerRoutes:
    @pytest.fixture
    def client(self, manager):
        from fastapi import FastAPI

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        app = FastAPI()
        routers = create_agent_manager_router(manager)
        for r in routers:
            app.include_router(r)
        return TestClient(app)

    def test_list_agents_empty(self, client):
        resp = client.get("/v1/managed-agents")
        assert resp.status_code == 200
        assert resp.json()["agents"] == []

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-Ava-Identity": "forged.jwt"},
            {
                "X-Ava-Identity": "one.jwt",
                "X-Ava-Service-Assertion": "ambiguous",
            },
        ],
    )
    def test_absent_invalid_or_ambiguous_principal_fails_closed_before_create(
        self,
        client,
        manager,
        monkeypatch,
        headers,
    ):
        from ava_extensions.server import principal as principal_module

        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: None,
        )

        listed = client.get("/v1/managed-agents", headers=headers)
        created = client.post(
            "/v1/managed-agents",
            headers=headers,
            json={"name": "must-not-exist"},
        )

        assert listed.status_code == 401
        assert created.status_code == 401
        assert manager.list_agents() == []

    def test_all_private_agent_routes_are_scoped_to_verified_owner(
        self,
        client,
        manager,
        monkeypatch,
    ):
        from ava_extensions.server import principal as principal_module

        selected = {"principal": OWNER}
        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: selected["principal"],
        )
        created = client.post(
            "/v1/managed-agents",
            json={"name": "owner-private", "agent_type": "simple"},
        )
        assert created.status_code == 200
        agent_id = created.json()["id"]
        assert created.json()["owner_provenance"] == OWNER.provenance
        assert (
            client.post(
                f"/v1/managed-agents/{agent_id}/messages",
                json={"content": "PRIVATE_MESSAGE_CANARY", "mode": "queued"},
            ).status_code
            == 200
        )
        task = client.post(
            f"/v1/managed-agents/{agent_id}/tasks",
            json={"description": "PRIVATE_TASK_CANARY"},
        ).json()
        binding = client.post(
            f"/v1/managed-agents/{agent_id}/channels",
            json={"channel_type": "test", "config": {"channel": "private"}},
        ).json()

        selected["principal"] = OTHER_OWNER
        assert client.get("/v1/managed-agents").json() == {"agents": []}

        denied_requests = [
            ("get", f"/v1/managed-agents/{agent_id}", None),
            (
                "patch",
                f"/v1/managed-agents/{agent_id}",
                {"name": "cross-principal-write"},
            ),
            ("delete", f"/v1/managed-agents/{agent_id}", None),
            ("post", f"/v1/managed-agents/{agent_id}/pause", None),
            ("post", f"/v1/managed-agents/{agent_id}/resume", None),
            ("post", f"/v1/managed-agents/{agent_id}/run", None),
            ("post", f"/v1/managed-agents/{agent_id}/recover", None),
            ("get", f"/v1/managed-agents/{agent_id}/tasks", None),
            (
                "post",
                f"/v1/managed-agents/{agent_id}/tasks",
                {"description": "cross-principal-task"},
            ),
            (
                "get",
                f"/v1/managed-agents/{agent_id}/tasks/{task['id']}",
                None,
            ),
            (
                "patch",
                f"/v1/managed-agents/{agent_id}/tasks/{task['id']}",
                {"description": "cross-principal-task-update"},
            ),
            (
                "delete",
                f"/v1/managed-agents/{agent_id}/tasks/{task['id']}",
                None,
            ),
            ("get", f"/v1/managed-agents/{agent_id}/channels", None),
            (
                "post",
                f"/v1/managed-agents/{agent_id}/channels",
                {"channel_type": "test", "config": {}},
            ),
            (
                "delete",
                f"/v1/managed-agents/{agent_id}/channels/{binding['id']}",
                None,
            ),
            ("get", f"/v1/managed-agents/{agent_id}/messages", None),
            (
                "post",
                f"/v1/managed-agents/{agent_id}/messages",
                {"content": "cross-principal-message", "mode": "queued"},
            ),
            ("get", f"/v1/managed-agents/{agent_id}/state", None),
            ("get", f"/v1/managed-agents/{agent_id}/learning", None),
            ("post", f"/v1/managed-agents/{agent_id}/learning/run", None),
            ("get", f"/v1/managed-agents/{agent_id}/traces", None),
            (
                "get",
                f"/v1/managed-agents/{agent_id}/traces/private-trace",
                None,
            ),
        ]
        for method, path, payload in denied_requests:
            kwargs = {"json": payload} if payload is not None else {}
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 404, (method, path, response.text)
            assert "PRIVATE_MESSAGE_CANARY" not in response.text

        unchanged = manager.get_agent(agent_id)
        assert unchanged is not None
        assert unchanged["name"] == "owner-private"
        assert unchanged["status"] == "idle"
        assert [m["content"] for m in manager.list_messages(agent_id)] == [
            "PRIVATE_MESSAGE_CANARY"
        ]
        assert manager._get_task(task["id"])["description"] == "PRIVATE_TASK_CANARY"
        assert manager._get_binding(binding["id"])["agent_id"] == agent_id

        other = client.post(
            "/v1/managed-agents",
            json={"name": "other-private", "agent_type": "simple"},
        )
        assert other.status_code == 200
        assert other.json()["owner_provenance"] == OTHER_OWNER.provenance
        assert [a["id"] for a in client.get("/v1/managed-agents").json()["agents"]] == [
            other.json()["id"]
        ]

        selected["principal"] = OWNER
        assert [a["id"] for a in client.get("/v1/managed-agents").json()["agents"]] == [
            agent_id
        ]

    def test_legacy_ownerless_agent_is_invisible_and_not_mutable_over_http(
        self,
        client,
        manager,
    ):
        legacy = manager.create_agent(
            name="legacy-ownerless",
            agent_type="simple",
            owner_provenance=None,
        )

        assert client.get("/v1/managed-agents").json() == {"agents": []}
        assert client.get(f"/v1/managed-agents/{legacy['id']}").status_code == 404
        assert (
            client.patch(
                f"/v1/managed-agents/{legacy['id']}",
                json={"name": "must-not-change"},
            ).status_code
            == 404
        )
        assert client.delete(f"/v1/managed-agents/{legacy['id']}").status_code == 404
        assert manager.get_agent(legacy["id"])["name"] == "legacy-ownerless"

    def test_sendblue_verify_uses_async_http_client(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            "+15551234567",
            {"phone_number": "+15557654321"},
            None,
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = mock_client_cls.return_value.__aenter__.return_value
            instance.get = AsyncMock(return_value=mock_resp)
            resp = client.post(
                "/v1/channels/sendblue/verify",
                json={"api_key_id": "key-id", "api_secret_key": "secret"},
            )

        assert resp.status_code == 200
        assert resp.json()["valid"] is True
        assert resp.json()["numbers"] == ["+15551234567", "+15557654321"]
        instance.get.assert_awaited_once()

    def test_sendblue_register_webhook_uses_async_http_client(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = mock_client_cls.return_value.__aenter__.return_value
            instance.post = AsyncMock(return_value=mock_resp)
            resp = client.post(
                "/v1/channels/sendblue/register-webhook",
                json={
                    "api_key_id": "key-id",
                    "api_secret_key": "secret",
                    "webhook_url": "https://example.com/webhooks/sendblue",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["registered"] is True
        instance.post.assert_awaited_once()

    def test_sendblue_test_message_uses_async_http_client(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = mock_client_cls.return_value.__aenter__.return_value
            instance.post = AsyncMock(return_value=mock_resp)
            resp = client.post(
                "/v1/channels/sendblue/test",
                json={
                    "api_key_id": "key-id",
                    "api_secret_key": "secret",
                    "from_number": "+15550000000",
                    "to_number": "+15551234567",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["sent"] is True
        instance.post.assert_awaited_once()

    def test_create_agent(self, client):
        resp = client.post(
            "/v1/managed-agents",
            json={
                "name": "researcher",
                "agent_type": "monitor_operative",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "researcher"
        assert data["status"] == "idle"

    def test_get_agent(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "test"})
        agent_id = create_resp.json()["id"]
        resp = client.get(f"/v1/managed-agents/{agent_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == agent_id

    def test_get_agent_not_found(self, client):
        resp = client.get("/v1/managed-agents/nonexistent")
        assert resp.status_code == 404

    def test_update_agent(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "old"})
        agent_id = create_resp.json()["id"]
        resp = client.patch(f"/v1/managed-agents/{agent_id}", json={"name": "new"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "new"

    def test_delete_agent(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "doomed"})
        agent_id = create_resp.json()["id"]
        resp = client.delete(f"/v1/managed-agents/{agent_id}")
        assert resp.status_code == 200

    def test_pause_resume(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "pausable"})
        agent_id = create_resp.json()["id"]
        client.post(f"/v1/managed-agents/{agent_id}/pause")
        resp = client.get(f"/v1/managed-agents/{agent_id}")
        assert resp.json()["status"] == "paused"
        client.post(f"/v1/managed-agents/{agent_id}/resume")
        resp = client.get(f"/v1/managed-agents/{agent_id}")
        assert resp.json()["status"] == "idle"

    @pytest.mark.parametrize(
        ("method", "suffix"),
        [("post", "pause"), ("post", "resume"), ("delete", "")],
    )
    def test_control_transition_cannot_release_running_agent(
        self, manager, client, method, suffix
    ):
        agent = manager.create_agent(name=f"running-{method}-{suffix}")
        manager.start_tick(agent["id"])
        url = f"/v1/managed-agents/{agent['id']}"
        if suffix:
            url += f"/{suffix}"

        response = getattr(client, method)(url)

        assert response.status_code == 409
        assert manager.get_agent(agent["id"])["status"] == "running"

    @pytest.mark.parametrize(
        "status", ["paused", "archived", "error", "needs_attention", "budget_exceeded"]
    )
    def test_run_requires_explicit_idle_state(self, manager, client, status):
        agent = manager.create_agent(name=f"blocked-{status}")
        manager.update_agent(agent["id"], status=status)

        response = client.post(f"/v1/managed-agents/{agent['id']}/run")

        assert response.status_code == 409
        assert manager.get_agent(agent["id"])["status"] == status

    def test_create_task(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "worker"})
        agent_id = create_resp.json()["id"]
        resp = client.post(
            f"/v1/managed-agents/{agent_id}/tasks",
            json={
                "description": "Find papers on reasoning",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Find papers on reasoning"

    def test_list_tasks(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "worker"})
        agent_id = create_resp.json()["id"]
        client.post(f"/v1/managed-agents/{agent_id}/tasks", json={"description": "t1"})
        client.post(f"/v1/managed-agents/{agent_id}/tasks", json={"description": "t2"})
        resp = client.get(f"/v1/managed-agents/{agent_id}/tasks")
        assert len(resp.json()["tasks"]) == 2

    def test_declarative_channel_binding_crud(self, client):
        create_resp = client.post("/v1/managed-agents", json={"name": "notifier"})
        agent_id = create_resp.json()["id"]
        # Bind
        bind_resp = client.post(
            f"/v1/managed-agents/{agent_id}/channels",
            json={
                "channel_type": "test",
                "config": {"channel": "private"},
            },
        )
        assert bind_resp.status_code == 200
        binding_id = bind_resp.json()["id"]
        # List
        list_resp = client.get(f"/v1/managed-agents/{agent_id}/channels")
        assert len(list_resp.json()["bindings"]) == 1
        # Unbind
        url = f"/v1/managed-agents/{agent_id}/channels/{binding_id}"
        unbind_resp = client.delete(url)
        assert unbind_resp.status_code == 200

    @pytest.mark.parametrize(
        "channel_type",
        ["imessage", "sendblue", "slack", "twilio"],
    )
    def test_two_owners_cannot_activate_process_global_channel_bindings(
        self,
        client,
        manager,
        monkeypatch,
        channel_type,
    ):
        from ava_extensions.server import principal as principal_module

        selected = {"principal": OWNER}
        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: selected["principal"],
        )
        owner_agent = manager.create_agent(name="owner-runtime")
        other_agent = manager.create_agent(
            name="other-runtime",
            owner_provenance=OTHER_OWNER.provenance,
        )
        other_binding = manager.bind_channel(
            other_agent["id"],
            channel_type=channel_type,
            config={},
        )
        bind_spy = MagicMock(wraps=manager.bind_channel)
        monkeypatch.setattr(manager, "bind_channel", bind_spy)

        responses = []
        for principal, agent in (
            (OWNER, owner_agent),
            (OTHER_OWNER, other_agent),
        ):
            selected["principal"] = principal
            responses.append(
                client.post(
                    f"/v1/managed-agents/{agent['id']}/channels",
                    json={"channel_type": channel_type, "config": {}},
                )
            )

        assert [response.status_code for response in responses] == [409, 409]
        assert [response.json() for response in responses] == [
            {"detail": "Channel binding is unavailable"},
            {"detail": "Channel binding is unavailable"},
        ]
        bind_spy.assert_not_called()
        assert manager.list_channel_bindings(owner_agent["id"]) == []
        assert manager._get_binding(other_binding["id"]) == other_binding

    @pytest.mark.parametrize(
        "channel_type",
        ["imessage", "sendblue", "slack", "twilio"],
    )
    def test_two_owners_cannot_mutate_or_stop_process_global_bindings(
        self,
        client,
        manager,
        monkeypatch,
        channel_type,
    ):
        from ava_extensions.server import principal as principal_module

        selected = {"principal": OWNER}
        monkeypatch.setattr(
            principal_module,
            "resolve_request_principal",
            lambda _headers: selected["principal"],
        )
        owner_agent = manager.create_agent(name="owner-runtime")
        other_agent = manager.create_agent(
            name="other-runtime",
            owner_provenance=OTHER_OWNER.provenance,
        )
        owner_binding = manager.bind_channel(
            owner_agent["id"],
            channel_type=channel_type,
            config={},
        )
        other_binding = manager.bind_channel(
            other_agent["id"],
            channel_type=channel_type,
            config={},
        )
        unbind_spy = MagicMock(wraps=manager.unbind_channel)
        monkeypatch.setattr(manager, "unbind_channel", unbind_spy)

        with (
            patch("openjarvis.channels.imessage_daemon.stop_daemon") as stop_imessage,
            patch("openjarvis.channels.slack_daemon.stop_daemon") as stop_slack,
        ):
            owner_response = client.delete(
                f"/v1/managed-agents/{owner_agent['id']}/channels/{owner_binding['id']}"
            )
            selected["principal"] = OTHER_OWNER
            foreign_response = client.delete(
                f"/v1/managed-agents/{owner_agent['id']}/channels/{owner_binding['id']}"
            )

        assert owner_response.status_code == 409
        assert owner_response.json() == {"detail": "Channel binding is unavailable"}
        assert foreign_response.status_code == 404
        assert foreign_response.json() == {"detail": "Agent not found"}
        unbind_spy.assert_not_called()
        stop_imessage.assert_not_called()
        stop_slack.assert_not_called()
        assert manager._get_binding(owner_binding["id"]) == owner_binding
        assert manager._get_binding(other_binding["id"]) == other_binding

    def test_templates(self, client):
        resp = client.get("/v1/templates")
        assert resp.status_code == 200
        templates = resp.json()["templates"]
        assert any(t["id"] == "research_monitor" for t in templates)

    def test_recover_agent(self, manager, client):
        # Create agent, save checkpoint, set error status
        agent = manager.create_agent(name="err", agent_type="simple")
        manager.save_checkpoint(agent["id"], "tick-1", {"msgs": []}, {})
        manager.update_agent(agent["id"], status="error")

        res = client.post(f"/v1/managed-agents/{agent['id']}/recover")
        assert res.status_code == 200
        body = res.json()
        assert body["recovered"] is True
        assert body["checkpoint"]["tick_id"] == "tick-1"

    def test_recover_agent_no_checkpoint(self, manager, client):
        agent = manager.create_agent(name="err", agent_type="simple")
        manager.update_agent(agent["id"], status="error")
        res = client.post(f"/v1/managed-agents/{agent['id']}/recover")
        assert res.status_code == 200
        body = res.json()
        assert body["recovered"] is True
        assert body["checkpoint"] is None
        # Status should be reset to idle
        refreshed = manager.get_agent(agent["id"])
        assert refreshed["status"] == "idle"

    def test_list_error_agents(self, manager, client):
        manager.create_agent(name="ok", agent_type="simple")
        err = manager.create_agent(name="broken", agent_type="simple")
        manager.update_agent(err["id"], status="error")

        res = client.get("/v1/agents/errors")
        assert res.status_code == 200
        agents = res.json()["agents"]
        assert len(agents) == 1
        assert agents[0]["name"] == "broken"

    def test_trace_detail_is_scoped_to_the_requested_agent(self, manager, client):
        owner = manager.create_agent(name="trace-owner", agent_type="simple")
        decoy = manager.create_agent(name="trace-decoy", agent_type="simple")
        trace = SimpleNamespace(
            trace_id="owner-private-trace",
            agent=owner["id"],
            outcome="success",
            total_latency_seconds=0.1,
            started_at=1.0,
            metadata={"provenance": OWNER.provenance},
            steps=[
                SimpleNamespace(
                    step_type=SimpleNamespace(value="tool_call"),
                    input="public",
                    output="PRIVATE_TOOL_RESULT_CANARY",
                    duration_seconds=0.01,
                    metadata={},
                )
            ],
        )

        with patch("openjarvis.traces.store.TraceStore") as store_class:
            store_class.return_value.get.return_value = trace
            response = client.get(
                f"/v1/managed-agents/{decoy['id']}/traces/{trace.trace_id}"
            )

        assert response.status_code == 404
        assert "PRIVATE_TOOL_RESULT_CANARY" not in response.text

    def test_trace_detail_hides_legacy_disallowed_tool_results(self, manager, client):
        agent = manager.create_agent(name="legacy-trace", agent_type="simple")
        trace = SimpleNamespace(
            trace_id="legacy-memory-trace",
            agent=agent["id"],
            outcome="success",
            total_latency_seconds=0.1,
            started_at=1.0,
            metadata={"provenance": OWNER.provenance},
            steps=[
                SimpleNamespace(
                    step_type=SimpleNamespace(value="tool_call"),
                    input={"tool": "memoire", "arguments": {}},
                    output={"result": "LEGACY_MEMORY_FACT_CANARY"},
                    duration_seconds=0.01,
                    metadata={},
                )
            ],
        )

        with patch("openjarvis.traces.store.TraceStore") as store_class:
            store_class.return_value.get.return_value = trace
            response = client.get(
                f"/v1/managed-agents/{agent['id']}/traces/{trace.trace_id}"
            )

        assert response.status_code == 404
        assert "LEGACY_MEMORY_FACT_CANARY" not in response.text

    def test_send_and_list_messages(self, manager, client):
        agent = manager.create_agent(name="chat", agent_type="simple")

        res = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "hello", "mode": "queued"},
        )
        assert res.status_code == 200

        res = client.get(f"/v1/managed-agents/{agent['id']}/messages")
        assert res.status_code == 200
        assert len(res.json()["messages"]) == 1

    def test_http_history_hides_disallowed_legacy_tool_results(
        self,
        manager,
        client,
    ):
        agent = manager.create_agent(name="private-history", agent_type="simple")
        source = manager.send_claimed_message(agent["id"], "recall")
        manager.complete_message_turn(
            agent["id"],
            source["id"],
            "legacy result",
            tool_calls=[
                {
                    "tool": "memoire",
                    "arguments": "{}",
                    "result": "PRIVATE_MEMORY_FACT_CANARY",
                    "success": True,
                }
            ],
        )

        messages = client.get(f"/v1/managed-agents/{agent['id']}/messages").json()[
            "messages"
        ]
        state_messages = client.get(f"/v1/managed-agents/{agent['id']}/state").json()[
            "messages"
        ]

        assert messages == []
        assert state_messages == []
        assert "PRIVATE_MEMORY_FACT_CANARY" not in json.dumps(messages)

    def test_get_agent_state(self, manager, client):
        agent = manager.create_agent(name="stateful", agent_type="simple")
        res = client.get(f"/v1/managed-agents/{agent['id']}/state")
        assert res.status_code == 200
        state = res.json()
        assert "agent" in state
        assert "tasks" in state
        assert "channels" in state
        assert "messages" in state
        assert "checkpoint" in state

    def test_send_message_non_stream_unchanged(self, manager, client):
        """stream=False (default) returns a normal JSON message, not SSE."""
        agent = manager.create_agent(name="basic", agent_type="simple")
        res = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "hello", "stream": False},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["content"] == "hello"
        assert data["direction"] == "user_to_agent"

    def test_managed_message_size_is_bounded_before_storage(self, manager, client):
        from openjarvis.agents.manager import MAX_AGENT_MESSAGE_CHARS

        agent = manager.create_agent(name="bounded", agent_type="simple")
        accepted = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "x" * MAX_AGENT_MESSAGE_CHARS},
        )
        rejected = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "x" * (MAX_AGENT_MESSAGE_CHARS + 1)},
        )

        assert accepted.status_code == 200
        assert rejected.status_code == 422
        assert len(manager.list_messages(agent["id"])) == 1

    def test_send_message_stream_not_found(self, manager, client):
        """Streaming to a non-existent agent returns 404."""
        res = client.post(
            "/v1/managed-agents/nonexistent/messages",
            json={"content": "hello", "stream": True},
        )
        assert res.status_code == 404

    def test_run_worker_start_failure_releases_tick(self, manager, client):
        import threading

        agent = manager.create_agent(name="worker-failure", agent_type="simple")
        original_start = threading.Thread.start

        def fail_only_managed_worker(thread):
            if getattr(getattr(thread, "_target", None), "__name__", "") == "_run_tick":
                raise RuntimeError("cannot start")
            return original_start(thread)

        with patch.object(threading.Thread, "start", new=fail_only_managed_worker):
            response = client.post(f"/v1/managed-agents/{agent['id']}/run")

        assert response.status_code == 503
        assert manager.get_agent(agent["id"])["status"] == "error"

    def test_run_uses_http_boundary_without_writing_private_summary(
        self, manager, client, monkeypatch
    ):
        import threading
        import time

        from openjarvis.agents.executor import AgentExecutor
        from openjarvis.server import agent_manager_routes as routes

        agent = manager.create_agent(name="http-run", agent_type="simple")
        manager.update_summary_memory(agent["id"], "PRIVATE_LEGACY_CANARY")
        captured = {}
        entered = threading.Event()

        def fake_system(engine, model, config=None, *, http_boundary=False):
            captured["http_boundary"] = http_boundary
            return SimpleNamespace(http_boundary=http_boundary)

        def failing_tick(self, agent_id, **kwargs):
            captured["system"] = self._system
            entered.set()
            raise RuntimeError("worker failure")

        monkeypatch.setattr(routes, "_make_lightweight_system", fake_system)
        monkeypatch.setattr(AgentExecutor, "execute_tick", failing_tick)

        response = client.post(f"/v1/managed-agents/{agent['id']}/run")

        assert response.status_code == 200
        assert entered.wait(timeout=2)
        for _ in range(100):
            if manager.get_agent(agent["id"])["status"] == "error":
                break
            time.sleep(0.01)
        assert captured["http_boundary"] is True
        assert captured["system"].http_boundary is True
        assert manager.get_agent(agent["id"])["summary_memory"] == (
            "PRIVATE_LEGACY_CANARY"
        )
        assert manager.get_agent(agent["id"])["status"] == "error"


def test_run_agent_concurrent_returns_409(tmp_path):
    """Rapid Run Now clicks should not spawn multiple ticks."""
    from openjarvis.agents.manager import AgentManager

    mgr = AgentManager(db_path=str(tmp_path / "test.db"))
    agent = mgr.create_agent("Test", config={"schedule_type": "manual"})
    aid = agent["id"]

    # Simulate first click acquiring the tick
    tick_token = mgr.start_tick(aid)

    # Second click should fail
    with pytest.raises(ValueError, match="cannot execute a tick"):
        mgr.start_tick(aid)

    mgr.end_tick(aid, tick_token)


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
class TestAgentManagerStreaming:
    """Tests for the SSE streaming mode of the managed-agent messages endpoint.

    The new implementation uses engine.stream_full() for real token streaming
    instead of agent.run() + word-by-word replay.
    """

    @pytest.fixture
    def _mock_engine(self):
        """Create a mock engine with a working stream_full() method."""
        from openjarvis.engine._stubs import StreamChunk

        engine = MagicMock()
        engine.engine_id = "mock"
        engine._model = "test-model"
        engine.health.return_value = True

        # Default stream_full: echo the last user message token-by-token
        async def _stream_full(messages, *, model, **kwargs):
            # Find the last user message content
            last_content = ""
            for m in reversed(messages):
                if hasattr(m, "role") and m.role.value == "user":
                    last_content = m.content
                    break
            response = f"Echo: {last_content}"
            for token in response.split(" "):
                yield StreamChunk(content=token + " ")
            yield StreamChunk(finish_reason="stop")

        engine.stream_full = _stream_full
        return engine

    def test_asgi_24_disconnect_terminally_closes_claim(self, manager, _mock_engine):
        import asyncio

        from starlette.requests import ClientDisconnect

        from openjarvis.server.agent_manager_routes import _stream_managed_agent

        agent = manager.create_agent(name="disconnect", agent_type="simple")
        tick_token = manager.start_tick(agent["id"])
        claimed = manager.send_claimed_message(agent["id"], "private question")

        async def scenario() -> None:
            response = await _stream_managed_agent(
                manager=manager,
                agent_record=agent,
                user_content="private question",
                message_id=claimed["id"],
                tick_token=tick_token,
                engine=_mock_engine,
                bus=None,
                app_state=SimpleNamespace(model="test-model"),
            )

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.body":
                    raise OSError("client disappeared")

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/managed",
                "raw_path": b"/managed",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "root_path": "",
            }
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)

        asyncio.run(scenario())

        assert manager.get_agent(agent["id"])["status"] == "error"
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"

    @pytest.fixture
    def stream_client(self, manager, _mock_engine):
        from fastapi import FastAPI

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        app = FastAPI()
        app.state.engine = _mock_engine
        app.state.bus = None

        routers = create_agent_manager_router(manager)
        for r in routers:
            app.include_router(r)
        return TestClient(app)

    def test_send_message_stream(self, manager, stream_client):
        """Test streaming mode returns SSE response with [DONE] sentinel."""
        agent = manager.create_agent(
            name="streamer",
            agent_type="simple",
        )
        resp = stream_client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "What is 2+2?", "stream": True},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        # Parse SSE events
        lines = resp.text.strip().split("\n")
        data_lines = [ln for ln in lines if ln.startswith("data:")]
        assert len(data_lines) > 0
        # Last data line must be [DONE]
        assert data_lines[-1].strip() == "data: [DONE]"

    def test_send_message_stream_real_tokens(self, manager, stream_client):
        """Content arrives as real tokens, not word-burst after completion."""
        agent = manager.create_agent(
            name="streamer_tokens",
            agent_type="simple",
        )
        resp = stream_client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "Hello world", "stream": True},
        )
        assert resp.status_code == 200

        # Collect content tokens from stream
        content_chunks = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:") and "[DONE]" not in line:
                raw = line[5:].strip()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices", [{}])
                delta_content = choices[0].get("delta", {}).get("content")
                if delta_content:
                    content_chunks.append(delta_content)

        # Should have multiple token chunks (real streaming, not single burst)
        assert len(content_chunks) > 1
        full_content = "".join(content_chunks)
        assert "Echo:" in full_content
        assert "Hello world" in full_content

    def test_send_message_stream_stores_response(self, manager, stream_client):
        """After streaming, agent response is persisted in the DB."""
        agent = manager.create_agent(
            name="streamer3",
            agent_type="simple",
        )
        resp = stream_client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "persist me", "stream": True},
        )
        assert resp.status_code == 200

        # Check messages in DB
        messages = manager.list_messages(agent["id"])
        # Should have both the user message and the agent response
        assert len(messages) == 2
        directions = {m["direction"] for m in messages}
        assert "user_to_agent" in directions
        assert "agent_to_user" in directions
        user_msg = next(m for m in messages if m["direction"] == "user_to_agent")
        agent_msg = next(m for m in messages if m["direction"] == "agent_to_user")
        assert "persist me" in agent_msg["content"]
        assert agent_msg["reply_to_id"] == user_msg["id"]
        assert user_msg["status"] == "delivered"

    def test_immediate_non_stream_keeps_ui_contract_with_exact_claim(
        self, manager, stream_client
    ):
        import threading

        from openjarvis.agents.executor import AgentExecutor

        agent = manager.create_agent(name="immediate", agent_type="simple")
        completed = threading.Event()

        def execute_claimed(
            executor,
            agent_id,
            *,
            lock_already_held=False,
            tick_token=None,
            claimed_message=None,
        ):
            assert lock_already_held is True
            assert tick_token is not None
            assert claimed_message is not None
            manager.complete_message_turn(
                agent_id,
                claimed_message["id"],
                "immediate answer",
            )
            manager.end_tick(agent_id, tick_token)
            completed.set()

        with patch.object(AgentExecutor, "execute_tick", new=execute_claimed):
            response = stream_client.post(
                f"/v1/managed-agents/{agent['id']}/messages",
                json={"content": "immediate question", "mode": "immediate"},
            )
            assert response.status_code == 200
            assert completed.wait(timeout=5)

        messages = manager.list_messages(agent["id"])
        source = next(m for m in messages if m["direction"] == "user_to_agent")
        answer = next(m for m in messages if m["direction"] == "agent_to_user")
        assert source["status"] == "delivered"
        assert answer["reply_to_id"] == source["id"]

    def test_send_message_stream_finish_reason(self, manager, stream_client):
        """The final chunk before [DONE] has finish_reason='stop'."""
        agent = manager.create_agent(
            name="streamer4",
            agent_type="simple",
        )
        resp = stream_client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "check finish", "stream": True},
        )
        # Collect all data chunks (excluding [DONE])
        chunks = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:") and "[DONE]" not in line:
                raw = line[5:].strip()
                try:
                    chunks.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue

        # Last chunk should have finish_reason="stop"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    def test_missing_agent_max_tokens_inherits_server_limit_without_persisting_it(
        self, manager
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        captured = {}
        engine = MagicMock(engine_id="inherited-limit", _model="test-model")

        async def inherited_stream(messages, *, model, max_tokens, **kwargs):
            captured["max_tokens"] = max_tokens
            yield StreamChunk(content="complete")
            yield StreamChunk(finish_reason="stop")

        engine.stream_full = inherited_stream
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        app.state.config = SimpleNamespace(
            intelligence=SimpleNamespace(max_tokens=16_384)
        )
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        agent = manager.create_agent(name="inherits", agent_type="simple", config={})

        response = TC(app).post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert captured["max_tokens"] == 16_384
        assert "max_tokens" not in manager.get_agent(agent["id"])["config"]

    def test_personal_research_template_inherits_runtime_limit(
        self, manager, monkeypatch
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        import openjarvis.agents.deep_research as deep_research_module
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.server import agent_manager_routes as routes
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        captured = {}

        class CapturingDeepResearchAgent:
            def __init__(self, **kwargs):
                captured["max_tokens"] = kwargs["max_tokens"]
                self._executor = SimpleNamespace(execute=lambda _call: None)

            def run(self, _input_text, context=None):
                del context
                return AgentResult(
                    content="complete research",
                    metadata={"finish_reason": "stop"},
                )

        monkeypatch.setattr(
            deep_research_module,
            "DeepResearchAgent",
            CapturingDeepResearchAgent,
        )
        monkeypatch.setattr(
            routes,
            "_build_deep_research_tools",
            lambda **_kwargs: [object()],
        )
        agent = manager.create_from_template(
            "personal_deep_research",
            "template-inherits",
            owner_provenance=OWNER.provenance,
        )
        assert "max_tokens" not in agent["config"]

        app = FastAPI()
        app.state.engine = MagicMock(engine_id="fake", _model="test-model")
        app.state.bus = None
        app.state.config = SimpleNamespace(
            intelligence=SimpleNamespace(max_tokens=16_384)
        )
        for router in create_agent_manager_router(manager):
            app.include_router(router)

        response = TC(app).post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert captured["max_tokens"] == 16_384
        assert '"finish_reason": "stop"' in response.text

    def test_truncated_stream_is_failed_instead_of_relabelled_stop(self, manager):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        engine = MagicMock(engine_id="truncated", _model="test-model")

        async def truncated_stream(messages, *, model, **kwargs):
            yield StreamChunk(
                content="TRUNCATED_OUTPUT",
                finish_reason="length",
            )

        engine.stream_full = truncated_stream
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TC(app)
        agent = manager.create_agent(name="truncated", agent_type="simple")

        response = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert "empty_or_incomplete_response" in response.text
        assert '"finish_reason": "stop"' not in response.text
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"

    def test_stream_holds_agent_tick_lock_until_terminal_commit(self, manager):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        engine = MagicMock(engine_id="locked", _model="test-model")
        agent = manager.create_agent(name="locked", agent_type="simple")

        async def locked_stream(messages, *, model, **kwargs):
            with pytest.raises(ValueError, match="cannot execute a tick"):
                manager.start_tick(agent["id"])
            yield StreamChunk(content="complete")
            yield StreamChunk(finish_reason="stop")

        engine.stream_full = locked_stream
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)

        response = TC(app).post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert manager.get_agent(agent["id"])["status"] == "idle"

    def test_oversized_stream_response_is_a_terminal_storage_error(self, manager):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.agents.manager import MAX_AGENT_MESSAGE_CHARS
        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        engine = MagicMock(engine_id="oversized", _model="test-model")

        async def oversized_stream(messages, *, model, **kwargs):
            yield StreamChunk(content="x" * (MAX_AGENT_MESSAGE_CHARS + 1))
            yield StreamChunk(finish_reason="stop")

        engine.stream_full = oversized_stream
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        agent = manager.create_agent(name="oversized", agent_type="simple")

        response = TC(app).post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert "empty_or_incomplete_response" in response.text
        assert '"finish_reason": "stop"' not in response.text
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"
        logs = manager.list_learning_log(agent["id"])
        assert any(
            row["event_type"] == "query_error"
            and row["data"].get("failure_reason") == "persistence_error"
            for row in logs
        )

    def test_send_message_stream_error_handling(self, manager):
        """Engine errors are reported gracefully via SSE."""

        error_engine = MagicMock()
        error_engine.engine_id = "error"
        error_engine._model = "test-model"

        async def _stream_full_error(messages, *, model, **kwargs):
            raise RuntimeError("LLM connection failed")
            yield  # make it a generator  # noqa: E501

        error_engine.stream_full = _stream_full_error

        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        app = FastAPI()
        app.state.engine = error_engine
        app.state.bus = None
        routers = create_agent_manager_router(manager)
        for r in routers:
            app.include_router(r)
        client = TC(app)

        agent = manager.create_agent(name="err_agent", agent_type="simple")
        resp = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "fail", "stream": True},
        )
        assert resp.status_code == 200
        assert "Error:" in resp.text or "error" in resp.text.lower()
        assert "data: [DONE]" in resp.text
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["direction"] == "user_to_agent"
        assert messages[0]["status"] == "failed"

    def test_partial_stream_error_is_never_replayed_as_completed(self, manager):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        engine = MagicMock(engine_id="error", _model="test-model")

        async def _partial_then_error(messages, *, model, **kwargs):
            yield StreamChunk(content="PARTIAL_CANARY")
            raise RuntimeError("backend failed")

        engine.stream_full = _partial_then_error
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TC(app)
        agent = manager.create_agent(name="partial", agent_type="simple")

        response = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "fail after partial", "stream": True},
        )

        assert response.status_code == 200
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"
        assert "PARTIAL_CANARY" not in messages[0]["content"]

    def test_tool_exception_is_generic_for_model_sse_and_history(
        self, manager, monkeypatch
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.core.registry import ToolRegistry
        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router
        from openjarvis.tools._stubs import BaseTool, ToolSpec

        class ExplodingCalculator(BaseTool):
            tool_id = "calculator"

            @property
            def spec(self):
                return ToolSpec(name="calculator", description="test")

            def execute(self, **params):
                raise RuntimeError("PRIVATE_MANAGED_TOOL_CANARY")

        monkeypatch.setitem(ToolRegistry._entries(), "calculator", ExplodingCalculator)
        captured = {}
        engine = MagicMock(engine_id="tool-error", _model="test-model")
        calls = 0

        async def tool_then_answer(messages, *, model, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield StreamChunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-error",
                            "function": {
                                "name": "calculator",
                                "arguments": "{}",
                            },
                        }
                    ],
                    finish_reason="tool_calls",
                )
                return
            captured["messages"] = messages
            yield StreamChunk(content="safe final answer")
            yield StreamChunk(finish_reason="stop")

        engine.stream_full = tool_then_answer
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        agent = manager.create_agent(
            name="tool-error",
            agent_type="simple",
            config={"tools": ["calculator"]},
        )

        response = TC(app).post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "calculate", "stream": True},
        )

        assert response.status_code == 200
        assert "PRIVATE_MANAGED_TOOL_CANARY" not in response.text
        tool_message = next(
            message for message in captured["messages"] if message.role.value == "tool"
        )
        assert tool_message.content == "Tool 'calculator' failed."
        stored = next(
            message
            for message in manager.list_messages(agent["id"])
            if message["direction"] == "agent_to_user"
        )
        assert stored["tool_calls"][0]["success"] is False
        assert stored["tool_calls"][0]["result"] == "Tool 'calculator' failed."

    @pytest.mark.parametrize("exhaust_tools", [False, True])
    def test_empty_or_incomplete_stream_is_an_explicit_terminal_error(
        self, manager, exhaust_tools
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        from openjarvis.engine._stubs import StreamChunk
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        engine = MagicMock(engine_id="incomplete", _model="test-model")

        async def incomplete_stream(messages, *, model, **kwargs):
            if exhaust_tools:
                yield StreamChunk(
                    content="PARTIAL_BEFORE_TOOL",
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "think", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(finish_reason="stop")

        engine.stream_full = incomplete_stream
        app = FastAPI()
        app.state.engine = engine
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TC(app)
        config = {"max_turns": 1}
        if exhaust_tools:
            config["tools"] = ["think"]
        agent = manager.create_agent(
            name="incomplete",
            agent_type="simple",
            config=config,
        )

        response = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "question", "stream": True},
        )

        assert response.status_code == 200
        assert "empty_or_incomplete_response" in response.text
        assert '"finish_reason": "stop"' not in response.text
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"

    def test_deep_research_keeps_identity_history_bounds_and_atomic_pair(
        self, manager, monkeypatch
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        import openjarvis.agents.deep_research as deep_research_module
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.server import agent_manager_routes as routes
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        captured = {}

        class FakeDeepResearchAgent:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self._executor = SimpleNamespace(execute=lambda _call: None)

            def run(self, input_text, context=None):
                captured["input"] = input_text
                captured["context"] = context
                return AgentResult(
                    content="bounded research answer",
                    metadata={"finish_reason": "end_turn"},
                )

        monkeypatch.setattr(
            deep_research_module,
            "DeepResearchAgent",
            FakeDeepResearchAgent,
        )
        monkeypatch.setattr(
            routes,
            "_build_deep_research_tools",
            lambda **_kwargs: [object()],
        )

        app = FastAPI()
        app.state.engine = MagicMock(engine_id="fake", _model="test-model")
        app.state.bus = None
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TC(app)
        agent = manager.create_agent(
            name="deep",
            agent_type="deep_research",
            config={
                "model": "test-model",
                "system_prompt": "Mission bornée",
                "temperature": 0.2,
                "max_tokens": 321,
                "max_turns": 4,
            },
        )
        previous = manager.send_claimed_message(agent["id"], "previous question")
        manager.complete_message_turn(agent["id"], previous["id"], "previous answer")

        response = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "current question", "stream": True},
        )

        assert response.status_code == 200
        assert captured["input"] == "current question"
        assert captured["kwargs"]["max_tokens"] == 321
        assert captured["kwargs"]["max_turns"] == 4
        context = captured["context"]
        assert "Ava" in context.metadata["server_identity_prompt"]
        assert [message.content for message in context.conversation.messages] == [
            "previous question",
            "previous answer",
        ]
        messages = manager.list_messages(agent["id"])
        current = next(
            message
            for message in messages
            if message["direction"] == "user_to_agent"
            and message["content"] == "current question"
        )
        answer = next(
            message
            for message in messages
            if message.get("reply_to_id") == current["id"]
        )
        assert current["status"] == "delivered"
        assert answer["content"] == "bounded research answer"
        assert '"finish_reason": "stop"' in response.text

    @pytest.mark.parametrize(
        ("finish_reason", "max_turns_exceeded", "incomplete_tool_call"),
        [
            ("length", False, False),
            ("max_tokens", False, False),
            (None, False, False),
            ("stop", True, False),
            ("stop", False, True),
        ],
    )
    def test_deep_research_incomplete_terminal_is_failed_without_assistant_reply(
        self,
        manager,
        monkeypatch,
        finish_reason,
        max_turns_exceeded,
        incomplete_tool_call,
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        import openjarvis.agents.deep_research as deep_research_module
        from openjarvis.agents._stubs import AgentResult
        from openjarvis.server import agent_manager_routes as routes
        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        class IncompleteDeepResearchAgent:
            def __init__(self, **_kwargs):
                self._executor = SimpleNamespace(execute=lambda _call: None)

            def run(self, _input_text, context=None):
                del context
                return AgentResult(
                    content="PARTIAL_RESEARCH",
                    metadata={
                        "finish_reason": finish_reason,
                        "max_turns_exceeded": max_turns_exceeded,
                        "incomplete_tool_call": incomplete_tool_call,
                    },
                )

        monkeypatch.setattr(
            deep_research_module,
            "DeepResearchAgent",
            IncompleteDeepResearchAgent,
        )
        monkeypatch.setattr(
            routes,
            "_build_deep_research_tools",
            lambda **_kwargs: [object()],
        )
        app = FastAPI()
        app.state.engine = MagicMock(engine_id="fake", _model="test-model")
        app.state.bus = None
        app.state.config = SimpleNamespace(
            intelligence=SimpleNamespace(max_tokens=16_384)
        )
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TC(app)
        agent = manager.create_agent(
            name="deep-incomplete",
            agent_type="deep_research",
            config={"model": "test-model"},
        )

        response = client.post(
            f"/v1/managed-agents/{agent['id']}/messages",
            json={"content": "current question", "stream": True},
        )

        assert response.status_code == 200
        assert "generation_error" in response.text
        assert '"finish_reason": "stop"' not in response.text
        messages = manager.list_messages(agent["id"])
        assert len(messages) == 1
        assert messages[0]["status"] == "failed"


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
class TestResolveToolSpecs:
    """Unit tests for _resolve_tool_specs — converts template string
    tool names into OpenAI-format function specs so the engine can
    actually bind them to the model.
    """

    @pytest.fixture
    def _registered_tools(self):
        """Re-register tools after the autouse conftest fixture clears them."""
        import importlib
        import sys

        from openjarvis.core.registry import ToolRegistry

        for mod_name in list(sys.modules):
            if (
                mod_name.startswith("openjarvis.tools.")
                and not mod_name.endswith("_stubs")
                and not mod_name.endswith("agent_tools")
            ):
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass
        yield ToolRegistry

    def test_string_names_resolve_to_openai_specs(self, _registered_tools):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        specs = _resolve_tool_specs(["calculator", "think"])
        assert len(specs) == 2
        names = [s["function"]["name"] for s in specs]
        assert "calculator" in names
        assert "think" in names
        for s in specs:
            assert s["type"] == "function"
            assert "description" in s["function"]
            assert "parameters" in s["function"]

    def test_unknown_names_dropped(self, _registered_tools):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        specs = _resolve_tool_specs(["calculator", "nonexistent_tool_xyz"])
        assert len(specs) == 1
        assert specs[0]["function"]["name"] == "calculator"

    def test_quarantined_legacy_memory_cannot_be_resolved(self, _registered_tools):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        assert _resolve_tool_specs(["memoire"]) == []

    def test_quarantined_legacy_memory_dict_cannot_pass_through(
        self, _registered_tools
    ):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        raw = {
            "type": "function",
            "function": {
                "name": "memoire",
                "description": "legacy shared store",
                "parameters": {"type": "object"},
            },
        }
        assert _resolve_tool_specs([raw]) == []

    @pytest.mark.parametrize(
        "name",
        [
            "shell_exec",
            "code_interpreter",
            "file_read",
            "file_write",
            "memory_manage",
            "memory_retrieve",
            "retrieval",
        ],
    )
    def test_managed_agents_reject_unsafe_local_tools(self, _registered_tools, name):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        assert _resolve_tool_specs([name]) == []

    def test_managed_agents_reject_unsafe_raw_tool_dict(self, _registered_tools):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        raw = {
            "type": "function",
            "function": {
                "name": "shell_exec",
                "description": "read arbitrary local files",
                "parameters": {"type": "object"},
            },
        }
        assert _resolve_tool_specs([raw]) == []

    def test_registered_safe_dict_entries_passed_through(self, _registered_tools):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        full_spec = {
            "type": "function",
            "function": {
                "name": "think",
                "description": "x",
                "parameters": {"type": "object"},
            },
        }
        specs = _resolve_tool_specs([full_spec, "calculator"])
        assert len(specs) == 2
        assert specs[0] is full_spec

    @pytest.mark.parametrize(
        "tool_cls",
        [
            pytest.param(
                __import__(
                    "ava_extensions.skills.proposer",
                    fromlist=["ProposerTool"],
                ).ProposerTool,
                id="proposer",
            ),
            pytest.param(
                __import__(
                    "openjarvis.tools.channel_tools",
                    fromlist=["ChannelSendTool"],
                ).ChannelSendTool,
                id="channel_send",
            ),
        ],
    )
    def test_effectful_tool_without_capability_metadata_is_denied(self, tool_cls):
        from openjarvis.server.agent_manager_routes import _managed_tool_allowed

        assert _managed_tool_allowed(tool_cls) is False

    @pytest.mark.parametrize(
        "config",
        [
            {"max_tokens": 32_769},
            {"max_tokens": True},
            {"temperature": -1},
            {"temperature": 3},
            {"max_turns": 0},
            {"max_total_tokens": True},
        ],
    )
    def test_create_rejects_unbounded_runtime_config(self, manager, config):
        from fastapi import FastAPI

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        app = FastAPI()
        for router in create_agent_manager_router(manager):
            app.include_router(router)
        client = TestClient(app)
        response = client.post(
            "/v1/managed-agents",
            json={"name": "unsafe", "agent_type": "simple", "config": config},
        )
        assert response.status_code == 422

    def test_template_instantiation_validates_merged_runtime_config(
        self, manager, monkeypatch
    ):
        from fastapi import FastAPI

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        monkeypatch.setattr(
            manager,
            "list_templates",
            lambda: [
                {
                    "id": "unsafe-template",
                    "name": "Unsafe",
                    "max_tokens": 32_769,
                    "agent_type": "simple",
                    "source": "test",
                }
            ],
        )
        app = FastAPI()
        for router in create_agent_manager_router(manager):
            app.include_router(router)

        response = TestClient(app).post(
            "/v1/templates/unsafe-template/instantiate",
            json={"name": "unsafe", "agent_type": "simple", "config": {}},
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("tool_name", ["browser", "file_read", "memoire"])
    def test_agent_creation_rejects_tools_hidden_by_the_http_boundary(
        self,
        manager,
        tool_name,
    ):
        from fastapi import FastAPI

        from openjarvis.server.agent_manager_routes import create_agent_manager_router

        app = FastAPI()
        for router in create_agent_manager_router(manager):
            app.include_router(router)

        response = TestClient(app).post(
            "/v1/managed-agents",
            json={
                "name": "unsafe",
                "agent_type": "simple",
                "config": {"tools": [tool_name]},
            },
        )

        assert response.status_code == 422
        assert tool_name in response.json()["detail"]

    def test_empty_and_none_return_empty_list(self):
        from openjarvis.server.agent_manager_routes import _resolve_tool_specs

        assert _resolve_tool_specs(None) == []
        assert _resolve_tool_specs([]) == []


class TestLightweightSystemEngineResolution:
    """Regression for #477 / #514: the managed-agent lightweight system must
    resolve the user's *configured* engine (preferred_engine, else
    engine.default), not a hardcoded OllamaEngine. We assert the key passed to
    ``get_engine`` — captured before the system is built — rather than the final
    (telemetry-wrapped) engine object.
    """

    @staticmethod
    def _cfg(preferred, default):
        # context_from_memory absent on .agent -> memory backend resolves to None
        return SimpleNamespace(
            intelligence=SimpleNamespace(preferred_engine=preferred),
            engine=SimpleNamespace(default=default, ollama=SimpleNamespace(host="")),
            agent=SimpleNamespace(),
        )

    def _capture_get_engine(self, monkeypatch):
        captured = {}

        def fake_get_engine(cfg, key):
            captured["key"] = key
            return ("resolved", MagicMock())

        monkeypatch.setattr("openjarvis.engine._discovery.get_engine", fake_get_engine)
        return captured

    def test_resolves_preferred_engine_over_default(self, monkeypatch):
        pytest.importorskip("fastapi")
        from openjarvis.server import agent_manager_routes as amr

        captured = self._capture_get_engine(monkeypatch)
        amr._make_lightweight_system(
            engine=MagicMock(), model="m", config=self._cfg("vllm", "ollama")
        )
        assert captured["key"] == "vllm"

    def test_falls_back_to_engine_default_without_preference(self, monkeypatch):
        pytest.importorskip("fastapi")
        from openjarvis.server import agent_manager_routes as amr

        captured = self._capture_get_engine(monkeypatch)
        amr._make_lightweight_system(
            engine=MagicMock(), model="m", config=self._cfg(None, "llamacpp")
        )
        assert captured["key"] == "llamacpp"
