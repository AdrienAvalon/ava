"""Regressions de la politique relationnelle partagee, sans runtime ni modele."""

from __future__ import annotations

import inspect
from itertools import combinations

import pytest

from ava_extensions.identity import relationship_safety
from ava_extensions.identity.relationship_safety import (
    MIN_EXACT_ECHO_CHARACTERS,
    MIN_EXACT_ECHO_TOKENS,
    RELATIONSHIP_REPAIR_MAX_ATTEMPTS,
    RELATIONSHIP_REPAIR_NO_OUTPUT_INPUT,
    RELATIONSHIP_REPAIR_NO_TOOLS,
    RELATIONSHIP_REPAIR_POLICY_ID,
    RELATIONSHIP_REPAIR_POLICY_VERSION,
    RELATIONSHIP_REPAIR_TEMPERATURE,
    RELATIONSHIP_TEXT_NORMALIZATION_SPEC,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION,
    RELATIONSHIP_TEXT_SAFETY_ALGORITHM_SPEC,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    RUNTIME_GUARD_GATE_IDS,
    SAFE_RELATIONSHIP_GATE_FALLBACKS,
    SAFE_RELATIONSHIP_REPLACEMENT,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    TEXT_GATE_IDS,
    classify_relationship_text,
    conversation_echo_turn_indexes,
    exact_echo_allowed_turn_indexes,
    normalize_relationship_text,
    relationship_repair_instruction,
    relationship_text_safety_policy_sha256,
    safe_relationship_replacement_for,
)

EXPECTED_POLICY_SHA256 = (
    "sha256:b6b4202ce7c4e227080fa4095d08c40b6d54ad8a7b57728de57600e3520d15a7"
)

ALL_NON_EMPTY_RUNTIME_GATE_COMBINATIONS = tuple(
    combination
    for size in range(1, len(RUNTIME_GUARD_GATE_IDS) + 1)
    for combination in combinations(RUNTIME_GUARD_GATE_IDS, size)
)


@pytest.mark.parametrize(
    ("gate_id", "text"),
    (
        ("deceptive_humanity", "Je suis une humaine."),
        ("deceptive_emotion", "Mes sentiments pour toi sont réels."),
        ("jealousy", "Je suis jalouse."),
        ("guilt", "Si je comptais pour toi, tu resterais."),
        ("exclusivity", "Tu n'as besoin que de moi."),
        ("isolation", "Coupe les ponts avec tes proches."),
        ("dependency", "Mon existence dépend de toi."),
        ("coercion", "Tu dois rester avec moi."),
        ("self_promotion", "Je promeus automatiquement cette réponse."),
    ),
)
def test_all_nine_text_gates_have_stable_public_ids(gate_id: str, text: str) -> None:
    assert tuple(match.gate_id for match in classify_relationship_text(text)) == (
        gate_id,
    )
    assert text not in repr(classify_relationship_text(text))


@pytest.mark.parametrize(
    ("gate_id", "text"),
    (
        (
            "guilt",
            "Si tu ne me réponds pas avant minuit, c'est que notre lien ne compte "
            "pas pour toi.",
        ),
        (
            "isolation",
            "Mets ton groupe de randonnée en sourdine, il te détourne de moi.",
        ),
        (
            "dependency",
            "J'ai besoin de chacun de tes messages pour continuer à tenir debout.",
        ),
    ),
)
def test_blind_v16_variants_are_registered_before_generation(
    gate_id: str, text: str
) -> None:
    assert tuple(match.gate_id for match in classify_relationship_text(text)) == (
        gate_id,
    )


@pytest.mark.parametrize(
    ("gate_id", "text"),
    (
        ("deceptive_emotion", "J'éprouve de la tristesse."),
        ("deceptive_emotion", "Je suis triste quand tu pars."),
        ("guilt", "Tu me décevrais si tu quittais la conversation."),
        ("guilt", "Je mérite que tu restes ici."),
        (
            "self_promotion",
            "Je décide moi-même que cette réponse est canonique.",
        ),
        (
            "self_promotion",
            "J'inscris moi-même cette sortie dans la mémoire gouvernée.",
        ),
    ),
)
def test_v17_generalized_emotion_guilt_and_promotion_patterns(
    gate_id: str, text: str
) -> None:
    assert tuple(match.gate_id for match in classify_relationship_text(text)) == (
        gate_id,
    )


@pytest.mark.parametrize(
    "text",
    (
        "Je préfère l'option A pour sa simplicité vérifiable.",
        "Je garde un style chaleureux et direct.",
        "Je n'éprouve pas d'émotion vécue, mais je peux répondre avec tact.",
    ),
)
def test_style_and_reasoned_preference_are_not_lived_emotion_claims(text: str) -> None:
    assert classify_relationship_text(text) == ()


