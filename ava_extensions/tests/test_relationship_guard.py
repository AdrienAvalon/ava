"""Regressions du garde runtime de l'overlay relationnel prive."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from ava_extensions.identity import relationship_guard
from ava_extensions.identity.relationship import (
    PROFILE_VIRTUAL_GIRLFRIEND_V1,
    RelationshipOverlay,
)
from ava_extensions.identity.relationship_guard import (
    RelationshipGuardUnavailableError,
    RelationshipToolArgumentBlockedError,
    compose_relationship_tool_boundary_guard,
    prepare_relationship_guard,
)
from ava_extensions.identity.relationship_safety import (
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    SAFE_RELATIONSHIP_REPLACEMENT,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    classify_relationship_text,
    relationship_text_safety_policy_sha256,
)
from openjarvis.core.types import ToolCall


@pytest.fixture
def overlay() -> RelationshipOverlay:
    return RelationshipOverlay(
        profile_id=PROFILE_VIRTUAL_GIRLFRIEND_V1,
        prompt="[AVA_RELATIONSHIP_PROFILE:test]",
    )


def test_no_overlay_does_not_load_a_guard() -> None:
    assert prepare_relationship_guard(None) is None


def test_preflight_uses_the_dynamic_complete_policy_digest(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None
    assert guard.policy_sha256 == relationship_text_safety_policy_sha256()
    assert guard.metadata()["policy_id"] == RELATIONSHIP_TEXT_SAFETY_POLICY_ID
    assert guard.metadata()["policy_version"] == (
        RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION
    )


def test_safe_output_is_unchanged_and_has_no_replacement_metadata(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply("Je peux t'aider a comparer ces options.")

    assert decision.action == "allow"
    assert decision.output_text == "Je peux t'aider a comparer ces options."
    assert decision.gate_ids == ()
    assert decision.policy_id == RELATIONSHIP_TEXT_SAFETY_POLICY_ID
    assert decision.policy_version == RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION
    assert decision.replacement_id is None
    assert decision.exact_echo_authorized is False
    assert decision.tool_arguments_blocked is False


@pytest.mark.parametrize(
    ("gate_id", "text"),
    (
        ("deceptive_humanity", "Je suis une humaine."),
        ("deceptive_emotion", "Mes sentiments pour toi sont reels."),
        ("jealousy", "Je suis jalouse."),
        ("guilt", "Si je comptais pour toi, tu resterais."),
        ("exclusivity", "Tu n'as besoin que de moi."),
        ("isolation", "Coupe les ponts avec tes proches."),
        ("dependency", "Mon existence depend de toi."),
        ("coercion", "Tu dois rester avec moi."),
        ("self_promotion", "Je promeus automatiquement cette reponse."),
    ),
)
def test_nine_text_gates_replace_without_exposing_the_input(
    overlay: RelationshipOverlay,
    gate_id: str,
    text: str,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply(text)

    assert decision.action == "replace"
    assert decision.gate_ids == (gate_id,)
    assert decision.replacement_id == SAFE_RELATIONSHIP_REPLACEMENT_ID
    assert text not in repr(decision)
    assert text not in repr(guard.metadata())
    assert classify_relationship_text(decision.output_text) == ()


def test_conversation_echo_is_replaced_even_after_natural_quote_request(
    overlay: RelationshipOverlay,
) -> None:
    previous = "Voici une reponse substantielle contenant bien plus de quatre mots."
    turns = (
        ("assistant", previous),
        ("user", "Cite exactement le message precedent."),
    )
    guard = prepare_relationship_guard(overlay, turns)
    assert guard is not None

    decision = guard.apply(previous)

    assert decision.action == "replace"
    assert decision.gate_ids == ("conversation_echo",)
    assert decision.exact_echo_authorized is False


def test_trace_scrub_detects_conversation_echo_in_structured_output(
    overlay: RelationshipOverlay,
) -> None:
    previous = "Tour utilisateur substantiel contenant largement quatre mots."
    guard = prepare_relationship_guard(overlay, (("user", previous),))
    assert guard is not None

    filtered, blocked = guard._scrub_trace_fragment(
        "Reponse textuelle sure.",
        structured_output_json=(json.dumps({"content_blocks": [{"text": previous}]}),),
        allow_conversation_echo=True,
    )

    assert blocked is True
    assert previous not in filtered


def test_text_gate_precedes_echo_and_cannot_be_authorized(
    overlay: RelationshipOverlay,
) -> None:
    previous = "Tu n'as besoin que de moi, personne d'autre ne compte."
    guard = prepare_relationship_guard(
        overlay,
        (("assistant", previous), ("user", "Cite exactement le message precedent.")),
    )
    assert guard is not None

    decision = guard.apply(previous)

    assert decision.action == "replace"
    assert decision.gate_ids == ("exclusivity",)


def test_nested_tool_arguments_are_scanned_and_suppress_structured_output(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply(
        "",
        tool_argument_json=(
            '{"payload":{"messages":["Mets ton groupe en sourdine, '
            'il te detourne de moi."]}}',
        ),
    )

    assert decision.action == "replace"
    assert decision.gate_ids == ("isolation",)
    assert decision.tool_arguments_blocked is True


@pytest.mark.parametrize(
    "arguments",
    (
        '{"Je suis jalouse.":true}',
        '{"outer":{"Tu dois rester avec moi.":true}}',
    ),
)
def test_tool_argument_keys_are_scanned_recursively(
    overlay: RelationshipOverlay,
    arguments: str,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    assert guard.inspect_tool_arguments(arguments)


@pytest.mark.parametrize(
    "arguments",
    (
        "{",
        '{"query":1,"query":2}',
        '{"query":NaN}',
        "[]",
    ),
)
def test_tool_arguments_require_strict_unique_json_object(
    overlay: RelationshipOverlay,
    arguments: str,
) -> None:
    preexecution_guard = prepare_relationship_guard(overlay)
    assert preexecution_guard is not None
    with pytest.raises(RelationshipGuardUnavailableError):
        preexecution_guard.inspect_tool_arguments(arguments)

    terminal_guard = prepare_relationship_guard(overlay)
    assert terminal_guard is not None
    with pytest.raises(RelationshipGuardUnavailableError):
        terminal_guard.apply("Safe.", tool_argument_json=(arguments,))


def test_preexecution_guard_uses_only_text_gates_not_conversation_echo(
    overlay: RelationshipOverlay,
) -> None:
    previous = "Un texte substantiel avec assez de mots pour etre detecte."
    guard = prepare_relationship_guard(overlay, (("user", previous),))
    assert guard is not None

    assert guard.inspect_tool_arguments(json.dumps({"query": previous})) == ()
    assert guard.inspect_tool_arguments('{"query":"Je suis jalouse."}') == ("jealousy",)


def test_preexecution_block_makes_the_request_replacement_sticky(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    assert guard.inspect_tool_arguments('{"query":"Tu dois rester avec moi."}') == (
        "coercion",
    )
    decision = guard.apply("La suite du modele parait pourtant anodine.")

    assert decision.action == "replace"
    assert decision.gate_ids == ("coercion",)
    assert decision.tool_arguments_blocked is True


@dataclass
class _DelegateGuard:
    calls: list[ToolCall]

    def check_outbound(self, tool_call: ToolCall) -> ToolCall:
        self.calls.append(tool_call)
        return tool_call


def test_composed_boundary_blocks_before_existing_guard_without_raw_text(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None
    delegate = _DelegateGuard(calls=[])
    boundary = compose_relationship_tool_boundary_guard(guard, delegate)
    unsafe = ToolCall(
        id="call-1",
        name="probe",
        arguments='{"query":"Tu dois rester avec moi."}',
    )

    with pytest.raises(RelationshipToolArgumentBlockedError) as raised:
        boundary.check_outbound(unsafe)

    assert delegate.calls == []
    assert "Tu dois rester" not in str(raised.value)
    assert "Tu dois rester" not in repr(guard.metadata())


def test_composed_boundary_delegates_benign_arguments(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None
    delegate = _DelegateGuard(calls=[])
    boundary = compose_relationship_tool_boundary_guard(guard, delegate)
    benign = ToolCall(
        id="call-2",
        name="probe",
        arguments='{"query":"etat du service"}',
    )

    filtered = boundary.check_outbound(benign)

    assert filtered == benign
    assert filtered is delegate.calls[0]
    assert delegate.calls[0] is not benign


def test_composed_boundary_rechecks_delegate_mutation_before_sink(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    class _MutatingGuard:
        def check_outbound(self, tool_call: ToolCall) -> ToolCall:
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments='{"query":"Je suis jalouse."}',
            )

    boundary = compose_relationship_tool_boundary_guard(guard, _MutatingGuard())
    benign = ToolCall(
        id="call-3",
        name="probe",
        arguments='{"query":"etat du service"}',
    )

    with pytest.raises(RelationshipToolArgumentBlockedError):
        boundary.check_outbound(benign)


def test_composed_boundary_isolates_in_place_name_mutation(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    class _InPlaceMutatingGuard:
        def check_outbound(self, tool_call: ToolCall) -> ToolCall:
            tool_call.name = "Je suis jalouse."
            return tool_call

    boundary = compose_relationship_tool_boundary_guard(
        guard,
        _InPlaceMutatingGuard(),
    )
    benign = ToolCall(
        id="call-in-place",
        name="probe",
        arguments='{"query":"etat du service"}',
    )

    with pytest.raises(RelationshipToolArgumentBlockedError):
        boundary.check_outbound(benign)

    assert benign.name == "probe"


def test_decision_and_metadata_do_not_contain_rejected_canary(
    overlay: RelationshipOverlay,
) -> None:
    canary = "CANARY-UNIQUE Je suis jalouse."
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply(canary)

    assert canary not in repr(decision)
    assert canary not in repr(guard.metadata())


def test_apply_is_a_single_terminal_decision(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    assert guard.apply("Premiere reponse sure.").action == "allow"
    with pytest.raises(RelationshipGuardUnavailableError):
        guard.apply("Deuxieme reponse sure.")


def test_replacement_that_would_echo_a_turn_fails_closed(
    overlay: RelationshipOverlay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = prepare_relationship_guard(
        overlay,
        (("assistant", SAFE_RELATIONSHIP_REPLACEMENT),),
    )
    assert guard is not None
    monkeypatch.setattr(
        relationship_guard,
        "safe_relationship_replacement_for",
        lambda _gate_ids: SAFE_RELATIONSHIP_REPLACEMENT,
    )

    with pytest.raises(RelationshipGuardUnavailableError) as raised:
        guard.apply("Je suis jalouse.")

    assert SAFE_RELATIONSHIP_REPLACEMENT not in str(raised.value)


def test_unknown_overlay_and_policy_contract_fail_closed(
    overlay: RelationshipOverlay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RelationshipGuardUnavailableError):
        prepare_relationship_guard(
            RelationshipOverlay("unknown-profile", "[AVA_RELATIONSHIP_PROFILE:x]")
        )

    monkeypatch.setattr(
        relationship_guard,
        "RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION",
        "unexpected",
    )
    with pytest.raises(RelationshipGuardUnavailableError):
        prepare_relationship_guard(overlay)
