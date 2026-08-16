"""RBAC capability system — fine-grained permission model for tool dispatch."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_POLICY_BYTES = 1024 * 1024
_MAX_AGENTS = 256
_MAX_RULES_PER_AGENT = 256


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate capability policy key: {key}")
        result[key] = value
    return result


def _policy_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned)
    ):
        raise ValueError(f"invalid {label}")
    return cleaned


class Capability(str, Enum):
    """Fine-grained capability labels."""

    FILE_READ = "file:read"
    FILE_WRITE = "file:write"
    NETWORK_FETCH = "network:fetch"
    CODE_EXECUTE = "code:execute"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    CHANNEL_SEND = "channel:send"
    TOOL_INVOKE = "tool:invoke"
    SCHEDULE_CREATE = "schedule:create"
    SYSTEM_ADMIN = "system:admin"


@dataclass(slots=True)
class CapabilityGrant:
    """A single capability grant for an agent."""

    capability: str  # Capability value or glob pattern
    pattern: str = "*"  # resource glob pattern


@dataclass(slots=True)
class AgentPolicy:
    """Policy for a specific agent."""

    agent_id: str
    grants: List[CapabilityGrant] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)  # explicit denials


class CapabilityPolicy:
    """RBAC capability policy for tool dispatch.

    Checks whether an agent has the required capability to invoke a tool.
    Policy can be loaded from a JSON file or configured programmatically.

    Default policy: if no explicit policy exists for an agent, all
    capabilities are granted (open by default). Set ``default_deny=True``
    to flip to deny-by-default.
    """

    def __init__(
        self,
        *,
        policy_path: Optional[str] = None,
        default_deny: bool = False,
    ) -> None:
        self._policies: Dict[str, AgentPolicy] = {}
        self._default_deny = default_deny

        from openjarvis._rust_bridge import get_rust_module

        _rust = get_rust_module()
        self._rust_impl = _rust.CapabilityPolicy(default_deny=default_deny)

        if policy_path:
            self._load_file(Path(policy_path))

    def grant(self, agent_id: str, capability: str, pattern: str = "*") -> None:
        """Grant a capability to an agent."""
        agent_id = _policy_text(agent_id, "agent_id")
        capability = _policy_text(capability, "capability", maximum=128)
        pattern = _policy_text(pattern, "resource pattern")
        policy = self._policies.setdefault(
            agent_id,
            AgentPolicy(agent_id=agent_id),
        )
        policy.grants.append(CapabilityGrant(capability=capability, pattern=pattern))
        self._rust_impl.grant(agent_id, capability, pattern)

    def deny(self, agent_id: str, capability: str) -> None:
        """Explicitly deny a capability to an agent."""
        agent_id = _policy_text(agent_id, "agent_id")
        capability = _policy_text(capability, "capability", maximum=128)
        policy = self._policies.setdefault(
            agent_id,
            AgentPolicy(agent_id=agent_id),
        )
        policy.deny.append(capability)
        self._rust_impl.deny(agent_id, capability)

    def check(self, agent_id: str, capability: str, resource: str = "") -> bool:
        """Check whether *agent_id* has *capability* for *resource*.

        Returns True if allowed, False if denied.
        """
        if not isinstance(agent_id, str) or not agent_id.strip():
            return False
        return self._rust_impl.check(agent_id, capability, resource)

    def _check_python(self, agent_id: str, capability: str, resource: str = "") -> bool:
        """Legacy Python check — kept for reference only."""
        policy = self._policies.get(agent_id)
        if policy is None:
            # No explicit policy — use default
            return not self._default_deny

        # Explicit denials take precedence
        for denied in policy.deny:
            if fnmatch.fnmatch(capability, denied):
                return False

        # Check grants
        for grant in policy.grants:
            if fnmatch.fnmatch(capability, grant.capability):
                if resource and grant.pattern != "*":
                    if fnmatch.fnmatch(resource, grant.pattern):
                        return True
                else:
                    return True

        # No matching grant found
        return not self._default_deny

    def list_grants(self, agent_id: str) -> List[CapabilityGrant]:
        """List all grants for an agent."""
        policy = self._policies.get(agent_id)
        return list(policy.grants) if policy else []

    def list_agents(self) -> List[str]:
        """List all agents with explicit policies."""
        return list(self._policies.keys())

    def _load_file(self, path: Path) -> None:
        """Load one strict JSON policy atomically.

        Missing files leave the policy empty.  Symlinks, duplicate keys,
        unknown fields and malformed rules are rejected before any grant is
        installed, so a damaged policy can never leave a permissive prefix in
        memory.
        """

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            logger.warning("Capability policy file is absent: %s", path)
            from openjarvis._rust_bridge import get_rust_module

            self._policies = {}
            self._rust_impl = get_rust_module().CapabilityPolicy(
                default_deny=self._default_deny
            )
            return
        except OSError as exc:
            raise ValueError(f"capability policy cannot be opened: {path}") from exc

        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("capability policy must be a regular file")
            if file_stat.st_size > _MAX_POLICY_BYTES:
                raise ValueError("capability policy is too large")
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                raw = handle.read(_MAX_POLICY_BYTES + 1)
            if len(raw) > _MAX_POLICY_BYTES:
                raise ValueError("capability policy is too large")
            try:
                data = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid capability policy JSON") from exc
            parsed = self._parse_policy(data)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        from openjarvis._rust_bridge import get_rust_module

        candidate_rust = get_rust_module().CapabilityPolicy(
            default_deny=self._default_deny
        )
        candidate_policies: Dict[str, AgentPolicy] = {}
        for agent in parsed:
            candidate = AgentPolicy(agent_id=agent.agent_id)
            for grant in agent.grants:
                candidate.grants.append(grant)
                candidate_rust.grant(
                    agent.agent_id,
                    grant.capability,
                    grant.pattern,
                )
            for denied in agent.deny:
                candidate.deny.append(denied)
                candidate_rust.deny(agent.agent_id, denied)
            candidate_policies[agent.agent_id] = candidate

        self._policies = candidate_policies
        self._rust_impl = candidate_rust

    @staticmethod
    def _parse_policy(data: Any) -> list[AgentPolicy]:
        if not isinstance(data, dict) or set(data) != {"agents"}:
            raise ValueError("invalid capability policy shape")
        agents = data["agents"]
        if not isinstance(agents, list) or len(agents) > _MAX_AGENTS:
            raise ValueError("invalid capability policy agents")

        parsed: list[AgentPolicy] = []
        seen_agents: set[str] = set()
        for raw_agent in agents:
            if not isinstance(raw_agent, dict) or set(raw_agent) != {
                "agent_id",
                "deny",
                "grants",
            }:
                raise ValueError("invalid capability agent policy shape")
            agent_id = _policy_text(raw_agent["agent_id"], "agent_id")
            if agent_id in seen_agents:
                raise ValueError("duplicate capability agent policy")
            seen_agents.add(agent_id)

            raw_grants = raw_agent["grants"]
            raw_deny = raw_agent["deny"]
            if (
                not isinstance(raw_grants, list)
                or len(raw_grants) > _MAX_RULES_PER_AGENT
                or not isinstance(raw_deny, list)
                or len(raw_deny) > _MAX_RULES_PER_AGENT
            ):
                raise ValueError("invalid capability rule list")

            grants: list[CapabilityGrant] = []
            seen_grants: set[tuple[str, str]] = set()
            for raw_grant in raw_grants:
                if not isinstance(raw_grant, dict) or set(raw_grant) not in (
                    {"capability"},
                    {"capability", "pattern"},
                ):
                    raise ValueError("invalid capability grant shape")
                grant = CapabilityGrant(
                    capability=_policy_text(
                        raw_grant["capability"],
                        "capability",
                        maximum=128,
                    ),
                    pattern=_policy_text(
                        raw_grant.get("pattern", "*"),
                        "resource pattern",
                    ),
                )
                grant_key = (grant.capability, grant.pattern)
                if grant_key in seen_grants:
                    raise ValueError("duplicate capability grant")
                seen_grants.add(grant_key)
                grants.append(grant)
            denied = [
                _policy_text(value, "denied capability", maximum=128)
                for value in raw_deny
            ]
            if len(set(denied)) != len(denied):
                raise ValueError("duplicate capability denial")
            if {grant.capability for grant in grants} & set(denied):
                raise ValueError("capability cannot be both granted and denied")
            parsed.append(AgentPolicy(agent_id=agent_id, grants=grants, deny=denied))
        return parsed

    def save(self, path: Path) -> None:
        """Save policy to a JSON file."""
        agents = []
        for agent_id, policy in self._policies.items():
            agents.append(
                {
                    "agent_id": agent_id,
                    "grants": [
                        {"capability": g.capability, "pattern": g.pattern}
                        for g in policy.grants
                    ],
                    "deny": policy.deny,
                }
            )
        path.write_text(json.dumps({"agents": agents}, indent=2))


# Default capability requirements for built-in tools
DEFAULT_TOOL_CAPABILITIES: Dict[str, List[str]] = {
    "file_read": [Capability.FILE_READ],
    "web_search": [Capability.NETWORK_FETCH],
    "code_interpreter": [Capability.CODE_EXECUTE],
    "memory_store": [Capability.MEMORY_WRITE],
    "memory_retrieve": [Capability.MEMORY_READ],
    "memory_search": [Capability.MEMORY_READ],
    "memory_index": [Capability.MEMORY_WRITE],
    "schedule_task": [Capability.SCHEDULE_CREATE],
    "channel_send": [Capability.CHANNEL_SEND],
}


__all__ = [
    "AgentPolicy",
    "Capability",
    "CapabilityGrant",
    "CapabilityPolicy",
    "DEFAULT_TOOL_CAPABILITIES",
]