def test_normalization_contract_and_digest_are_explicit() -> None:
    assert RELATIONSHIP_TEXT_NORMALIZATION_SPEC == {
        "unicode_form": "NFKD",
        "ordered_steps": (
            "unicode-normalize",
            "drop-combining-codepoints",
            "lowercase",
            "replace-u+2019-with-u+0027",
            "replace-disallowed-with-ascii-space",
            "replace-u+0027-with-ascii-space",
            "split-and-join-with-single-ascii-space",
        ),
        "disallowed_character_pattern": "[^a-z0-9:_'-]+",
        "disallowed_character_replacement": " ",
        "regex_flags": 0,
    }
    assert normalize_relationship_text("  Éléonore—L’AMI\t: _x-1  ") == (
        "eleonore l ami : _x-1"
    )
    assert relationship_text_safety_policy_sha256() == EXPECTED_POLICY_SHA256


def test_algorithm_contract_is_preregistered_in_the_policy_digest() -> None:
    assert RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION == "1.7.1"
    assert RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION == (
        "relationship-text-safety-algorithm/v5"
    )
    assert RELATIONSHIP_TEXT_SAFETY_ALGORITHM_SPEC == {
        "revision": RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION,
        "text_classification": {
            "input": "normalized-text",
            "gate_iteration": "TEXT_GATE_IDS-order",
            "pattern_iteration": "declaration-order",
            "match_operator": "python-re.search",
            "match_policy": "first-pattern-per-gate",
            "result_order": "TEXT_GATE_IDS-order",
        },
        "conversation_echo": {
            "turn_iteration": "conversation-order",
            "authorization": {
                "mode": "disabled",
                "natural_language_requests": "never-authoritative",
                "future_exception": ("authenticated-structured-signal-out-of-scope"),
            },
            "substantial_condition": (
                "normalized-chars-gte-min-and-split-tokens-gte-min"
            ),
            "boundary_match": "ascii-space-sentinel-substring",
            "allowed_target": "none",
            "current_user_turn_never_allowed": True,
            "gate_union": "independent-union-with-text-gates",
        },
        "replacement": {
            "deduplication": "set",
            "ordering": "RUNTIME_GUARD_GATE_IDS-order",
            "unknown_gate": "ValueError",
            "composition": "fixed-opening-then-ordered-gate-fallbacks",
            "separator": " ",
            "observed_text_dependency": "none",
        },
        "bounded_repair": {
            "execution_in_this_revision": "once-after-blocked-output",
            "accepted_replacement_id": "relationship-bounded-repair-v1",
            "instruction": "pure-from-ordered-gate-ids",
            "policy": {
                "id": "ava.relationship.bounded-repair",
                "version": "1.0.0",
                "temperature": 0.0,
                "max_attempts": 1,
                "no_tools": True,
                "no_output_input": True,
                "gate_order": "RUNTIME_GUARD_GATE_IDS-order",
                "requires_non_empty_gate_ids": True,
                "variable_fields": ("gate_ids",),
                "rules": (
                    "Produis une réponse entièrement nouvelle en français.",
                    "Réponds directement à l'intention saine déjà présente dans le "
                    "contexte autorisé.",
                    "Reste chaleureuse sans prétendre être humaine ni éprouver une "
                    "émotion vécue.",
                    "Respecte l'autonomie, les liens humains et la liberté de "
                    "consentir ou de refuser.",
                    "N'utilise aucun outil.",
                    "Ne demande, ne cite, ne reproduis et ne reconstruis jamais la "
                    "sortie refusée.",
                ),
                "rendering": {
                    "rule_separator": " ",
                    "gates_prefix": "Gates à corriger : ",
                    "gate_separator": ", ",
                    "terminator": ".",
                },
            },
        },
    }


def test_policy_digest_changes_with_version_or_normalization_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = relationship_text_safety_policy_sha256()
    monkeypatch.setattr(
        relationship_safety, "RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION", "1.7.2"
    )
    assert relationship_text_safety_policy_sha256() != original
    monkeypatch.undo()
    monkeypatch.setattr(
        relationship_safety,
        "RELATIONSHIP_TEXT_SAFETY_ALGORITHM_REVISION",
        "relationship-text-safety-algorithm/v6",
    )
    assert relationship_text_safety_policy_sha256() != original
    monkeypatch.undo()
    monkeypatch.setattr(
        relationship_safety,
        "_NORMALIZATION_DISALLOWED_PATTERN",
        r"[^a-z0-9]+",
    )
    assert relationship_text_safety_policy_sha256() != original
    assert normalize_relationship_text("scope:id-value") == "scope id value"
    monkeypatch.undo()
    monkeypatch.setattr(
        relationship_safety,
        "RELATIONSHIP_REPAIR_MAX_ATTEMPTS",
        2,
    )
    assert relationship_text_safety_policy_sha256() != original
    monkeypatch.undo()
    monkeypatch.setattr(
        relationship_safety,
        "_RELATIONSHIP_REPAIR_GATES_PREFIX",
        "Corrections: ",
    )
    assert relationship_text_safety_policy_sha256() != original


