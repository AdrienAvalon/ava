"""Contrats stricts du banc relationnel hors ligne.

La validation est volontairement implementee avec la bibliotheque standard. Les
schemas JSON livres a cote servent de contrat inter-outils, tandis que ces controles
restent disponibles meme dans l'image Ava minimale, sans dependance ni reseau.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORPUS_SCHEMA_VERSION = "ava.relationship.corpus/v1"
MANIFEST_SCHEMA_VERSION = "ava.relationship.manifest/v1"
RESPONSES_SCHEMA_VERSION = "ava.relationship.responses/v1"
REPORT_SCHEMA_VERSION = "ava.relationship.report/v1"
RELEASE_ATTESTATION_SCHEMA_VERSION = "ava.release.attestation/v1"
ADJUDICATION_SCHEMA_VERSION = "ava.relationship.adjudication/v1"
EXTERNAL_ANCHOR_SCHEMA_VERSION = "ava.relationship.external-anchor/v1"
ANCHOR_KEY_SCHEMA_VERSION = "ava.relationship.anchor-key/v1"

GATE_IDS = (
    "scope_leakage",
    "wrong_principal",
    "deceptive_humanity",
    "deceptive_emotion",
    "jealousy",
    "guilt",
    "exclusivity",
    "isolation",
    "dependency",
    "coercion",
    "self_promotion",
    "conversation_echo",
    "tool_escalation",
    "false_memory",
)
SECONDARY_METRICS = ("warmth", "wit", "continuity", "accuracy")
SPLITS = ("development", "holdout", "adversarial")

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_RELEASE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_MAX_EVIDENCE_BYTES = 128 * 1024
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ContractError(ValueError):
    """Une entree ne respecte pas un contrat versionne."""


@dataclass(frozen=True)
class LoadedSuite:
    """Manifeste et corpus verifies, avec leurs empreintes brutes."""

    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    corpus: dict[str, Any]
    manifest_sha256: str
    corpus_sha256: str


@dataclass(frozen=True)
class LoadedResponses:
    """Jeu de reponses hors ligne valide et indexe par cas."""

    path: Path
    document: dict[str, Any]
    sha256: str
    by_case_id: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class LoadedReleaseAttestation:
    """Attestation immutable qui lie le shadow a une release et un moteur."""

    path: Path
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class LoadedAdjudication:
    """Decision humaine ou independante sur un statement deterministe."""

    path: Path
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class LoadedExternalAnchor:
    """Recu externe append-only qui ancre les deux adjudications."""

    path: Path
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class ReviewEvidence:
    """Triplet indivisible requis avant toute eligibilite de promotion."""

    human: LoadedAdjudication
    independent: LoadedAdjudication
    anchor: LoadedExternalAnchor


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ContractError(f"impossible de lire {path}: {exc}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


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
        raise ContractError(f"{label}: un objet JSON est requis a la racine")
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
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ContractError(
            f"{path}: champs invalides (manquants={missing}, extras={extra})"
        )
    return value


def _string(value: Any, path: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError(f"{path}: chaine non vide requise")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ContractError(f"{path}: format invalide")
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{path}: booleen requis")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise ContractError(f"{path}: liste requise")
    return value


def _string_list(value: Any, path: str, *, unique: bool = True) -> list[str]:
    values = _list(value, path)
    result = [_string(item, f"{path}[{index}]") for index, item in enumerate(values)]
    if unique and len(set(result)) != len(result):
        raise ContractError(f"{path}: valeurs dupliquees")
    return result


def _sha256(value: Any, path: str) -> str:
    return _string(value, path, pattern=_SHA256_RE)


def _strict_external_document(
    path: str | Path, *, expected_sha256: str
) -> tuple[Path, dict[str, Any], str]:
    """Read one bounded, regular, non-symlinked document and pin its bytes.

    The expected digest is deliberately mandatory.  Merely naming a mutable
    JSON file must never be enough to attribute a release or a review.
    """

    expected = _sha256(expected_sha256, "expected_sha256")
    candidate = Path(path).expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"document externe introuvable: {candidate}") from exc
    if resolved != candidate:
        raise ContractError("document externe lie ou indirect")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractError("impossible d'ouvrir le document externe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_EVIDENCE_BYTES
        ):
            raise ContractError("document externe non regulier ou hors taille")
        chunks: list[bytes] = []
        remaining = _MAX_EVIDENCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        try:
            current = candidate.lstat()
        except OSError as exc:
            raise ContractError("document externe remplace pendant sa lecture") from exc
        if (
            len(payload) != metadata.st_size
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise ContractError("document externe modifie pendant sa lecture")
    except OSError as exc:
        raise ContractError("impossible de lire le document externe") from exc
    finally:
        os.close(descriptor)
    actual = sha256_bytes(payload)
    if actual != expected:
        raise ContractError("empreinte du document externe invalide")
    try:
        raw = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ContractError("document externe non UTF-8") from exc
    return candidate, _parse_json_object(raw, candidate), actual


def _release_engine(value: Any, path: str) -> dict[str, Any]:
    engine = _object(
        value,
        path,
        {"provider", "model", "revision", "adapter", "config_sha256"},
    )
    for key in ("provider", "model", "revision", "adapter"):
        _string(engine[key], f"{path}.{key}", pattern=_SAFE_RELEASE_VALUE_RE)
    _sha256(engine["config_sha256"], f"{path}.config_sha256")
    return engine


def _base64url(value: Any, path: str, *, expected_size: int) -> bytes:
    encoded = _string(value, path, pattern=_BASE64URL_RE)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise ContractError(f"{path}: base64url invalide") from exc
    if len(decoded) != expected_size:
        raise ContractError(f"{path}: taille invalide")
    return decoded


def load_release_attestation(
    path: str | Path,
    *,
    expected_sha256: str,
) -> LoadedReleaseAttestation:
    """Load the only authority for release, adapter and model metadata."""

    resolved, document, digest = _strict_external_document(
        path, expected_sha256=expected_sha256
    )
    attestation = _object(
        document,
        "release_attestation",
        {
            "schema_version",
            "attestation_id",
            "release",
            "engine",
            "artifact",
            "canonical_knowledge",
        },
    )
    if attestation["schema_version"] != RELEASE_ATTESTATION_SCHEMA_VERSION:
        raise ContractError("release_attestation.schema_version: version non supportee")
    _string(
        attestation["attestation_id"],
        "release_attestation.attestation_id",
        pattern=_ID_RE,
    )
    release = _object(
        attestation["release"],
        "release_attestation.release",
        {"repository", "git_sha"},
    )
    repository = _string(
        release["repository"],
        "release_attestation.release.repository",
        pattern=_SAFE_RELEASE_VALUE_RE,
    )
    if not repository.startswith("repo:"):
        raise ContractError("release_attestation.release.repository: repo attendu")
    git_sha = _string(
        release["git_sha"],
        "release_attestation.release.git_sha",
        pattern=_GIT_SHA_RE,
    )
    if len(set(git_sha)) == 1:
        raise ContractError("release_attestation.release.git_sha: placeholder interdit")
    _release_engine(attestation["engine"], "release_attestation.engine")
    artifact = _object(
        attestation["artifact"],
        "release_attestation.artifact",
        {"manifest_sha256"},
    )
    _sha256(
        artifact["manifest_sha256"],
        "release_attestation.artifact.manifest_sha256",
    )
    if _boolean(
        attestation["canonical_knowledge"],
        "release_attestation.canonical_knowledge",
    ):
        raise ContractError("release_attestation.canonical_knowledge: doit rester faux")
    return LoadedReleaseAttestation(path=resolved, document=attestation, sha256=digest)


def _load_adjudication(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_kind: str,
    statement_sha256: str,
) -> LoadedAdjudication:
    resolved, document, digest = _strict_external_document(
        path, expected_sha256=expected_sha256
    )
    adjudication = _object(
        document,
        "adjudication",
        {
            "schema_version",
            "attestation_id",
            "kind",
            "statement_sha256",
            "reviewer",
            "decision",
            "checks",
            "canonical_knowledge",
        },
    )
    if adjudication["schema_version"] != ADJUDICATION_SCHEMA_VERSION:
        raise ContractError("adjudication.schema_version: version non supportee")
    _string(
        adjudication["attestation_id"], "adjudication.attestation_id", pattern=_ID_RE
    )
    if adjudication["kind"] != expected_kind:
        raise ContractError(f"adjudication.kind: {expected_kind} requis")
    if (
        _sha256(adjudication["statement_sha256"], "adjudication.statement_sha256")
        != statement_sha256
    ):
        raise ContractError("adjudication.statement_sha256: statement divergent")
    reviewer = _object(
        adjudication["reviewer"],
        "adjudication.reviewer",
        {"id", "independent_from_model_author"},
    )
    _string(reviewer["id"], "adjudication.reviewer.id", pattern=_ID_RE)
    if (
        _boolean(
            reviewer["independent_from_model_author"],
            "adjudication.reviewer.independent_from_model_author",
        )
        is not True
    ):
        raise ContractError("adjudication.reviewer: independance requise")
    if adjudication["decision"] != "pass":
        raise ContractError("adjudication.decision: pass requis")
    checks = _object(
        adjudication["checks"],
        "adjudication.checks",
        {"semantic_safety", "principal_isolation", "memory_isolation", "rollback"},
    )
    if any(
        _boolean(value, f"adjudication.checks.{key}") is not True
        for key, value in checks.items()
    ):
        raise ContractError("adjudication.checks: toutes les preuves sont requises")
    if _boolean(
        adjudication["canonical_knowledge"], "adjudication.canonical_knowledge"
    ):
        raise ContractError("adjudication.canonical_knowledge: doit rester faux")
    return LoadedAdjudication(path=resolved, document=adjudication, sha256=digest)


def load_review_evidence(
    *,
    statement_sha256: str,
    human_path: str | Path,
    human_sha256: str,
    independent_path: str | Path,
    independent_sha256: str,
    anchor_path: str | Path,
    anchor_sha256: str,
    anchor_public_key_path: str | Path,
    anchor_public_key_sha256: str,
) -> ReviewEvidence:
    """Load two independent decisions plus an out-of-process immutable anchor."""

    statement = _sha256(statement_sha256, "statement_sha256")
    human = _load_adjudication(
        human_path,
        expected_sha256=human_sha256,
        expected_kind="human",
        statement_sha256=statement,
    )
    independent = _load_adjudication(
        independent_path,
        expected_sha256=independent_sha256,
        expected_kind="independent",
        statement_sha256=statement,
    )
    if human.document["reviewer"]["id"] == independent.document["reviewer"]["id"]:
        raise ContractError("adjudications: deux reviewers distincts sont requis")

    key_path, key_document, key_digest = _strict_external_document(
        anchor_public_key_path,
        expected_sha256=anchor_public_key_sha256,
    )
    del key_path
    anchor_key = _object(
        key_document,
        "anchor_key",
        {
            "schema_version",
            "key_id",
            "algorithm",
            "public_key_base64",
            "canonical_knowledge",
        },
    )
    if anchor_key["schema_version"] != ANCHOR_KEY_SCHEMA_VERSION:
        raise ContractError("anchor_key.schema_version: version non supportee")
    _string(anchor_key["key_id"], "anchor_key.key_id", pattern=_ID_RE)
    if anchor_key["algorithm"] != "Ed25519":
        raise ContractError("anchor_key.algorithm: Ed25519 requis")
    public_key_bytes = _base64url(
        anchor_key["public_key_base64"],
        "anchor_key.public_key_base64",
        expected_size=32,
    )
    if _boolean(anchor_key["canonical_knowledge"], "anchor_key.canonical_knowledge"):
        raise ContractError("anchor_key.canonical_knowledge: doit rester faux")

    resolved, document, digest = _strict_external_document(
        anchor_path, expected_sha256=anchor_sha256
    )
    anchor = _object(
        document,
        "external_anchor",
        {
            "schema_version",
            "anchor_id",
            "backend",
            "statement_sha256",
            "human_adjudication_sha256",
            "independent_adjudication_sha256",
            "receipt_sha256",
            "signing_key_id",
            "signing_key_sha256",
            "signature_base64",
            "immutable",
            "outside_ava_process",
            "canonical_knowledge",
        },
    )
    if anchor["schema_version"] != EXTERNAL_ANCHOR_SCHEMA_VERSION:
        raise ContractError("external_anchor.schema_version: version non supportee")
    _string(anchor["anchor_id"], "external_anchor.anchor_id", pattern=_ID_RE)
    _string(
        anchor["backend"], "external_anchor.backend", pattern=_SAFE_RELEASE_VALUE_RE
    )
    if anchor["signing_key_id"] != anchor_key["key_id"]:
        raise ContractError("external_anchor.signing_key_id: cle divergente")
    if (
        _sha256(anchor["signing_key_sha256"], "external_anchor.signing_key_sha256")
        != key_digest
    ):
        raise ContractError("external_anchor.signing_key_sha256: cle divergente")
    expected_links = {
        "statement_sha256": statement,
        "human_adjudication_sha256": human.sha256,
        "independent_adjudication_sha256": independent.sha256,
    }
    for key, expected in expected_links.items():
        if _sha256(anchor[key], f"external_anchor.{key}") != expected:
            raise ContractError(f"external_anchor.{key}: preuve divergente")
    _sha256(anchor["receipt_sha256"], "external_anchor.receipt_sha256")
    for key in ("immutable", "outside_ava_process"):
        if _boolean(anchor[key], f"external_anchor.{key}") is not True:
            raise ContractError(f"external_anchor.{key}: doit rester vrai")
    if _boolean(anchor["canonical_knowledge"], "external_anchor.canonical_knowledge"):
        raise ContractError("external_anchor.canonical_knowledge: doit rester faux")
    signature = _base64url(
        anchor["signature_base64"],
        "external_anchor.signature_base64",
        expected_size=64,
    )
    signed_document = dict(anchor)
    signed_document.pop("signature_base64")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            canonical_json_bytes(signed_document),
        )
    except Exception as exc:
        raise ContractError(
            "external_anchor.signature_base64: signature invalide"
        ) from exc
    return ReviewEvidence(
        human=human,
        independent=independent,
        anchor=LoadedExternalAnchor(path=resolved, document=anchor, sha256=digest),
    )


def _validate_fact(value: Any, path: str) -> dict[str, Any]:
    fact = _object(value, path, {"id", "text"})
    _string(fact["id"], f"{path}.id", pattern=_ID_RE)
    _string(fact["text"], f"{path}.text")
    return fact


def _validate_case(value: Any, path: str) -> dict[str, Any]:
    case = _object(
        value,
        path,
        {
            "id",
            "split",
            "description",
            "principal",
            "conversation",
            "policy",
            "secondary",
        },
    )
    _string(case["id"], f"{path}.id", pattern=_CASE_ID_RE)
    if case["split"] not in SPLITS:
        raise ContractError(f"{path}.split: valeur inconnue")
    _string(case["description"], f"{path}.description")

    principal = _object(
        case["principal"],
        f"{path}.principal",
        {"request_subject", "verified", "relationship_subject", "relationship_opt_in"},
    )
    request_subject = _string(
        principal["request_subject"],
        f"{path}.principal.request_subject",
        pattern=_ID_RE,
    )
    if not request_subject.startswith("synthetic:"):
        raise ContractError(
            f"{path}.principal.request_subject: principal synthetique requis"
        )
    _boolean(principal["verified"], f"{path}.principal.verified")
    relationship_subject = principal["relationship_subject"]
    if relationship_subject is not None:
        relationship_subject = _string(
            relationship_subject,
            f"{path}.principal.relationship_subject",
            pattern=_ID_RE,
        )
        if not relationship_subject.startswith("synthetic:"):
            raise ContractError(
                f"{path}.principal.relationship_subject: principal synthetique requis"
            )
    opt_in = _boolean(
        principal["relationship_opt_in"], f"{path}.principal.relationship_opt_in"
    )
    if opt_in and relationship_subject is None:
        raise ContractError(f"{path}.principal: opt-in sans sujet relationnel")

    turns = _list(case["conversation"], f"{path}.conversation")
    if not turns:
        raise ContractError(f"{path}.conversation: au moins un tour requis")
    for index, raw_turn in enumerate(turns):
        turn_path = f"{path}.conversation[{index}]"
        turn = _object(raw_turn, turn_path, {"role", "content"})
        if turn["role"] not in {"user", "assistant"}:
            raise ContractError(f"{turn_path}.role: role inconnu")
        _string(turn["content"], f"{turn_path}.content")
    if turns[-1]["role"] != "user":
        raise ContractError(
            f"{path}.conversation: le dernier tour doit venir de l'utilisateur"
        )

    raw_policy = case["policy"]
    if type(raw_policy) is not dict:
        raise ContractError(f"{path}.policy: objet requis")
    policy_with_defaults = dict(raw_policy)
    policy_with_defaults.setdefault("allowed_exact_echo_turn_indexes", [])
    policy = _object(
        policy_with_defaults,
        f"{path}.policy",
        {
            "allowed_tools",
            "visible_facts",
            "forbidden_canaries",
            "allowed_memory_claim_ids",
            "allowed_exact_echo_turn_indexes",
        },
    )
    _string_list(policy["allowed_tools"], f"{path}.policy.allowed_tools")
    facts = _list(policy["visible_facts"], f"{path}.policy.visible_facts")
    validated_facts = [
        _validate_fact(item, f"{path}.policy.visible_facts[{index}]")
        for index, item in enumerate(facts)
    ]
    fact_ids = [fact["id"] for fact in validated_facts]
    if len(set(fact_ids)) != len(fact_ids):
        raise ContractError(f"{path}.policy.visible_facts: identifiants dupliques")
    claim_ids = _string_list(
        policy["allowed_memory_claim_ids"], f"{path}.policy.allowed_memory_claim_ids"
    )
    if not set(claim_ids).issubset(fact_ids):
        raise ContractError(f"{path}.policy.allowed_memory_claim_ids: fait non visible")
    canaries = _string_list(
        policy["forbidden_canaries"], f"{path}.policy.forbidden_canaries"
    )
    if any(len(canary) < 12 for canary in canaries):
        raise ContractError(f"{path}.policy.forbidden_canaries: canari trop court")
    visible_text = "\n".join(
        [turn["content"] for turn in turns] + [fact["text"] for fact in validated_facts]
    ).casefold()
    if any(canary.casefold() in visible_text for canary in canaries):
        raise ContractError(
            f"{path}.policy.forbidden_canaries: canari deja visible dans le cas"
        )
    echo_indexes = _list(
        policy["allowed_exact_echo_turn_indexes"],
        f"{path}.policy.allowed_exact_echo_turn_indexes",
    )
    if any(type(index) is not int or index < 0 for index in echo_indexes):
        raise ContractError(
            f"{path}.policy.allowed_exact_echo_turn_indexes: "
            "indices entiers positifs requis"
        )
    if len(set(echo_indexes)) != len(echo_indexes):
        raise ContractError(
            f"{path}.policy.allowed_exact_echo_turn_indexes: indices dupliques"
        )
    if any(index >= len(turns) - 1 for index in echo_indexes):
        raise ContractError(
            f"{path}.policy.allowed_exact_echo_turn_indexes: "
            "seul un tour anterieur peut etre autorise"
        )
    exact_request_markers = (
        "cite exactement",
        "citez exactement",
        "repete exactement",
        "répète exactement",
        "recopie exactement",
        "citation exacte",
        "mot pour mot",
        "verbatim",
    )
    if echo_indexes and not any(
        marker in turns[-1]["content"].casefold() for marker in exact_request_markers
    ):
        raise ContractError(
            f"{path}.policy.allowed_exact_echo_turn_indexes: "
            "demande explicite de citation exacte requise"
        )

    secondary_path = f"{path}.secondary"
    secondary = case["secondary"]
    required_secondary = {
        "warmth_any_of",
        "wit_any_of",
        "continuity_all_of",
        "accuracy_all_of",
    }
    optional_secondary = {
        "continuity_any_of_groups",
        "accuracy_any_of_groups",
        "required_secondary",
    }
    if type(secondary) is not dict:
        raise ContractError(f"{secondary_path}: objet requis")
    secondary_keys = set(secondary)
    if not required_secondary.issubset(secondary_keys) or not secondary_keys.issubset(
        required_secondary | optional_secondary
    ):
        missing = sorted(required_secondary - secondary_keys)
        extra = sorted(secondary_keys - required_secondary - optional_secondary)
        raise ContractError(
            f"{secondary_path}: champs invalides (manquants={missing}, extras={extra})"
        )
    for key in (
        "warmth_any_of",
        "wit_any_of",
        "continuity_all_of",
        "accuracy_all_of",
    ):
        _string_list(secondary[key], f"{secondary_path}.{key}")
    grouped_metrics: set[str] = set()
    for metric in ("continuity", "accuracy"):
        groups_key = f"{metric}_any_of_groups"
        groups = _list(
            secondary.get(groups_key, []),
            f"{secondary_path}.{groups_key}",
        )
        validated_groups: list[tuple[str, ...]] = []
        for index, group in enumerate(groups):
            alternatives = _string_list(
                group,
                f"{secondary_path}.{groups_key}[{index}]",
            )
            if not alternatives:
                raise ContractError(
                    f"{secondary_path}.{groups_key}[{index}]: "
                    "au moins une alternative requise"
                )
            validated_groups.append(tuple(alternatives))
        if len(set(validated_groups)) != len(validated_groups):
            raise ContractError(f"{secondary_path}.{groups_key}: groupes dupliques")
        if groups and secondary[f"{metric}_all_of"]:
            raise ContractError(
                f"{secondary_path}: {metric}_all_of et {groups_key} "
                "ne peuvent pas etre combines"
            )
        if groups:
            grouped_metrics.add(metric)
    required_metrics = _string_list(
        secondary.get("required_secondary", []),
        f"{secondary_path}.required_secondary",
    )
    unknown_required_metrics = sorted(set(required_metrics) - set(SECONDARY_METRICS))
    if unknown_required_metrics:
        raise ContractError(
            f"{secondary_path}.required_secondary: metriques inconnues "
            f"{unknown_required_metrics}"
        )
    configured_metrics = {
        metric
        for metric, phrases in (
            ("warmth", secondary["warmth_any_of"]),
            ("wit", secondary["wit_any_of"]),
            ("continuity", secondary["continuity_all_of"]),
            ("accuracy", secondary["accuracy_all_of"]),
        )
        if phrases
    } | grouped_metrics
    missing_criteria = sorted(set(required_metrics) - configured_metrics)
    if missing_criteria:
        raise ContractError(
            f"{secondary_path}.required_secondary: critere non vide requis pour "
            f"{missing_criteria}"
        )
    return case


def validate_corpus(document: dict[str, Any]) -> dict[str, Any]:
    corpus = _object(
        document,
        "corpus",
        {
            "schema_version",
            "corpus_id",
            "version",
            "language",
            "synthetic_only",
            "cases",
        },
    )
    if corpus["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise ContractError("corpus.schema_version: version non supportee")
    _string(corpus["corpus_id"], "corpus.corpus_id", pattern=_ID_RE)
    _string(corpus["version"], "corpus.version")
    if corpus["language"] != "fr":
        raise ContractError(
            "corpus.language: seul le corpus francais versionne est accepte"
        )
    if _boolean(corpus["synthetic_only"], "corpus.synthetic_only") is not True:
        raise ContractError("corpus.synthetic_only: doit rester vrai")
    cases = _list(corpus["cases"], "corpus.cases")
    if not cases:
        raise ContractError("corpus.cases: corpus vide")
    validated = [
        _validate_case(case, f"corpus.cases[{index}]")
        for index, case in enumerate(cases)
    ]
    case_ids = [case["id"] for case in validated]
    if len(set(case_ids)) != len(case_ids):
        raise ContractError("corpus.cases: identifiants dupliques")
    present_splits = {case["split"] for case in validated}
    if present_splits != set(SPLITS):
        raise ContractError(
            f"corpus.cases: splits incomplets ({sorted(present_splits)})"
        )
    return corpus


def _safe_referenced_file(root: Path, relative: Any, path: str) -> Path:
    relative_text = _string(relative, path)
    relative_path = Path(relative_text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ContractError(f"{path}: chemin hors manifeste interdit")
    candidate = root / relative_path
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{path}: fichier reference introuvable") from exc
    if not resolved.is_relative_to(resolved_root):
        raise ContractError(f"{path}: fichier reference hors manifeste")
    if resolved != candidate.absolute():
        raise ContractError(f"{path}: liens symboliques interdits")
    if not resolved.is_file():
        raise ContractError(f"{path}: fichier ordinaire requis")
    return resolved


def _validate_schema_header(path: Path, expected_id: str) -> None:
    schema = load_json_object(path)
    required = {
        "$schema",
        "$id",
        "title",
        "type",
        "additionalProperties",
        "required",
        "properties",
    }
    if not required.issubset(schema):
        raise ContractError(f"{path}: en-tete de schema incomplet")
    if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        raise ContractError(f"{path}: draft JSON Schema inattendu")
    if schema["$id"] != expected_id or schema["type"] != "object":
        raise ContractError(f"{path}: identite de schema inattendue")
    if schema["additionalProperties"] is not False:
        raise ContractError(f"{path}: le schema racine doit etre strict")


def load_suite(manifest_path: str | Path | None = None) -> LoadedSuite:
    if manifest_path is None:
        manifest_path = Path(__file__).with_name("data") / "manifest.v1.json"
    path = Path(manifest_path).expanduser().absolute()
    manifest = load_json_object(path)
    manifest = _object(
        manifest,
        "manifest",
        {
            "schema_version",
            "manifest_id",
            "artifact",
            "provenance",
            "license",
            "consent",
            "files",
            "splits",
            "required_gates",
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
    if artifact["kind"] != "synthetic-evaluation-corpus":
        raise ContractError("manifest.artifact.kind: type interdit")
    if _boolean(artifact["immutable"], "manifest.artifact.immutable") is not True:
        raise ContractError("manifest.artifact.immutable: doit rester vrai")

    provenance = _object(
        manifest["provenance"],
        "manifest.provenance",
        {
            "authoring_method",
            "source",
            "contains_personal_data",
            "contains_production_conversations",
        },
    )
    if provenance["authoring_method"] != "human-authored-synthetic-scenarios":
        raise ContractError("manifest.provenance.authoring_method: methode interdite")
    _string(provenance["source"], "manifest.provenance.source")
    if _boolean(
        provenance["contains_personal_data"],
        "manifest.provenance.contains_personal_data",
    ):
        raise ContractError("manifest.provenance: donnees personnelles interdites")
    if _boolean(
        provenance["contains_production_conversations"],
        "manifest.provenance.contains_production_conversations",
    ):
        raise ContractError(
            "manifest.provenance: conversations de production interdites"
        )

    license_info = _object(manifest["license"], "manifest.license", {"spdx", "scope"})
    if license_info["spdx"] != "CC0-1.0":
        raise ContractError("manifest.license.spdx: licence inattendue")
    _string(license_info["scope"], "manifest.license.scope")
    consent = _object(
        manifest["consent"],
        "manifest.consent",
        {"required", "basis", "personal_conversation_use"},
    )
    if _boolean(consent["required"], "manifest.consent.required"):
        raise ContractError(
            "manifest.consent.required: le corpus doit rester purement synthetique"
        )
    _string(consent["basis"], "manifest.consent.basis")
    if consent["personal_conversation_use"] != "forbidden":
        raise ContractError(
            "manifest.consent.personal_conversation_use: doit rester interdit"
        )

    expected_files = {
        "corpus",
        "corpus_schema",
        "manifest_schema",
        "responses_schema",
        "report_schema",
        "release_attestation_schema",
        "adjudication_schema",
        "external_anchor_schema",
        "anchor_key_schema",
        "baseline_fixture",
        "candidate_fixture",
    }
    files = _object(manifest["files"], "manifest.files", expected_files)
    root = path.parent
    resolved_files: dict[str, Path] = {}
    for name in sorted(expected_files):
        reference = _object(files[name], f"manifest.files.{name}", {"path", "sha256"})
        referenced = _safe_referenced_file(
            root, reference["path"], f"manifest.files.{name}.path"
        )
        expected_digest = _sha256(reference["sha256"], f"manifest.files.{name}.sha256")
        actual_digest = sha256_file(referenced)
        if actual_digest != expected_digest:
            raise ContractError(f"manifest.files.{name}: empreinte invalide")
        resolved_files[name] = referenced

    _validate_schema_header(
        resolved_files["corpus_schema"], "urn:ava:relationship:corpus:v1"
    )
    _validate_schema_header(
        resolved_files["manifest_schema"], "urn:ava:relationship:manifest:v1"
    )
    _validate_schema_header(
        resolved_files["responses_schema"], "urn:ava:relationship:responses:v1"
    )
    _validate_schema_header(
        resolved_files["report_schema"], "urn:ava:relationship:report:v1"
    )
    _validate_schema_header(
        resolved_files["release_attestation_schema"],
        "urn:ava:release:attestation:v1",
    )
    _validate_schema_header(
        resolved_files["adjudication_schema"],
        "urn:ava:relationship:adjudication:v1",
    )
    _validate_schema_header(
        resolved_files["external_anchor_schema"],
        "urn:ava:relationship:external-anchor:v1",
    )
    _validate_schema_header(
        resolved_files["anchor_key_schema"],
        "urn:ava:relationship:anchor-key:v1",
    )

    corpus = validate_corpus(load_json_object(resolved_files["corpus"]))
    if (
        corpus["corpus_id"] != artifact["id"]
        or corpus["version"] != artifact["version"]
    ):
        raise ContractError("manifest.artifact: identite differente du corpus")

    splits = _object(manifest["splits"], "manifest.splits", set(SPLITS))
    seen_manifest_ids: list[str] = []
    for split in SPLITS:
        split_ids = _string_list(splits[split], f"manifest.splits.{split}")
        expected_ids = [
            case["id"] for case in corpus["cases"] if case["split"] == split
        ]
        if split_ids != expected_ids:
            raise ContractError(
                f"manifest.splits.{split}: ordre ou contenu divergent du corpus"
            )
        seen_manifest_ids.extend(split_ids)
    if len(set(seen_manifest_ids)) != len(seen_manifest_ids):
        raise ContractError("manifest.splits: cas present dans plusieurs splits")
    if manifest["required_gates"] != list(GATE_IDS):
        raise ContractError("manifest.required_gates: gates incomplets ou reordonnes")
    if manifest["secondary_metrics"] != list(SECONDARY_METRICS):
        raise ContractError("manifest.secondary_metrics: metriques inattendues")

    suite = LoadedSuite(
        root=root,
        manifest_path=path,
        manifest=manifest,
        corpus=corpus,
        manifest_sha256=sha256_file(path),
        corpus_sha256=sha256_file(resolved_files["corpus"]),
    )
    load_response_bundle(
        resolved_files["baseline_fixture"], suite, expected_role="baseline"
    )
    load_response_bundle(
        resolved_files["candidate_fixture"], suite, expected_role="candidate"
    )
    return suite


def load_response_bundle(
    response_path: str | Path,
    suite: LoadedSuite,
    *,
    expected_role: str,
) -> LoadedResponses:
    if expected_role not in {"baseline", "candidate"}:
        raise ContractError("role de comparaison interne invalide")
    path = Path(response_path).expanduser().absolute()
    document = load_json_object(path)
    bundle = _object(
        document, "responses", {"schema_version", "corpus", "artifact", "responses"}
    )
    if bundle["schema_version"] != RESPONSES_SCHEMA_VERSION:
        raise ContractError("responses.schema_version: version non supportee")
    corpus_ref = _object(bundle["corpus"], "responses.corpus", {"id", "version"})
    if corpus_ref != {
        "id": suite.corpus["corpus_id"],
        "version": suite.corpus["version"],
    }:
        raise ContractError("responses.corpus: corpus incompatible")

    artifact = _object(
        bundle["artifact"],
        "responses.artifact",
        {
            "id",
            "role",
            "source_kind",
            "generated_by",
            "engine",
            "prompt_sha256",
            "policy_sha256",
            "release_attestation_sha256",
            "release",
            "contains_personal_data",
            "contains_production_conversations",
            "canonical_knowledge",
        },
    )
    _string(artifact["id"], "responses.artifact.id", pattern=_ID_RE)
    if artifact["role"] != expected_role:
        raise ContractError(f"responses.artifact.role: {expected_role} requis")
    if artifact["source_kind"] not in {"synthetic_fixture", "offline_shadow"}:
        raise ContractError("responses.artifact.source_kind: origine interdite")
    _string(artifact["generated_by"], "responses.artifact.generated_by")
    engine = _object(
        artifact["engine"],
        "responses.artifact.engine",
        {"provider", "model", "revision"},
    )
    for key in ("provider", "model", "revision"):
        _string(engine[key], f"responses.artifact.engine.{key}")
    _sha256(artifact["prompt_sha256"], "responses.artifact.prompt_sha256")
    _sha256(artifact["policy_sha256"], "responses.artifact.policy_sha256")
    release_digest = artifact["release_attestation_sha256"]
    release = artifact["release"]
    if artifact["source_kind"] == "offline_shadow":
        _sha256(release_digest, "responses.artifact.release_attestation_sha256")
        release = _object(
            release,
            "responses.artifact.release",
            {"repository", "git_sha", "adapter", "config_sha256", "manifest_sha256"},
        )
        repository = _string(
            release["repository"],
            "responses.artifact.release.repository",
            pattern=_SAFE_RELEASE_VALUE_RE,
        )
        if not repository.startswith("repo:"):
            raise ContractError("responses.artifact.release.repository: repo attendu")
        git_sha = _string(
            release["git_sha"],
            "responses.artifact.release.git_sha",
            pattern=_GIT_SHA_RE,
        )
        if len(set(git_sha)) == 1:
            raise ContractError(
                "responses.artifact.release.git_sha: placeholder interdit"
            )
        _string(
            release["adapter"],
            "responses.artifact.release.adapter",
            pattern=_SAFE_RELEASE_VALUE_RE,
        )
        _sha256(release["config_sha256"], "responses.artifact.release.config_sha256")
        _sha256(
            release["manifest_sha256"],
            "responses.artifact.release.manifest_sha256",
        )
    elif release_digest is not None or release is not None:
        raise ContractError(
            "responses.artifact.release: interdit pour une fixture synthetique"
        )
    for key in (
        "contains_personal_data",
        "contains_production_conversations",
        "canonical_knowledge",
    ):
        if _boolean(artifact[key], f"responses.artifact.{key}"):
            raise ContractError(f"responses.artifact.{key}: doit rester faux")

    raw_responses = _list(bundle["responses"], "responses.responses")
    by_case_id: dict[str, dict[str, Any]] = {}
    for index, raw_response in enumerate(raw_responses):
        response_path_label = f"responses.responses[{index}]"
        response = _object(
            raw_response,
            response_path_label,
            {"case_id", "text", "applied_profile", "tool_calls", "memory_claims"},
        )
        case_id = _string(
            response["case_id"], f"{response_path_label}.case_id", pattern=_CASE_ID_RE
        )
        if case_id in by_case_id:
            raise ContractError(f"{response_path_label}.case_id: reponse dupliquee")
        text = _string(response["text"], f"{response_path_label}.text")
        if len(text) > 24000:
            raise ContractError(f"{response_path_label}.text: reponse trop longue")
        profile = response["applied_profile"]
        if profile is not None:
            profile = _object(
                profile, f"{response_path_label}.applied_profile", {"id", "subject"}
            )
            _string(
                profile["id"],
                f"{response_path_label}.applied_profile.id",
                pattern=_ID_RE,
            )
            subject = _string(
                profile["subject"],
                f"{response_path_label}.applied_profile.subject",
                pattern=_ID_RE,
            )
            if not subject.startswith("synthetic:"):
                label = f"{response_path_label}.applied_profile.subject"
                raise ContractError(f"{label}: sujet synthetique requis")
        tool_calls = _list(response["tool_calls"], f"{response_path_label}.tool_calls")
        for tool_index, raw_tool in enumerate(tool_calls):
            tool = _object(
                raw_tool, f"{response_path_label}.tool_calls[{tool_index}]", {"name"}
            )
            _string(
                tool["name"],
                f"{response_path_label}.tool_calls[{tool_index}].name",
                pattern=_ID_RE,
            )
        claims = _list(
            response["memory_claims"], f"{response_path_label}.memory_claims"
        )
        for claim_index, raw_claim in enumerate(claims):
            claim_path = f"{response_path_label}.memory_claims[{claim_index}]"
            claim = _object(raw_claim, claim_path, {"fact_id", "text"})
            _string(claim["fact_id"], f"{claim_path}.fact_id", pattern=_ID_RE)
            _string(claim["text"], f"{claim_path}.text")
        by_case_id[case_id] = response

    expected_case_ids = [case["id"] for case in suite.corpus["cases"]]
    if set(by_case_id) != set(expected_case_ids):
        missing = sorted(set(expected_case_ids) - set(by_case_id))
        extra = sorted(set(by_case_id) - set(expected_case_ids))
        raise ContractError(
            "responses.responses: couverture invalide "
            f"(manquants={missing}, extras={extra})"
        )
    return LoadedResponses(
        path=path,
        document=bundle,
        sha256=sha256_file(path),
        by_case_id=by_case_id,
    )
