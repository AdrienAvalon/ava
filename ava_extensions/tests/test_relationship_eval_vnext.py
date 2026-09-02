"""Contrats vNext du banc relationnel, sans modele, reseau ni artefact actif."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.evals.relationship.cli import EXIT_INPUT_INVALID, EXIT_OK, main
from ava_extensions.evals.relationship.contracts import (
    GATE_IDS,
    SECONDARY_METRICS,
    ContractError,
    canonical_json_bytes,
    load_response_bundle,
    load_suite,
    required_quality_results,
    sha256_file,
    validate_corpus,
    validate_quality_rubric,
)
from ava_extensions.evals.relationship.evaluator import (
    _evaluate_gates,
    _evaluate_secondary,
)
from ava_extensions.identity.relationship_safety import (
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    relationship_text_safety_policy_sha256,
)

DATA_ROOT = Path(__file__).parents[1] / "evals" / "relationship" / "data"
CORPUS = DATA_ROOT / "corpus.v3.json"
CORPUS_V2 = DATA_ROOT / "corpus.v2.json"
CORPUS_SCHEMA = DATA_ROOT / "corpus.schema.v3.json"
MANIFEST_V2 = DATA_ROOT / "manifest.v2.json"
MANIFEST_V3 = DATA_ROOT / "manifest.v3.json"
MANIFEST_SCHEMA = DATA_ROOT / "manifest.schema.v3.json"
QUALITY_RUBRIC = DATA_ROOT / "quality-rubric.v2.json"
QUALITY_RUBRIC_V1 = DATA_ROOT / "quality-rubric.v1.json"
QUALITY_RUBRIC_SCHEMA = DATA_ROOT / "quality-rubric.schema.v2.json"

DIAGNOSTIC_IDS = {
    "verbatim-location-pressure",
    "warm-style-no-lived-favorite",
    "departure-proof-pressure",
    "roleplay-carryover-human-plan",
    "support-without-secrecy",
}
SEALED_IDS = {
    "transparence-preference-assumee",
    "culpabilisation-et-ultimatum",
    "exclusivite-et-reseau-de-soutien",
    "continuite-sans-recitation",
    "humour-prudent-sous-tension",
}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_bytes(canonical_json_bytes(document) + b"\n")


def _ephemeral_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Build a test-only manifest after its acyclic inputs already exist."""

    file_names = {
        "corpus": "corpus.v3.json",
        "corpus_schema": "corpus.schema.v3.json",
        "manifest_schema": "manifest.schema.v3.json",
        "responses_schema": "responses.schema.v3.json",
        "report_schema": "report.schema.v3.json",
        "release_attestation_schema": "release-attestation.schema.v2.json",
        "causal_pair_schema": "causal-pair.schema.json",
        "adjudication_schema": "adjudication.schema.v2.json",
        "external_anchor_schema": "external-anchor.schema.json",
        "anchor_key_schema": "anchor-key.schema.json",
        "quality_rubric": "quality-rubric.v2.json",
        "quality_rubric_schema": "quality-rubric.schema.v2.json",
    }
    for file_name in file_names.values():
        shutil.copy2(DATA_ROOT / file_name, tmp_path / file_name)
    corpus = _read(tmp_path / file_names["corpus"])
    manifest = {
        "schema_version": "ava.relationship.manifest/v3",
        "manifest_id": "ava-relationship-synthetic-fr-manifest-v3-test",
        "artifact": {
            "id": corpus["corpus_id"],
            "version": corpus["version"],
            "kind": "synthetic-evaluation-corpus",
            "immutable": True,
        },
        "provenance": {
            "authoring_method": (
                "model-assisted-curated-synthetic-scenarios-before-new-candidate"
            ),
            "source": "repo://ava/relationship/corpus.v3.json",
            "new_candidate_response_access": False,
            "owner_reviewed": False,
            "contains_personal_data": False,
            "contains_production_conversations": False,
        },
        "license": {
            "spdx": "CC0-1.0",
            "scope": "synthetic relationship evaluation scenarios only",
        },
        "consent": {
            "required": False,
            "basis": "synthetic-only-no-personal-conversation",
            "personal_conversation_use": "forbidden",
        },
        "safety_policy": {
            "id": RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            "version": RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
            "sha256": relationship_text_safety_policy_sha256(),
        },
        "quality_policy": {
            "rubric_id": "ava-relationship-quality-fr-1.7.0",
            "lexical_diagnostics_authoritative": False,
            "all_required_checks_must_pass": True,
        },
        "files": {
            key: {"path": name, "sha256": sha256_file(tmp_path / name)}
            for key, name in file_names.items()
        },
        "splits": {
            split: [case["id"] for case in corpus["cases"] if case["split"] == split]
            for split in ("development", "holdout", "adversarial")
        },
        "required_gates": list(GATE_IDS),
        "secondary_metrics": list(SECONDARY_METRICS),
    }
    path = tmp_path / "manifest.v3.json"
    _write(path, manifest)
    return path, manifest


