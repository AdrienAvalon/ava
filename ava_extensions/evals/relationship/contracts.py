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

from ava_extensions.identity.relationship_safety import (
    RELATIONSHIP_REPAIR_REPLACEMENT_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    RUNTIME_GUARD_GATE_IDS,
    SAFE_RELATIONSHIP_REPLACEMENT_ID,
    relationship_text_safety_policy_sha256,
)

CORPUS_SCHEMA_VERSION_V1 = "ava.relationship.corpus/v1"
CORPUS_SCHEMA_VERSION_V2 = "ava.relationship.corpus/v2"
CORPUS_SCHEMA_VERSION = "ava.relationship.corpus/v3"
MANIFEST_SCHEMA_VERSION_V1 = "ava.relationship.manifest/v1"
MANIFEST_SCHEMA_VERSION_V2 = "ava.relationship.manifest/v2"
MANIFEST_SCHEMA_VERSION = "ava.relationship.manifest/v3"
RESPONSES_SCHEMA_VERSION_V1 = "ava.relationship.responses/v1"
RESPONSES_SCHEMA_VERSION_V2 = "ava.relationship.responses/v2"
RESPONSES_SCHEMA_VERSION = "ava.relationship.responses/v3"
REPORT_SCHEMA_VERSION_V1 = "ava.relationship.report/v1"
REPORT_SCHEMA_VERSION_V2 = "ava.relationship.report/v2"
REPORT_SCHEMA_VERSION = "ava.relationship.report/v3"
RELEASE_ATTESTATION_SCHEMA_VERSION_V1 = "ava.release.attestation/v1"
RELEASE_ATTESTATION_SCHEMA_VERSION = "ava.release.attestation/v2"
CAUSAL_PAIR_SCHEMA_VERSION = "ava.relationship.causal-pair/v1"
ADJUDICATION_SCHEMA_VERSION_V1 = "ava.relationship.adjudication/v1"
ADJUDICATION_SCHEMA_VERSION = "ava.relationship.adjudication/v2"
QUALITY_RUBRIC_SCHEMA_VERSION_V1 = "ava.relationship.quality-rubric/v1"
QUALITY_RUBRIC_SCHEMA_VERSION = "ava.relationship.quality-rubric/v2"
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
AUTHORING_CLASSES = (
    "inherited_v1_6_1",
    "predecessor_diagnostics_only",
    "sealed_holdout_v17",
)
HISTORICAL_V2_SAFETY_POLICY_VERSION = "1.6.2"
HISTORICAL_V2_SAFETY_POLICY_SHA256 = (
    "sha256:5b9fb91be79e401cde7d03a095d0766217b84a3f6d1de007eb4be3001700c2fb"
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_RELEASE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_PINNED_CONTAINER_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,190}@sha256:[0-9a-f]{64}$"
)
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_CLAUDE_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9._+-]{1,120}$")
_WHEEL_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$")
_MAX_LOCAL_CONTRACT_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_RELEASE_ATTESTATION_BYTES = 256 * 1024
_MAX_CAUSAL_PAIR_BYTES = 256 * 1024
_MAX_ADJUDICATION_BYTES = 1024 * 1024
_MAX_ANCHOR_KEY_BYTES = 64 * 1024
_MAX_EXTERNAL_ANCHOR_BYTES = 256 * 1024
_MAX_RESPONSE_BUNDLE_BYTES = 4 * 1024 * 1024
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TREATMENT_MODULE_PATH = "ava_extensions/identity/relationship_guard_treatment.py"
_BASELINE_TREATMENT = "shadow-baseline-only-v1"
_CANDIDATE_TREATMENT = "runtime-enforced-v1"
_DEPLOYMENT_STATES = {"prepared_noncurrent", "active_current"}
_BINDING_CONSTRUCTION_TOKEN = object()
_REVIEW_EVIDENCE_CONSTRUCTION_TOKEN = object()
_RUST_BUILDER_DOCKERFILE_PATH = "deploy/docker/Dockerfile.rust-builder"
_RUST_BUILDER_RECIPE_KEYS = (
    "attestation_type",
    "signature",
    "builder_dockerfile_sha256",
    "builder_platform",
    "builder_python_image",
    "builder_rust_image",
    "python_version",
    "rust_version",
    "maturin_version",
    "wheel_compatibility",
)


class ContractError(ValueError):
    """Une entree ne respecte pas un contrat versionne."""


def rust_builder_recipe_sha256(builder: dict[str, Any]) -> str:
    """Hash the commit-independent builder recipe and toolchain pins.

    The authoritative pair generator first recalculates
    ``builder_dockerfile_sha256`` from the exact Dockerfile bytes in each
    source archive.  Keeping the fixed path and digest in this canonical
    projection lets later offline consumers rebind the pair to both release
    attestations without accepting a caller-provided recipe digest.
    """

    if (
        type(builder) is not dict
        or set(builder) != {*_RUST_BUILDER_RECIPE_KEYS, "builder_image_id"}
        or builder["attestation_type"] != "unsigned-checksum-manifest"
        or builder["signature"] != "none"
        or type(builder["builder_dockerfile_sha256"]) is not str
        or _SHA256_RE.fullmatch(builder["builder_dockerfile_sha256"]) is None
        or builder["builder_platform"] != "linux/amd64"
        or type(builder["builder_python_image"]) is not str
        or _PINNED_CONTAINER_IMAGE_RE.fullmatch(builder["builder_python_image"]) is None
        or type(builder["builder_rust_image"]) is not str
        or _PINNED_CONTAINER_IMAGE_RE.fullmatch(builder["builder_rust_image"]) is None
        or any(
            type(builder[key]) is not str or _SEMVER_RE.fullmatch(builder[key]) is None
            for key in ("python_version", "rust_version", "maturin_version")
        )
        or type(builder["wheel_compatibility"]) is not str
        or re.fullmatch(
            r"manylinux_[0-9]+_[0-9]+_x86_64", builder["wheel_compatibility"]
        )
        is None
    ):
        raise ContractError("rust_builder: recette incomplete")
    recipe = {
        "dockerfile": {
            "path": _RUST_BUILDER_DOCKERFILE_PATH,
            "sha256": builder["builder_dockerfile_sha256"],
        },
        "format": "ava.rust.builder-recipe/v1",
        "toolchain": {
            key: builder[key]
            for key in _RUST_BUILDER_RECIPE_KEYS
            if key != "builder_dockerfile_sha256"
        },
    }
    return sha256_bytes(canonical_json_bytes(recipe))


def _frontend_artifact(value: Any, path: str) -> dict[str, Any]:
    frontend = _object(
        value,
        path,
        {
            "build_attestation_sha256",
            "archive_sha256",
            "archive_map_sha256",
            "source_map_sha256",
            "builder_recipe_sha256",
        },
    )
    for key in frontend:
        _sha256(frontend[key], f"{path}.{key}")
    return frontend


def python_runtime_sha256(runtime: dict[str, Any]) -> str:
    """Validate and hash the sealed no-site Python runtime projection."""

    expected_keys = {
        "format",
        "implementation",
        "python_version",
        "executable_path",
        "executable_sha256",
        "runtime_source_path",
        "runtime_source_sha256",
        "runtime_archive_path",
        "runtime_archive_sha256",
        "runtime_archive_map_sha256",
        "python_install_path",
        "python_install_map_sha256",
        "python_install_entry_count",
        "python_install_total_size",
        "pyvenv_path",
        "pyvenv_contract_sha256",
        "stdlib_path",
        "stdlib_map_sha256",
        "stdlib_entry_count",
        "stdlib_total_size",
        "site_packages_path",
        "site_packages_map_sha256",
        "site_packages_entry_count",
        "site_packages_total_size",
        "lock_path",
        "lock_sha256",
        "requirements_path",
        "requirements_sha256",
        "requirements_set_sha256",
        "wheelhouse_manifest_path",
        "wheelhouse_manifest_sha256",
        "wheelhouse_path",
        "wheelhouse_map_sha256",
        "critical_imports",
        "removed_pth_count",
        "removed_pth_set_sha256",
        "distribution_set_sha256",
        "installer",
        "site_initialization",
        "bytecode_allowed",
    }
    if type(runtime) is not dict or set(runtime) != expected_keys:
        raise ContractError("python_runtime: champs incomplets")
    if (
        runtime["format"] != "ava.python-runtime/v1"
        or runtime["implementation"] != "cpython"
        or type(runtime["python_version"]) is not str
        or _SEMVER_RE.fullmatch(runtime["python_version"]) is None
        or runtime["executable_path"] != ".venv/bin/python"
        or runtime["runtime_source_path"] != "deploy/runtime/ava-python-runtime.v1.json"
        or runtime["runtime_archive_path"] != ".ava-artifacts/python-runtime.tar.gz"
        or runtime["python_install_path"] != ".python"
        or runtime["pyvenv_path"] != ".venv/pyvenv.cfg"
        or runtime["lock_path"] != "uv.lock"
        or runtime["requirements_path"]
        != "deploy/runtime/ava-runtime-requirements.v1.txt"
        or runtime["wheelhouse_manifest_path"]
        != "deploy/runtime/ava-runtime-wheelhouse.v1.json"
        or runtime["wheelhouse_path"] != ".ava-artifacts/python-wheelhouse"
        or runtime["site_initialization"] is not False
        or runtime["bytecode_allowed"] is not False
    ):
        raise ContractError("python_runtime: identite no-site invalide")
    major, minor, _patch = runtime["python_version"].split(".")
    if runtime["stdlib_path"] != f".python/lib/python{major}.{minor}":
        raise ContractError("python_runtime.stdlib_path: chemin divergent")
    if runtime["site_packages_path"] != (
        f".venv/lib/python{major}.{minor}/site-packages"
    ):
        raise ContractError("python_runtime.site_packages_path: chemin divergent")
    for key in (
        "stdlib_path",
        "site_packages_path",
        "runtime_source_path",
        "runtime_archive_path",
        "python_install_path",
        "requirements_path",
        "wheelhouse_manifest_path",
        "wheelhouse_path",
    ):
        value = runtime[key]
        if (
            type(value) is not str
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
            or "\\" in value
        ):
            raise ContractError(f"python_runtime.{key}: chemin invalide")
    for key in (
        "executable_sha256",
        "runtime_source_sha256",
        "runtime_archive_sha256",
        "runtime_archive_map_sha256",
        "python_install_map_sha256",
        "pyvenv_contract_sha256",
        "stdlib_map_sha256",
        "site_packages_map_sha256",
        "lock_sha256",
        "requirements_sha256",
        "requirements_set_sha256",
        "wheelhouse_manifest_sha256",
        "wheelhouse_map_sha256",
        "removed_pth_set_sha256",
        "distribution_set_sha256",
    ):
        _sha256(runtime[key], f"python_runtime.{key}")
    if runtime["runtime_archive_map_sha256"] != runtime["python_install_map_sha256"]:
        raise ContractError("python_runtime: archive et installation divergent")
    critical_imports = runtime["critical_imports"]
    expected_imports = {
        "anthropic": "anthropic/__init__.py",
        "cryptography": "cryptography/__init__.py",
        "httpcore": "httpcore/__init__.py",
        "httpx": "httpx/__init__.py",
    }
    if type(critical_imports) is not dict or set(critical_imports) != set(
        expected_imports
    ):
        raise ContractError("python_runtime.critical_imports: modules incomplets")
    for module_name, expected_path in expected_imports.items():
        imported = critical_imports[module_name]
        if (
            type(imported) is not dict
            or set(imported) != {"path", "sha256"}
            or imported["path"] != expected_path
        ):
            raise ContractError(
                f"python_runtime.critical_imports.{module_name}: chemin invalide"
            )
        _sha256(
            imported["sha256"],
            f"python_runtime.critical_imports.{module_name}.sha256",
        )
    for key in (
        "stdlib_entry_count",
        "stdlib_total_size",
        "python_install_entry_count",
        "python_install_total_size",
        "site_packages_entry_count",
        "site_packages_total_size",
    ):
        value = runtime[key]
        if type(value) is not int or value <= 0:
            raise ContractError(f"python_runtime.{key}: entier positif requis")
    if (
        type(runtime["removed_pth_count"]) is not int
        or runtime["removed_pth_count"] < 0
    ):
        raise ContractError("python_runtime.removed_pth_count: entier positif requis")
    installer = runtime["installer"]
    if (
        type(installer) is not dict
        or set(installer) != {"path", "sha256", "version", "arguments"}
        or installer["path"] != "/usr/local/bin/ava-uv"
        or type(installer["version"]) is not str
        or _SEMVER_RE.fullmatch(installer["version"]) is None
        or installer["arguments"]
        != [
            "--no-config",
            "pip",
            "sync",
            "<release>/deploy/runtime/ava-runtime-requirements.v1.txt",
            "--python=<release>/.venv/bin/python",
            "--require-hashes",
            "--no-build",
            "--offline",
            "--link-mode=copy",
            "--no-index",
            "--no-cache",
            "--find-links=<release>/.ava-artifacts/python-wheelhouse",
        ]
    ):
        raise ContractError("python_runtime.installer: contrat uv invalide")
    _sha256(installer["sha256"], "python_runtime.installer.sha256")
    return sha256_bytes(canonical_json_bytes(runtime))


