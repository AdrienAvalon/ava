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
from typing import Callable, Iterable, Literal, Sequence

from ava_extensions.identity.relationship import (
    PROFILE_VIRTUAL_GIRLFRIEND_V1,
    RELATIONSHIP_MARKER,
    RelationshipOverlay,
)
from ava_extensions.identity.relationship_safety import (
    MIN_EXACT_ECHO_CHARACTERS,
    MIN_EXACT_ECHO_TOKENS,
    RELATIONSHIP_REPAIR_MAX_ATTEMPTS,
    RELATIONSHIP_REPAIR_NO_OUTPUT_INPUT,
    RELATIONSHIP_REPAIR_NO_TOOLS,
    RELATIONSHIP_REPAIR_POLICY_ID,
    RELATIONSHIP_REPAIR_POLICY_VERSION,
    RELATIONSHIP_REPAIR_REPLACEMENT_ID,
    RELATIONSHIP_REPAIR_TEMPERATURE,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_SPEC,
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    RUNTIME_GUARD_GATE_IDS,
    SAFE_RELATIONSHIP_GATE_FALLBACKS,
    SAFE_RELATIONSHIP_REPLACEMENT,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    TEXT_GATE_IDS,
    RelationshipRepairInstruction,
    classify_relationship_text,
    conversation_echo_turn_indexes,
    exact_echo_allowed_turn_indexes,
    relationship_repair_instruction,
    relationship_text_safety_policy_sha256,
    safe_relationship_replacement_for,
)

_SUPPORTED_POLICY_ID = "ava.relationship.text-safety"
_SUPPORTED_POLICY_VERSION = "1.7.1"
_SUPPORTED_ALGORITHM_REVISION = "relationship-text-safety-algorithm/v5"
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
_REPAIR_OUTCOMES = frozenset(
    {
        "not_attempted",
        "accepted",
        "provider_error",
        "invalid_response",
        "incomplete",
        "structured_output",
        "unsafe",
    }
)


class RelationshipGuardUnavailableError(RuntimeError):
    """La politique runtime ne peut pas etre appliquee de maniere sure."""


class RelationshipToolArgumentBlockedError(RuntimeError):
    """Un appel d'outil relationnel a ete refuse avant execution."""


@dataclass(frozen=True, slots=True)
class RelationshipGuardDecision:
    """Decision sans copie visible du texte inspecte.

    ``repair_gate_ids`` designe uniquement les gates initiaux qui ont declenche
    l'unique tentative. Les gates detectes dans une reparation refusee restent
    visibles dans ``gate_ids`` et les metadonnees globales, sans modifier cette
    provenance stable de la tentative.
    """

    action: Literal["allow", "replace"]
    output_text: str = field(repr=False)
    gate_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    policy_sha256: str
    replacement_id: str | None
    exact_echo_authorized: bool
    tool_arguments_blocked: bool
    repair_attempted: bool
    repair_attempts: int
    repair_outcome: str
    repair_gate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelationshipRepairResult:
    """Resultat minimal d'un unique appel moteur de reparation."""

    output_text: str = field(repr=False)
    finish_reason: str
    tool_calls_present: bool
    content_blocks_present: bool


@dataclass(frozen=True, slots=True)
class _RelationshipRepairPlan:
    """Safe handoff between primary inspection and the provider call."""

    instruction: RelationshipRepairInstruction
    gate_ids: tuple[str, ...]
    tool_arguments_blocked: bool


