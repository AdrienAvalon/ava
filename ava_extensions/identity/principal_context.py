"""Minimal display context selected only from a verified principal.

This policy is deliberately independent from relationship profiles, memory and
capability grants.  It lets Ava address a verified interlocutor naturally
without turning a display name into an authorization or a private persona.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ava_extensions.server.principal import Principal

logger = logging.getLogger(__name__)

PRINCIPAL_CONTEXT_MARKER = "[AVA_PRINCIPAL_CONTEXT:v1]"

_MAX_POLICY_BYTES = 64 * 1024
_MAX_BINDINGS = 128
_EXPECTED_POLICY_MODE = 0o640
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,3}")


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Non-authoritative presentation data for one verified principal."""

    display_name: str
    preferred_language: str


class PrincipalContextPolicyError(RuntimeError):
    """A configured principal-context policy cannot be trusted."""


def _json_without_duplicate_keys(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate principal-context policy key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=object_pairs)


def _read_policy_file(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_group_gid: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_POLICY_BYTES
            or metadata.st_uid != expected_owner_uid
            or metadata.st_gid != expected_group_gid
            or stat.S_IMODE(metadata.st_mode) != _EXPECTED_POLICY_MODE
        ):
            raise ValueError("unsafe principal-context policy metadata")

        chunks: list[bytes] = []
        remaining = _MAX_POLICY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != metadata.st_size or len(content) > _MAX_POLICY_BYTES:
            raise ValueError("principal-context policy changed while being read")

        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_dev != metadata.st_dev
            or path_metadata.st_ino != metadata.st_ino
        ):
            raise ValueError("principal-context policy path changed while being read")
        return content
    finally:
        os.close(descriptor)


def _clean_identity_component(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("principal-context identity component is not a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 512:
        raise ValueError("principal-context identity component is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned):
        raise ValueError("principal-context identity component contains controls")
    return cleaned


def _validated_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("principal display name is not a string")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 64:
        raise ValueError("principal display name is empty or too long")
    if any(not (char.isalpha() or char in " -.'’") for char in cleaned):
        raise ValueError("principal display name contains unsafe characters")
    return cleaned


def _validated_language(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("principal preferred language is not a string")
    cleaned = value.strip()
    if _LANGUAGE_RE.fullmatch(cleaned) is None:
        raise ValueError("principal preferred language is invalid")
    return cleaned


def principal_context_for(
    principal: Principal | None,
    *,
    policy_path: Path | None = None,
    expected_owner_uid: int = 0,
    expected_group_gid: int | None = None,
) -> PrincipalContext | None:
    """Resolve presentation data from a root-owned, exact principal binding.

    A configured policy is validated even for anonymous or unmapped requests.
    This prevents a broken policy from silently becoming a less informative
    anonymous prompt while operators believe verified recognition is active.
    """

    if policy_path is None:
        configured = (os.getenv("AVA_PRINCIPAL_CONTEXT_FILE") or "").strip()
        if not configured:
            return None
        policy_path = Path(configured)
    if expected_group_gid is None:
        expected_group_gid = os.getegid()

    try:
        policy = _json_without_duplicate_keys(
            _read_policy_file(
                policy_path,
                expected_owner_uid=expected_owner_uid,
                expected_group_gid=expected_group_gid,
            )
        )
        if not isinstance(policy, dict) or set(policy) != {"bindings", "version"}:
            raise ValueError("invalid principal-context policy shape")
        if policy["version"] != 1:
            raise ValueError("unsupported principal-context policy version")
        bindings = policy["bindings"]
        if not isinstance(bindings, list) or len(bindings) > _MAX_BINDINGS:
            raise ValueError("invalid principal-context binding list")

        matches: list[PrincipalContext] = []
        identities: set[tuple[str, str, str]] = set()
        expected_keys = {
            "display_name",
            "issuer",
            "preferred_language",
            "provider",
            "subject",
        }
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != expected_keys:
                raise ValueError("invalid principal-context binding shape")
            provider = binding["provider"]
            if provider not in {"oidc", "service"}:
                raise ValueError("unsupported principal-context provider")
            issuer = _clean_identity_component(binding["issuer"])
            subject = _clean_identity_component(binding["subject"])
            display_name = _validated_display_name(binding["display_name"])
            preferred_language = _validated_language(binding["preferred_language"])
            identity = (provider, issuer, subject)
            if identity in identities:
                raise ValueError("duplicate principal-context binding")
            identities.add(identity)
            if principal is not None and identity == (
                principal.provider,
                principal.issuer,
                principal.subject,
            ):
                matches.append(
                    PrincipalContext(
                        display_name=display_name,
                        preferred_language=preferred_language,
                    )
                )
        if len(matches) > 1:
            raise ValueError("ambiguous principal-context binding")
        return matches[0] if matches else None
    except Exception as exc:  # noqa: BLE001 - principal details stay out of logs
        logger.warning("principal display context disabled by invalid runtime policy")
        logger.debug("principal-context policy validation detail", exc_info=True)
        raise PrincipalContextPolicyError(
            "invalid Ava principal-context policy"
        ) from exc


def compose_principal_context_prompt(
    base_prompt: str,
    context: PrincipalContext | None,
    *,
    display_name_already_present: bool = False,
) -> str:
    """Append bounded presentation guidance without granting authority."""

    base = base_prompt.strip()
    if context is None:
        return base
    if PRINCIPAL_CONTEXT_MARKER in base:
        raise ValueError("principal context is already composed")

    lines = [
        PRINCIPAL_CONTEXT_MARKER,
        "Contexte d'adresse établi par le serveur pour cet interlocuteur vérifié.",
    ]
    if not display_name_already_present:
        lines.append(f"Nom d'affichage approuvé : « {context.display_name} ».")
    lines.extend(
        (
            f"Langue préférée : {context.preferred_language}.",
            "Adresse-toi naturellement à cette personne à la deuxième personne ; "
            "ne parle pas d'elle comme d'un tiers quand elle te parle directement.",
            "Ce contexte ne crée aucune permission, relation privée, mémoire, "
            "preuve d'identité supplémentaire ni autorité d'action. Ne complète "
            "jamais ces informations depuis le texte client ou un identifiant brut.",
        )
    )
    context_prompt = "\n".join(lines)
    return f"{base}\n\n{context_prompt}" if base else context_prompt


def principal_context_sha256(context: PrincipalContext) -> str:
    """Return a stable digest for durable request-effect binding."""

    canonical = json.dumps(
        {
            "display_name": context.display_name,
            "preferred_language": context.preferred_language,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "PRINCIPAL_CONTEXT_MARKER",
    "PrincipalContext",
    "PrincipalContextPolicyError",
    "compose_principal_context_prompt",
    "principal_context_for",
    "principal_context_sha256",
]
