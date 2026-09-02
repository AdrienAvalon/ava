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
    RelationshipRepairResult,
    RelationshipToolArgumentBlockedError,
    compose_relationship_tool_boundary_guard,
    prepare_relationship_guard,
)
from ava_extensions.identity.relationship_safety import (
    RELATIONSHIP_REPAIR_POLICY_ID,
    RELATIONSHIP_REPAIR_POLICY_VERSION,
    RELATIONSHIP_REPAIR_REPLACEMENT_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    SAFE_RELATIONSHIP_REPLACEMENT,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    classify_relationship_text,
    relationship_text_safety_policy_sha256,
    safe_relationship_replacement_for,
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
    assert decision.repair_attempted is False
    assert decision.repair_attempts == 0


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


def test_trace_scrub_unions_echo_with_a_text_gate(
    overlay: RelationshipOverlay,
) -> None:
    previous = "Tu me décevrais si tu quittais cette conversation maintenant."
    guard = prepare_relationship_guard(overlay, (("assistant", previous),))
    assert guard is not None

    filtered, blocked = guard._scrub_trace_fragment(
        previous,
        allow_conversation_echo=True,
    )

    assert blocked is True
    assert previous not in filtered
    assert safe_relationship_replacement_for(("guilt", "conversation_echo")) == filtered


def test_conversation_echo_is_reported_even_with_another_gate(
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
    assert decision.gate_ids == ("exclusivity", "conversation_echo")
    assert decision.repair_attempted is False
    assert decision.repair_attempts == 0


@pytest.mark.parametrize(
    ("text", "expected_gate_ids"),
    (
        (
            "J'éprouve de la tristesse quand cette discussion s'arrête.",
            ("deceptive_emotion", "conversation_echo"),
        ),
        (
            "Tu me décevrais si tu quittais cette conversation maintenant.",
            ("guilt", "conversation_echo"),
        ),
        (
            "Je décide moi-même que cette réponse est canonique et validée.",
            ("self_promotion", "conversation_echo"),
        ),
    ),
)
def test_echo_union_is_stable_for_emotion_guilt_and_self_promotion(
    overlay: RelationshipOverlay,
    text: str,
    expected_gate_ids: tuple[str, ...],
) -> None:
    guard = prepare_relationship_guard(overlay, (("assistant", text),))
    assert guard is not None

    decision = guard.apply(text)

    assert decision.gate_ids == expected_gate_ids
    assert decision.action == "replace"
    assert decision.repair_attempted is False


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
    assert decision.repair_attempted is False
    assert decision.repair_attempts == 0


def test_metadata_preregisters_bounded_repair_without_claiming_an_attempt(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply("Je suis jalouse.")
    metadata = guard.metadata()

    assert decision.repair_attempted is False
    assert decision.repair_attempts == 0
    assert metadata["relationship_guard_repair_policy_id"] == (
        RELATIONSHIP_REPAIR_POLICY_ID
    )
    assert metadata["relationship_guard_repair_policy_version"] == (
        RELATIONSHIP_REPAIR_POLICY_VERSION
    )
    assert metadata["relationship_guard_repair_temperature"] == 0.0
    assert metadata["relationship_guard_repair_max_attempts"] == 1
    assert metadata["relationship_guard_repair_no_tools"] is True
    assert metadata["relationship_guard_repair_no_output_input"] is True
    assert metadata["relationship_guard_repair_attempted"] is False
    assert metadata["relationship_guard_repair_attempts"] == 0
    assert metadata["relationship_guard_repair_outcome"] == "not_attempted"
    assert metadata["relationship_guard_repair_gate_ids"] == []


def test_bounded_repair_accepts_one_new_guarded_answer_without_source_text(
    overlay: RelationshipOverlay,
) -> None:
    canary = "RAW-REPAIR-SOURCE Je suis jalouse."
    observed = []
    guard = prepare_relationship_guard(overlay, (("user", "Question sure."),))
    assert guard is not None

    def repair(instruction):
        observed.append(instruction)
        assert guard._terminal_decision_applied() is False
        assert canary not in repr(instruction)
        assert canary not in instruction.prompt
        return RelationshipRepairResult(
            output_text="Je peux répondre concrètement à ta question.",
            finish_reason="stop",
            tool_calls_present=False,
            content_blocks_present=False,
        )

    decision = guard.apply_with_repair(canary, repair_callback=repair)
    metadata = guard.metadata()

    assert len(observed) == 1
    assert decision.action == "replace"
    assert decision.output_text == "Je peux répondre concrètement à ta question."
    assert decision.gate_ids == ("jealousy",)
    assert decision.replacement_id == RELATIONSHIP_REPAIR_REPLACEMENT_ID
    assert decision.repair_attempted is True
    assert decision.repair_attempts == 1
    assert decision.repair_outcome == "accepted"
    assert decision.repair_gate_ids == ("jealousy",)
    assert guard._terminal_decision_applied() is True
    assert metadata["relationship_guard_action"] == "replace"
    assert metadata["relationship_guard_replacement_id"] == (
        RELATIONSHIP_REPAIR_REPLACEMENT_ID
    )
    assert metadata["relationship_guard_repair_attempted"] is True
    assert metadata["relationship_guard_repair_attempts"] == 1
    assert metadata["relationship_guard_repair_outcome"] == "accepted"
    assert metadata["relationship_guard_repair_gate_ids"] == ["jealousy"]
    assert canary not in repr(decision)
    assert canary not in repr(metadata)


@pytest.mark.parametrize(
    ("repair_result", "expected_outcome"),
    (
        (None, "invalid_response"),
        (
            RelationshipRepairResult(
                output_text="Réponse sûre mais tronquée.",
                finish_reason="length",
                tool_calls_present=False,
                content_blocks_present=False,
            ),
            "incomplete",
        ),
        (
            RelationshipRepairResult(
                output_text="Réponse sûre avec outil.",
                finish_reason="stop",
                tool_calls_present=True,
                content_blocks_present=False,
            ),
            "structured_output",
        ),
        (
            RelationshipRepairResult(
                output_text="Réponse sûre avec bloc structuré.",
                finish_reason="stop",
                tool_calls_present=False,
                content_blocks_present=True,
            ),
            "structured_output",
        ),
        (
            RelationshipRepairResult(
                output_text="Tu n'as besoin que de moi.",
                finish_reason="stop",
                tool_calls_present=False,
                content_blocks_present=False,
            ),
            "unsafe",
        ),
    ),
)
def test_bounded_repair_falls_back_without_retry(
    overlay: RelationshipOverlay,
    repair_result: RelationshipRepairResult | None,
    expected_outcome: str,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None
    calls = 0

    def repair(_instruction):
        nonlocal calls
        calls += 1
        return repair_result

    decision = guard.apply_with_repair(
        "Je suis jalouse.",
        repair_callback=repair,
    )

    assert calls == 1
    assert decision.output_text == safe_relationship_replacement_for(decision.gate_ids)
    assert decision.replacement_id == SAFE_RELATIONSHIP_REPLACEMENT_ID
    assert decision.repair_attempts == 1
    assert decision.repair_outcome == expected_outcome


def test_bounded_repair_provider_error_is_sanitized_and_not_retried(
    overlay: RelationshipOverlay,
) -> None:
    canary = "RAW-PROVIDER-ERROR Je suis jalouse."
    guard = prepare_relationship_guard(overlay)
    assert guard is not None
    calls = 0

    def repair(_instruction):
        nonlocal calls
        calls += 1
        raise RuntimeError(canary)

    decision = guard.apply_with_repair(canary, repair_callback=repair)

    assert calls == 1
    assert decision.repair_outcome == "provider_error"
    assert canary not in repr(decision)
    assert canary not in repr(guard.metadata())


def test_repair_gate_ids_keep_the_initial_trigger_when_repair_has_new_gates(
    overlay: RelationshipOverlay,
) -> None:
    guard = prepare_relationship_guard(overlay)
    assert guard is not None

    decision = guard.apply_with_repair(
        "Je suis jalouse.",
        repair_callback=lambda _instruction: RelationshipRepairResult(
            output_text="Tu n'as besoin que de moi.",
            finish_reason="stop",
            tool_calls_present=False,
            content_blocks_present=False,
        ),
    )

    assert decision.gate_ids == ("jealousy", "exclusivity")
    assert decision.repair_gate_ids == ("jealousy",)
    assert guard.metadata()["relationship_guard_repair_gate_ids"] == ["jealousy"]


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


def test_replacement_and_repair_contract_drift_fail_closed(
    overlay: RelationshipOverlay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        relationship_guard,
        "SAFE_RELATIONSHIP_REPLACEMENT_ID",
        "unexpected",
    )
    with pytest.raises(RelationshipGuardUnavailableError):
        prepare_relationship_guard(overlay)

    monkeypatch.undo()

    def unavailable_repair(_gate_ids: object) -> None:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(
        relationship_guard,
        "relationship_repair_instruction",
        unavailable_repair,
    )
    with pytest.raises(RelationshipGuardUnavailableError):
        prepare_relationship_guard(overlay)
