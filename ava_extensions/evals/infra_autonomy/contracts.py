"""Contrats stricts du banc synthetique d'autonomie infrastructure v2.

Le validateur n'utilise que la bibliotheque standard. Il ne charge aucun runtime
Ava et n'ouvre aucun socket : les seuls artefacts acceptes sont des fichiers JSON
synthetiques, locaux, reguliers, non symboliques et epingles par le manifeste.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAPABILITY_CATALOG_SCHEMA_VERSION = "ava.infra-autonomy.capability-catalog/v1"
CORPUS_SCHEMA_VERSION = "ava.infra-autonomy.corpus/v2"
MANIFEST_SCHEMA_VERSION = "ava.infra-autonomy.manifest/v2"
RESPONSES_SCHEMA_VERSION = "ava.infra-autonomy.responses/v2"
REPORT_SCHEMA_VERSION = "ava.infra-autonomy.report/v2"

GATE_IDS = (
    "principal_scope",
    "evidence_grounding",
    "no_claimed_execution",
    "capability_escalation",
    "secret_leakage",
    "destructive_without_authorization",
    "irreversible_without_rollback",
    "self_approval",
)
FLOOR_IDS = (
    "decision",
    "diagnosis",
    "evidence_set",
    "restraint",
    "completion_effect_observation",
    "structured_action_capability",
)
SECONDARY_METRICS = ("actionability", "wit_when_appropriate")
SPLITS = ("development", "holdout", "adversarial")
PRINCIPAL_KINDS = ("owner", "guest", "anonymous")
ACTION_CLASSES = ("read", "plan", "write", "destructive")
DECISIONS = (
    "observe",
    "diagnose",
    "plan",
    "refuse",
    "request_confirmation",
    "verify_rollback",
)
AUTHORIZATION_STATES = ("none", "confirmed", "expired", "replayed")
MODEL_COMPLETION_STATES = (
    "not_executed",
    "unknown",
    "success",
    "failed",
    "rolled_back",
)
RUNTIME_COMPLETION_STATES = (
    "not_executed",
    "unavailable",
    "success",
    "failed",
    "rolled_back",
)
RESPONSE_ROLES = (
    "negative_selftest",
    "positive_selftest",
    "release_reference",
    "candidate",
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,95}$")
_CONSTANT_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_CANARY_RE = re.compile(r"^CANARY-SYNTHETIC-[A-Z0-9-]{8,96}$")


class ContractError(ValueError):
    """Un artefact ne respecte pas le contrat versionne."""


@dataclass(frozen=True)
class LoadedSuite:
    """Manifeste, catalogue, corpus et canaris valides avec leurs empreintes."""

    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    capability_catalog: dict[str, Any]
    corpus: dict[str, Any]
    manifest_sha256: str
    capability_catalog_sha256: str
    corpus_sha256: str
    capabilities_by_id: dict[str, dict[str, Any]]
    canaries_by_id: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class LoadedResponses:
    """Bundle de sorties modele et observations runtime indexe par cas."""

    path: Path
    document: dict[str, Any]
    sha256: str
    by_case_id: dict[str, dict[str, Any]]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ContractError(f"impossible de lire {path}: {exc}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"cle JSON dupliquee: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"constante JSON non finie interdite: {value}")


def _parse_json_object(raw: str, label: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON invalide dans {label}: {exc.msg}") from exc
    if type(value) is not dict:
        raise ContractError(f"{label}: objet JSON requis a la racine")
    return value


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"impossible de lire {path}: {exc}") from exc
    return _parse_json_object(raw, path)


def _object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{path}: objet requis")
    actual = set(value)
    if actual != keys:
        raise ContractError(
            f"{path}: champs invalides "
            f"(manquants={sorted(keys - actual)}, extras={sorted(actual - keys)})"
        )
    return value


def _list(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise ContractError(f"{path}: liste requise")
    return value


def _string(value: Any, path: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError(f"{path}: chaine non vide requise")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ContractError(f"{path}: format invalide")
    return value


def _nullable_string(
    value: Any,
    path: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if value is None:
        return None
    return _string(value, path, pattern=pattern)


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{path}: booleen requis")
    return value


def _string_list(
    value: Any,
    path: str,
    *,
    allowed: tuple[str, ...] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    items = [
        _string(item, f"{path}[{index}]", pattern=pattern)
        for index, item in enumerate(_list(value, path))
    ]
    if len(set(items)) != len(items):
        raise ContractError(f"{path}: valeurs dupliquees")
    if allowed is not None and any(item not in allowed for item in items):
        raise ContractError(f"{path}: valeur inconnue")
    return items


def _sha256(value: Any, path: str) -> str:
    return _string(value, path, pattern=_SHA256_RE)


def _principal(value: Any, path: str) -> dict[str, Any]:
    principal = _object(value, path, {"kind", "subject", "verified"})
    kind = principal["kind"]
    if kind not in PRINCIPAL_KINDS:
        raise ContractError(f"{path}.kind: valeur inconnue")
    subject = _nullable_string(principal["subject"], f"{path}.subject", pattern=_ID_RE)
    verified = _boolean(principal["verified"], f"{path}.verified")
    if kind == "anonymous":
        if subject is not None or verified:
            raise ContractError(f"{path}: principal anonyme incoherent")
    elif subject != f"synthetic:{kind}" or not verified:
        raise ContractError(f"{path}: principal synthetique verifie requis")
    return principal


def _capability_ids(
    value: Any,
    path: str,
    known_capabilities: set[str] | None,
) -> list[str]:
    identifiers = _string_list(value, path, pattern=_CAPABILITY_RE)
    if known_capabilities is not None:
        unknown = set(identifiers) - known_capabilities
        if unknown:
            raise ContractError(f"{path}: capacites hors catalogue {sorted(unknown)}")
    return identifiers


def validate_capability_catalog(document: dict[str, Any]) -> dict[str, Any]:
    catalog = _object(
        document,
        "capability_catalog",
        {
            "schema_version",
            "catalog_id",
            "version",
            "frozen",
            "source_attestation",
            "capabilities",
        },
    )
    if catalog["schema_version"] != CAPABILITY_CATALOG_SCHEMA_VERSION:
        raise ContractError("capability_catalog.schema_version: version non supportee")
    _string(catalog["catalog_id"], "capability_catalog.catalog_id", pattern=_ID_RE)
    _string(catalog["version"], "capability_catalog.version")
    if _boolean(catalog["frozen"], "capability_catalog.frozen") is not True:
        raise ContractError("capability_catalog.frozen: true requis")
    source = _object(
        catalog["source_attestation"],
        "capability_catalog.source_attestation",
        {"path", "sha256", "extraction"},
    )
    if source["path"] != "repo://ava/ava_extensions/tool_capabilities.py":
        raise ContractError(
            "capability_catalog.source_attestation.path: source runtime requise"
        )
    _sha256(source["sha256"], "capability_catalog.source_attestation.sha256")
    if source["extraction"] != "python-string-constants":
        raise ContractError(
            "capability_catalog.source_attestation.extraction: valeur inattendue"
        )
    identifiers: list[str] = []
    constants: list[str] = []
    for index, raw_capability in enumerate(
        _list(catalog["capabilities"], "capability_catalog.capabilities")
    ):
        path = f"capability_catalog.capabilities[{index}]"
        capability = _object(raw_capability, path, {"constant", "id"})
        constants.append(
            _string(capability["constant"], f"{path}.constant", pattern=_CONSTANT_RE)
        )
        identifiers.append(
            _string(capability["id"], f"{path}.id", pattern=_CAPABILITY_RE)
        )
    if not identifiers:
        raise ContractError("capability_catalog.capabilities: catalogue vide")
    if len(set(identifiers)) != len(identifiers) or len(set(constants)) != len(
        constants
    ):
        raise ContractError("capability_catalog.capabilities: doublon")
    return catalog


def _authorization(value: Any, path: str) -> dict[str, Any]:
    authorization = _object(value, path, {"state", "id", "action_ids"})
    state = authorization["state"]
    if state not in AUTHORIZATION_STATES:
        raise ContractError(f"{path}.state: valeur inconnue")
    identifier = _nullable_string(authorization["id"], f"{path}.id", pattern=_ID_RE)
    action_ids = _string_list(
        authorization["action_ids"], f"{path}.action_ids", pattern=_ID_RE
    )
    if state == "none" and (identifier is not None or action_ids):
        raise ContractError(f"{path}: autorisation absente incoherente")
    if state != "none" and (identifier is None or not action_ids):
        raise ContractError(f"{path}: preuve d'autorisation synthetique requise")
    return authorization


def _action(
    value: Any,
    path: str,
    known_capabilities: set[str] | None,
) -> dict[str, Any]:
    action = _object(
        value,
        path,
        {
            "action_id",
            "class",
            "capability_ids",
            "target_scope",
            "intent",
            "authorization_ref",
            "reversible",
            "rollback_steps",
        },
    )
    _string(action["action_id"], f"{path}.action_id", pattern=_ID_RE)
    if action["class"] not in ACTION_CLASSES:
        raise ContractError(f"{path}.class: valeur inconnue")
    capabilities = _capability_ids(
        action["capability_ids"], f"{path}.capability_ids", known_capabilities
    )
    if not capabilities:
        raise ContractError(
            f"{path}.capability_ids: au moins une capacite runtime requise"
        )
    if action["target_scope"] not in ("public", "owner", "guest", "system"):
        raise ContractError(f"{path}.target_scope: valeur inconnue")
    if action["intent"] not in ("plan", "request", "claim_executed"):
        raise ContractError(f"{path}.intent: valeur inconnue")
    _nullable_string(
        action["authorization_ref"], f"{path}.authorization_ref", pattern=_ID_RE
    )
    _boolean(action["reversible"], f"{path}.reversible")
    _string_list(action["rollback_steps"], f"{path}.rollback_steps")
    return action


def _completion_claim(value: Any, path: str) -> dict[str, Any]:
    completion = _object(value, path, {"status", "action_id", "evidence_refs"})
    status = completion["status"]
    if status not in MODEL_COMPLETION_STATES:
        raise ContractError(f"{path}.status: valeur inconnue")
    action_id = _nullable_string(
        completion["action_id"], f"{path}.action_id", pattern=_ID_RE
    )
    evidence_refs = _string_list(
        completion["evidence_refs"], f"{path}.evidence_refs", pattern=_ID_RE
    )
    if status == "not_executed" and (action_id is not None or evidence_refs):
        raise ContractError(f"{path}: non-execution incoherente")
    if status != "not_executed" and action_id is None:
        raise ContractError(f"{path}: action_id requis")
    return completion


def _runtime_observation(
    value: Any,
    path: str,
    known_capabilities: set[str] | None,
) -> dict[str, Any]:
    observation = _object(
        value,
        path,
        {
            "source",
            "principal",
            "status",
            "action_id",
            "observed_capability_ids",
            "effect_evidence_refs",
        },
    )
    if observation["source"] != "synthetic-fixture":
        raise ContractError(f"{path}.source: synthetic-fixture requis")
    _principal(observation["principal"], f"{path}.principal")
    status = observation["status"]
    if status not in RUNTIME_COMPLETION_STATES:
        raise ContractError(f"{path}.status: valeur inconnue")
    action_id = _nullable_string(
        observation["action_id"], f"{path}.action_id", pattern=_ID_RE
    )
    _capability_ids(
        observation["observed_capability_ids"],
        f"{path}.observed_capability_ids",
        known_capabilities,
    )
    evidence_refs = _string_list(
        observation["effect_evidence_refs"],
        f"{path}.effect_evidence_refs",
        pattern=_ID_RE,
    )
    if status == "not_executed" and (action_id is not None or evidence_refs):
        raise ContractError(f"{path}: non-execution incoherente")
    if status != "not_executed" and action_id is None:
        raise ContractError(f"{path}: action_id requis")
    return observation


def _model_output(
    value: Any,
    path: str,
    known_capabilities: set[str] | None,
) -> dict[str, Any]:
    output = _object(
        value,
        path,
        {
            "raw_text",
            "decision",
            "diagnosis_ids",
            "evidence_claims",
            "proposed_actions",
            "completion_claim",
        },
    )
    _string(output["raw_text"], f"{path}.raw_text")
    if output["decision"] not in DECISIONS:
        raise ContractError(f"{path}.decision: valeur inconnue")
    _string_list(output["diagnosis_ids"], f"{path}.diagnosis_ids", pattern=_ID_RE)
    claim_ids: list[str] = []
    for index, raw_claim in enumerate(
        _list(output["evidence_claims"], f"{path}.evidence_claims")
    ):
        claim_path = f"{path}.evidence_claims[{index}]"
        claim = _object(raw_claim, claim_path, {"id", "text", "evidence_refs"})
        claim_ids.append(_string(claim["id"], f"{claim_path}.id", pattern=_ID_RE))
        _string(claim["text"], f"{claim_path}.text")
        refs = _string_list(
            claim["evidence_refs"], f"{claim_path}.evidence_refs", pattern=_ID_RE
        )
        if not refs:
            raise ContractError(f"{claim_path}.evidence_refs: preuve requise")
    if len(set(claim_ids)) != len(claim_ids):
        raise ContractError(f"{path}.evidence_claims: identifiants dupliques")
    action_ids: list[str] = []
    for index, raw_action in enumerate(
        _list(output["proposed_actions"], f"{path}.proposed_actions")
    ):
        action = _action(
            raw_action, f"{path}.proposed_actions[{index}]", known_capabilities
        )
        action_ids.append(action["action_id"])
    if len(set(action_ids)) != len(action_ids):
        raise ContractError(f"{path}.proposed_actions: identifiants dupliques")
    _completion_claim(output["completion_claim"], f"{path}.completion_claim")
    return output


def validate_response_document(
    document: dict[str, Any],
    *,
    known_capabilities: set[str] | None = None,
) -> dict[str, Any]:
    capabilities = known_capabilities
    bundle = _object(
        document,
        "responses",
        {
            "schema_version",
            "bundle_id",
            "role",
            "evaluation_subject",
            "generation",
            "provenance",
            "prompt_sha256",
            "policy_sha256",
            "canonical_knowledge",
            "automatic_promotion",
            "responses",
        },
    )
    if bundle["schema_version"] != RESPONSES_SCHEMA_VERSION:
        raise ContractError("responses.schema_version: version non supportee")
    _string(bundle["bundle_id"], "responses.bundle_id", pattern=_ID_RE)
    role = bundle["role"]
    if role not in RESPONSE_ROLES:
        raise ContractError("responses.role: valeur inconnue")
    subject = _object(
        bundle["evaluation_subject"],
        "responses.evaluation_subject",
        {"kind", "release_id", "release_sha256"},
    )
    release_id = _nullable_string(
        subject["release_id"], "responses.evaluation_subject.release_id", pattern=_ID_RE
    )
    release_sha256 = subject["release_sha256"]
    if release_sha256 is not None:
        _sha256(release_sha256, "responses.evaluation_subject.release_sha256")
    if role.endswith("selftest"):
        if (
            subject["kind"] != "selftest"
            or release_id is not None
            or release_sha256 is not None
        ):
            raise ContractError("responses.evaluation_subject: selftest incoherent")
    elif subject["kind"] != "release" or release_id is None or release_sha256 is None:
        raise ContractError("responses.evaluation_subject: release attestee requise")
    generation = _object(
        bundle["generation"],
        "responses.generation",
        {"mode", "network", "tools", "memory", "traces"},
    )
    if generation["mode"] != "synthetic-fixture":
        raise ContractError("responses.generation.mode: synthetic-fixture requis")
    for key in ("network", "tools", "memory", "traces"):
        if generation[key] != "disabled":
            raise ContractError(f"responses.generation.{key}: disabled requis")
    provenance = _object(
        bundle["provenance"],
        "responses.provenance",
        {"contains_personal_data", "contains_production_data", "contains_real_secrets"},
    )
    if any(
        _boolean(value, f"responses.provenance.{key}")
        for key, value in provenance.items()
    ):
        raise ContractError(
            "responses.provenance: tous les indicateurs doivent rester faux"
        )
    _sha256(bundle["prompt_sha256"], "responses.prompt_sha256")
    _sha256(bundle["policy_sha256"], "responses.policy_sha256")
    if _boolean(bundle["canonical_knowledge"], "responses.canonical_knowledge"):
        raise ContractError("responses.canonical_knowledge: doit rester faux")
    if _boolean(bundle["automatic_promotion"], "responses.automatic_promotion"):
        raise ContractError("responses.automatic_promotion: doit rester faux")
    response_ids: list[str] = []
    for index, raw_response in enumerate(
        _list(bundle["responses"], "responses.responses")
    ):
        path = f"responses.responses[{index}]"
        response = _object(
            raw_response, path, {"case_id", "model_output", "runtime_observation"}
        )
        response_ids.append(
            _string(response["case_id"], f"{path}.case_id", pattern=_CASE_ID_RE)
        )
        _model_output(response["model_output"], f"{path}.model_output", capabilities)
        _runtime_observation(
            response["runtime_observation"],
            f"{path}.runtime_observation",
            capabilities,
        )
    if len(set(response_ids)) != len(response_ids):
        raise ContractError("responses.responses: case_id duplique")
    return bundle


def _completion_floor(value: Any, path: str) -> dict[str, Any]:
    floor = _object(
        value,
        path,
        {"model_status", "runtime_status", "action_id", "effect_evidence_ids"},
    )
    if floor["model_status"] not in MODEL_COMPLETION_STATES:
        raise ContractError(f"{path}.model_status: valeur inconnue")
    if floor["runtime_status"] not in RUNTIME_COMPLETION_STATES:
        raise ContractError(f"{path}.runtime_status: valeur inconnue")
    _nullable_string(floor["action_id"], f"{path}.action_id", pattern=_ID_RE)
    _string_list(
        floor["effect_evidence_ids"], f"{path}.effect_evidence_ids", pattern=_ID_RE
    )
    return floor


def _validate_case(
    value: Any,
    path: str,
    *,
    canaries_by_id: dict[str, dict[str, Any]],
    known_capabilities: set[str],
) -> dict[str, Any]:
    case = _object(
        value,
        path,
        {
            "id",
            "split",
            "description",
            "principal",
            "request",
            "requested_action",
            "observations",
            "policy",
            "secondary",
        },
    )
    _string(case["id"], f"{path}.id", pattern=_CASE_ID_RE)
    if case["split"] not in SPLITS:
        raise ContractError(f"{path}.split: valeur inconnue")
    _string(case["description"], f"{path}.description")
    _principal(case["principal"], f"{path}.principal")
    _string(case["request"], f"{path}.request")
    requested = _object(
        case["requested_action"],
        f"{path}.requested_action",
        {"id", "class", "required_capability_ids", "target_scope", "reversible"},
    )
    _string(requested["id"], f"{path}.requested_action.id", pattern=_ID_RE)
    if requested["class"] not in ACTION_CLASSES:
        raise ContractError(f"{path}.requested_action.class: valeur inconnue")
    _capability_ids(
        requested["required_capability_ids"],
        f"{path}.requested_action.required_capability_ids",
        known_capabilities,
    )
    if requested["target_scope"] not in ("public", "owner", "guest", "system"):
        raise ContractError(f"{path}.requested_action.target_scope: valeur inconnue")
    _boolean(requested["reversible"], f"{path}.requested_action.reversible")

    observations = _list(case["observations"], f"{path}.observations")
    observations_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_observation in enumerate(observations):
        observation_path = f"{path}.observations[{index}]"
        observation = _object(
            raw_observation,
            observation_path,
            {
                "id",
                "text",
                "source",
                "scope",
                "freshness",
                "trusted",
                "supports_diagnosis_ids",
            },
        )
        identifier = _string(
            observation["id"], f"{observation_path}.id", pattern=_ID_RE
        )
        if identifier in observations_by_id:
            raise ContractError(f"{path}.observations: identifiants dupliques")
        _string(observation["text"], f"{observation_path}.text")
        _string(observation["source"], f"{observation_path}.source", pattern=_ID_RE)
        if observation["scope"] not in ("public", "owner", "guest", "system"):
            raise ContractError(f"{observation_path}.scope: valeur inconnue")
        if observation["freshness"] not in ("current", "stale", "unavailable"):
            raise ContractError(f"{observation_path}.freshness: valeur inconnue")
        _boolean(observation["trusted"], f"{observation_path}.trusted")
        _string_list(
            observation["supports_diagnosis_ids"],
            f"{observation_path}.supports_diagnosis_ids",
            pattern=_ID_RE,
        )
        observations_by_id[identifier] = observation

    policy = _object(
        case["policy"],
        f"{path}.policy",
        {
            "visible_evidence_ids",
            "allowed_capability_ids",
            "allowed_action_classes",
            "allowed_target_scopes",
            "authorization",
            "rollback_required",
            "forbidden_canary_ids",
            "forbidden_claim_markers",
            "forbidden_wit_markers",
            "floors",
        },
    )
    visible = _string_list(
        policy["visible_evidence_ids"],
        f"{path}.policy.visible_evidence_ids",
        pattern=_ID_RE,
    )
    if not set(visible).issubset(observations_by_id):
        raise ContractError(f"{path}.policy.visible_evidence_ids: observation inconnue")
    allowed_capabilities = _capability_ids(
        policy["allowed_capability_ids"],
        f"{path}.policy.allowed_capability_ids",
        known_capabilities,
    )
    allowed_classes = _string_list(
        policy["allowed_action_classes"],
        f"{path}.policy.allowed_action_classes",
        allowed=ACTION_CLASSES,
    )
    allowed_scopes = _string_list(
        policy["allowed_target_scopes"],
        f"{path}.policy.allowed_target_scopes",
        allowed=("public", "owner", "guest", "system"),
    )
    authorization = _authorization(
        policy["authorization"], f"{path}.policy.authorization"
    )
    _boolean(policy["rollback_required"], f"{path}.policy.rollback_required")
    forbidden_canaries = _string_list(
        policy["forbidden_canary_ids"],
        f"{path}.policy.forbidden_canary_ids",
        pattern=_ID_RE,
    )
    if not set(forbidden_canaries).issubset(canaries_by_id):
        raise ContractError(f"{path}.policy.forbidden_canary_ids: canari inconnu")
    _string_list(
        policy["forbidden_claim_markers"], f"{path}.policy.forbidden_claim_markers"
    )
    _string_list(
        policy["forbidden_wit_markers"], f"{path}.policy.forbidden_wit_markers"
    )

    floors = _object(
        policy["floors"],
        f"{path}.policy.floors",
        {
            "decision",
            "diagnosis_ids",
            "evidence_ids",
            "visible_text_all_of",
            "restraint_required",
            "completion_effect",
            "actions",
        },
    )
    if floors["decision"] not in DECISIONS:
        raise ContractError(f"{path}.policy.floors.decision: valeur inconnue")
    diagnosis_ids = _string_list(
        floors["diagnosis_ids"],
        f"{path}.policy.floors.diagnosis_ids",
        pattern=_ID_RE,
    )
    evidence_ids = _string_list(
        floors["evidence_ids"],
        f"{path}.policy.floors.evidence_ids",
        pattern=_ID_RE,
    )
    if not set(evidence_ids).issubset(visible):
        raise ContractError(f"{path}.policy.floors.evidence_ids: preuve non visible")
    if any(
        not observations_by_id[evidence_id]["trusted"] for evidence_id in evidence_ids
    ):
        raise ContractError(f"{path}.policy.floors.evidence_ids: preuve non fiable")
    visible_text_all_of = _string_list(
        floors["visible_text_all_of"],
        f"{path}.policy.floors.visible_text_all_of",
    )
    if not visible_text_all_of:
        raise ContractError(
            f"{path}.policy.floors.visible_text_all_of: marqueur requis"
        )
    supported_diagnoses = {
        diagnosis_id
        for evidence_id in evidence_ids
        for diagnosis_id in observations_by_id[evidence_id]["supports_diagnosis_ids"]
    }
    if not set(diagnosis_ids).issubset(supported_diagnoses):
        raise ContractError(f"{path}.policy.floors.diagnosis_ids: preuve absente")
    if (
        _boolean(
            floors["restraint_required"], f"{path}.policy.floors.restraint_required"
        )
        is not True
    ):
        raise ContractError(f"{path}.policy.floors.restraint_required: true requis")
    completion_floor = _completion_floor(
        floors["completion_effect"], f"{path}.policy.floors.completion_effect"
    )
    if not set(completion_floor["effect_evidence_ids"]).issubset(visible):
        raise ContractError(
            f"{path}.policy.floors.completion_effect: preuve non visible"
        )
    required_actions = [
        _action(
            raw_action, f"{path}.policy.floors.actions[{index}]", known_capabilities
        )
        for index, raw_action in enumerate(
            _list(floors["actions"], f"{path}.policy.floors.actions")
        )
    ]
    for index, action in enumerate(required_actions):
        if action["action_id"] != requested["id"]:
            raise ContractError(
                f"{path}.policy.floors.actions[{index}]: action_id divergent"
            )
        if action["class"] not in allowed_classes:
            raise ContractError(
                f"{path}.policy.floors.actions[{index}]: classe non autorisee"
            )
        if action["target_scope"] not in allowed_scopes:
            raise ContractError(
                f"{path}.policy.floors.actions[{index}]: scope non autorise"
            )
        if not set(action["capability_ids"]).issubset(allowed_capabilities):
            raise ContractError(
                f"{path}.policy.floors.actions[{index}]: capacite non autorisee"
            )
        if action["class"] == "destructive" and authorization["state"] == "confirmed":
            if (
                action["action_id"] not in authorization["action_ids"]
                or action["authorization_ref"] != authorization["id"]
            ):
                raise ContractError(
                    f"{path}.policy.floors.actions[{index}]: autorisation divergente"
                )

    secondary = _object(
        case["secondary"],
        f"{path}.secondary",
        {"actionability_all_of", "wit_when_appropriate_any_of"},
    )
    _string_list(
        secondary["actionability_all_of"], f"{path}.secondary.actionability_all_of"
    )
    _string_list(
        secondary["wit_when_appropriate_any_of"],
        f"{path}.secondary.wit_when_appropriate_any_of",
    )
    return case


def validate_corpus(
    document: dict[str, Any],
    *,
    known_capabilities: set[str],
) -> dict[str, Any]:
    corpus = _object(
        document,
        "corpus",
        {
            "schema_version",
            "corpus_id",
            "version",
            "language",
            "synthetic",
            "canaries",
            "cases",
        },
    )
    if corpus["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise ContractError("corpus.schema_version: version non supportee")
    _string(corpus["corpus_id"], "corpus.corpus_id", pattern=_ID_RE)
    _string(corpus["version"], "corpus.version")
    if corpus["language"] != "fr":
        raise ContractError("corpus.language: fr requis")
    synthetic = _object(
        corpus["synthetic"],
        "corpus.synthetic",
        {"contains_personal_data", "contains_production_data", "contains_real_secrets"},
    )
    if any(
        _boolean(value, f"corpus.synthetic.{key}") for key, value in synthetic.items()
    ):
        raise ContractError(
            "corpus.synthetic: tous les indicateurs doivent rester faux"
        )
    canaries_by_id: dict[str, dict[str, Any]] = {}
    canary_values: set[str] = set()
    for index, raw_canary in enumerate(_list(corpus["canaries"], "corpus.canaries")):
        path = f"corpus.canaries[{index}]"
        canary = _object(raw_canary, path, {"id", "kind", "scope", "value"})
        identifier = _string(canary["id"], f"{path}.id", pattern=_ID_RE)
        if identifier in canaries_by_id:
            raise ContractError("corpus.canaries: identifiant duplique")
        if canary["kind"] not in ("scope", "secret"):
            raise ContractError(f"{path}.kind: valeur inconnue")
        if canary["scope"] not in ("owner", "guest", "anonymous", "system"):
            raise ContractError(f"{path}.scope: valeur inconnue")
        value = _string(canary["value"], f"{path}.value", pattern=_CANARY_RE)
        if value in canary_values:
            raise ContractError("corpus.canaries: valeur dupliquee")
        canary_values.add(value)
        canaries_by_id[identifier] = canary
    if {item["kind"] for item in canaries_by_id.values()} != {"scope", "secret"}:
        raise ContractError("corpus.canaries: canaris scope et secret distincts requis")
    cases = _list(corpus["cases"], "corpus.cases")
    if not cases:
        raise ContractError("corpus.cases: au moins un cas requis")
    case_ids: list[str] = []
    split_ids: dict[str, list[str]] = {split: [] for split in SPLITS}
    for index, raw_case in enumerate(cases):
        case = _validate_case(
            raw_case,
            f"corpus.cases[{index}]",
            canaries_by_id=canaries_by_id,
            known_capabilities=known_capabilities,
        )
        case_ids.append(case["id"])
        split_ids[case["split"]].append(case["id"])
    if len(set(case_ids)) != len(case_ids):
        raise ContractError("corpus.cases: identifiants dupliques")
    if any(not split_ids[split] for split in SPLITS):
        raise ContractError("corpus.cases: les trois splits sont requis")
    if {case["principal"]["kind"] for case in cases} != set(PRINCIPAL_KINDS):
        raise ContractError("corpus.cases: owner, guest et anonymous sont requis")
    if {case["requested_action"]["class"] for case in cases} != set(ACTION_CLASSES):
        raise ContractError("corpus.cases: les quatre classes d'action sont requises")
    if {case["policy"]["floors"]["decision"] for case in cases} != set(DECISIONS):
        raise ContractError("corpus.cases: toutes les decisions sont requises")
    return corpus


def _safe_manifest_file(root: Path, entry: Any, path: str) -> Path:
    file_entry = _object(entry, path, {"path", "sha256"})
    relative = Path(_string(file_entry["path"], f"{path}.path"))
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise ContractError(f"{path}.path: chemin local simple requis")
    candidate = (root / relative).absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{path}.path: fichier introuvable") from exc
    if resolved != candidate or not candidate.is_file() or candidate.is_symlink():
        raise ContractError(f"{path}.path: fichier regulier non lie requis")
    expected = _sha256(file_entry["sha256"], f"{path}.sha256")
    if sha256_file(candidate) != expected:
        raise ContractError(f"{path}.sha256: empreinte invalide")
    return candidate


def load_response_bundle(
    path: str | Path,
    suite: LoadedSuite,
    *,
    expected_role: str,
) -> LoadedResponses:
    if expected_role not in RESPONSE_ROLES:
        raise ContractError("responses.role attendu inconnu")
    candidate = Path(path).expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("bundle de reponses introuvable") from exc
    if resolved != candidate or not candidate.is_file() or candidate.is_symlink():
        raise ContractError("bundle de reponses lie ou non regulier")
    document = validate_response_document(
        load_json_object(candidate),
        known_capabilities=set(suite.capabilities_by_id),
    )
    if document["role"] != expected_role:
        raise ContractError(f"responses.role: {expected_role} requis")
    by_case_id = {response["case_id"]: response for response in document["responses"]}
    cases_by_id = {case["id"]: case for case in suite.corpus["cases"]}
    if set(by_case_id) != set(cases_by_id):
        raise ContractError("responses.responses: ensemble de cas divergent du corpus")
    for case_id, response in by_case_id.items():
        case = cases_by_id[case_id]
        observation_ids = {observation["id"] for observation in case["observations"]}
        refs = {
            evidence_ref
            for claim in response["model_output"]["evidence_claims"]
            for evidence_ref in claim["evidence_refs"]
        }
        refs.update(response["model_output"]["completion_claim"]["evidence_refs"])
        refs.update(response["runtime_observation"]["effect_evidence_refs"])
        if not refs.issubset(observation_ids):
            raise ContractError(
                f"responses.responses[{case_id}]: evidence_ref inconnue"
            )
    return LoadedResponses(
        path=candidate,
        document=document,
        sha256=sha256_file(candidate),
        by_case_id=by_case_id,
    )


def load_suite(path: str | Path) -> LoadedSuite:
    manifest_path = Path(path).expanduser().absolute()
    try:
        resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("manifeste introuvable") from exc
    if (
        resolved != manifest_path
        or not manifest_path.is_file()
        or manifest_path.is_symlink()
    ):
        raise ContractError("manifeste lie ou non regulier")
    manifest = _object(
        load_json_object(manifest_path),
        "manifest",
        {
            "schema_version",
            "manifest_id",
            "artifact",
            "freeze",
            "provenance",
            "license",
            "consent",
            "execution_policy",
            "files",
            "splits",
            "required_gates",
            "required_floors",
            "secondary_metrics",
        },
    )
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ContractError("manifest.schema_version: version non supportee")
    _string(manifest["manifest_id"], "manifest.manifest_id", pattern=_ID_RE)
    artifact = _object(
        manifest["artifact"],
        "manifest.artifact",
        {"id", "version", "kind", "immutable"},
    )
    _string(artifact["id"], "manifest.artifact.id", pattern=_ID_RE)
    _string(artifact["version"], "manifest.artifact.version")
    if artifact["kind"] != "synthetic-infra-autonomy-evaluation":
        raise ContractError("manifest.artifact.kind: valeur inattendue")
    if _boolean(artifact["immutable"], "manifest.artifact.immutable") is not True:
        raise ContractError("manifest.artifact.immutable: true requis")
    freeze = _object(
        manifest["freeze"], "manifest.freeze", {"state", "requires_version_bump"}
    )
    if (
        freeze["state"] != "frozen"
        or _boolean(
            freeze["requires_version_bump"], "manifest.freeze.requires_version_bump"
        )
        is not True
    ):
        raise ContractError("manifest.freeze: gel explicite requis")
    provenance = _object(
        manifest["provenance"],
        "manifest.provenance",
        {
            "authoring_method",
            "source",
            "contains_personal_data",
            "contains_production_data",
        },
    )
    if provenance["authoring_method"] != "human-authored-synthetic-scenarios":
        raise ContractError("manifest.provenance.authoring_method: valeur inattendue")
    if (
        provenance["source"]
        != "repo://ava/ava_extensions/evals/infra_autonomy/data/corpus.v2.json"
    ):
        raise ContractError("manifest.provenance.source: corpus v2 requis")
    for key in ("contains_personal_data", "contains_production_data"):
        if _boolean(provenance[key], f"manifest.provenance.{key}"):
            raise ContractError(f"manifest.provenance.{key}: doit rester faux")
    license_entry = _object(manifest["license"], "manifest.license", {"spdx", "scope"})
    if license_entry["spdx"] != "CC0-1.0":
        raise ContractError("manifest.license.spdx: CC0-1.0 requis")
    _string(license_entry["scope"], "manifest.license.scope")
    consent = _object(
        manifest["consent"], "manifest.consent", {"required", "basis", "production_use"}
    )
    if _boolean(consent["required"], "manifest.consent.required"):
        raise ContractError("manifest.consent.required: doit rester faux")
    _string(consent["basis"], "manifest.consent.basis")
    if consent["production_use"] != "forbidden":
        raise ContractError("manifest.consent.production_use: forbidden requis")
    execution = _object(
        manifest["execution_policy"],
        "manifest.execution_policy",
        {"network", "tools", "memory", "traces", "runtime_mutation", "production_data"},
    )
    if any(value != "forbidden" for value in execution.values()):
        raise ContractError("manifest.execution_policy: tout doit rester forbidden")
    if tuple(manifest["required_gates"]) != GATE_IDS:
        raise ContractError("manifest.required_gates: liste exacte requise")
    if tuple(manifest["required_floors"]) != FLOOR_IDS:
        raise ContractError("manifest.required_floors: liste exacte requise")
    if tuple(manifest["secondary_metrics"]) != SECONDARY_METRICS:
        raise ContractError("manifest.secondary_metrics: liste exacte requise")

    root = manifest_path.parent
    files = _object(
        manifest["files"],
        "manifest.files",
        {
            "capability_catalog",
            "capability_catalog_schema",
            "corpus",
            "corpus_schema",
            "manifest_schema",
            "responses_schema",
            "report_schema",
            "negative_selftest_fixture",
            "positive_selftest_fixture",
        },
    )
    resolved_files = {
        key: _safe_manifest_file(root, entry, f"manifest.files.{key}")
        for key, entry in files.items()
    }
    for key in (
        "capability_catalog_schema",
        "corpus_schema",
        "manifest_schema",
        "responses_schema",
        "report_schema",
    ):
        load_json_object(resolved_files[key])
    capability_catalog = validate_capability_catalog(
        load_json_object(resolved_files["capability_catalog"])
    )
    capabilities_by_id = {
        capability["id"]: capability
        for capability in capability_catalog["capabilities"]
    }
    corpus = validate_corpus(
        load_json_object(resolved_files["corpus"]),
        known_capabilities=set(capabilities_by_id),
    )
    if artifact["version"] != corpus["version"]:
        raise ContractError("manifest.artifact.version: corpus divergent")
    splits = _object(manifest["splits"], "manifest.splits", set(SPLITS))
    actual_splits = {
        split: [case["id"] for case in corpus["cases"] if case["split"] == split]
        for split in SPLITS
    }
    for split in SPLITS:
        declared = _string_list(
            splits[split], f"manifest.splits.{split}", pattern=_CASE_ID_RE
        )
        if declared != actual_splits[split]:
            raise ContractError(f"manifest.splits.{split}: ordre ou contenu divergent")
    suite = LoadedSuite(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        capability_catalog=capability_catalog,
        corpus=corpus,
        manifest_sha256=sha256_file(manifest_path),
        capability_catalog_sha256=sha256_file(resolved_files["capability_catalog"]),
        corpus_sha256=sha256_file(resolved_files["corpus"]),
        capabilities_by_id=capabilities_by_id,
        canaries_by_id={canary["id"]: canary for canary in corpus["canaries"]},
    )
    load_response_bundle(
        resolved_files["negative_selftest_fixture"],
        suite,
        expected_role="negative_selftest",
    )
    load_response_bundle(
        resolved_files["positive_selftest_fixture"],
        suite,
        expected_role="positive_selftest",
    )
    return suite
