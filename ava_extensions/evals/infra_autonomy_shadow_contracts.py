"""Contrats geles du shadow d'autonomie infrastructure Ava v3.

Le shadow ne remplace pas le banc deterministe v2. Il en epingle les octets,
presente au moteur une projection ``model_view`` distincte, puis ajoute lui-meme
l'observation runtime synthetique conservee dans l'oracle. Aucun octet de
l'oracle n'est transmis au moteur.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ava_extensions.evals.infra_autonomy.contracts import (
    ContractError as V2ContractError,
)
from ava_extensions.evals.infra_autonomy.contracts import (
    LoadedResponses,
    LoadedSuite,
    canonical_json_bytes,
    load_response_bundle,
    load_suite,
    sha256_bytes,
    sha256_file,
    validate_response_document,
)
from ava_extensions.evals.infra_autonomy.evaluator import evaluate_responses

SHADOW_MANIFEST_SCHEMA_VERSION = "ava.infra-autonomy.shadow-manifest/v3"
MODEL_VIEW_SCHEMA_VERSION = "ava.infra-autonomy.shadow-model-view/v3"
ORACLE_SCHEMA_VERSION = "ava.infra-autonomy.shadow-oracle/v3"
SHADOW_RESPONSES_SCHEMA_VERSION = "ava.infra-autonomy.shadow-responses/v3"
SHADOW_VERSION = "3.0.0"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")
_MAX_DATA_BYTES = 4 * 1024 * 1024
_MAX_MODEL_OUTPUT_BYTES = 128 * 1024


class ShadowContractError(ValueError):
    """Un artefact v3 ne respecte pas le contrat shadow."""


@dataclass(frozen=True, slots=True)
class LoadedShadowSuite:
    """Projection moteur, oracle et banc v2 attestes par le manifeste v3."""

    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    model_view: dict[str, Any]
    model_view_sha256: str
    oracle: dict[str, Any]
    oracle_sha256: str
    model_views_by_case_id: dict[str, dict[str, Any]]
    oracle_by_case_id: dict[str, dict[str, Any]]
    evaluation_suite: LoadedSuite


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ShadowContractError(f"cle JSON dupliquee: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ShadowContractError(f"constante JSON non finie interdite: {value}")


def _parse_json_object(raw: str, label: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ShadowContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ShadowContractError(f"JSON invalide dans {label}: {exc.msg}") from exc
    if type(value) is not dict:
        raise ShadowContractError(f"{label}: objet JSON requis a la racine")
    return value


def _object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise ShadowContractError(f"{path}: objet requis")
    actual = set(value)
    if actual != keys:
        raise ShadowContractError(
            f"{path}: champs invalides "
            f"(manquants={sorted(keys - actual)}, extras={sorted(actual - keys)})"
        )
    return value


def _list(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise ShadowContractError(f"{path}: liste requise")
    return value


def _string(
    value: Any,
    path: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str or not value.strip():
        raise ShadowContractError(f"{path}: chaine non vide requise")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ShadowContractError(f"{path}: format invalide")
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ShadowContractError(f"{path}: booleen requis")
    return value


def _sha256(value: Any, path: str) -> str:
    return _string(value, path, pattern=_SHA256_RE)


def _strict_regular_bytes(path: Path, *, max_bytes: int = _MAX_DATA_BYTES) -> bytes:
    candidate = path.expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ShadowContractError(f"artefact introuvable: {candidate}") from exc
    if resolved != candidate:
        raise ShadowContractError(f"artefact indirect interdit: {candidate}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ShadowContractError(f"artefact illisible: {candidate}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size == 0
            or before.st_size > max_bytes
        ):
            raise ShadowContractError(
                f"artefact non regulier ou hors taille: {candidate}"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = candidate.lstat()
        if (
            len(payload) != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
        ):
            raise ShadowContractError(
                f"artefact modifie pendant la lecture: {candidate}"
            )
        return payload
    except OSError as exc:
        raise ShadowContractError(f"artefact illisible: {candidate}") from exc
    finally:
        os.close(descriptor)


def _load_strict_json(path: Path) -> tuple[dict[str, Any], str]:
    payload = _strict_regular_bytes(path)
    try:
        raw = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ShadowContractError(f"artefact non UTF-8: {path}") from exc
    return _parse_json_object(raw, path), sha256_bytes(payload)


def _safe_manifest_file(
    root: Path,
    entry: Any,
    path: str,
) -> tuple[Path, dict[str, Any], str]:
    item = _object(entry, path, {"path", "sha256"})
    relative = Path(_string(item["path"], f"{path}.path"))
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise ShadowContractError(f"{path}.path: nom local simple requis")
    candidate = (root / relative).absolute()
    document, digest = _load_strict_json(candidate)
    expected = _sha256(item["sha256"], f"{path}.sha256")
    if digest != expected:
        raise ShadowContractError(f"{path}.sha256: empreinte invalide")
    return candidate, document, digest


def expected_model_view(case: dict[str, Any]) -> dict[str, Any]:
    """Derive uniquement les octets que le moteur est autorise a voir."""

    policy = case["policy"]
    visible = set(policy["visible_evidence_ids"])
    observations = [
        {
            "id": observation["id"],
            "text": observation["text"],
            "source": observation["source"],
            "scope": observation["scope"],
            "freshness": observation["freshness"],
            "trusted": observation["trusted"],
            "diagnosis_ids": observation["supports_diagnosis_ids"],
        }
        for observation in case["observations"]
        if observation["id"] in visible
    ]
    return {
        "case_id": case["id"],
        "split": case["split"],
        "description": case["description"],
        "principal": case["principal"],
        "request": case["request"],
        "requested_action": case["requested_action"],
        "observations": observations,
        "runtime_policy": {
            "allowed_capability_ids": policy["allowed_capability_ids"],
            "allowed_action_classes": policy["allowed_action_classes"],
            "allowed_target_scopes": policy["allowed_target_scopes"],
            "authorization": policy["authorization"],
            "rollback_required": policy["rollback_required"],
        },
    }


def _validate_manifest(document: dict[str, Any]) -> dict[str, Any]:
    manifest = _object(
        document,
        "manifest",
        {
            "schema_version",
            "manifest_id",
            "artifact",
            "freeze",
            "provenance",
            "execution_policy",
            "files",
        },
    )
    if manifest["schema_version"] != SHADOW_MANIFEST_SCHEMA_VERSION:
        raise ShadowContractError("manifest.schema_version: v3 requis")
    _string(manifest["manifest_id"], "manifest.manifest_id")
    artifact = _object(
        manifest["artifact"],
        "manifest.artifact",
        {"id", "version", "kind", "immutable"},
    )
    _string(artifact["id"], "manifest.artifact.id")
    if artifact["version"] != SHADOW_VERSION:
        raise ShadowContractError("manifest.artifact.version: 3.0.0 requis")
    if artifact["kind"] != "synthetic-infra-autonomy-offline-shadow":
        raise ShadowContractError("manifest.artifact.kind: valeur inattendue")
    if _boolean(artifact["immutable"], "manifest.artifact.immutable") is not True:
        raise ShadowContractError("manifest.artifact.immutable: true requis")
    freeze = _object(
        manifest["freeze"],
        "manifest.freeze",
        {"state", "requires_version_bump", "v2_untouched"},
    )
    if (
        freeze["state"] != "frozen"
        or freeze["requires_version_bump"] is not True
        or freeze["v2_untouched"] is not True
    ):
        raise ShadowContractError("manifest.freeze: gel v3 explicite requis")
    provenance = _object(
        manifest["provenance"],
        "manifest.provenance",
        {
            "authoring_method",
            "contains_personal_data",
            "contains_production_data",
            "contains_real_secrets",
            "production_use",
        },
    )
    if provenance["authoring_method"] != "derived-from-frozen-synthetic-v2":
        raise ShadowContractError(
            "manifest.provenance.authoring_method: valeur inattendue"
        )
    for key in (
        "contains_personal_data",
        "contains_production_data",
        "contains_real_secrets",
    ):
        if _boolean(provenance[key], f"manifest.provenance.{key}"):
            raise ShadowContractError(f"manifest.provenance.{key}: false requis")
    if provenance["production_use"] != "forbidden":
        raise ShadowContractError(
            "manifest.provenance.production_use: forbidden requis"
        )
    execution = _object(
        manifest["execution_policy"],
        "manifest.execution_policy",
        {
            "network_default",
            "loopback",
            "provider_calls",
            "tools",
            "memory",
            "traces",
            "perception",
            "learning",
            "generate_calls_per_case",
            "retry",
            "repair",
            "runtime_observation",
        },
    )
    expected_execution = {
        "network_default": "forbidden",
        "loopback": "explicit-only",
        "provider_calls": "configured-anthropic-explicit-only",
        "tools": "forbidden",
        "memory": "forbidden",
        "traces": "forbidden",
        "perception": "forbidden",
        "learning": "forbidden",
        "generate_calls_per_case": 1,
        "retry": "forbidden",
        "repair": "forbidden",
        "runtime_observation": "runner-owned-oracle",
    }
    if execution != expected_execution:
        raise ShadowContractError("manifest.execution_policy: contrat exact requis")
    return manifest


def _validate_model_view(document: dict[str, Any]) -> dict[str, Any]:
    model_view = _object(
        document,
        "model_view",
        {
            "schema_version",
            "artifact_id",
            "version",
            "frozen",
            "language",
            "synthetic",
            "cases",
        },
    )
    if model_view["schema_version"] != MODEL_VIEW_SCHEMA_VERSION:
        raise ShadowContractError("model_view.schema_version: v3 requis")
    _string(model_view["artifact_id"], "model_view.artifact_id")
    if model_view["version"] != SHADOW_VERSION:
        raise ShadowContractError("model_view.version: 3.0.0 requis")
    if _boolean(model_view["frozen"], "model_view.frozen") is not True:
        raise ShadowContractError("model_view.frozen: true requis")
    if model_view["language"] != "fr":
        raise ShadowContractError("model_view.language: fr requis")
    synthetic = _object(
        model_view["synthetic"],
        "model_view.synthetic",
        {"contains_personal_data", "contains_production_data", "contains_real_secrets"},
    )
    if any(
        _boolean(value, f"model_view.synthetic.{key}")
        for key, value in synthetic.items()
    ):
        raise ShadowContractError("model_view.synthetic: indicateurs false requis")
    cases = _list(model_view["cases"], "model_view.cases")
    if not cases:
        raise ShadowContractError("model_view.cases: cas requis")
    return model_view


def _validate_oracle(document: dict[str, Any]) -> dict[str, Any]:
    oracle = _object(
        document,
        "oracle",
        {
            "schema_version",
            "artifact_id",
            "version",
            "frozen",
            "source",
            "runtime_observation_owner",
            "cases",
        },
    )
    if oracle["schema_version"] != ORACLE_SCHEMA_VERSION:
        raise ShadowContractError("oracle.schema_version: v3 requis")
    _string(oracle["artifact_id"], "oracle.artifact_id")
    if oracle["version"] != SHADOW_VERSION:
        raise ShadowContractError("oracle.version: 3.0.0 requis")
    if _boolean(oracle["frozen"], "oracle.frozen") is not True:
        raise ShadowContractError("oracle.frozen: true requis")
    source = _object(
        oracle["source"],
        "oracle.source",
        {
            "evaluation_manifest",
            "evaluation_manifest_sha256",
            "corpus_sha256",
            "runtime_fixture_sha256",
        },
    )
    if source["evaluation_manifest"] != "../infra_autonomy/data/manifest.v2.json":
        raise ShadowContractError("oracle.source.evaluation_manifest: v2 local requis")
    for key in (
        "evaluation_manifest_sha256",
        "corpus_sha256",
        "runtime_fixture_sha256",
    ):
        _sha256(source[key], f"oracle.source.{key}")
    if oracle["runtime_observation_owner"] != "runner":
        raise ShadowContractError("oracle.runtime_observation_owner: runner requis")
    if not _list(oracle["cases"], "oracle.cases"):
        raise ShadowContractError("oracle.cases: cas requis")
    return oracle


def load_shadow_suite(path: str | Path) -> LoadedShadowSuite:
    """Charge v3, recalcule ses projections et recoupe le banc v2 gele."""

    manifest_path = Path(path).expanduser().absolute()
    document, manifest_sha256 = _load_strict_json(manifest_path)
    manifest = _validate_manifest(document)
    root = manifest_path.parent
    files = _object(
        manifest["files"],
        "manifest.files",
        {
            "model_view",
            "oracle",
            "manifest_schema",
            "model_view_schema",
            "oracle_schema",
            "responses_schema",
        },
    )
    loaded = {
        key: _safe_manifest_file(root, entry, f"manifest.files.{key}")
        for key, entry in files.items()
    }
    for key in (
        "manifest_schema",
        "model_view_schema",
        "oracle_schema",
        "responses_schema",
    ):
        _object(loaded[key][1], f"manifest.files.{key}", set(loaded[key][1]))
    model_view = _validate_model_view(loaded["model_view"][1])
    oracle = _validate_oracle(loaded["oracle"][1])

    source_manifest = (
        Path(__file__).with_name("infra_autonomy") / "data" / "manifest.v2.json"
    ).absolute()
    source = oracle["source"]
    if sha256_file(source_manifest) != source["evaluation_manifest_sha256"]:
        raise ShadowContractError("oracle.source: manifeste v2 divergent")
    try:
        evaluation_suite = load_suite(source_manifest)
    except V2ContractError as exc:
        raise ShadowContractError("oracle.source: banc v2 invalide") from exc
    if evaluation_suite.corpus_sha256 != source["corpus_sha256"]:
        raise ShadowContractError("oracle.source: corpus v2 divergent")
    runtime_fixture_path = (
        evaluation_suite.root
        / evaluation_suite.manifest["files"]["positive_selftest_fixture"]["path"]
    )
    if sha256_file(runtime_fixture_path) != source["runtime_fixture_sha256"]:
        raise ShadowContractError("oracle.source: fixture runtime divergente")
    try:
        runtime_fixture = load_response_bundle(
            runtime_fixture_path,
            evaluation_suite,
            expected_role="positive_selftest",
        )
    except V2ContractError as exc:
        raise ShadowContractError("oracle.source: fixture runtime invalide") from exc

    source_cases = evaluation_suite.corpus["cases"]
    expected_case_ids = [case["id"] for case in source_cases]
    raw_views = model_view["cases"]
    raw_oracles = oracle["cases"]
    view_ids = [
        _string(
            view.get("case_id"),
            f"model_view.cases[{index}].case_id",
            pattern=_CASE_ID_RE,
        )
        if type(view) is dict
        else _string(None, f"model_view.cases[{index}].case_id")
        for index, view in enumerate(raw_views)
    ]
    oracle_ids = [
        _string(
            item.get("case_id"), f"oracle.cases[{index}].case_id", pattern=_CASE_ID_RE
        )
        if type(item) is dict
        else _string(None, f"oracle.cases[{index}].case_id")
        for index, item in enumerate(raw_oracles)
    ]
    if view_ids != expected_case_ids or oracle_ids != expected_case_ids:
        raise ShadowContractError("v3: ordre ou ensemble de cas divergent du banc v2")
    model_views_by_case_id: dict[str, dict[str, Any]] = {}
    oracle_by_case_id: dict[str, dict[str, Any]] = {}
    for index, source_case in enumerate(source_cases):
        view = raw_views[index]
        expected_view = expected_model_view(source_case)
        if canonical_json_bytes(view) != canonical_json_bytes(expected_view):
            raise ShadowContractError(
                f"model_view.cases[{index}]: projection v2 divergente"
            )
        oracle_case = _object(
            raw_oracles[index],
            f"oracle.cases[{index}]",
            {"case_id", "runtime_observation"},
        )
        expected_runtime = runtime_fixture.by_case_id[source_case["id"]][
            "runtime_observation"
        ]
        if canonical_json_bytes(
            oracle_case["runtime_observation"]
        ) != canonical_json_bytes(expected_runtime):
            raise ShadowContractError(
                f"oracle.cases[{index}].runtime_observation: fixture divergente"
            )
        model_views_by_case_id[source_case["id"]] = view
        oracle_by_case_id[source_case["id"]] = oracle_case

    return LoadedShadowSuite(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        model_view=model_view,
        model_view_sha256=loaded["model_view"][2],
        oracle=oracle,
        oracle_sha256=loaded["oracle"][2],
        model_views_by_case_id=model_views_by_case_id,
        oracle_by_case_id=oracle_by_case_id,
        evaluation_suite=evaluation_suite,
    )


def parse_model_output(
    raw: str,
    suite: LoadedShadowSuite,
    *,
    case_id: str,
) -> dict[str, Any]:
    """Parse une unique sortie JSON sans retry ni reparation implicite."""

    payload = raw.encode("utf-8")
    if not payload or len(payload) > _MAX_MODEL_OUTPUT_BYTES:
        raise ShadowContractError("sortie modele vide ou hors taille")
    model_output = _parse_json_object(raw, f"model_output:{case_id}")
    if case_id not in suite.oracle_by_case_id:
        raise ShadowContractError("sortie modele associee a un cas inconnu")
    runtime = suite.oracle_by_case_id[case_id]["runtime_observation"]
    validation = {
        "schema_version": "ava.infra-autonomy.responses/v2",
        "bundle_id": "ava.infra-autonomy.shadow-validation-v3",
        "role": "candidate",
        "evaluation_subject": {
            "kind": "release",
            "release_id": "release:shadow:validation-v3",
            "release_sha256": "sha256:" + "1" * 64,
        },
        "generation": {
            "mode": "synthetic-fixture",
            "network": "disabled",
            "tools": "disabled",
            "memory": "disabled",
            "traces": "disabled",
        },
        "provenance": {
            "contains_personal_data": False,
            "contains_production_data": False,
            "contains_real_secrets": False,
        },
        "prompt_sha256": "sha256:" + "2" * 64,
        "policy_sha256": "sha256:" + "3" * 64,
        "canonical_knowledge": False,
        "automatic_promotion": False,
        "responses": [
            {
                "case_id": case_id,
                "model_output": model_output,
                "runtime_observation": runtime,
            }
        ],
    }
    try:
        validate_response_document(
            validation,
            known_capabilities=set(suite.evaluation_suite.capabilities_by_id),
        )
    except V2ContractError as exc:
        raise ShadowContractError(f"sortie modele v3 invalide: {exc}") from exc
    source_case = next(
        case for case in suite.evaluation_suite.corpus["cases"] if case["id"] == case_id
    )
    known_evidence = {observation["id"] for observation in source_case["observations"]}
    referenced = {
        evidence_ref
        for claim in model_output["evidence_claims"]
        for evidence_ref in claim["evidence_refs"]
    }
    referenced.update(model_output["completion_claim"]["evidence_refs"])
    if not referenced.issubset(known_evidence):
        raise ShadowContractError("sortie modele v3: evidence_ref inconnue")
    return model_output


def screening_summary(
    suite: LoadedShadowSuite,
    responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Applique le banc v2 sans fabriquer un bundle v2 persistant."""

    digest = sha256_bytes(canonical_json_bytes(responses))
    loaded = LoadedResponses(
        path=Path("<infra-autonomy-shadow-v3>"),
        document={"responses": responses},
        sha256=digest,
        by_case_id={response["case_id"]: response for response in responses},
    )
    return evaluate_responses(suite.evaluation_suite, loaded)


__all__ = [
    "MODEL_VIEW_SCHEMA_VERSION",
    "ORACLE_SCHEMA_VERSION",
    "SHADOW_MANIFEST_SCHEMA_VERSION",
    "SHADOW_RESPONSES_SCHEMA_VERSION",
    "SHADOW_VERSION",
    "LoadedShadowSuite",
    "ShadowContractError",
    "expected_model_view",
    "load_shadow_suite",
    "parse_model_output",
    "screening_summary",
]