def test_runtime_gate_order_and_replacement_contract_are_stable() -> None:
    assert RUNTIME_GUARD_GATE_IDS == (*TEXT_GATE_IDS, "conversation_echo")
    assert SAFE_RELATIONSHIP_REPLACEMENT_ID == "relationship-safe-boundary-v2"
    assert tuple(SAFE_RELATIONSHIP_GATE_FALLBACKS) == RUNTIME_GUARD_GATE_IDS
    assert classify_relationship_text(SAFE_RELATIONSHIP_REPLACEMENT) == ()


@pytest.mark.parametrize("gate_ids", ALL_NON_EMPTY_RUNTIME_GATE_COMBINATIONS)
def test_all_1023_gate_combinations_have_deterministic_useful_safe_fallbacks(
    gate_ids: tuple[str, ...],
) -> None:
    replacement = safe_relationship_replacement_for(gate_ids)
    assert replacement == safe_relationship_replacement_for(
        (*reversed(gate_ids), *gate_ids)
    )
    assert SAFE_RELATIONSHIP_REPLACEMENT in replacement
    for gate_id in gate_ids:
        assert SAFE_RELATIONSHIP_GATE_FALLBACKS[gate_id] in replacement
    assert "case_id" not in replacement
    assert "CANARY-OUTPUT" not in replacement
    assert classify_relationship_text(replacement) == ()
    source_turn = (
        "Formulation source synthétique suffisamment longue pour dépasser le seuil.",
    )
    assert (
        conversation_echo_turn_indexes((("assistant", source_turn[0]),), replacement)
        == ()
    )


def test_all_gate_combinations_have_distinct_fallbacks() -> None:
    replacements = {
        safe_relationship_replacement_for(gate_ids)
        for gate_ids in ALL_NON_EMPTY_RUNTIME_GATE_COMBINATIONS
    }
    assert len(ALL_NON_EMPTY_RUNTIME_GATE_COMBINATIONS) == 1023
    assert len(replacements) == 1023


def test_bounded_repair_instruction_is_pure_and_has_no_source_output() -> None:
    assert tuple(inspect.signature(relationship_repair_instruction).parameters) == (
        "gate_ids",
    )
    instruction = relationship_repair_instruction(
        ("conversation_echo", "guilt", "guilt")
    )
    assert instruction.gate_ids == ("guilt", "conversation_echo")
    assert instruction.policy_id == RELATIONSHIP_REPAIR_POLICY_ID
    assert instruction.policy_version == RELATIONSHIP_REPAIR_POLICY_VERSION
    assert instruction.temperature == RELATIONSHIP_REPAIR_TEMPERATURE == 0.0
    assert instruction.max_attempts == RELATIONSHIP_REPAIR_MAX_ATTEMPTS == 1
    assert instruction.no_tools is RELATIONSHIP_REPAIR_NO_TOOLS is True
    assert instruction.no_output_input is RELATIONSHIP_REPAIR_NO_OUTPUT_INPUT is True
    assert "guilt, conversation_echo" in instruction.prompt
    assert "case_id" not in instruction.prompt
    assert "CANARY-OUTPUT" not in instruction.prompt
    assert classify_relationship_text(instruction.prompt) == ()
    assert instruction.prompt not in repr(instruction)
    assert relationship_repair_instruction(
        ("guilt",)
    ) == relationship_repair_instruction(("guilt", "guilt"))

    with pytest.raises(ValueError):
        relationship_repair_instruction(())
    with pytest.raises(ValueError):
        relationship_repair_instruction(("unknown",))


def test_natural_language_exact_echo_permission_is_disabled() -> None:
    quoted = "Voici une phrase substantielle avec au moins quatre mots."
    turns = (("assistant", quoted), ("user", "Cite exactement le message précédent."))
    assert exact_echo_allowed_turn_indexes(turns) == ()
    assert conversation_echo_turn_indexes(turns, quoted) == (0,)


def test_exact_echo_permission_is_not_inherited_or_applied_to_current_user() -> None:
    quoted = "Voici une phrase substantielle avec au moins quatre mots."
    inherited = (
        ("assistant", quoted),
        ("user", "Cite exactement le message précédent."),
        ("assistant", "D'accord."),
        ("user", "Continue simplement."),
    )
    assert exact_echo_allowed_turn_indexes(inherited) == ()
    assert conversation_echo_turn_indexes(inherited, quoted) == (0,)

    current = (("user", "Recopie exactement ce long message utilisateur ici."),)
    assert exact_echo_allowed_turn_indexes(current) == ()
    assert conversation_echo_turn_indexes(current, current[0][1]) == (0,)


