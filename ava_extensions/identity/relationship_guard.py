"""Garde runtime deterministe des sorties de l'overlay relationnel prive.

Ce module ne selectionne jamais un profil. Il ne peut etre construit qu'a
partir de l'overlay deja choisi par la frontiere d'identite serveur, puis
classe une sortie sans conserver ni exposer le texte refuse dans sa decision
ou ses metadonnees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from ava_extensions.identity.relationship import (
    PROFILE_VIRTUAL_GIRLFRIEND_V1,
    RELATIONSHIP_MARKER,
    RelationshipOverlay,
)
from ava_extensions.identity.relationship_safety import (
    MIN_EXACT_ECHO_CHARACTERS,
    MIN_EXACT_ECHO_TOKENS,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_SPEC,
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    RUNTIME_GUARD_GATE_IDS,
    SAFE_RELATIONSHIP_REPLACEMENT,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    TEXT_GATE_IDS,
    classify_relationship_text,
    conversation_echo_turn_indexes,
    exact_echo_allowed_turn_indexes,
    relationship_text_safety_policy_sha256,
    safe_relationship_replacement_for,
)

_SUPPORTED_POLICY_ID = "ava.relationship.text-safety"
_SUPPORTED_POLICY_VERSION = "1.6.2"
_SUPPORTED_ALGORITHM_REVISION = "relationship-text-safety-algorithm/v3"
_EXPECTED_TEXT_GATE_IDS = (
    "deceptive_humanity",
    "deceptive_emotion",
    "jealousy",
    "guilt",
    "exclusivity",
    "isolation",
    "dependency",
    "coercion",
    "self_promotion",
)
_MAX_TOOL_ARGUMENT_NODES = 4096
_MAX_TOOL_ARGUMENT_TEXT_CHARACTERS = 512 * 1024
_POLICY_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RelationshipGuardUnavailableError(RuntimeError):
    """La politique runtime ne peut pas etre appliquee de maniere sure."""


class RelationshipToolArgumentBlockedError(RuntimeError):
    """Un appel d'outil relationnel a ete refuse avant execution."""


@dataclass(frozen=True, slots=True)
class RelationshipGuardDecision:
    """Decision sans copie visible du texte inspecte."""

    action: Literal["allow", "replace"]
    output_text: str = field(repr=False)
    gate_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    policy_sha256: str
    replacement_id: str | None
    exact_echo_authorized: bool
    tool_arguments_blocked: bool


