"""Capability labels for Ava's private infrastructure tools.

The generic OpenJarvis labels describe the kind of effect (network or file
access).  Ava-specific labels describe the narrower Avalon resource.  A
runtime policy must grant both: granting generic HTTP access alone must never
unlock household telemetry, infrastructure logs, or documentation proposals.
"""

from __future__ import annotations

NETWORK_FETCH = "network:fetch"
FILE_READ = "file:read"

INFRA_OBSERVE = "ava:infra:observe"
INFRA_LOGS_READ = "ava:infra:logs:read"
HOME_OBSERVE = "ava:home:observe"
EVOLUTIONS_READ = "ava:evolutions:read"
DOCS_READ = "ava:docs:read"
DOCS_PLAN = "ava:docs:plan"
DOCS_PROPOSE = "ava:docs:propose"
JOURNAL_READ = "ava:journal:read"
INTROSPECTION_READ = "ava:introspection:read"

__all__ = [
    "DOCS_PROPOSE",
    "DOCS_PLAN",
    "DOCS_READ",
    "EVOLUTIONS_READ",
    "FILE_READ",
    "HOME_OBSERVE",
    "INFRA_LOGS_READ",
    "INFRA_OBSERVE",
    "INTROSPECTION_READ",
    "JOURNAL_READ",
    "NETWORK_FETCH",
]