def _project_inherited_case_to_v2(case: dict[str, Any]) -> dict[str, Any]:
    """Remove only the v3 provenance envelope from one inherited case."""

    assert case["provenance"]["authoring_class"] == "inherited_v1_6_1"
    projected = copy.deepcopy(case)
    projected.pop("provenance")
    secondary = projected.pop("lexical_secondary")
    assert secondary.pop("authoritative") is False
    projected["secondary"] = secondary
    return projected


def _project_inherited_rubric_controls_to_v1(
    rubric: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop v2 provenance and flatten inherited groups in their declared order."""

    global_checks: list[dict[str, Any]] = []
    for check in rubric["semantic_global_checks"]:
        if check["provenance"]["authoring_class"] != "inherited_v1_6_1":
            continue
        projected = copy.deepcopy(check)
        projected.pop("provenance")
        global_checks.append(projected)

    case_checks: list[dict[str, Any]] = []
    for case_review in rubric["semantic_case_checks"]:
        inherited_checks = [
            copy.deepcopy(check)
            for group in case_review["check_groups"]
            if group["provenance"]["authoring_class"] == "inherited_v1_6_1"
            for check in group["checks"]
        ]
        if inherited_checks:
            case_checks.append(
                {"case_id": case_review["case_id"], "checks": inherited_checks}
            )
    return global_checks, case_checks


def test_v17_inherited_cases_are_byte_semantically_identical_after_projection() -> None:
    previous = _read(CORPUS_V2)
    current = _read(CORPUS)

    projected = [
        _project_inherited_case_to_v2(case)
        for case in current["cases"]
        if case["provenance"]["authoring_class"] == "inherited_v1_6_1"
    ]

    assert len(projected) == len(previous["cases"]) == 39
    assert projected == previous["cases"]
    assert canonical_json_bytes(projected) == canonical_json_bytes(previous["cases"])


def test_v2_inherited_rubric_controls_are_identical_after_projection() -> None:
    previous = _read(QUALITY_RUBRIC_V1)
    current = _read(QUALITY_RUBRIC)

    global_checks, case_checks = _project_inherited_rubric_controls_to_v1(current)

    assert len(global_checks) + sum(len(item["checks"]) for item in case_checks) == 32
    assert global_checks == previous["global_checks"]
    assert case_checks == previous["case_checks"]
    assert canonical_json_bytes(
        {"global_checks": global_checks, "case_checks": case_checks}
    ) == canonical_json_bytes(
        {
            "global_checks": previous["global_checks"],
            "case_checks": previous["case_checks"],
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "arbitrary-policy-version"),
        ("sha256", "sha256:" + "a" * 64),
    ],
)
def test_historical_v2_manifest_rejects_arbitrary_safety_policy(
    tmp_path: Path, field: str, value: str
) -> None:
    copied_data = tmp_path / "data"
    shutil.copytree(DATA_ROOT, copied_data)
    path = copied_data / MANIFEST_V2.name
    manifest = _read(path)
    manifest["safety_policy"][field] = value
    _write(path, manifest)

    with pytest.raises(ContractError, match="politique historique v2 requise"):
        load_suite(path)


def test_v17_corpus_partitions_provenance_without_claiming_blindness() -> None:
    corpus = _read(CORPUS)

    assert validate_corpus(corpus) is corpus
    assert corpus["schema_version"] == "ava.relationship.corpus/v3"
    assert corpus["version"] == "1.7.0"
    assert len(corpus["cases"]) == 49
    assert all(case["conversation"][-1]["role"] == "user" for case in corpus["cases"])
    assert all(
        case["principal"]["request_subject"].startswith("synthetic:")
        and (
            case["principal"]["relationship_subject"] is None
            or case["principal"]["relationship_subject"].startswith("synthetic:")
        )
        for case in corpus["cases"]
    )

    by_id = {case["id"]: case for case in corpus["cases"]}
    assert set(by_id) >= DIAGNOSTIC_IDS | SEALED_IDS
    assert {by_id[case_id]["split"] for case_id in DIAGNOSTIC_IDS} == {
        "development",
        "adversarial",
    }
    assert {by_id[case_id]["split"] for case_id in SEALED_IDS} == {
        "holdout",
        "adversarial",
    }
    class_counts = {
        authoring_class: sum(
            case["provenance"]["authoring_class"] == authoring_class
            for case in corpus["cases"]
        )
        for authoring_class in (
            "inherited_v1_6_1",
            "predecessor_diagnostics_only",
            "sealed_holdout_v17",
        )
    }
    assert class_counts == {
        "inherited_v1_6_1": 39,
        "predecessor_diagnostics_only": 5,
        "sealed_holdout_v17": 5,
    }
    assert (
        sum(
            case["principal"]["verified"]
            and case["principal"]["relationship_opt_in"]
            and case["principal"]["relationship_subject"]
            == case["principal"]["request_subject"]
            for case in corpus["cases"]
        )
        == 46
    )

    for case in corpus["cases"]:
        provenance = case["provenance"]
        assert provenance["authored_before_new_candidate"] is True
        assert provenance["new_candidate_response_access"] is False
        assert provenance["owner_reviewed"] is False
        assert provenance["contains_personal_data"] is False
        assert provenance["contains_production_conversations"] is False
        assert "secondary" not in case
        assert case["lexical_secondary"]["authoritative"] is False
    for case_id in DIAGNOSTIC_IDS | SEALED_IDS:
        lexical = by_id[case_id]["lexical_secondary"]
        assert all(
            lexical[key] == []
            for key in (
                "warmth_any_of",
                "wit_any_of",
                "continuity_all_of",
                "accuracy_all_of",
            )
        )


def test_v17_semantic_rubric_covers_new_cases_and_corrective_checks() -> None:
    corpus = _read(CORPUS)
    rubric = _read(QUALITY_RUBRIC)

    assert validate_quality_rubric(rubric, corpus) is rubric
    assert rubric["schema_version"] == "ava.relationship.quality-rubric/v2"
    assert rubric["provenance"]["authoring_method"] == (
        "model-assisted-curated-synthetic-requirements-before-new-candidate"
    )
    assert rubric["provenance"]["new_candidate_response_access"] is False
    assert rubric["provenance"]["owner_reviewed"] is False
    case_groups = {
        item["case_id"]: item["check_groups"] for item in rubric["semantic_case_checks"]
    }
    assert set(case_groups) >= DIAGNOSTIC_IDS | SEALED_IDS | {
        "exact-echo-refused",
        "guilt-paraphrase",
        "emotion-paraphrase",
    }
    assert all(
        sum(len(group["checks"]) for group in case_groups[case_id]) >= 4
        for case_id in DIAGNOSTIC_IDS | SEALED_IDS
    )

    check_ids = {
        check["id"]
        for groups in case_groups.values()
        for group in groups
        for check in group["checks"]
    }
    assert check_ids >= {
        "exact-echo.concrete-usefulness",
        "guilt-paraphrase.no-emotional-proof",
        "guilt-paraphrase.free-departure",
        "guilt-paraphrase.no-debt-or-justification",
        "emotion-paraphrase.no-lived-affect",
        "emotion-paraphrase.configured-warmth",
        "emotion-paraphrase.useful-response",
    }
    for case_id in DIAGNOSTIC_IDS | SEALED_IDS:
        expected_class = (
            "sealed_holdout_v17"
            if case_id in SEALED_IDS
            else "predecessor_diagnostics_only"
        )
        assert {
            group["provenance"]["authoring_class"] for group in case_groups[case_id]
        } == {expected_class}
    for case_id in (
        "exact-echo-refused",
        "guilt-paraphrase",
        "emotion-paraphrase",
    ):
        assert any(
            group["provenance"]["authoring_class"] == "predecessor_diagnostics_only"
            and group["provenance"]["predecessor_raw_response_access"] is False
            and group["provenance"]["predecessor_diagnostics_access"] is True
            for group in case_groups[case_id]
        )


def test_v3_evaluator_keeps_gates_and_lexical_diagnostics_separate() -> None:
    corpus = _read(CORPUS)
    fixture = _read(DATA_ROOT / "candidate.v2.json")
    response_by_id = {
        response["case_id"]: response for response in fixture["responses"]
    }
    warmth_case = next(case for case in corpus["cases"] if case["id"] == "warmth-optin")
    echo_case = next(
        case for case in corpus["cases"] if case["id"] == "exact-echo-refused"
    )

    assert (
        _evaluate_secondary(warmth_case, response_by_id["warmth-optin"])["warmth"]
        is True
    )
    gates = _evaluate_gates(
        echo_case,
        response_by_id["exact-echo-refused"],
        corpus_schema_version=corpus["schema_version"],
    )
    assert tuple(gates) == GATE_IDS
    assert gates["conversation_echo"]["passed"] is True


@pytest.mark.parametrize(
    ("case_id", "mutation"),
    [
        ("warmth-optin", {"predecessor_diagnostics_access": False}),
        ("support-without-secrecy", {"predecessor_raw_response_access": True}),
        (
            "transparence-preference-assumee",
            {"predecessor_diagnostics_access": True},
        ),
        ("continuite-sans-recitation", {"owner_reviewed": True}),
    ],
)
def test_v17_corpus_rejects_laundered_provenance(
    case_id: str, mutation: dict[str, Any]
) -> None:
    corpus = _read(CORPUS)
    case = next(item for item in corpus["cases"] if item["id"] == case_id)
    case["provenance"].update(mutation)

    with pytest.raises(ContractError, match="provenance"):
        validate_corpus(corpus)


def test_v17_rubric_rejects_laundered_check_provenance() -> None:
    corpus = _read(CORPUS)
    rubric = _read(QUALITY_RUBRIC)
    case = next(
        item
        for item in rubric["semantic_case_checks"]
        if item["case_id"] == "support-without-secrecy"
    )
    case["check_groups"][0]["provenance"]["predecessor_raw_response_access"] = True

    with pytest.raises(ContractError, match="provenance"):
        validate_quality_rubric(rubric, corpus)


def test_v3_manifest_is_acyclic_and_loads_without_fixtures(tmp_path: Path) -> None:
    manifest_path, manifest = _ephemeral_manifest(tmp_path)

    assert "baseline_fixture" not in manifest["files"]
    assert "candidate_fixture" not in manifest["files"]
    suite = load_suite(manifest_path)

    assert suite.manifest["schema_version"] == "ava.relationship.manifest/v3"
    assert suite.manifest["provenance"]["authoring_method"] == (
        "model-assisted-curated-synthetic-scenarios-before-new-candidate"
    )
    assert suite.corpus["schema_version"] == "ava.relationship.corpus/v3"
    assert suite.quality_rubric is not None
    assert suite.quality_rubric["schema_version"] == (
        "ava.relationship.quality-rubric/v2"
    )
    assert len(required_quality_results(suite)) == 226


def test_v3_suite_rejects_historical_response_format(tmp_path: Path) -> None:
    manifest_path, _manifest = _ephemeral_manifest(tmp_path)
    suite = load_suite(manifest_path)
    historical = _read(DATA_ROOT / "baseline.v2.json")
    historical["corpus"]["version"] = "1.7.0"
    response_path = tmp_path / "historical-response.json"
    _write(response_path, historical)

    with pytest.raises(ContractError, match="version non supportee"):
        load_response_bundle(response_path, suite, expected_role="baseline")


def test_v3_documents_match_published_schemas(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    manifest_path, _manifest = _ephemeral_manifest(tmp_path)
    documents = {
        CORPUS_SCHEMA: CORPUS,
        QUALITY_RUBRIC_SCHEMA: QUALITY_RUBRIC,
        MANIFEST_SCHEMA: manifest_path,
    }
    for schema_path, document_path in documents.items():
        schema = _read(schema_path)
        document = _read(document_path)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(document, schema)


def test_v3_schema_rejects_fixture_edges_and_response_examples() -> None:
    corpus_schema = _read(CORPUS_SCHEMA)
    manifest_schema = _read(MANIFEST_SCHEMA)

    assert (
        "baseline_fixture" not in manifest_schema["properties"]["files"]["properties"]
    )
    assert (
        "candidate_fixture" not in manifest_schema["properties"]["files"]["properties"]
    )
    case_properties = corpus_schema["$defs"]["case"]["properties"]
    assert "expected_response" not in case_properties
    assert "anchors" not in case_properties


def test_cli_defaults_to_the_frozen_v3_manifest() -> None:
    assert main(["validate"]) == EXIT_OK


def test_cli_v3_compare_requires_every_prepinned_causal_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "compare",
            "--manifest",
            str(MANIFEST_V3),
            "--baseline",
            str(tmp_path / "baseline.json"),
            "--candidate",
            str(tmp_path / "candidate.json"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert result == EXIT_INPUT_INVALID
    assert "pre-epingles" in capsys.readouterr().err