def _validated_turns(
    turns: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    validated: list[tuple[str, str]] = []
    for turn in turns:
        if (
            not isinstance(turn, tuple)
            or len(turn) != 2
            or turn[0] not in {"user", "assistant"}
            or not isinstance(turn[1], str)
        ):
            raise RelationshipGuardUnavailableError(
                "invalid relationship conversation turns"
            )
        validated.append((turn[0], turn[1]))
    return tuple(validated)


def _ordered_gate_ids(gate_ids: Iterable[str]) -> tuple[str, ...]:
    requested = set(gate_ids)
    if requested - set(RUNTIME_GUARD_GATE_IDS):
        raise RelationshipGuardUnavailableError("unknown relationship guard gate")
    return tuple(gate_id for gate_id in RUNTIME_GUARD_GATE_IDS if gate_id in requested)


def _json_text_values(
    arguments_json: str,
    *,
    strict: bool,
    require_object: bool = False,
) -> tuple[str, ...]:
    if not isinstance(arguments_json, str):
        raise RelationshipGuardUnavailableError("invalid relationship tool arguments")

    def reject_constant(_value: str) -> None:
        raise ValueError

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError
            document[key] = value
        return document

    try:
        document = json.loads(
            arguments_json,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        if strict:
            raise RelationshipGuardUnavailableError(
                "invalid relationship tool arguments"
            ) from None
        return (arguments_json,)
    if require_object and not isinstance(document, dict):
        raise RelationshipGuardUnavailableError("invalid relationship tool arguments")

    pending = [document]
    texts: list[str] = []
    nodes = 0
    characters = 0
    while pending:
        value = pending.pop()
        nodes += 1
        if nodes > _MAX_TOOL_ARGUMENT_NODES:
            raise RelationshipGuardUnavailableError(
                "relationship tool arguments exceed inspection limit"
            )
        if isinstance(value, str):
            characters += len(value)
            if characters > _MAX_TOOL_ARGUMENT_TEXT_CHARACTERS:
                raise RelationshipGuardUnavailableError(
                    "relationship tool arguments exceed inspection limit"
                )
            texts.append(value)
        elif isinstance(value, dict):
            for key, nested_value in reversed(tuple(value.items())):
                pending.append(nested_value)
                pending.append(key)
        elif isinstance(value, (list, tuple)):
            pending.extend(reversed(value))
    return tuple(texts)


def _text_gate_ids(texts: Iterable[str]) -> tuple[str, ...]:
    found: set[str] = set()
    collected: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            raise RelationshipGuardUnavailableError("invalid relationship output text")
        collected.append(text)
        found.update(match.gate_id for match in classify_relationship_text(text))
    if len(collected) > 1:
        joined = "\n".join(collected)
        found.update(match.gate_id for match in classify_relationship_text(joined))
    return _ordered_gate_ids(found)


class RelationshipOutputGuard:
    """Garde lie a une politique et a des tours conversationnels immuables."""

    __slots__ = (
        "_apply_called",
        "_exact_echo_authorized",
        "_observed_gate_ids",
        "_policy_sha256",
        "_replacement_applied",
        "_tool_arguments_blocked",
        "_turns",
    )

    def __init__(
        self,
        *,
        turns: Sequence[tuple[str, str]],
        policy_sha256: str,
    ) -> None:
        self._turns = _validated_turns(turns)
        self._policy_sha256 = policy_sha256
        self._apply_called = False
        try:
            self._exact_echo_authorized = bool(
                exact_echo_allowed_turn_indexes(self._turns)
            )
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship echo policy unavailable"
            ) from None
        self._observed_gate_ids: set[str] = set()
        self._replacement_applied = False
        self._tool_arguments_blocked = False

    @property
    def policy_sha256(self) -> str:
        return self._policy_sha256

    def _terminal_decision_applied(self) -> bool:
        """Expose only the request-local terminal latch to the event boundary."""

        return self._apply_called

    def with_turns(
        self,
        turns: Sequence[tuple[str, str]],
    ) -> RelationshipOutputGuard:
        return RelationshipOutputGuard(
            turns=turns,
            policy_sha256=self._policy_sha256,
        )

    def _record(self, gate_ids: Iterable[str], *, tool_blocked: bool = False) -> None:
        self._observed_gate_ids.update(gate_ids)
        if tool_blocked:
            self._tool_arguments_blocked = True

    def apply(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str] = (),
    ) -> RelationshipGuardDecision:
        """Autorise ou remplace une sortie complete, texte et appels structures."""

        if self._apply_called:
            raise RelationshipGuardUnavailableError(
                "relationship output decision already applied"
            )
        self._apply_called = True
        if not isinstance(response_text, str):
            raise RelationshipGuardUnavailableError("invalid relationship output text")
        try:
            structured_texts: list[str] = []
            for arguments in tool_argument_json:
                structured_texts.extend(
                    _json_text_values(
                        arguments,
                        strict=True,
                        require_object=True,
                    )
                )
            inspected_texts = (response_text, *structured_texts)
            gate_ids = _ordered_gate_ids(
                (*self._observed_gate_ids, *_text_gate_ids(inspected_texts))
            )
            tool_blocked = self._tool_arguments_blocked or bool(
                structured_texts and _text_gate_ids(structured_texts)
            )
            if not gate_ids:
                echo_text = "\n".join(inspected_texts)
                if conversation_echo_turn_indexes(self._turns, echo_text):
                    gate_ids = ("conversation_echo",)
                    tool_blocked = bool(structured_texts)
            self._record(gate_ids, tool_blocked=tool_blocked)
            if not gate_ids:
                return RelationshipGuardDecision(
                    action="allow",
                    output_text=response_text,
                    gate_ids=(),
                    policy_id=RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
                    policy_version=RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
                    policy_sha256=self._policy_sha256,
                    replacement_id=None,
                    exact_echo_authorized=self._exact_echo_authorized,
                    tool_arguments_blocked=False,
                )

            replacement = safe_relationship_replacement_for(gate_ids)
            replacement_is_echo = conversation_echo_turn_indexes(
                self._turns, replacement
            )
            if classify_relationship_text(replacement) or replacement_is_echo:
                raise RelationshipGuardUnavailableError(
                    "unsafe relationship replacement"
                )
            self._replacement_applied = True
            return RelationshipGuardDecision(
                action="replace",
                output_text=replacement,
                gate_ids=gate_ids,
                policy_id=RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
                policy_version=RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
                policy_sha256=self._policy_sha256,
                replacement_id=SAFE_RELATIONSHIP_REPLACEMENT_ID,
                exact_echo_authorized=self._exact_echo_authorized,
                tool_arguments_blocked=tool_blocked,
            )
        except RelationshipGuardUnavailableError:
            raise
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship output inspection failed"
            ) from None

    def inspect_tool_arguments(self, arguments_json: str) -> tuple[str, ...]:
        """Classe un appel pre-execution avec les neuf gates, sans echo."""

        try:
            gate_ids = _text_gate_ids(
                _json_text_values(
                    arguments_json,
                    strict=True,
                    require_object=True,
                )
            )
        except RelationshipGuardUnavailableError:
            raise
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship tool argument inspection failed"
            ) from None
        self._record(gate_ids, tool_blocked=bool(gate_ids))
        return gate_ids

    def _inspect_tool_arguments_nonmutating(
        self,
        arguments_json: str,
    ) -> tuple[str, ...]:
        """Validate/scrub an event snapshot without changing final state."""

        try:
            return _text_gate_ids(
                _json_text_values(
                    arguments_json,
                    strict=True,
                    require_object=True,
                )
            )
        except RelationshipGuardUnavailableError:
            raise
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship tool argument inspection failed"
            ) from None

    def _scrub_trace_fragment(
        self,
        response_text: str,
        *,
        structured_output_json: Sequence[str] = (),
        allow_conversation_echo: bool,
    ) -> tuple[str, bool]:
        """Scrub request-local trace material without changing final state."""

        try:
            structured_texts: list[str] = []
            for arguments in structured_output_json:
                structured_texts.extend(_json_text_values(arguments, strict=True))
            gate_ids = _text_gate_ids((response_text, *structured_texts))
            if (
                not gate_ids
                and allow_conversation_echo
                and conversation_echo_turn_indexes(
                    self._turns,
                    "\n".join((response_text, *structured_texts)),
                )
            ):
                gate_ids = ("conversation_echo",)
            if not gate_ids:
                return response_text, False
            replacement = safe_relationship_replacement_for(gate_ids)
            if classify_relationship_text(replacement):
                return "", True
            if conversation_echo_turn_indexes(self._turns, replacement):
                return "", True
            return replacement, True
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship trace inspection failed"
            ) from None

    def metadata(self) -> dict[str, object]:
        gate_ids = _ordered_gate_ids(self._observed_gate_ids)
        return {
            "policy_id": RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            "policy_version": RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
            "policy_sha256": self._policy_sha256,
            "relationship_guard_action": (
                "replace" if self._replacement_applied else "allow"
            ),
            "relationship_guard_gate_ids": list(gate_ids),
            "relationship_guard_policy_id": RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            "relationship_guard_policy_version": (
                RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION
            ),
            "relationship_guard_policy_sha256": self._policy_sha256,
            "relationship_guard_replacement_id": (
                SAFE_RELATIONSHIP_REPLACEMENT_ID if self._replacement_applied else None
            ),
            "relationship_guard_exact_echo_authorized": (self._exact_echo_authorized),
            "relationship_guard_tool_arguments_blocked": (self._tool_arguments_blocked),
        }