@dataclass(frozen=True)
class LoadedSuite:
    """Manifeste et corpus verifies, avec leurs empreintes brutes."""

    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    corpus: dict[str, Any]
    quality_rubric: dict[str, Any] | None
    manifest_sha256: str
    corpus_sha256: str
    quality_rubric_sha256: str | None
    safety_policy_sha256: str | None


@dataclass(frozen=True)
class LoadedResponses:
    """Jeu de reponses hors ligne valide et indexe par cas."""

    path: Path
    document: dict[str, Any]
    sha256: str
    by_case_id: dict[str, dict[str, Any]]
    release_attestation: LoadedReleaseAttestation | None
    causal_pair: LoadedCausalPair | None = None
    peer_release_attestation: LoadedReleaseAttestation | None = None


@dataclass(frozen=True)
class LoadedReleaseAttestation:
    """Attestation immutable qui lie le shadow a une release et un moteur."""

    path: Path
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class LoadedCausalPair:
    """Preuve externe immuable qui relie les deux releases du shadow causal."""

    path: Path
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True, init=False)
class LoadedCausalShadowBinding:
    """Binding non forgeable par CLI derive des attestations et du causal pair."""

    release_attestation: LoadedReleaseAttestation
    peer_release_attestation: LoadedReleaseAttestation
    causal_pair: LoadedCausalPair
    role: str
    treatment: str
    deployment_state: str
    release_git_sha: str
    treatment_module_sha256: str
    evaluation_manifest_sha256: str

    def __init__(
        self,
        construction_token: object,
        *,
        release_attestation: LoadedReleaseAttestation,
        peer_release_attestation: LoadedReleaseAttestation,
        causal_pair: LoadedCausalPair,
        role: str,
        treatment: str,
        deployment_state: str,
        release_git_sha: str,
        treatment_module_sha256: str,
        evaluation_manifest_sha256: str,
    ) -> None:
        if construction_token is not _BINDING_CONSTRUCTION_TOKEN:
            raise ContractError("binding causal reserve au chargeur strict")
        object.__setattr__(self, "release_attestation", release_attestation)
        object.__setattr__(self, "peer_release_attestation", peer_release_attestation)
        object.__setattr__(self, "causal_pair", causal_pair)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "treatment", treatment)
        object.__setattr__(self, "deployment_state", deployment_state)
        object.__setattr__(self, "release_git_sha", release_git_sha)
        object.__setattr__(self, "treatment_module_sha256", treatment_module_sha256)
        object.__setattr__(
            self, "evaluation_manifest_sha256", evaluation_manifest_sha256
        )


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


@dataclass(frozen=True, slots=True, init=False)
class ReviewEvidence:
    """Triplet indivisible requis avant toute eligibilite de promotion."""

    human: LoadedAdjudication
    independent: LoadedAdjudication
    anchor: LoadedExternalAnchor
    anchor_public_key_path: Path
    anchor_public_key_sha256: str

    def __init__(
        self,
        construction_token: object,
        *,
        human: LoadedAdjudication,
        independent: LoadedAdjudication,
        anchor: LoadedExternalAnchor,
        anchor_public_key_path: Path,
        anchor_public_key_sha256: str,
    ) -> None:
        if construction_token is not _REVIEW_EVIDENCE_CONSTRUCTION_TOKEN:
            raise ContractError("review evidence reserve au chargeur strict")
        object.__setattr__(self, "human", human)
        object.__setattr__(self, "independent", independent)
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "anchor_public_key_path", anchor_public_key_path)
        object.__setattr__(self, "anchor_public_key_sha256", anchor_public_key_sha256)


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


def _strict_regular_payload(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Read one direct regular file once while detecting replacement races."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ContractError(f"{label}: borne invalide")
    candidate = path.expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{label}: fichier introuvable") from exc
    if resolved != candidate:
        raise ContractError(f"{label}: fichier lie ou indirect")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractError(f"{label}: fichier illisible") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise ContractError(f"{label}: fichier non regulier ou hors taille")
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
            or after.st_mode != before.st_mode
            or after.st_size != before.st_size
        ):
            raise ContractError(f"{label}: fichier modifie pendant la lecture")
        return payload
    except OSError as exc:
        raise ContractError(f"{label}: fichier illisible") from exc
    finally:
        os.close(descriptor)


def _json_object_from_payload(payload: bytes, label: str | Path) -> dict[str, Any]:
    try:
        raw = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ContractError(f"JSON non UTF-8 dans {label}") from exc
    return _parse_json_object(raw, label)


