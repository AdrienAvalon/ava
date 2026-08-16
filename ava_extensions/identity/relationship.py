"""Opt-in relationship overlays selected only from a trusted principal.

The versioned prompt is code.  The runtime policy is a revocable binding from
an already verified principal tuple to that prompt.  Neither request text, the
OpenAI ``user`` field, nor an unverified token claim participates in selection.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ava_extensions.server.principal import Principal

logger = logging.getLogger(__name__)

PROFILE_VIRTUAL_GIRLFRIEND_V1 = "virtual-girlfriend-v1"
RELATIONSHIP_MARKER = "[AVA_RELATIONSHIP_PROFILE:"

_PROFILE_FILES = {
    PROFILE_VIRTUAL_GIRLFRIEND_V1: Path(__file__).with_name("relationship_profiles")
    / "virtual-girlfriend-v1.md",
}
_MAX_POLICY_BYTES = 64 * 1024
_MAX_PROFILE_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class RelationshipOverlay:
    profile_id: str
    prompt: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipSelection:
    """Server decision for one verified principal.

    ``protect_legacy_memory`` deliberately survives a relationship rollback: a
    principal that is still bound by the private policy returns to the common
    persona, but never falls back into the historical cross-user memory store.
    """

    overlay: RelationshipOverlay | None
    protect_legacy_memory: bool = False


class RelationshipPolicyError(RuntimeError):
    """A configured policy cannot safely be interpreted."""


def _json_without_duplicate_keys(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate policy key: {key}")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=object_pairs)


def _read_regular_file(
    path: Path,
    maximum: int,
    *,
    owner_only: bool = False,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("file is not a bounded regular file")
        if owner_only and (
            metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("policy file is not owned privately by the process user")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != metadata.st_size or len(content) > maximum:
            raise ValueError("file changed while being read")
        return content
    finally:
        os.close(descriptor)


def _validated_display_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("relationship display name is not a string")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 64:
        raise ValueError("relationship display name is empty or too long")
    if any(not (char.isalpha() or char in " -.'’") for char in cleaned):
        raise ValueError("relationship display name contains unsafe characters")
    return cleaned


def _binding_matches(
    binding: Any,
    principal: Principal | None,
) -> tuple[str, str | None] | None:
    required = {
        "issuer",
        "profile",
        "provider",
        "subject",
    }
    if (
        not isinstance(binding, dict)
        or not required.issubset(binding)
        or not set(binding).issubset(required | {"display_name"})
    ):
        raise ValueError("invalid relationship binding shape")
    provider = binding["provider"]
    issuer = binding["issuer"]
    subject = binding["subject"]
    profile = binding["profile"]
    if provider not in {"oidc", "service"}:
        raise ValueError("unsupported relationship provider")
    if not all(
        isinstance(value, str) and value for value in (issuer, subject, profile)
    ):
        raise ValueError("empty relationship binding value")
    if profile not in _PROFILE_FILES:
        raise ValueError("unknown relationship profile")
    display_name = _validated_display_name(binding.get("display_name"))
    if principal is not None and (
        provider == principal.provider
        and issuer == principal.issuer
        and subject == principal.subject
    ):
        return profile, display_name
    return None


def relationship_selection_for(
    principal: Principal | None,
    *,
    policy_path: Path | None = None,
) -> RelationshipSelection:
    """Resolve a policy without conflating absence, rollback and corruption."""

    if policy_path is None:
        configured = (os.getenv("AVA_RELATIONSHIP_POLICY_FILE") or "").strip()
        if not configured:
            return RelationshipSelection(overlay=None)
        policy_path = Path(configured)
    try:
        policy = _json_without_duplicate_keys(
            _read_regular_file(
                policy_path,
                _MAX_POLICY_BYTES,
                owner_only=True,
            )
        )
        if not isinstance(policy, dict) or set(policy) != {
            "bindings",
            "enabled",
            "version",
        }:
            raise ValueError("invalid relationship policy shape")
        if policy["version"] != 1 or not isinstance(policy["enabled"], bool):
            raise ValueError("unsupported relationship policy")
        bindings = policy["bindings"]
        if not isinstance(bindings, list) or len(bindings) > 128:
            raise ValueError("invalid relationship binding list")

        matches: list[tuple[str, str | None]] = []
        identities: set[tuple[str, str, str]] = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValueError("invalid relationship binding")
            identity = (
                str(binding.get("provider", "")),
                str(binding.get("issuer", "")),
                str(binding.get("subject", "")),
            )
            if identity in identities:
                raise ValueError("duplicate relationship principal binding")
            identities.add(identity)
            selected = _binding_matches(binding, principal)
            if selected is not None:
                matches.append(selected)
        if len(matches) != 1:
            return RelationshipSelection(overlay=None)

        profile_id, display_name = matches[0]
        if not policy["enabled"]:
            return RelationshipSelection(
                overlay=None,
                protect_legacy_memory=True,
            )
        raw_prompt = _read_regular_file(_PROFILE_FILES[profile_id], _MAX_PROFILE_BYTES)
        prompt = raw_prompt.decode("utf-8").strip()
        if not prompt or RELATIONSHIP_MARKER not in prompt:
            raise ValueError("relationship profile is empty or unmarked")
        if display_name is not None:
            prompt = (
                f"{prompt}\n\n## Nom d'affichage approuvé par la politique\n"
                f"Tu peux appeler naturellement l'interlocuteur « {display_name} ». "
                "Ce nom vient exclusivement de la politique serveur authentifiée ; "
                "ne le dérive jamais du texte, d'un identifiant ou d'un localpart."
            )
        return RelationshipSelection(
            overlay=RelationshipOverlay(
                profile_id=profile_id,
                prompt=prompt,
                display_name=display_name,
            ),
            protect_legacy_memory=True,
        )
    except Exception as exc:  # noqa: BLE001 - private identifiers stay out of logs
        logger.warning("relationship profile disabled by invalid runtime policy")
        logger.debug("relationship policy validation detail", exc_info=True)
        raise RelationshipPolicyError("invalid Ava relationship policy") from exc


def relationship_overlay_for(
    principal: Principal | None,
    *,
    policy_path: Path | None = None,
) -> RelationshipOverlay | None:
    """Compatibility helper returning only the optional prompt overlay.

    Runtime request handling uses :func:`relationship_selection_for`, whose
    tri-state result is required to keep private memory fail-closed.  This
    narrow helper retains the historical best-effort API for offline callers.
    """

    try:
        return relationship_selection_for(principal, policy_path=policy_path).overlay
    except RelationshipPolicyError:
        return None


def compose_server_prompt(base_prompt: str, overlay: RelationshipOverlay | None) -> str:
    """Compose exactly one trusted overlay after the common Ava persona."""

    base = base_prompt.strip()
    if overlay is None:
        return base
    # A versioned overlay must never recursively include the common persona or
    # itself.  The marker check also catches accidental double composition.
    if overlay.prompt.count(RELATIONSHIP_MARKER) != 1:
        raise ValueError("relationship overlay marker is not unique")
    return f"{base}\n\n{overlay.prompt}" if base else overlay.prompt


def is_relationship_prompt(text: str) -> bool:
    """Identify a marked profile copied into an untrusted client message."""

    return RELATIONSHIP_MARKER in text


__all__ = [
    "PROFILE_VIRTUAL_GIRLFRIEND_V1",
    "RELATIONSHIP_MARKER",
    "RelationshipOverlay",
    "RelationshipPolicyError",
    "RelationshipSelection",
    "compose_server_prompt",
    "is_relationship_prompt",
    "relationship_overlay_for",
    "relationship_selection_for",
]