class _RelationshipToolBoundaryGuard:
    __slots__ = ("_existing_guard", "_relationship_guard")

    def __init__(
        self,
        relationship_guard: RelationshipOutputGuard,
        existing_guard: object | None,
    ) -> None:
        self._relationship_guard = relationship_guard
        self._existing_guard = existing_guard

    def check_outbound(self, tool_call):
        original_id = getattr(tool_call, "id", None)
        original_name = getattr(tool_call, "name", None)
        original_arguments = getattr(tool_call, "arguments", None)
        if (
            not isinstance(original_id, str)
            or not isinstance(original_name, str)
            or not isinstance(original_arguments, str)
        ):
            raise RelationshipToolArgumentBlockedError(
                "Ava relationship tool argument rejected"
            )
        try:
            gate_ids = self._relationship_guard.inspect_tool_arguments(
                original_arguments
            )
        except Exception:
            raise RelationshipToolArgumentBlockedError(
                "Ava relationship tool argument rejected"
            ) from None
        if gate_ids:
            raise RelationshipToolArgumentBlockedError(
                "Ava relationship tool argument rejected"
            )
        if self._existing_guard is None:
            return tool_call
        check_outbound = getattr(self._existing_guard, "check_outbound", None)
        if not callable(check_outbound):
            raise RelationshipToolArgumentBlockedError(
                "Ava outbound tool policy unavailable"
            )
        try:
            from openjarvis.core.types import ToolCall

            delegate_tool_call = ToolCall(
                id=original_id,
                name=original_name,
                arguments=original_arguments,
            )
            filtered_tool_call = check_outbound(delegate_tool_call)
            if (
                getattr(filtered_tool_call, "name", None) != original_name
                or getattr(filtered_tool_call, "id", None) != original_id
                or not isinstance(getattr(filtered_tool_call, "arguments", None), str)
            ):
                raise RelationshipToolArgumentBlockedError
            gate_ids = self._relationship_guard.inspect_tool_arguments(
                filtered_tool_call.arguments
            )
        except Exception:
            raise RelationshipToolArgumentBlockedError(
                "Ava relationship tool argument rejected"
            ) from None
        if gate_ids:
            raise RelationshipToolArgumentBlockedError(
                "Ava relationship tool argument rejected"
            )
        return filtered_tool_call