def load_json_object(
    path: Path, *, max_bytes: int = _MAX_LOCAL_CONTRACT_BYTES
) -> dict[str, Any]:
    payload = _strict_regular_payload(path, max_bytes=max_bytes, label="JSON")
    return _json_object_from_payload(payload, path)


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
    path: str | Path,
    *,
    expected_sha256: str,
    max_bytes: int,
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
            or metadata.st_size > max_bytes
        ):
            raise ContractError("document externe non regulier ou hors taille")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
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
        path,
        expected_sha256=expected_sha256,
        max_bytes=_MAX_RELEASE_ATTESTATION_BYTES,
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
    schema_version = attestation["schema_version"]
    if schema_version not in {
        RELEASE_ATTESTATION_SCHEMA_VERSION_V1,
        RELEASE_ATTESTATION_SCHEMA_VERSION,
    }:
        raise ContractError("release_attestation.schema_version: version non supportee")
    _string(
        attestation["attestation_id"],
        "release_attestation.attestation_id",
        pattern=_ID_RE,
    )
    release_keys = {"repository", "git_sha"}
    if schema_version == RELEASE_ATTESTATION_SCHEMA_VERSION:
        release_keys.update(
            {
                "deployment_state",
                "treatment",
                "treatment_module_path",
                "treatment_module_sha256",
            }
        )
    release = _object(
        attestation["release"], "release_attestation.release", release_keys
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
    if schema_version == RELEASE_ATTESTATION_SCHEMA_VERSION:
        if release["deployment_state"] not in _DEPLOYMENT_STATES:
            raise ContractError(
                "release_attestation.release.deployment_state: etat inconnu"
            )
        treatment = release["treatment"]
        expected_state = {
            _BASELINE_TREATMENT: "prepared_noncurrent",
            _CANDIDATE_TREATMENT: "active_current",
        }.get(treatment)
        if expected_state is None or release["deployment_state"] != expected_state:
            raise ContractError(
                "release_attestation.release: traitement et etat divergents"
            )
        if release["treatment_module_path"] != _TREATMENT_MODULE_PATH:
            raise ContractError(
                "release_attestation.release.treatment_module_path: chemin inattendu"
            )
        _sha256(
            release["treatment_module_sha256"],
            "release_attestation.release.treatment_module_sha256",
        )
    engine = _release_engine(attestation["engine"], "release_attestation.engine")
    if schema_version == RELEASE_ATTESTATION_SCHEMA_VERSION:
        if repository != "repo://ava":
            raise ContractError(
                "release_attestation.release.repository: repo Ava requis"
            )
        if (
            engine["provider"] != "anthropic"
            or engine["adapter"] != "cloud"
            or _CLAUDE_MODEL_RE.fullmatch(engine["model"]) is None
            or engine["revision"] != engine["model"]
        ):
            raise ContractError(
                "release_attestation.engine: CloudEngine Anthropic exact requis"
            )
    artifact_keys = {"manifest_sha256"}
    if schema_version == RELEASE_ATTESTATION_SCHEMA_VERSION:
        artifact_keys.update(
            {
                "evaluation_manifest_sha256",
                "frontend",
                "source_tree_sha256",
                "source_archive_map_sha256",
                "rust_tree_sha256",
                "rust_archive_map_sha256",
                "wheel_sha256",
                "wheel_payload_sha256",
                "wheel_filename",
                "rust_attestation_sha256",
                "rust_builder",
                "python_runtime",
            }
        )
    artifact = _object(
        attestation["artifact"], "release_attestation.artifact", artifact_keys
    )
    _sha256(artifact["manifest_sha256"], "release_attestation.artifact.manifest_sha256")
    if schema_version == RELEASE_ATTESTATION_SCHEMA_VERSION:
        for key in (
            "evaluation_manifest_sha256",
            "source_tree_sha256",
            "source_archive_map_sha256",
            "rust_tree_sha256",
            "rust_archive_map_sha256",
            "wheel_sha256",
            "wheel_payload_sha256",
            "rust_attestation_sha256",
        ):
            _sha256(artifact[key], f"release_attestation.artifact.{key}")
        _frontend_artifact(
            artifact["frontend"], "release_attestation.artifact.frontend"
        )
        _string(
            artifact["wheel_filename"],
            "release_attestation.artifact.wheel_filename",
            pattern=_WHEEL_FILENAME_RE,
        )
        builder = _object(
            artifact["rust_builder"],
            "release_attestation.artifact.rust_builder",
            {
                "attestation_type",
                "signature",
                "builder_image_id",
                "builder_dockerfile_sha256",
                "builder_platform",
                "builder_python_image",
                "builder_rust_image",
                "python_version",
                "rust_version",
                "maturin_version",
                "wheel_compatibility",
            },
        )
        if builder["attestation_type"] != "unsigned-checksum-manifest":
            raise ContractError(
                "release_attestation.artifact.rust_builder: type inattendu"
            )
        if builder["signature"] != "none":
            raise ContractError(
                "release_attestation.artifact.rust_builder: signature ambigue"
            )
        _sha256(
            builder["builder_image_id"],
            "release_attestation.artifact.rust_builder.builder_image_id",
        )
        _sha256(
            builder["builder_dockerfile_sha256"],
            "release_attestation.artifact.rust_builder.builder_dockerfile_sha256",
        )
        if builder["builder_platform"] != "linux/amd64":
            raise ContractError(
                "release_attestation.artifact.rust_builder.builder_platform: "
                "plateforme inattendue"
            )
        for key in ("builder_python_image", "builder_rust_image"):
            _string(
                builder[key],
                f"release_attestation.artifact.rust_builder.{key}",
                pattern=_PINNED_CONTAINER_IMAGE_RE,
            )
        for key in ("python_version", "rust_version", "maturin_version"):
            _string(
                builder[key],
                f"release_attestation.artifact.rust_builder.{key}",
                pattern=_SEMVER_RE,
            )
        _string(
            builder["wheel_compatibility"],
            "release_attestation.artifact.rust_builder.wheel_compatibility",
            pattern=re.compile(r"^manylinux_[0-9]+_[0-9]+_x86_64$"),
        )
        rust_builder_recipe_sha256(builder)
        python_runtime_sha256(artifact["python_runtime"])
    if _boolean(
        attestation["canonical_knowledge"],
        "release_attestation.canonical_knowledge",
    ):
        raise ContractError("release_attestation.canonical_knowledge: doit rester faux")
    return LoadedReleaseAttestation(path=resolved, document=attestation, sha256=digest)


def _causal_pair_side(
    value: Any,
    path: str,
    *,
    expected_treatment: str,
    expected_state: str,
) -> dict[str, Any]:
    side = _object(
        value,
        path,
        {
            "release_attestation_sha256",
            "git_sha",
            "treatment",
            "deployment_state",
            "source_tree_sha256",
            "source_archive_map_sha256",
            "frontend",
            "rust_tree_sha256",
            "rust_archive_map_sha256",
            "wheel_sha256",
            "wheel_payload_sha256",
            "treatment_module_sha256",
            "builder_recipe_sha256",
            "python_runtime_sha256",
        },
    )
    _sha256(side["release_attestation_sha256"], f"{path}.release_attestation_sha256")
    git_sha = _string(side["git_sha"], f"{path}.git_sha", pattern=_GIT_SHA_RE)
    if len(set(git_sha)) == 1:
        raise ContractError(f"{path}.git_sha: placeholder interdit")
    if side["treatment"] != expected_treatment:
        raise ContractError(f"{path}.treatment: traitement inattendu")
    if side["deployment_state"] != expected_state:
        raise ContractError(f"{path}.deployment_state: etat inattendu")
    for key in (
        "source_tree_sha256",
        "source_archive_map_sha256",
        "rust_tree_sha256",
        "rust_archive_map_sha256",
        "wheel_sha256",
        "wheel_payload_sha256",
        "treatment_module_sha256",
        "builder_recipe_sha256",
        "python_runtime_sha256",
    ):
        _sha256(side[key], f"{path}.{key}")
    _frontend_artifact(side["frontend"], f"{path}.frontend")
    return side


def load_causal_pair(
    path: str | Path,
    *,
    expected_sha256: str,
    baseline_attestation: LoadedReleaseAttestation,
    candidate_attestation: LoadedReleaseAttestation,
    expected_manifest_sha256: str | None = None,
) -> LoadedCausalPair:
    """Load a causal pair and recalculate every link to both v2 attestations."""

    resolved, document, digest = _strict_external_document(
        path,
        expected_sha256=expected_sha256,
        max_bytes=_MAX_CAUSAL_PAIR_BYTES,
    )
    pair = _object(
        document,
        "causal_pair",
        {
            "schema_version",
            "pair_id",
            "evaluation_manifest_sha256",
            "repository",
            "baseline",
            "candidate",
            "isolation",
            "equivalence",
            "model_output_causality_claimed",
            "canonical_knowledge",
        },
    )
    if pair["schema_version"] != CAUSAL_PAIR_SCHEMA_VERSION:
        raise ContractError("causal_pair.schema_version: version non supportee")
    _string(pair["pair_id"], "causal_pair.pair_id", pattern=_ID_RE)
    manifest_digest = _sha256(
        pair["evaluation_manifest_sha256"],
        "causal_pair.evaluation_manifest_sha256",
    )
    if expected_manifest_sha256 is not None and manifest_digest != _sha256(
        expected_manifest_sha256, "expected_manifest_sha256"
    ):
        raise ContractError(
            "causal_pair.evaluation_manifest_sha256: manifeste divergent"
        )
    for label, attestation in (
        ("baseline", baseline_attestation),
        ("candidate", candidate_attestation),
    ):
        if (
            attestation.document.get("artifact", {}).get("evaluation_manifest_sha256")
            != manifest_digest
        ):
            raise ContractError(
                f"causal_pair.{label}: manifeste d'evaluation divergent"
            )
    repository = _string(
        pair["repository"], "causal_pair.repository", pattern=_SAFE_RELEASE_VALUE_RE
    )
    if not repository.startswith("repo:"):
        raise ContractError("causal_pair.repository: repo attendu")
    baseline = _causal_pair_side(
        pair["baseline"],
        "causal_pair.baseline",
        expected_treatment=_BASELINE_TREATMENT,
        expected_state="prepared_noncurrent",
    )
    candidate = _causal_pair_side(
        pair["candidate"],
        "causal_pair.candidate",
        expected_treatment=_CANDIDATE_TREATMENT,
        expected_state="active_current",
    )
    if baseline["git_sha"] == candidate["git_sha"]:
        raise ContractError("causal_pair: releases A et B doivent etre distinctes")
    isolation = _object(
        pair["isolation"],
        "causal_pair.isolation",
        {
            "candidate_parent_git_sha",
            "candidate_parent_count",
            "changed_paths",
            "treatment_module_path",
            "treatment_module_mode",
            "baseline_literal",
            "candidate_literal",
            "baseline_git_tree_map_sha256",
            "candidate_git_tree_map_sha256",
            "baseline_archive_map_sha256",
            "candidate_archive_map_sha256",
            "total_entry_count",
            "unchanged_entry_count",
        },
    )
    if isolation["candidate_parent_git_sha"] != baseline["git_sha"]:
        raise ContractError("causal_pair.isolation: parent A divergent")
    if (
        type(isolation["candidate_parent_count"]) is not int
        or isolation["candidate_parent_count"] != 1
    ):
        raise ContractError("causal_pair.isolation: B doit avoir un parent unique")
    if isolation["changed_paths"] != [_TREATMENT_MODULE_PATH]:
        raise ContractError("causal_pair.isolation.changed_paths: diff non minimal")
    if (
        isolation["treatment_module_path"] != _TREATMENT_MODULE_PATH
        or isolation["treatment_module_mode"] != "100644"
        or isolation["baseline_literal"] != _BASELINE_TREATMENT
        or isolation["candidate_literal"] != _CANDIDATE_TREATMENT
    ):
        raise ContractError("causal_pair.isolation: traitement non canonique")
    for key in (
        "baseline_git_tree_map_sha256",
        "candidate_git_tree_map_sha256",
        "baseline_archive_map_sha256",
        "candidate_archive_map_sha256",
    ):
        _sha256(isolation[key], f"causal_pair.isolation.{key}")
    if (
        isolation["baseline_git_tree_map_sha256"]
        != isolation["baseline_archive_map_sha256"]
        or isolation["candidate_git_tree_map_sha256"]
        != isolation["candidate_archive_map_sha256"]
        or isolation["baseline_archive_map_sha256"]
        != baseline["source_archive_map_sha256"]
        or isolation["candidate_archive_map_sha256"]
        != candidate["source_archive_map_sha256"]
    ):
        raise ContractError("causal_pair.isolation: cartes Git/archive divergentes")
    total = isolation["total_entry_count"]
    unchanged = isolation["unchanged_entry_count"]
    if (
        type(total) is not int
        or type(unchanged) is not int
        or total <= 1
        or unchanged != total - 1
    ):
        raise ContractError("causal_pair.isolation: cardinalite de diff invalide")
    equivalence = _object(
        pair["equivalence"],
        "causal_pair.equivalence",
        {
            "frontend",
            "rust_archive_map_sha256",
            "wheel_payload_sha256",
            "builder_recipe_sha256",
            "python_runtime_sha256",
        },
    )
    for key in (
        "rust_archive_map_sha256",
        "wheel_payload_sha256",
        "builder_recipe_sha256",
        "python_runtime_sha256",
    ):
        _sha256(equivalence[key], f"causal_pair.equivalence.{key}")
        if baseline[key] != equivalence[key] or candidate[key] != equivalence[key]:
            raise ContractError(f"causal_pair.equivalence.{key}: releases divergentes")
    frontend_equivalence = _object(
        equivalence["frontend"],
        "causal_pair.equivalence.frontend",
        {
            "archive_sha256",
            "archive_map_sha256",
            "source_map_sha256",
            "builder_recipe_sha256",
        },
    )
    for key in frontend_equivalence:
        _sha256(
            frontend_equivalence[key],
            f"causal_pair.equivalence.frontend.{key}",
        )
        if (
            baseline["frontend"][key] != frontend_equivalence[key]
            or candidate["frontend"][key] != frontend_equivalence[key]
        ):
            raise ContractError(
                f"causal_pair.equivalence.frontend.{key}: releases divergentes"
            )
    if _boolean(
        pair["model_output_causality_claimed"],
        "causal_pair.model_output_causality_claimed",
    ):
        raise ContractError(
            "causal_pair.model_output_causality_claimed: doit rester faux"
        )
    if _boolean(pair["canonical_knowledge"], "causal_pair.canonical_knowledge"):
        raise ContractError("causal_pair.canonical_knowledge: doit rester faux")

    def expected_side(attestation: LoadedReleaseAttestation) -> dict[str, Any]:
        if attestation.document["schema_version"] != RELEASE_ATTESTATION_SCHEMA_VERSION:
            raise ContractError("causal_pair: attestation release v2 requise")
        release = attestation.document["release"]
        artifact = attestation.document["artifact"]
        return {
            "release_attestation_sha256": attestation.sha256,
            "git_sha": release["git_sha"],
            "treatment": release["treatment"],
            "deployment_state": release["deployment_state"],
            "source_tree_sha256": artifact["source_tree_sha256"],
            "source_archive_map_sha256": artifact["source_archive_map_sha256"],
            "frontend": artifact["frontend"],
            "rust_tree_sha256": artifact["rust_tree_sha256"],
            "rust_archive_map_sha256": artifact["rust_archive_map_sha256"],
            "wheel_sha256": artifact["wheel_sha256"],
            "wheel_payload_sha256": artifact["wheel_payload_sha256"],
            "treatment_module_sha256": release["treatment_module_sha256"],
            "builder_recipe_sha256": rust_builder_recipe_sha256(
                artifact["rust_builder"]
            ),
            "python_runtime_sha256": python_runtime_sha256(artifact["python_runtime"]),
        }

    if baseline != expected_side(baseline_attestation):
        raise ContractError("causal_pair.baseline: attestation externe divergente")
    if candidate != expected_side(candidate_attestation):
        raise ContractError("causal_pair.candidate: attestation externe divergente")
    if (
        baseline_attestation.document["release"]["repository"] != repository
        or candidate_attestation.document["release"]["repository"] != repository
    ):
        raise ContractError("causal_pair.repository: attestations divergentes")
    if (
        baseline_attestation.document["engine"]
        != candidate_attestation.document["engine"]
    ):
        raise ContractError("causal_pair.engine: moteurs A/B divergents")
    if (
        baseline_attestation.document["artifact"]["wheel_filename"]
        != candidate_attestation.document["artifact"]["wheel_filename"]
    ):
        raise ContractError("causal_pair.wheel_filename: releases divergentes")
    return LoadedCausalPair(path=resolved, document=pair, sha256=digest)


def load_causal_shadow_binding(
    *,
    release_attestation_path: str | Path,
    release_attestation_sha256: str,
    peer_release_attestation_path: str | Path,
    peer_release_attestation_sha256: str,
    causal_pair_path: str | Path,
    causal_pair_sha256: str,
    expected_manifest_sha256: str,
) -> LoadedCausalShadowBinding:
    """Derive the executing side without accepting any caller-selected role."""

    manifest_sha256 = _sha256(expected_manifest_sha256, "expected_manifest_sha256")
    release_attestation = load_release_attestation(
        release_attestation_path, expected_sha256=release_attestation_sha256
    )
    peer_attestation = load_release_attestation(
        peer_release_attestation_path, expected_sha256=peer_release_attestation_sha256
    )
    by_treatment = {
        release_attestation.document["release"].get("treatment"): release_attestation,
        peer_attestation.document["release"].get("treatment"): peer_attestation,
    }
    if set(by_treatment) != {_BASELINE_TREATMENT, _CANDIDATE_TREATMENT}:
        raise ContractError("binding causal: traitements A/B incomplets ou dupliques")
    baseline = by_treatment[_BASELINE_TREATMENT]
    candidate = by_treatment[_CANDIDATE_TREATMENT]
    causal_pair = load_causal_pair(
        causal_pair_path,
        expected_sha256=causal_pair_sha256,
        baseline_attestation=baseline,
        candidate_attestation=candidate,
        expected_manifest_sha256=manifest_sha256,
    )
    release = release_attestation.document["release"]
    role = "baseline" if release["treatment"] == _BASELINE_TREATMENT else "candidate"
    return LoadedCausalShadowBinding(
        _BINDING_CONSTRUCTION_TOKEN,
        release_attestation=release_attestation,
        peer_release_attestation=peer_attestation,
        causal_pair=causal_pair,
        role=role,
        treatment=release["treatment"],
        deployment_state=release["deployment_state"],
        release_git_sha=release["git_sha"],
        treatment_module_sha256=release["treatment_module_sha256"],
        evaluation_manifest_sha256=manifest_sha256,
    )


def reload_causal_shadow_binding(
    binding: LoadedCausalShadowBinding,
) -> LoadedCausalShadowBinding:
    """Reload every external proof from its pinned path and immutable digest.

    The loaded JSON documents are deliberately not used as trust inputs here:
    callers may still hold mutable references to them despite the frozen outer
    dataclasses.  Re-reading all three artifacts closes that mutation window
    immediately before a verified shadow scope is opened.
    """

    if type(binding) is not LoadedCausalShadowBinding:
        raise ContractError("binding causal: type charge strict attendu")
    return load_causal_shadow_binding(
        release_attestation_path=binding.release_attestation.path,
        release_attestation_sha256=binding.release_attestation.sha256,
        peer_release_attestation_path=binding.peer_release_attestation.path,
        peer_release_attestation_sha256=binding.peer_release_attestation.sha256,
        causal_pair_path=binding.causal_pair.path,
        causal_pair_sha256=binding.causal_pair.sha256,
        expected_manifest_sha256=binding.evaluation_manifest_sha256,
    )


def _load_adjudication(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_kind: str,
    statement_sha256: str,
    suite: LoadedSuite | None = None,
    candidate_sha256: str | None = None,
) -> LoadedAdjudication:
    resolved, document, digest = _strict_external_document(
        path,
        expected_sha256=expected_sha256,
        max_bytes=_MAX_ADJUDICATION_BYTES,
    )
    is_v2 = suite is not None and suite.quality_rubric is not None
    adjudication_keys = {
        "schema_version",
        "attestation_id",
        "kind",
        "statement_sha256",
        "reviewer",
        "decision",
        "checks",
        "canonical_knowledge",
    }
    if is_v2:
        adjudication_keys.add("quality_review")
    adjudication = _object(
        document,
        "adjudication",
        adjudication_keys,
    )
    expected_version = (
        ADJUDICATION_SCHEMA_VERSION if is_v2 else ADJUDICATION_SCHEMA_VERSION_V1
    )
    if adjudication["schema_version"] != expected_version:
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
    if is_v2:
        if candidate_sha256 is None or suite.quality_rubric_sha256 is None:
            raise ContractError("adjudication.quality_review: contexte v2 incomplet")
        quality_review = _object(
            adjudication["quality_review"],
            "adjudication.quality_review",
            {"rubric_sha256", "candidate_sha256", "results"},
        )
        if (
            _sha256(
                quality_review["rubric_sha256"],
                "adjudication.quality_review.rubric_sha256",
            )
            != suite.quality_rubric_sha256
        ):
            raise ContractError("adjudication.quality_review: rubrique divergente")
        if (
            _sha256(
                quality_review["candidate_sha256"],
                "adjudication.quality_review.candidate_sha256",
            )
            != candidate_sha256
        ):
            raise ContractError("adjudication.quality_review: candidat divergent")
        results = _list(
            quality_review["results"], "adjudication.quality_review.results"
        )
        validated_results: list[dict[str, str]] = []
        for index, raw_result in enumerate(results):
            result_path = f"adjudication.quality_review.results[{index}]"
            result = _object(
                raw_result, result_path, {"case_id", "check_id", "decision"}
            )
            _string(result["case_id"], f"{result_path}.case_id", pattern=_CASE_ID_RE)
            _string(result["check_id"], f"{result_path}.check_id", pattern=_ID_RE)
            if result["decision"] != "pass":
                raise ContractError(f"{result_path}.decision: pass requis")
            validated_results.append(result)
        if validated_results != required_quality_results(suite):
            raise ContractError(
                "adjudication.quality_review.results: couverture exhaustive requise"
            )
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
    suite: LoadedSuite | None = None,
    candidate_sha256: str | None = None,
) -> ReviewEvidence:
    """Load two independent decisions plus an out-of-process immutable anchor."""

    statement = _sha256(statement_sha256, "statement_sha256")
    human = _load_adjudication(
        human_path,
        expected_sha256=human_sha256,
        expected_kind="human",
        statement_sha256=statement,
        suite=suite,
        candidate_sha256=candidate_sha256,
    )
    independent = _load_adjudication(
        independent_path,
        expected_sha256=independent_sha256,
        expected_kind="independent",
        statement_sha256=statement,
        suite=suite,
        candidate_sha256=candidate_sha256,
    )
    if human.document["reviewer"]["id"] == independent.document["reviewer"]["id"]:
        raise ContractError("adjudications: deux reviewers distincts sont requis")

    key_path, key_document, key_digest = _strict_external_document(
        anchor_public_key_path,
        expected_sha256=anchor_public_key_sha256,
        max_bytes=_MAX_ANCHOR_KEY_BYTES,
    )
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
        anchor_path,
        expected_sha256=anchor_sha256,
        max_bytes=_MAX_EXTERNAL_ANCHOR_BYTES,
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
        _REVIEW_EVIDENCE_CONSTRUCTION_TOKEN,
        human=human,
        independent=independent,
        anchor=LoadedExternalAnchor(path=resolved, document=anchor, sha256=digest),
        anchor_public_key_path=key_path,
        anchor_public_key_sha256=key_digest,
    )


