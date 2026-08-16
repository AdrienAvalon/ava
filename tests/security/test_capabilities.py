"""Tests for RBAC capabilities system (Phase 14.4)."""

from __future__ import annotations

import json

import pytest

from openjarvis.security.capabilities import (
    DEFAULT_TOOL_CAPABILITIES,
    Capability,
    CapabilityPolicy,
)


class TestCapability:
    def test_capability_values(self):
        assert Capability.FILE_READ == "file:read"
        assert Capability.NETWORK_FETCH == "network:fetch"
        assert Capability.CODE_EXECUTE == "code:execute"
        assert Capability.SYSTEM_ADMIN == "system:admin"

    def test_all_capabilities_exist(self):
        expected = {
            "file:read",
            "file:write",
            "network:fetch",
            "code:execute",
            "memory:read",
            "memory:write",
            "channel:send",
            "tool:invoke",
            "schedule:create",
            "system:admin",
        }
        actual = {c.value for c in Capability}
        assert expected == actual


class TestCapabilityPolicy:
    def test_default_allow(self):
        policy = CapabilityPolicy()
        assert policy.check("agent1", "file:read")
        assert policy.check("agent1", "code:execute")

    def test_default_deny(self):
        policy = CapabilityPolicy(default_deny=True)
        assert not policy.check("agent1", "file:read")

    def test_explicit_grant(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:read")
        assert policy.check("agent1", "file:read")
        assert not policy.check("agent1", "code:execute")

    def test_explicit_deny(self):
        policy = CapabilityPolicy()
        policy.deny("agent1", "code:execute")
        assert not policy.check("agent1", "code:execute")
        assert policy.check("agent1", "file:read")

    def test_deny_overrides_grant(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "code:execute")
        policy.deny("agent1", "code:execute")
        assert not policy.check("agent1", "code:execute")

    def test_resource_pattern(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:read", pattern="/safe/*")
        assert policy.check("agent1", "file:read", "/safe/data.txt")
        assert not policy.check("agent1", "file:read", "/etc/passwd")

    def test_glob_pattern(self):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("agent1", "file:*")
        assert policy.check("agent1", "file:read")
        assert policy.check("agent1", "file:write")
        assert not policy.check("agent1", "code:execute")

    def test_list_grants(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.grant("agent1", "code:execute")
        grants = policy.list_grants("agent1")
        assert len(grants) == 2

    def test_list_agents(self):
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.grant("agent2", "code:execute")
        agents = policy.list_agents()
        assert set(agents) == {"agent1", "agent2"}

    def test_no_policy_agent(self):
        policy = CapabilityPolicy()
        assert policy.list_grants("unknown") == []

    def test_empty_agent_can_never_be_granted(self):
        policy = CapabilityPolicy()

        with pytest.raises(ValueError, match="agent_id"):
            policy.grant("", "file:read")
        with pytest.raises(ValueError, match="agent_id"):
            policy.deny("   ", "file:read")

        assert policy.check("", "file:read") is False

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "policy.json"
        policy = CapabilityPolicy()
        policy.grant("agent1", "file:read")
        policy.deny("agent1", "code:execute")
        policy.save(path)

        loaded = CapabilityPolicy(policy_path=str(path))
        assert loaded.check("agent1", "file:read")
        assert not loaded.check("agent1", "code:execute")

    def test_load_nonexistent_file(self):
        policy = CapabilityPolicy(policy_path="/nonexistent/path.json")
        # Should not raise, just have no policies
        assert policy.check("agent1", "file:read")

    def test_missing_runtime_policy_is_empty_and_default_deny(self, tmp_path):
        policy = CapabilityPolicy(
            policy_path=str(tmp_path / "absent.json"),
            default_deny=True,
        )

        assert policy.list_agents() == []
        assert policy.check("owner", "file:read", "file_read") is False

    def test_missing_policy_reload_clears_old_grants(self, tmp_path):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("owner", "file:read", "file_read")

        policy._load_file(tmp_path / "absent.json")

        assert policy.list_agents() == []
        assert policy.check("owner", "file:read", "file_read") is False

    @pytest.mark.parametrize(
        "document",
        [
            {"agents": [], "extra": True},
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [],
                        "deny": [],
                        "extra": True,
                    }
                ]
            },
            {
                "agents": [
                    {"agent_id": "owner", "grants": [], "deny": []},
                    {"agent_id": "owner", "grants": [], "deny": []},
                ]
            },
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [{"pattern": "file_read"}],
                        "deny": [],
                    }
                ]
            },
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [],
                    }
                ]
            },
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [
                            {"capability": "file:read", "pattern": "file_read"},
                            {"capability": "file:read", "pattern": "file_read"},
                        ],
                        "deny": [],
                    }
                ]
            },
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [],
                        "deny": ["file:read", "file:read"],
                    }
                ]
            },
            {
                "agents": [
                    {
                        "agent_id": "owner",
                        "grants": [{"capability": "file:read", "pattern": "file_read"}],
                        "deny": ["file:read"],
                    }
                ]
            },
        ],
    )
    def test_strict_policy_rejects_extra_duplicate_and_overlap(
        self, tmp_path, document
    ):
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(ValueError):
            CapabilityPolicy(policy_path=str(path), default_deny=True)

    def test_strict_policy_rejects_duplicate_json_keys(self, tmp_path):
        path = tmp_path / "policy.json"
        path.write_text('{"agents": [], "agents": []}', encoding="utf-8")

        with pytest.raises(ValueError, match="duplicate"):
            CapabilityPolicy(policy_path=str(path), default_deny=True)

    def test_strict_policy_rejects_symlink(self, tmp_path):
        target = tmp_path / "target.json"
        target.write_text('{"agents": []}', encoding="utf-8")
        link = tmp_path / "policy.json"
        link.symlink_to(target)

        with pytest.raises(ValueError, match="cannot be opened"):
            CapabilityPolicy(policy_path=str(link), default_deny=True)

    def test_invalid_policy_does_not_install_a_valid_prefix(self, tmp_path):
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("existing", "file:read", "file_read")
        path = tmp_path / "policy.json"
        path.write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "agent_id": "owner",
                            "grants": [
                                {
                                    "capability": "file:read",
                                    "pattern": "file_read",
                                },
                                {"capability": ""},
                            ],
                            "deny": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            policy._load_file(path)

        assert policy.check("owner", "file:read", "file_read") is False
        assert policy.check("existing", "file:read", "file_read") is True

    def test_default_tool_capabilities(self):
        assert "file:read" in DEFAULT_TOOL_CAPABILITIES.get("file_read", [])
        assert "network:fetch" in DEFAULT_TOOL_CAPABILITIES.get("web_search", [])
        assert "code:execute" in DEFAULT_TOOL_CAPABILITIES.get("code_interpreter", [])