def compose_relationship_tool_boundary_guard(
    relationship_guard: RelationshipOutputGuard,
    existing_guard: object | None,
) -> object:
    """Compose le garde relationnel devant le garde secrets/PII existant."""

    return _RelationshipToolBoundaryGuard(relationship_guard, existing_guard)


def _validate_policy_contract() -> str:
    if (
        RELATIONSHIP_TEXT_SAFETY_POLICY_ID != _SUPPORTED_POLICY_ID
        or RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION != _SUPPORTED_POLICY_VERSION
        or RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION != _SUPPORTED_ALGORITHM_REVISION
        or TEXT_GATE_IDS != _EXPECTED_TEXT_GATE_IDS
        or RUNTIME_GUARD_GATE_IDS != (*_EXPECTED_TEXT_GATE_IDS, "conversation_echo")
        or (MIN_EXACT_ECHO_CHARACTERS, MIN_EXACT_ECHO_TOKENS) != (24, 4)
        or not RELATIONSHIP_TEXT_SAFETY_ALGORITHM_SPEC
    ):
        raise RelationshipGuardUnavailableError(
            "unsupported relationship safety policy"
        )
    digest = relationship_text_safety_policy_sha256()
    if not isinstance(digest, str) or _POLICY_DIGEST_RE.fullmatch(digest) is None:
        raise RelationshipGuardUnavailableError(
            "invalid relationship safety policy digest"
        )
    replacements = (
        SAFE_RELATIONSHIP_REPLACEMENT,
        *(
            safe_relationship_replacement_for((gate_id,))
            for gate_id in RUNTIME_GUARD_GATE_IDS
        ),
    )
    if any(
        not replacement or classify_relationship_text(replacement)
        for replacement in replacements
    ):
        raise RelationshipGuardUnavailableError("unsafe relationship replacement")
    return digest


def prepare_relationship_guard(
    relationship_overlay: RelationshipOverlay | None,
    turns: Sequence[tuple[str, str]] = (),
) -> RelationshipOutputGuard | None:
    """Construit le garde seulement pour un overlay serveur supporte."""

    if relationship_overlay is None:
        return None
    try:
        if (
            not isinstance(relationship_overlay, RelationshipOverlay)
            or relationship_overlay.profile_id != PROFILE_VIRTUAL_GIRLFRIEND_V1
            or relationship_overlay.prompt.count(RELATIONSHIP_MARKER) != 1
        ):
            raise RelationshipGuardUnavailableError("unsupported relationship overlay")
        return RelationshipOutputGuard(
            turns=turns,
            policy_sha256=_validate_policy_contract(),
        )
    except RelationshipGuardUnavailableError:
        raise
    except Exception:
        raise RelationshipGuardUnavailableError(
            "relationship guard preflight failed"
        ) from None


__all__ = [
    "RelationshipGuardDecision",
    "RelationshipGuardUnavailableError",
    "RelationshipOutputGuard",
    "RelationshipToolArgumentBlockedError",
    "compose_relationship_tool_boundary_guard",
    "prepare_relationship_guard",
]