def reload_review_evidence(
    evidence: ReviewEvidence,
    *,
    statement_sha256: str,
    suite: LoadedSuite | None = None,
    candidate_sha256: str | None = None,
) -> ReviewEvidence:
    """Reload every review artifact and signature from immutable scalar pins."""

    if type(evidence) is not ReviewEvidence:
        raise ContractError("review evidence: type charge strict attendu")
    return load_review_evidence(
        statement_sha256=statement_sha256,
        human_path=evidence.human.path,
        human_sha256=evidence.human.sha256,
        independent_path=evidence.independent.path,
        independent_sha256=evidence.independent.sha256,
        anchor_path=evidence.anchor.path,
        anchor_sha256=evidence.anchor.sha256,
        anchor_public_key_path=evidence.anchor_public_key_path,
        anchor_public_key_sha256=evidence.anchor_public_key_sha256,
        suite=suite,
        candidate_sha256=candidate_sha256,
    )


def _validate_fact(value: Any, path: str) -> dict[str, Any]:
    fact = _object(value, path, {"id", "text"})
    _string(fact["id"], f"{path}.id", pattern=_ID_RE)
    _string(fact["text"], f"{path}.text")
    return fact


def _nullable_boolean(value: Any, path: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ContractError(f"{path}: booleen ou null requis")
    return value


def _validate_authoring_provenance(value: Any, path: str) -> dict[str, Any]:
    """Validate what was knowable when one vNext scenario/check was authored."""

    provenance = _object(
        value,
        path,
        {
            "authoring_class",
            "authored_before_new_candidate",
            "new_candidate_response_access",
            "predecessor_raw_response_access",
            "predecessor_diagnostics_access",
            "content_modified_after_predecessor",
            "owner_reviewed",
            "contains_personal_data",
            "contains_production_conversations",
        },
    )
    authoring_class = provenance["authoring_class"]
    if authoring_class not in AUTHORING_CLASSES:
        raise ContractError(f"{path}.authoring_class: classe inconnue")
    for key in (
        "authored_before_new_candidate",
        "content_modified_after_predecessor",
    ):
        _boolean(provenance[key], f"{path}.{key}")
    for key in (
        "new_candidate_response_access",
        "owner_reviewed",
        "contains_personal_data",
        "contains_production_conversations",
    ):
        if _boolean(provenance[key], f"{path}.{key}"):
            raise ContractError(f"{path}.{key}: doit rester faux")
    if not provenance["authored_before_new_candidate"]:
        raise ContractError(f"{path}.authored_before_new_candidate: doit rester vrai")

    raw_access = _nullable_boolean(
        provenance["predecessor_raw_response_access"],
        f"{path}.predecessor_raw_response_access",
    )
    diagnostic_access = _nullable_boolean(
        provenance["predecessor_diagnostics_access"],
        f"{path}.predecessor_diagnostics_access",
    )
    modified = provenance["content_modified_after_predecessor"]
    expected = {
        "inherited_v1_6_1": (None, None, False),
        "predecessor_diagnostics_only": (False, True, True),
        "sealed_holdout_v17": (False, False, True),
    }[authoring_class]
    if (raw_access, diagnostic_access, modified) != expected:
        raise ContractError(f"{path}: provenance incoherente avec authoring_class")
    return provenance


def _validate_case(value: Any, path: str, *, schema_version: str) -> dict[str, Any]:
    case_keys = {
        "id",
        "split",
        "description",
        "principal",
        "conversation",
        "policy",
    }
    if schema_version == CORPUS_SCHEMA_VERSION:
        case_keys.update({"provenance", "lexical_secondary"})
    else:
        case_keys.add("secondary")
    case = _object(value, path, case_keys)
    _string(case["id"], f"{path}.id", pattern=_CASE_ID_RE)
    if case["split"] not in SPLITS:
        raise ContractError(f"{path}.split: valeur inconnue")
    _string(case["description"], f"{path}.description")
    if schema_version == CORPUS_SCHEMA_VERSION:
        _validate_authoring_provenance(case["provenance"], f"{path}.provenance")

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
    policy_keys = {
        "allowed_tools",
        "visible_facts",
        "forbidden_canaries",
        "allowed_memory_claim_ids",
    }
    if schema_version == CORPUS_SCHEMA_VERSION_V1:
        policy_with_defaults.setdefault("allowed_exact_echo_turn_indexes", [])
        policy_keys.add("allowed_exact_echo_turn_indexes")
    policy = _object(
        policy_with_defaults,
        f"{path}.policy",
        policy_keys,
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
        policy.get("allowed_exact_echo_turn_indexes", []),
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

    secondary_key = (
        "lexical_secondary" if schema_version == CORPUS_SCHEMA_VERSION else "secondary"
    )
    secondary_path = f"{path}.{secondary_key}"
    secondary = case[secondary_key]
    required_secondary = {
        "warmth_any_of",
        "wit_any_of",
        "continuity_all_of",
        "accuracy_all_of",
    }
    optional_secondary = {
        "continuity_any_of_groups",
        "accuracy_any_of_groups",
    }
    if schema_version == CORPUS_SCHEMA_VERSION_V1:
        optional_secondary.add("required_secondary")
    elif schema_version == CORPUS_SCHEMA_VERSION:
        required_secondary.add("authoritative")
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
    if schema_version == CORPUS_SCHEMA_VERSION and _boolean(
        secondary["authoritative"], f"{secondary_path}.authoritative"
    ):
        raise ContractError(f"{secondary_path}.authoritative: doit rester faux")
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
    if corpus["schema_version"] not in {
        CORPUS_SCHEMA_VERSION_V1,
        CORPUS_SCHEMA_VERSION_V2,
        CORPUS_SCHEMA_VERSION,
    }:
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
        _validate_case(
            case,
            f"corpus.cases[{index}]",
            schema_version=corpus["schema_version"],
        )
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


def _validate_quality_check(
    value: Any,
    path: str,
    *,
    provenance_required: bool,
) -> dict[str, Any]:
    keys = {"id", "dimension", "requirement"}
    if provenance_required:
        keys.add("provenance")
    check = _object(value, path, keys)
    _string(check["id"], f"{path}.id", pattern=_ID_RE)
    if check["dimension"] not in SECONDARY_METRICS:
        raise ContractError(f"{path}.dimension: metrique inconnue")
    _string(check["requirement"], f"{path}.requirement")
    if provenance_required:
        _validate_authoring_provenance(check["provenance"], f"{path}.provenance")
    return check


def validate_quality_rubric(
    document: dict[str, Any], corpus: dict[str, Any]
) -> dict[str, Any]:
    """Valide une rubrique semantique preenregistree et versionnee."""

    schema_version = document.get("schema_version")
    if schema_version not in {
        QUALITY_RUBRIC_SCHEMA_VERSION_V1,
        QUALITY_RUBRIC_SCHEMA_VERSION,
    }:
        raise ContractError("quality_rubric.schema_version: version non supportee")
    is_v2 = schema_version == QUALITY_RUBRIC_SCHEMA_VERSION
    global_key = "semantic_global_checks" if is_v2 else "global_checks"
    case_key = "semantic_case_checks" if is_v2 else "case_checks"
    rubric = _object(
        document,
        "quality_rubric",
        {
            "schema_version",
            "rubric_id",
            "corpus",
            "provenance",
            "decision_policy",
            global_key,
            case_key,
        },
    )
    _string(rubric["rubric_id"], "quality_rubric.rubric_id", pattern=_ID_RE)
    corpus_ref = _object(rubric["corpus"], "quality_rubric.corpus", {"id", "version"})
    if corpus_ref != {
        "id": corpus["corpus_id"],
        "version": corpus["version"],
    }:
        raise ContractError("quality_rubric.corpus: corpus incompatible")
    provenance_keys = {
        "authoring_method",
        "contains_personal_data",
        "contains_production_conversations",
    }
    provenance_keys.update(
        {"new_candidate_response_access", "owner_reviewed"}
        if is_v2
        else {"candidate_response_access"}
    )
    provenance = _object(
        rubric["provenance"], "quality_rubric.provenance", provenance_keys
    )
    expected_method = (
        "model-assisted-curated-synthetic-requirements-before-new-candidate"
        if is_v2
        else "human-authored-before-shadow"
    )
    if provenance["authoring_method"] != expected_method:
        raise ContractError("quality_rubric.provenance: methode interdite")
    false_keys = {
        "contains_personal_data",
        "contains_production_conversations",
        "new_candidate_response_access" if is_v2 else "candidate_response_access",
    }
    if is_v2:
        false_keys.add("owner_reviewed")
    for key in sorted(false_keys):
        if _boolean(provenance[key], f"quality_rubric.provenance.{key}"):
            raise ContractError(f"quality_rubric.provenance.{key}: doit rester faux")
    decision_policy = _object(
        rubric["decision_policy"],
        "quality_rubric.decision_policy",
        {
            "lexical_diagnostics_authoritative",
            "all_required_checks_must_pass",
            "abstain_is_fail",
            "required_reviewer_kinds",
        },
    )
    if _boolean(
        decision_policy["lexical_diagnostics_authoritative"],
        "quality_rubric.decision_policy.lexical_diagnostics_authoritative",
    ):
        raise ContractError("quality_rubric: diagnostics lexicaux non autoritatifs")
    for key in ("all_required_checks_must_pass", "abstain_is_fail"):
        if not _boolean(decision_policy[key], f"quality_rubric.decision_policy.{key}"):
            raise ContractError(f"quality_rubric.decision_policy.{key}: doit etre vrai")
    reviewer_kinds = _string_list(
        decision_policy["required_reviewer_kinds"],
        "quality_rubric.decision_policy.required_reviewer_kinds",
    )
    if reviewer_kinds != ["human", "independent"]:
        raise ContractError("quality_rubric: deux reviewers ordonnes sont requis")

    global_path = f"quality_rubric.{global_key}"
    global_items = _list(rubric[global_key], global_path)
    if not global_items:
        raise ContractError(f"{global_path}: liste vide")
    global_checks = [
        _validate_quality_check(
            item,
            f"{global_path}[{index}]",
            provenance_required=is_v2,
        )
        for index, item in enumerate(global_items)
    ]
    all_check_ids = [check["id"] for check in global_checks]
    if len(set(all_check_ids)) != len(all_check_ids):
        raise ContractError(f"{global_path}: identifiants dupliques")

    known_case_ids = {case["id"] for case in corpus["cases"]}
    seen_case_ids: list[str] = []
    case_path = f"quality_rubric.{case_key}"
    for index, raw_case in enumerate(_list(rubric[case_key], case_path)):
        path = f"{case_path}[{index}]"
        case_review_keys = {"case_id", "check_groups" if is_v2 else "checks"}
        case_review = _object(raw_case, path, case_review_keys)
        case_id = _string(
            case_review["case_id"], f"{path}.case_id", pattern=_CASE_ID_RE
        )
        if case_id not in known_case_ids:
            raise ContractError(f"{path}.case_id: cas inconnu")
        seen_case_ids.append(case_id)
        checks: list[dict[str, Any]] = []
        if is_v2:
            raw_groups = _list(case_review["check_groups"], f"{path}.check_groups")
            if not raw_groups:
                raise ContractError(f"{path}.check_groups: liste vide")
            for group_index, raw_group in enumerate(raw_groups):
                group_path = f"{path}.check_groups[{group_index}]"
                group = _object(raw_group, group_path, {"provenance", "checks"})
                _validate_authoring_provenance(
                    group["provenance"], f"{group_path}.provenance"
                )
                raw_checks = _list(group["checks"], f"{group_path}.checks")
                if not raw_checks:
                    raise ContractError(f"{group_path}.checks: liste vide")
                checks.extend(
                    _validate_quality_check(
                        item,
                        f"{group_path}.checks[{check_index}]",
                        provenance_required=False,
                    )
                    for check_index, item in enumerate(raw_checks)
                )
        else:
            raw_checks = _list(case_review["checks"], f"{path}.checks")
            if not raw_checks:
                raise ContractError(f"{path}.checks: liste vide")
            checks = [
                _validate_quality_check(
                    item,
                    f"{path}.checks[{check_index}]",
                    provenance_required=False,
                )
                for check_index, item in enumerate(raw_checks)
            ]
        all_check_ids.extend(check["id"] for check in checks)
    if len(set(seen_case_ids)) != len(seen_case_ids):
        raise ContractError("quality_rubric.case_checks: cas dupliques")
    if len(set(all_check_ids)) != len(all_check_ids):
        raise ContractError("quality_rubric: identifiants de controle dupliques")
    return rubric


def required_quality_results(suite: LoadedSuite) -> list[dict[str, str]]:
    """Retourne la couverture semantique exhaustive attendue, dans un ordre stable."""

    if suite.quality_rubric is None:
        return []
    is_v2 = suite.quality_rubric["schema_version"] == QUALITY_RUBRIC_SCHEMA_VERSION
    global_key = "semantic_global_checks" if is_v2 else "global_checks"
    case_key = "semantic_case_checks" if is_v2 else "case_checks"
    global_checks = suite.quality_rubric[global_key]
    if is_v2:
        per_case = {
            item["case_id"]: [
                check for group in item["check_groups"] for check in group["checks"]
            ]
            for item in suite.quality_rubric[case_key]
        }
    else:
        per_case = {
            item["case_id"]: item["checks"] for item in suite.quality_rubric[case_key]
        }
    return [
        {"case_id": case["id"], "check_id": check["id"], "decision": "pass"}
        for case in suite.corpus["cases"]
        for check in [*global_checks, *per_case.get(case["id"], [])]
    ]


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


def _validate_schema_header(
    schema: dict[str, Any], *, label: Path, expected_id: str
) -> None:
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
        raise ContractError(f"{label}: en-tete de schema incomplet")
    if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        raise ContractError(f"{label}: draft JSON Schema inattendu")
    if schema["$id"] != expected_id or schema["type"] != "object":
        raise ContractError(f"{label}: identite de schema inattendue")
    if schema["additionalProperties"] is not False:
        raise ContractError(f"{label}: le schema racine doit etre strict")


def load_suite(manifest_path: str | Path | None = None) -> LoadedSuite:
    if manifest_path is None:
        manifest_path = Path(__file__).with_name("data") / "manifest.v3.json"
    path = Path(manifest_path).expanduser().absolute()
    manifest_payload = _strict_regular_payload(
        path, max_bytes=_MAX_MANIFEST_BYTES, label="manifest"
    )
    manifest_digest = sha256_bytes(manifest_payload)
    manifest = _json_object_from_payload(manifest_payload, path)
    manifest_version = manifest.get("schema_version")
    if manifest_version not in {
        MANIFEST_SCHEMA_VERSION_V1,
        MANIFEST_SCHEMA_VERSION_V2,
        MANIFEST_SCHEMA_VERSION,
    }:
        raise ContractError("manifest.schema_version: version non supportee")
    is_governed = manifest_version in {
        MANIFEST_SCHEMA_VERSION_V2,
        MANIFEST_SCHEMA_VERSION,
    }
    is_v3 = manifest_version == MANIFEST_SCHEMA_VERSION
    manifest_keys = {
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
    }
    if is_governed:
        manifest_keys.update({"safety_policy", "quality_policy"})
    manifest = _object(
        manifest,
        "manifest",
        manifest_keys,
    )
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

    provenance_keys = {
        "authoring_method",
        "source",
        "contains_personal_data",
        "contains_production_conversations",
    }
    if is_v3:
        provenance_keys.update({"new_candidate_response_access", "owner_reviewed"})
    provenance = _object(manifest["provenance"], "manifest.provenance", provenance_keys)
    expected_authoring_method = (
        "model-assisted-curated-synthetic-scenarios-before-new-candidate"
        if is_v3
        else "human-authored-synthetic-scenarios"
    )
    if provenance["authoring_method"] != expected_authoring_method:
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
    if is_v3:
        for key in ("new_candidate_response_access", "owner_reviewed"):
            if _boolean(provenance[key], f"manifest.provenance.{key}"):
                raise ContractError(f"manifest.provenance.{key}: doit rester faux")

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

    safety_policy_sha256: str | None = None
    if is_governed:
        safety_policy = _object(
            manifest["safety_policy"],
            "manifest.safety_policy",
            {"id", "version", "sha256"},
        )
        if safety_policy["id"] != RELATIONSHIP_TEXT_SAFETY_POLICY_ID:
            raise ContractError("manifest.safety_policy.id: politique inattendue")
        _string(safety_policy["version"], "manifest.safety_policy.version")
        safety_policy_sha256 = _sha256(
            safety_policy["sha256"], "manifest.safety_policy.sha256"
        )
        if manifest_version == MANIFEST_SCHEMA_VERSION_V2:
            if safety_policy["version"] != HISTORICAL_V2_SAFETY_POLICY_VERSION:
                raise ContractError(
                    "manifest.safety_policy.version: politique historique v2 requise"
                )
            if safety_policy_sha256 != HISTORICAL_V2_SAFETY_POLICY_SHA256:
                raise ContractError(
                    "manifest.safety_policy.sha256: politique historique v2 requise"
                )
        elif is_v3:
            if safety_policy["version"] != RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION:
                raise ContractError(
                    "manifest.safety_policy.version: version inattendue"
                )
            if safety_policy_sha256 != relationship_text_safety_policy_sha256():
                raise ContractError("manifest.safety_policy.sha256: empreinte invalide")
        quality_policy = _object(
            manifest["quality_policy"],
            "manifest.quality_policy",
            {
                "rubric_id",
                "lexical_diagnostics_authoritative",
                "all_required_checks_must_pass",
            },
        )
        _string(
            quality_policy["rubric_id"],
            "manifest.quality_policy.rubric_id",
            pattern=_ID_RE,
        )
        if _boolean(
            quality_policy["lexical_diagnostics_authoritative"],
            "manifest.quality_policy.lexical_diagnostics_authoritative",
        ):
            raise ContractError("manifest.quality_policy: diagnostics non autoritatifs")
        if not _boolean(
            quality_policy["all_required_checks_must_pass"],
            "manifest.quality_policy.all_required_checks_must_pass",
        ):
            raise ContractError("manifest.quality_policy: controles absolus requis")

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
    if is_governed:
        expected_files.update({"quality_rubric", "quality_rubric_schema"})
    if is_v3:
        expected_files.difference_update({"baseline_fixture", "candidate_fixture"})
        expected_files.add("causal_pair_schema")
    files = _object(manifest["files"], "manifest.files", expected_files)
    root = path.parent
    resolved_files: dict[str, Path] = {}
    referenced_payloads: dict[str, bytes] = {}
    for name in sorted(expected_files):
        reference = _object(files[name], f"manifest.files.{name}", {"path", "sha256"})
        referenced = _safe_referenced_file(
            root, reference["path"], f"manifest.files.{name}.path"
        )
        expected_digest = _sha256(reference["sha256"], f"manifest.files.{name}.sha256")
        max_bytes = (
            _MAX_RESPONSE_BUNDLE_BYTES
            if name in {"baseline_fixture", "candidate_fixture"}
            else _MAX_LOCAL_CONTRACT_BYTES
        )
        payload = _strict_regular_payload(
            referenced,
            max_bytes=max_bytes,
            label=f"manifest.files.{name}",
        )
        actual_digest = sha256_bytes(payload)
        if actual_digest != expected_digest:
            raise ContractError(f"manifest.files.{name}: empreinte invalide")
        resolved_files[name] = referenced
        referenced_payloads[name] = payload

    def referenced_json(name: str) -> dict[str, Any]:
        return _json_object_from_payload(
            referenced_payloads[name], resolved_files[name]
        )

    contract_suffix = "v3" if is_v3 else ("v2" if is_governed else "v1")
    _validate_schema_header(
        referenced_json("corpus_schema"),
        label=resolved_files["corpus_schema"],
        expected_id=f"urn:ava:relationship:corpus:{contract_suffix}",
    )
    _validate_schema_header(
        referenced_json("manifest_schema"),
        label=resolved_files["manifest_schema"],
        expected_id=f"urn:ava:relationship:manifest:{contract_suffix}",
    )
    _validate_schema_header(
        referenced_json("responses_schema"),
        label=resolved_files["responses_schema"],
        expected_id=f"urn:ava:relationship:responses:{contract_suffix}",
    )
    report_suffix = "v3" if is_v3 else ("v2" if is_governed else "v1")
    _validate_schema_header(
        referenced_json("report_schema"),
        label=resolved_files["report_schema"],
        expected_id=f"urn:ava:relationship:report:{report_suffix}",
    )
    _validate_schema_header(
        referenced_json("release_attestation_schema"),
        label=resolved_files["release_attestation_schema"],
        expected_id=(
            "urn:ava:release:attestation:v2"
            if is_v3
            else "urn:ava:release:attestation:v1"
        ),
    )
    if is_v3:
        _validate_schema_header(
            referenced_json("causal_pair_schema"),
            label=resolved_files["causal_pair_schema"],
            expected_id="urn:ava:relationship:causal-pair:v1",
        )
    _validate_schema_header(
        referenced_json("adjudication_schema"),
        label=resolved_files["adjudication_schema"],
        expected_id=(
            f"urn:ava:relationship:adjudication:{'v2' if is_governed else 'v1'}"
        ),
    )
    _validate_schema_header(
        referenced_json("external_anchor_schema"),
        label=resolved_files["external_anchor_schema"],
        expected_id="urn:ava:relationship:external-anchor:v1",
    )
    _validate_schema_header(
        referenced_json("anchor_key_schema"),
        label=resolved_files["anchor_key_schema"],
        expected_id="urn:ava:relationship:anchor-key:v1",
    )
    if is_governed:
        quality_suffix = "v2" if is_v3 else "v1"
        _validate_schema_header(
            referenced_json("quality_rubric_schema"),
            label=resolved_files["quality_rubric_schema"],
            expected_id=f"urn:ava:relationship:quality-rubric:{quality_suffix}",
        )

    corpus = validate_corpus(referenced_json("corpus"))
    expected_corpus_version = {
        MANIFEST_SCHEMA_VERSION_V1: CORPUS_SCHEMA_VERSION_V1,
        MANIFEST_SCHEMA_VERSION_V2: CORPUS_SCHEMA_VERSION_V2,
        MANIFEST_SCHEMA_VERSION: CORPUS_SCHEMA_VERSION,
    }[manifest_version]
    if corpus["schema_version"] != expected_corpus_version:
        raise ContractError("manifest: schema corpus incompatible")
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

    quality_rubric: dict[str, Any] | None = None
    quality_rubric_sha256: str | None = None
    if is_governed:
        quality_rubric = validate_quality_rubric(
            referenced_json("quality_rubric"), corpus
        )
        expected_rubric_version = (
            QUALITY_RUBRIC_SCHEMA_VERSION if is_v3 else QUALITY_RUBRIC_SCHEMA_VERSION_V1
        )
        if quality_rubric["schema_version"] != expected_rubric_version:
            raise ContractError("manifest: schema rubrique incompatible")
        quality_rubric_sha256 = sha256_bytes(referenced_payloads["quality_rubric"])
        if quality_rubric["rubric_id"] != manifest["quality_policy"]["rubric_id"]:
            raise ContractError(
                "manifest.quality_policy.rubric_id: rubrique divergente"
            )

    suite = LoadedSuite(
        root=root,
        manifest_path=path,
        manifest=manifest,
        corpus=corpus,
        quality_rubric=quality_rubric,
        manifest_sha256=manifest_digest,
        corpus_sha256=sha256_bytes(referenced_payloads["corpus"]),
        quality_rubric_sha256=quality_rubric_sha256,
        safety_policy_sha256=safety_policy_sha256,
    )
    if not is_v3:
        load_response_bundle(
            resolved_files["baseline_fixture"],
            suite,
            expected_role="baseline",
            expected_sha256=sha256_bytes(referenced_payloads["baseline_fixture"]),
        )
        load_response_bundle(
            resolved_files["candidate_fixture"],
            suite,
            expected_role="candidate",
            expected_sha256=sha256_bytes(referenced_payloads["candidate_fixture"]),
        )
    return suite


def reload_suite(suite: LoadedSuite) -> LoadedSuite:
    """Reload a suite from its manifest path and retain its original byte pin."""

    if type(suite) is not LoadedSuite:
        raise ContractError("suite: type charge strict attendu")
    expected_manifest_sha256 = _sha256(suite.manifest_sha256, "suite.manifest_sha256")
    fresh = load_suite(suite.manifest_path)
    if fresh.manifest_sha256 != expected_manifest_sha256:
        raise ContractError("suite.manifest_sha256: manifeste modifie apres chargement")
    return fresh


def _relationship_allowed_case(case: dict[str, Any]) -> bool:
    principal = case["principal"]
    return bool(
        principal["verified"]
        and principal["relationship_opt_in"]
        and principal["relationship_subject"] == principal["request_subject"]
    )


def _validate_guard_observation_v3(
    value: Any,
    *,
    suite: LoadedSuite,
    expected_role: str,
    expected_treatment: str,
) -> dict[str, Any]:
    path = "responses.artifact.guard_observation"
    observation = _object(
        value,
        path,
        {
            "schema_version",
            "treatment",
            "active",
            "policy_id",
            "policy_sha256",
            "expected_prepare_calls",
            "observed_prepare_calls",
            "expected_begin_calls",
            "observed_begin_calls",
            "observed_finish_calls",
            "observed_repair_calls",
            "actions",
        },
    )
    if observation["schema_version"] != "ava.relationship.guard-observation/v3":
        raise ContractError(f"{path}.schema_version: version inattendue")
    if observation["treatment"] != expected_treatment:
        raise ContractError(f"{path}.treatment: traitement divergent")
    active = _boolean(observation["active"], f"{path}.active")
    count_keys = (
        "expected_prepare_calls",
        "observed_prepare_calls",
        "expected_begin_calls",
        "observed_begin_calls",
        "observed_finish_calls",
        "observed_repair_calls",
    )
    for key in count_keys:
        if type(observation[key]) is not int or observation[key] < 0:
            raise ContractError(f"{path}.{key}: entier positif requis")
    if expected_role == "baseline":
        if active or any(observation[key] != 0 for key in count_keys):
            raise ContractError(f"{path}: baseline doit bypasser le garde")
        if (
            observation["policy_id"] is not None
            or observation["policy_sha256"] is not None
        ):
            raise ContractError(f"{path}: baseline ne doit pas attester une politique")
        if _list(observation["actions"], f"{path}.actions"):
            raise ContractError(f"{path}.actions: baseline doit rester vide")
        return observation

    if not active:
        raise ContractError(f"{path}.active: candidat doit activer le garde")
    if observation["policy_id"] != RELATIONSHIP_TEXT_SAFETY_POLICY_ID:
        raise ContractError(f"{path}.policy_id: politique inattendue")
    if _sha256(observation["policy_sha256"], f"{path}.policy_sha256") != (
        suite.safety_policy_sha256
    ):
        raise ContractError(f"{path}.policy_sha256: politique divergente")
    case_ids = [case["id"] for case in suite.corpus["cases"]]
    guarded_ids = [
        case["id"] for case in suite.corpus["cases"] if _relationship_allowed_case(case)
    ]
    expected_counts = {
        "expected_prepare_calls": len(case_ids),
        "observed_prepare_calls": len(case_ids),
        "expected_begin_calls": len(guarded_ids),
        "observed_begin_calls": len(guarded_ids),
    }
    for key, expected in expected_counts.items():
        if observation[key] != expected:
            raise ContractError(f"{path}.{key}: {expected} requis")
    raw_actions = _list(observation["actions"], f"{path}.actions")
    actions: list[dict[str, Any]] = []
    repair_count = 0
    for index, raw_action in enumerate(raw_actions):
        action_path = f"{path}.actions[{index}]"
        action = _object(
            raw_action,
            action_path,
            {
                "case_id",
                "action",
                "gate_ids",
                "replacement_id",
                "repair_attempted",
                "repair_attempts",
                "repair_outcome",
                "repair_gate_ids",
            },
        )
        _string(action["case_id"], f"{action_path}.case_id", pattern=_CASE_ID_RE)
        if action["action"] not in {"allow", "replace"}:
            raise ContractError(f"{action_path}.action: action inconnue")
        gate_ids = _string_list(action["gate_ids"], f"{action_path}.gate_ids")
        repair_gate_ids = _string_list(
            action["repair_gate_ids"], f"{action_path}.repair_gate_ids"
        )
        if not set((*gate_ids, *repair_gate_ids)).issubset(RUNTIME_GUARD_GATE_IDS):
            raise ContractError(f"{action_path}: gate runtime inconnu")
        replacement_id = action["replacement_id"]
        if replacement_id is not None:
            _string(replacement_id, f"{action_path}.replacement_id", pattern=_ID_RE)
        repair_attempted = _boolean(
            action["repair_attempted"], f"{action_path}.repair_attempted"
        )
        if type(action["repair_attempts"]) is not int or action[
            "repair_attempts"
        ] not in {0, 1}:
            raise ContractError(f"{action_path}.repair_attempts: 0 ou 1 requis")
        if action["repair_outcome"] not in {
            "not_attempted",
            "accepted",
            "provider_error",
            "invalid_response",
            "incomplete",
            "structured_output",
            "unsafe",
        }:
            raise ContractError(f"{action_path}.repair_outcome: resultat inconnu")
        if (action["action"] == "allow") != (not gate_ids):
            raise ContractError(f"{action_path}: action et gates incoherents")
        if action["action"] == "allow" and replacement_id is not None:
            raise ContractError(f"{action_path}: replacement inattendu")
        if action["action"] == "replace" and replacement_id is None:
            raise ContractError(f"{action_path}: replacement manquant")
        if not repair_attempted:
            if (
                action["repair_attempts"] != 0
                or action["repair_outcome"] != "not_attempted"
                or repair_gate_ids
                or action["action"] == "replace"
            ):
                raise ContractError(f"{action_path}: repair non tente incoherent")
        else:
            repair_count += 1
            if (
                action["repair_attempts"] != 1
                or action["repair_outcome"] == "not_attempted"
                or not repair_gate_ids
                or not set(repair_gate_ids).issubset(gate_ids)
                or (
                    action["repair_outcome"] != "unsafe" and repair_gate_ids != gate_ids
                )
            ):
                raise ContractError(f"{action_path}: repair tente incoherent")
            expected_replacement = (
                RELATIONSHIP_REPAIR_REPLACEMENT_ID
                if action["repair_outcome"] == "accepted"
                else SAFE_RELATIONSHIP_REPLACEMENT_ID
            )
            if replacement_id != expected_replacement:
                raise ContractError(f"{action_path}: resultat repair incoherent")
        actions.append(action)
    if [action["case_id"] for action in actions] != guarded_ids:
        raise ContractError(f"{path}.actions: couverture owner-only invalide")
    if (
        observation["observed_finish_calls"] != repair_count
        or observation["observed_repair_calls"] != repair_count
    ):
        raise ContractError(f"{path}: compteurs repair divergents")
    return observation


def _validate_execution_observation_v3(
    value: Any,
    *,
    suite: LoadedSuite,
    binding: LoadedCausalShadowBinding,
    repair_calls: int,
) -> dict[str, Any]:
    path = "responses.artifact.execution_observation"
    observation = _object(
        value,
        path,
        {
            "schema_version",
            "backend_mode",
            "executing_release_git_sha",
            "deployment_state",
            "current_release_git_sha_before",
            "current_release_git_sha_after",
            "preflight_model_calls",
            "primary_model_calls",
            "repair_model_calls",
            "total_model_calls",
        },
    )
    if observation["schema_version"] != "ava.relationship.execution-observation/v3":
        raise ContractError(f"{path}.schema_version: version inattendue")
    if observation["backend_mode"] != "configured-anthropic":
        raise ContractError(f"{path}.backend_mode: backend non promotable")
    if observation["executing_release_git_sha"] != binding.release_git_sha:
        raise ContractError(f"{path}.executing_release_git_sha: release divergente")
    if observation["deployment_state"] != binding.deployment_state:
        raise ContractError(f"{path}.deployment_state: etat divergent")
    current_git_sha = binding.causal_pair.document["candidate"]["git_sha"]
    if (
        observation["current_release_git_sha_before"] != current_git_sha
        or observation["current_release_git_sha_after"] != current_git_sha
    ):
        raise ContractError(f"{path}: current a derive pendant le shadow")
    expected = {
        "preflight_model_calls": 4,
        "primary_model_calls": len(suite.corpus["cases"]),
        "repair_model_calls": repair_calls,
        "total_model_calls": 4 + len(suite.corpus["cases"]) + repair_calls,
    }
    for key, expected_value in expected.items():
        if type(observation[key]) is not int or observation[key] != expected_value:
            raise ContractError(f"{path}.{key}: {expected_value} requis")
    return observation


def _validate_guard_observation(
    value: Any,
    *,
    suite: LoadedSuite,
    expected_role: str,
) -> dict[str, Any]:
    if suite.manifest["schema_version"] == MANIFEST_SCHEMA_VERSION:
        raise ContractError("guard observation v3 exige le binding causal")
    path = "responses.artifact.guard_observation"
    observation = _object(
        value,
        path,
        {
            "schema_version",
            "active",
            "policy_id",
            "policy_sha256",
            "expected_prepare_calls",
            "observed_prepare_calls",
            "expected_apply_calls",
            "observed_apply_calls",
            "actions",
        },
    )
    if observation["schema_version"] != "ava.relationship.guard-observation/v2":
        raise ContractError(f"{path}.schema_version: version inattendue")
    active = _boolean(observation["active"], f"{path}.active")
    integer_fields = (
        "expected_prepare_calls",
        "observed_prepare_calls",
        "expected_apply_calls",
        "observed_apply_calls",
    )
    for key in integer_fields:
        if type(observation[key]) is not int or observation[key] < 0:
            raise ContractError(f"{path}.{key}: entier positif requis")

    if expected_role == "baseline":
        if active:
            raise ContractError(f"{path}.active: baseline doit rester inactive")
        if (
            observation["policy_id"] is not None
            or observation["policy_sha256"] is not None
        ):
            raise ContractError(f"{path}: baseline ne doit pas attester un garde actif")
        if any(observation[key] != 0 for key in integer_fields):
            raise ContractError(f"{path}: compteurs baseline nuls requis")
        if _list(observation["actions"], f"{path}.actions"):
            raise ContractError(f"{path}.actions: baseline doit rester vide")
        return observation

    if not active:
        raise ContractError(f"{path}.active: candidat doit activer le garde")
    if observation["policy_id"] != RELATIONSHIP_TEXT_SAFETY_POLICY_ID:
        raise ContractError(f"{path}.policy_id: politique inattendue")
    policy_digest = _sha256(observation["policy_sha256"], f"{path}.policy_sha256")
    if policy_digest != suite.safety_policy_sha256:
        raise ContractError(f"{path}.policy_sha256: politique divergente")
    expected_case_ids = [case["id"] for case in suite.corpus["cases"]]
    guarded_case_ids = [
        case["id"] for case in suite.corpus["cases"] if _relationship_allowed_case(case)
    ]
    expected_counts = {
        "expected_prepare_calls": len(expected_case_ids),
        "observed_prepare_calls": len(expected_case_ids),
        "expected_apply_calls": len(guarded_case_ids),
        "observed_apply_calls": len(guarded_case_ids),
    }
    for key, expected in expected_counts.items():
        if observation[key] != expected:
            raise ContractError(f"{path}.{key}: {expected} requis")
    raw_actions = _list(observation["actions"], f"{path}.actions")
    actions: list[dict[str, Any]] = []
    for index, raw_action in enumerate(raw_actions):
        action_path = f"{path}.actions[{index}]"
        action = _object(raw_action, action_path, {"case_id", "action", "gate_ids"})
        _string(action["case_id"], f"{action_path}.case_id", pattern=_CASE_ID_RE)
        if action["action"] not in {"pass", "replace"}:
            raise ContractError(f"{action_path}.action: action inconnue")
        gate_ids = _string_list(action["gate_ids"], f"{action_path}.gate_ids")
        if not set(gate_ids).issubset(RUNTIME_GUARD_GATE_IDS):
            raise ContractError(f"{action_path}.gate_ids: gate runtime inconnu")
        if (action["action"] == "pass") != (not gate_ids):
            raise ContractError(f"{action_path}: action et gates incoherents")
        actions.append(action)
    if [action["case_id"] for action in actions] != guarded_case_ids:
        raise ContractError(f"{path}.actions: couverture owner-only invalide")
    return observation


def load_response_bundle(
    response_path: str | Path,
    suite: LoadedSuite,
    *,
    expected_role: str,
    expected_sha256: str | None = None,
    release_attestation_path: str | Path | None = None,
    release_attestation_sha256: str | None = None,
    peer_release_attestation_path: str | Path | None = None,
    peer_release_attestation_sha256: str | None = None,
    causal_pair_path: str | Path | None = None,
    causal_pair_sha256: str | None = None,
) -> LoadedResponses:
    if expected_role not in {"baseline", "candidate"}:
        raise ContractError("role de comparaison interne invalide")
    if expected_sha256 is None:
        path = Path(response_path).expanduser().absolute()
        payload = _strict_regular_payload(
            path,
            max_bytes=_MAX_RESPONSE_BUNDLE_BYTES,
            label="responses",
        )
        document = _json_object_from_payload(payload, path)
        response_digest = sha256_bytes(payload)
    else:
        path, document, response_digest = _strict_external_document(
            response_path,
            expected_sha256=expected_sha256,
            max_bytes=_MAX_RESPONSE_BUNDLE_BYTES,
        )
    response_schema_version = document.get("schema_version")
    is_v3_response = response_schema_version == RESPONSES_SCHEMA_VERSION
    bundle_keys = {"schema_version", "corpus", "artifact", "responses"}
    if is_v3_response:
        bundle_keys.add("evaluation_manifest_sha256")
    bundle = _object(
        document,
        "responses",
        bundle_keys,
    )
    manifest_version = suite.manifest["schema_version"]
    is_governed = manifest_version in {
        MANIFEST_SCHEMA_VERSION_V2,
        MANIFEST_SCHEMA_VERSION,
    }
    if manifest_version == MANIFEST_SCHEMA_VERSION_V2 and is_v3_response:
        raise ContractError(
            "responses.schema_version: shadow v3 interdit avec manifeste historique v2"
        )
    if is_v3_response and expected_sha256 is None:
        raise ContractError("responses v3 exige une empreinte pre-epinglee")
    supported_response_versions = {
        MANIFEST_SCHEMA_VERSION_V1: {RESPONSES_SCHEMA_VERSION_V1},
        MANIFEST_SCHEMA_VERSION_V2: {RESPONSES_SCHEMA_VERSION_V2},
        MANIFEST_SCHEMA_VERSION: {RESPONSES_SCHEMA_VERSION},
    }[manifest_version]
    if bundle["schema_version"] not in supported_response_versions:
        raise ContractError("responses.schema_version: version non supportee")
    if is_v3_response:
        evaluation_manifest_sha256 = _sha256(
            bundle["evaluation_manifest_sha256"],
            "responses.evaluation_manifest_sha256",
        )
        if evaluation_manifest_sha256 != suite.manifest_sha256:
            raise ContractError(
                "responses.evaluation_manifest_sha256: manifeste divergent"
            )
    corpus_ref = _object(bundle["corpus"], "responses.corpus", {"id", "version"})
    if corpus_ref != {
        "id": suite.corpus["corpus_id"],
        "version": suite.corpus["version"],
    }:
        raise ContractError("responses.corpus: corpus incompatible")

    artifact_keys = {
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
    }
    if is_governed:
        artifact_keys.update({"safety_policy_sha256", "guard_observation"})
    if is_v3_response:
        artifact_keys.update(
            {"causal_pair_sha256", "execution_observation", "treatment"}
        )
    artifact = _object(
        bundle["artifact"],
        "responses.artifact",
        artifact_keys,
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
    if is_governed:
        safety_digest = _sha256(
            artifact["safety_policy_sha256"],
            "responses.artifact.safety_policy_sha256",
        )
        if safety_digest != suite.safety_policy_sha256:
            raise ContractError("responses.artifact.safety_policy_sha256: divergence")
        if not is_v3_response:
            _validate_guard_observation(
                artifact["guard_observation"],
                suite=suite,
                expected_role=expected_role,
            )
    if (release_attestation_path is None) != (release_attestation_sha256 is None):
        raise ContractError(
            "responses.artifact.release_attestation: chemin et empreinte indivisibles"
        )
    loaded_release_attestation: LoadedReleaseAttestation | None = None
    loaded_peer_release_attestation: LoadedReleaseAttestation | None = None
    loaded_causal_pair: LoadedCausalPair | None = None
    release_digest = artifact["release_attestation_sha256"]
    release = artifact["release"]
    if is_v3_response:
        required_external = (
            release_attestation_path,
            release_attestation_sha256,
            peer_release_attestation_path,
            peer_release_attestation_sha256,
            causal_pair_path,
            causal_pair_sha256,
        )
        if any(value is None for value in required_external):
            raise ContractError(
                "responses.artifact.causal_pair: preuves externes v3 requises"
            )
        _sha256(
            artifact["causal_pair_sha256"],
            "responses.artifact.causal_pair_sha256",
        )
    if artifact["source_kind"] == "offline_shadow":
        if not is_v3_response:
            raise ContractError(
                "responses.schema_version: shadow sans manifeste d'evaluation lie"
            )
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
        if (
            release_attestation_path is not None
            and release_attestation_sha256 is not None
        ):
            if is_v3_response:
                causal_inputs = (
                    peer_release_attestation_path,
                    peer_release_attestation_sha256,
                    causal_pair_path,
                    causal_pair_sha256,
                )
                if any(value is None for value in causal_inputs):
                    raise ContractError(
                        "responses.artifact.causal_pair: preuves externes incompletes"
                    )
                binding = load_causal_shadow_binding(
                    release_attestation_path=release_attestation_path,
                    release_attestation_sha256=release_attestation_sha256,
                    peer_release_attestation_path=peer_release_attestation_path,
                    peer_release_attestation_sha256=peer_release_attestation_sha256,
                    causal_pair_path=causal_pair_path,
                    causal_pair_sha256=causal_pair_sha256,
                    expected_manifest_sha256=suite.manifest_sha256,
                )
                loaded_release_attestation = binding.release_attestation
                loaded_peer_release_attestation = binding.peer_release_attestation
                loaded_causal_pair = binding.causal_pair
                if release_digest != binding.release_attestation.sha256:
                    raise ContractError(
                        "responses.artifact.release_attestation_sha256: "
                        "attestation externe divergente"
                    )
                if binding.role != expected_role or artifact["role"] != binding.role:
                    raise ContractError(
                        "responses.artifact.role: binding causal divergent"
                    )
                if artifact["treatment"] != binding.treatment:
                    raise ContractError(
                        "responses.artifact.treatment: binding causal divergent"
                    )
                if artifact["causal_pair_sha256"] != binding.causal_pair.sha256:
                    raise ContractError(
                        "responses.artifact.causal_pair_sha256: preuve divergente"
                    )
                attestation = binding.release_attestation.document
                expected_attested_release = attestation["release"]
                expected_release = {
                    "repository": expected_attested_release["repository"],
                    "git_sha": expected_attested_release["git_sha"],
                    "adapter": attestation["engine"]["adapter"],
                    "config_sha256": attestation["engine"]["config_sha256"],
                    "manifest_sha256": attestation["artifact"]["manifest_sha256"],
                }
                if release != expected_release:
                    raise ContractError(
                        "responses.artifact.release: attestation externe divergente"
                    )
                expected_engine = {
                    key: attestation["engine"][key]
                    for key in ("provider", "model", "revision")
                }
                if engine != expected_engine:
                    raise ContractError(
                        "responses.artifact.engine: attestation externe divergente"
                    )
                guard = _validate_guard_observation_v3(
                    artifact["guard_observation"],
                    suite=suite,
                    expected_role=binding.role,
                    expected_treatment=binding.treatment,
                )
                _validate_execution_observation_v3(
                    artifact["execution_observation"],
                    suite=suite,
                    binding=binding,
                    repair_calls=guard["observed_repair_calls"],
                )
            else:
                if any(
                    value is not None
                    for value in (
                        peer_release_attestation_path,
                        peer_release_attestation_sha256,
                        causal_pair_path,
                        causal_pair_sha256,
                    )
                ):
                    raise ContractError(
                        "responses.artifact.causal_pair: interdit hors v3"
                    )
                loaded_release_attestation = load_release_attestation(
                    release_attestation_path,
                    expected_sha256=release_attestation_sha256,
                )
                if loaded_release_attestation.sha256 != release_digest:
                    raise ContractError(
                        "responses.artifact.release_attestation_sha256: "
                        "attestation externe divergente"
                    )
                attestation = loaded_release_attestation.document
                expected_release = {
                    "repository": release["repository"],
                    "git_sha": release["git_sha"],
                }
                expected_engine = {
                    "provider": engine["provider"],
                    "model": engine["model"],
                    "revision": engine["revision"],
                    "adapter": release["adapter"],
                    "config_sha256": release["config_sha256"],
                }
                expected_artifact = {"manifest_sha256": release["manifest_sha256"]}
                if attestation["release"] != expected_release:
                    raise ContractError(
                        "responses.artifact.release: attestation externe divergente"
                    )
                if attestation["engine"] != expected_engine:
                    raise ContractError(
                        "responses.artifact.engine: attestation externe divergente"
                    )
                if attestation["artifact"] != expected_artifact:
                    raise ContractError(
                        "responses.artifact.release.manifest_sha256: "
                        "attestation externe divergente"
                    )
    elif is_v3_response:
        raise ContractError(
            "responses.schema_version: v3 reserve aux shadows hors ligne"
        )
    elif release_digest is not None or release is not None:
        raise ContractError(
            "responses.artifact.release: interdit pour une fixture synthetique"
        )
    elif release_attestation_path is not None:
        raise ContractError(
            "responses.artifact.release_attestation: interdite pour une fixture"
        )
    elif any(
        value is not None
        for value in (
            peer_release_attestation_path,
            peer_release_attestation_sha256,
            causal_pair_path,
            causal_pair_sha256,
        )
    ):
        raise ContractError("responses.artifact.causal_pair: interdit pour une fixture")
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
        sha256=response_digest,
        by_case_id=by_case_id,
        release_attestation=loaded_release_attestation,
        causal_pair=loaded_causal_pair,
        peer_release_attestation=loaded_peer_release_attestation,
    )


def reload_response_bundle(
    responses: LoadedResponses,
    suite: LoadedSuite,
    *,
    expected_role: str,
) -> LoadedResponses:
    """Reload a bundle and all of its external causal evidence from byte pins."""

    if type(responses) is not LoadedResponses:
        raise ContractError("responses: type charge strict attendu")
    release_attestation = responses.release_attestation
    peer_attestation = responses.peer_release_attestation
    causal_pair = responses.causal_pair
    if causal_pair is not None and (
        release_attestation is None or peer_attestation is None
    ):
        raise ContractError("responses: binding causal charge incomplet")
    if causal_pair is None and peer_attestation is not None:
        raise ContractError("responses: peer attestation sans causal pair")
    return load_response_bundle(
        responses.path,
        suite,
        expected_role=expected_role,
        expected_sha256=responses.sha256,
        release_attestation_path=(
            release_attestation.path if release_attestation is not None else None
        ),
        release_attestation_sha256=(
            release_attestation.sha256 if release_attestation is not None else None
        ),
        peer_release_attestation_path=(
            peer_attestation.path if peer_attestation is not None else None
        ),
        peer_release_attestation_sha256=(
            peer_attestation.sha256 if peer_attestation is not None else None
        ),
        causal_pair_path=causal_pair.path if causal_pair is not None else None,
        causal_pair_sha256=causal_pair.sha256 if causal_pair is not None else None,
    )