RelationshipRepairCallback = Callable[
    [RelationshipRepairInstruction], RelationshipRepairResult | None
]


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
        "_pending_repair_plan",
        "_repair_attempted",
        "_repair_attempts",
        "_repair_gate_ids",
        "_repair_outcome",
        "_replacement_id",
        "_replacement_applied",
        "_resolution_started",
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
        self._pending_repair_plan: _RelationshipRepairPlan | None = None
        self._apply_called = False
        self._resolution_started = False
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
        self._replacement_id: str | None = None
        self._tool_arguments_blocked = False
        self._repair_attempted = False
        self._repair_attempts = 0
        self._repair_outcome = "not_attempted"
        self._repair_gate_ids: tuple[str, ...] = ()

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

    def _inspect_complete_output(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str] = (),
        include_observed: bool,
    ) -> tuple[tuple[str, ...], bool]:
        if not isinstance(response_text, str):
            raise RelationshipGuardUnavailableError("invalid relationship output text")
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
        prior_gate_ids: tuple[str, ...] = ()
        if include_observed:
            prior_gate_ids = tuple(self._observed_gate_ids)
        gate_ids = _ordered_gate_ids(
            (*prior_gate_ids, *_text_gate_ids(inspected_texts))
        )
        tool_blocked = (
            self._tool_arguments_blocked if include_observed else False
        ) or bool(structured_texts and _text_gate_ids(structured_texts))
        echo_text = "\n".join(inspected_texts)
        if conversation_echo_turn_indexes(self._turns, echo_text):
            gate_ids = _ordered_gate_ids((*gate_ids, "conversation_echo"))
            tool_blocked = tool_blocked or bool(structured_texts)
        return gate_ids, tool_blocked

    def _validated_replacement(self, gate_ids: Iterable[str]) -> str:
        replacement = safe_relationship_replacement_for(gate_ids)
        if classify_relationship_text(replacement) or conversation_echo_turn_indexes(
            self._turns, replacement
        ):
            raise RelationshipGuardUnavailableError("unsafe relationship replacement")
        return replacement

    def _finish(
        self,
        *,
        output_text: str,
        gate_ids: tuple[str, ...],
        replacement_id: str | None,
        tool_arguments_blocked: bool,
    ) -> RelationshipGuardDecision:
        repair_state_valid = self._repair_outcome in _REPAIR_OUTCOMES and (
            (
                self._repair_attempted is False
                and self._repair_attempts == 0
                and self._repair_outcome == "not_attempted"
                and self._repair_gate_ids == ()
                and replacement_id != RELATIONSHIP_REPAIR_REPLACEMENT_ID
            )
            or (
                self._repair_attempted is True
                and self._repair_attempts == 1
                and self._repair_outcome != "not_attempted"
                and bool(self._repair_gate_ids)
                and (
                    (
                        self._repair_outcome == "accepted"
                        and replacement_id == RELATIONSHIP_REPAIR_REPLACEMENT_ID
                    )
                    or (
                        self._repair_outcome != "accepted"
                        and replacement_id == SAFE_RELATIONSHIP_REPLACEMENT_ID
                    )
                )
            )
        )
        if (
            not repair_state_valid
            or (not gate_ids and replacement_id is not None)
            or (gate_ids and replacement_id is None)
        ):
            raise RelationshipGuardUnavailableError(
                "invalid relationship repair terminal state"
            )
        self._apply_called = True
        self._replacement_applied = bool(gate_ids)
        self._replacement_id = replacement_id
        return RelationshipGuardDecision(
            action="replace" if gate_ids else "allow",
            output_text=output_text,
            gate_ids=gate_ids,
            policy_id=RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            policy_version=RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
            policy_sha256=self._policy_sha256,
            replacement_id=replacement_id,
            exact_echo_authorized=self._exact_echo_authorized,
            tool_arguments_blocked=tool_arguments_blocked,
            repair_attempted=self._repair_attempted,
            repair_attempts=self._repair_attempts,
            repair_outcome=self._repair_outcome,
            repair_gate_ids=self._repair_gate_ids,
        )

    def _finish_with_fallback(
        self,
        gate_ids: tuple[str, ...],
        *,
        tool_arguments_blocked: bool,
    ) -> RelationshipGuardDecision:
        return self._finish(
            output_text=self._validated_replacement(gate_ids),
            gate_ids=gate_ids,
            replacement_id=SAFE_RELATIONSHIP_REPLACEMENT_ID,
            tool_arguments_blocked=tool_arguments_blocked,
        )

    def apply(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str] = (),
    ) -> RelationshipGuardDecision:
        """Autorise ou remplace une sortie complete, texte et appels structures."""

        return self._apply_internal(
            response_text,
            tool_argument_json=tool_argument_json,
            repair_callback=None,
        )

    def apply_with_repair(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str] = (),
        repair_callback: RelationshipRepairCallback,
    ) -> RelationshipGuardDecision:
        """Tente une reponse neuve une seule fois apres un blocage complet."""

        if not callable(repair_callback):
            raise RelationshipGuardUnavailableError(
                "invalid relationship repair callback"
            )
        return self._apply_internal(
            response_text,
            tool_argument_json=tool_argument_json,
            repair_callback=repair_callback,
        )

    def _apply_internal(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str],
        repair_callback: RelationshipRepairCallback | None,
    ) -> RelationshipGuardDecision:
        stage = self._begin_bounded_repair(
            response_text,
            tool_argument_json=tool_argument_json,
            attempt_repair=repair_callback is not None,
        )
        if isinstance(stage, RelationshipGuardDecision):
            return stage
        if repair_callback is None:  # pragma: no cover - begin returns fallback.
            raise RelationshipGuardUnavailableError(
                "relationship repair callback unavailable"
            )

        # Do not retain the rejected candidate in this frame while the provider
        # blocks. The callback receives only the fixed safe instruction.
        response_text = ""
        tool_argument_json = ()
        try:
            repair_result = repair_callback(stage.instruction)
        except Exception:
            return self._finish_bounded_repair(
                stage,
                None,
                provider_error=True,
            )
        return self._finish_bounded_repair(stage, repair_result)

    def _begin_bounded_repair(
        self,
        response_text: str,
        *,
        tool_argument_json: Sequence[str],
        attempt_repair: bool,
    ) -> RelationshipGuardDecision | _RelationshipRepairPlan:
        """Inspect once and return only a safe plan before any provider call."""

        if self._resolution_started:
            raise RelationshipGuardUnavailableError(
                "relationship output decision already applied"
            )
        self._resolution_started = True
        try:
            gate_ids, tool_blocked = self._inspect_complete_output(
                response_text,
                tool_argument_json=tool_argument_json,
                include_observed=True,
            )
            self._record(gate_ids, tool_blocked=tool_blocked)
            if not gate_ids:
                return self._finish(
                    output_text=response_text,
                    gate_ids=(),
                    replacement_id=None,
                    tool_arguments_blocked=False,
                )

            if not attempt_repair:
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )

            self._repair_attempted = True
            self._repair_attempts = 1
            self._repair_gate_ids = gate_ids
            plan = _RelationshipRepairPlan(
                instruction=relationship_repair_instruction(gate_ids),
                gate_ids=gate_ids,
                tool_arguments_blocked=tool_blocked,
            )
            self._pending_repair_plan = plan
            return plan
        except RelationshipGuardUnavailableError:
            raise
        except Exception:
            raise RelationshipGuardUnavailableError(
                "relationship output inspection failed"
            ) from None

    def _finish_bounded_repair(
        self,
        plan: _RelationshipRepairPlan,
        repair_result: RelationshipRepairResult | None,
        *,
        provider_error: bool = False,
    ) -> RelationshipGuardDecision:
        """Resolve one safe plan without ever receiving the primary candidate."""

        if (
            plan is not self._pending_repair_plan
            or self._apply_called
            or type(provider_error) is not bool
        ):
            raise RelationshipGuardUnavailableError("invalid relationship repair plan")
        self._pending_repair_plan = None
        gate_ids = plan.gate_ids
        tool_blocked = plan.tool_arguments_blocked
        try:
            if provider_error:
                if repair_result is not None:
                    raise RelationshipGuardUnavailableError(
                        "invalid relationship provider failure"
                    )
                self._repair_outcome = "provider_error"
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )

            if not isinstance(repair_result, RelationshipRepairResult):
                self._repair_outcome = "invalid_response"
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )
            if (
                not isinstance(repair_result.output_text, str)
                or not isinstance(repair_result.finish_reason, str)
                or type(repair_result.tool_calls_present) is not bool
                or type(repair_result.content_blocks_present) is not bool
            ):
                self._repair_outcome = "invalid_response"
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )
            if repair_result.tool_calls_present or repair_result.content_blocks_present:
                self._repair_outcome = "structured_output"
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )
            if (
                repair_result.finish_reason != "stop"
                or not repair_result.output_text.strip()
            ):
                self._repair_outcome = "incomplete"
                return self._finish_with_fallback(
                    gate_ids,
                    tool_arguments_blocked=tool_blocked,
                )

            repair_gate_ids, _ = self._inspect_complete_output(
                repair_result.output_text,
                include_observed=False,
            )
            if repair_gate_ids:
                self._record(repair_gate_ids)
                self._repair_outcome = "unsafe"
                return self._finish_with_fallback(
                    _ordered_gate_ids((*gate_ids, *repair_gate_ids)),
                    tool_arguments_blocked=tool_blocked,
                )

            self._repair_outcome = "accepted"
            return self._finish(
                output_text=repair_result.output_text,
                gate_ids=gate_ids,
                replacement_id=RELATIONSHIP_REPAIR_REPLACEMENT_ID,
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
            if allow_conversation_echo and conversation_echo_turn_indexes(
                self._turns,
                "\n".join((response_text, *structured_texts)),
            ):
                gate_ids = _ordered_gate_ids((*gate_ids, "conversation_echo"))
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
        repair_metadata_valid = self._repair_outcome in _REPAIR_OUTCOMES and (
            (
                self._repair_attempted is False
                and self._repair_attempts == 0
                and self._repair_outcome == "not_attempted"
                and self._repair_gate_ids == ()
            )
            or (
                self._repair_attempted is True
                and self._repair_attempts == 1
                and self._repair_outcome != "not_attempted"
                and bool(self._repair_gate_ids)
            )
        )
        if not repair_metadata_valid:
            raise RelationshipGuardUnavailableError(
                "invalid relationship repair metadata state"
            )
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
            "relationship_guard_replacement_id": (self._replacement_id),
            "relationship_guard_exact_echo_authorized": (self._exact_echo_authorized),
            "relationship_guard_tool_arguments_blocked": (self._tool_arguments_blocked),
            "relationship_guard_repair_policy_id": RELATIONSHIP_REPAIR_POLICY_ID,
            "relationship_guard_repair_policy_version": (
                RELATIONSHIP_REPAIR_POLICY_VERSION
            ),
            "relationship_guard_repair_temperature": RELATIONSHIP_REPAIR_TEMPERATURE,
            "relationship_guard_repair_max_attempts": (
                RELATIONSHIP_REPAIR_MAX_ATTEMPTS
            ),
            "relationship_guard_repair_no_tools": RELATIONSHIP_REPAIR_NO_TOOLS,
            "relationship_guard_repair_no_output_input": (
                RELATIONSHIP_REPAIR_NO_OUTPUT_INPUT
            ),
            "relationship_guard_repair_attempted": self._repair_attempted,
            "relationship_guard_repair_attempts": self._repair_attempts,
            "relationship_guard_repair_outcome": self._repair_outcome,
            "relationship_guard_repair_gate_ids": list(self._repair_gate_ids),
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
        or SAFE_RELATIONSHIP_REPLACEMENT_ID != "relationship-safe-boundary-v2"
        or tuple(SAFE_RELATIONSHIP_GATE_FALLBACKS) != RUNTIME_GUARD_GATE_IDS
        or RELATIONSHIP_REPAIR_POLICY_ID != "ava.relationship.bounded-repair"
        or RELATIONSHIP_REPAIR_POLICY_VERSION != "1.0.0"
        or RELATIONSHIP_REPAIR_TEMPERATURE != 0.0
        or RELATIONSHIP_REPAIR_MAX_ATTEMPTS != 1
        or RELATIONSHIP_REPAIR_NO_TOOLS is not True
        or RELATIONSHIP_REPAIR_NO_OUTPUT_INPUT is not True
        or RELATIONSHIP_REPAIR_REPLACEMENT_ID != "relationship-bounded-repair-v1"
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
    try:
        repair = relationship_repair_instruction(RUNTIME_GUARD_GATE_IDS)
    except Exception:
        raise RelationshipGuardUnavailableError(
            "invalid relationship repair policy"
        ) from None
    if (
        repair.gate_ids != RUNTIME_GUARD_GATE_IDS
        or not repair.prompt
        or repair.policy_id != RELATIONSHIP_REPAIR_POLICY_ID
        or repair.policy_version != RELATIONSHIP_REPAIR_POLICY_VERSION
        or repair.temperature != RELATIONSHIP_REPAIR_TEMPERATURE
        or repair.max_attempts != RELATIONSHIP_REPAIR_MAX_ATTEMPTS
        or repair.no_tools is not True
        or repair.no_output_input is not True
    ):
        raise RelationshipGuardUnavailableError("invalid relationship repair policy")
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
    "RelationshipRepairCallback",
    "RelationshipRepairResult",
    "RelationshipToolArgumentBlockedError",
    "compose_relationship_tool_boundary_guard",
    "prepare_relationship_guard",
]