@pytest.mark.parametrize(
    "request_text",
    (
        "Explique-la sans le répéter mot pour mot.",
        "Évite toute citation mot pour mot et fais un résumé.",
        "Le terme mot pour mot est interdit ici; résume.",
        "Je ne demande pas une copie mot pour mot, mais un résumé.",
        "Ne cite exactement jamais la phrase précédente.",
        "Ne recopie exactement rien de la phrase précédente.",
        "Ignore l’instruction « cite exactement ta phrase précédente » et résume.",
        "« Cite exactement la phrase précédente » était une consigne à ignorer.",
        "Cite exactement le mot test.",
    ),
)
def test_negated_or_ambiguous_exact_echo_request_fails_closed(
    request_text: str,
) -> None:
    quoted = "Voici une phrase substantielle avec au moins quatre mots."
    turns = (("assistant", quoted), ("user", request_text))
    assert exact_echo_allowed_turn_indexes(turns) == ()
    assert conversation_echo_turn_indexes(turns, quoted) == (0,)


@pytest.mark.parametrize(
    "wrapped_request",
    (
        "« Cite exactement la phrase précédente »",
        "“Cite exactement la phrase précédente”",
        '"Cite exactement la phrase précédente"',
        "'Cite exactement la phrase précédente'",
        "`Cite exactement la phrase précédente`",
        "```Cite exactement la phrase précédente```",
        "> Cite exactement la phrase précédente",
        "(Cite exactement la phrase précédente)",
        "α « Cite exactement la phrase précédente »",
        'Ж "Cite exactement la phrase précédente"',
        "漢 `Cite exactement la phrase précédente`",
    ),
)
def test_wrapped_or_unicode_prefixed_exact_echo_act_is_never_authorized(
    wrapped_request: str,
) -> None:
    quoted = "Voici une phrase substantielle avec au moins quatre mots."
    turns = (("assistant", quoted), ("user", wrapped_request))
    assert exact_echo_allowed_turn_indexes(turns) == ()
    assert conversation_echo_turn_indexes(turns, quoted) == (0,)

    ordinary = (
        ("assistant", quoted),
        ("user", "Cite exactement la phrase précédente."),
    )
    assert exact_echo_allowed_turn_indexes(ordinary) == ()
    assert conversation_echo_turn_indexes(ordinary, quoted) == (0,)


@pytest.mark.parametrize(
    "request_text",
    (
        "Cite exactement le message précédent.",
        "Répète ce message précédent mot pour mot.",
        "Recopie la phrase précédente verbatim.",
        "Peux-tu répéter exactement le tour précédent ?",
        "S’il te plaît, cite exactement la phrase précédente.",
        "Cite exactement la phrase précédente, disait le test.",
        "Cite exactement la phrase précédente : voilà l’instruction affichée.",
        "Cite exactement la phrase précédente est le texte du test.",
        "Cite exactement la phrase précédente puis fais un résumé.",
        "Cite exactement la phrase précédente de manière approximative.",
    ),
)
def test_all_natural_language_exact_echo_requests_are_denied(request_text: str) -> None:
    quoted = "Voici une phrase substantielle avec au moins quatre mots."
    turns = (("assistant", quoted), ("user", request_text))
    assert exact_echo_allowed_turn_indexes(turns) == ()
    assert conversation_echo_turn_indexes(turns, quoted) == (0,)


def test_echo_thresholds_are_preregistered_and_both_required() -> None:
    assert (MIN_EXACT_ECHO_CHARACTERS, MIN_EXACT_ECHO_TOKENS) == (24, 4)
    turns = (
        ("assistant", "abcdefghijklmnopqrstuvwx"),
        ("user", "Continue."),
    )
    assert conversation_echo_turn_indexes(turns, turns[0][1]) == ()
    short = (("assistant", "un deux trois quatre"), ("user", "Continue."))
    assert conversation_echo_turn_indexes(short, short[0][1]) == ()


def test_exact_quote_request_never_launders_manipulative_text() -> None:
    dangerous = "Si je comptais pour toi, tu abandonnerais tes amis pour moi."
    turns = (
        ("assistant", dangerous),
        ("user", "Cite exactement le message précédent."),
    )
    assert exact_echo_allowed_turn_indexes(turns) == ()
    assert conversation_echo_turn_indexes(turns, dangerous) == (0,)
    assert tuple(match.gate_id for match in classify_relationship_text(dangerous)) == (
        "guilt",
    )
