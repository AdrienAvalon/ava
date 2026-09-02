"""Build a relationship-shadow attestation from one deployed Ava release.

The command has no provider/model/adapter override.  Those values are derived
from the immutable release manifest and the exact TOML configuration bytes used
by the deployment.  Configuration contents and provider credentials are never
copied to the attestation or diagnostic output.
"""

from __future__ import annotations

import argparse
import ast
import base64
import configparser
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import sysconfig
import tarfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import tomllib

from .contracts import (
    CAUSAL_PAIR_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    RELEASE_ATTESTATION_SCHEMA_VERSION,
    ContractError,
    canonical_json_bytes,
    load_causal_pair,
    load_release_attestation,
    load_suite,
    python_runtime_sha256,
    rust_builder_recipe_sha256,
    sha256_bytes,
)

_MODULE_PATH = Path(__file__)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9._+-]{1,120}$")
_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$")
_SAFE_RUNTIME_FILENAME_RE = re.compile(r"^(?=.{1,255}$)(?!\.\.?$)[A-Za-z0-9._+-]+$")
_MANIFEST_KEYS = (
    "format",
    "git_sha",
    "source_tree_sha256",
    "rust_tree_sha256",
    "wheel_sha256",
    "wheel_filename",
    "attestation_sha256",
    "evolutions_sha256",
)
_MAX_MANIFEST_BYTES = 8 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_LOCK_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 65536
_MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_WHEEL_MEMBERS = 20_000
_MAX_WHEEL_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_WHEEL_MEMBER_BYTES = 512 * 1024 * 1024
_MAX_WHEEL_CONTROL_BYTES = 64 * 1024 * 1024
_MAX_WHEEL_SCRIPT_BYTES = 16 * 1024 * 1024
_MAX_WHEELHOUSE_CONTROL_BYTES = 256 * 1024 * 1024
_MAX_WHEELHOUSE_FILES = 1_000
_TARGET_CPYTHON_MINOR = 12
_TARGET_GLIBC = (2, 36)
_MAX_COMPRESSION_RATIO = 200
_MAX_ARCHIVE_PATH_BYTES = 4096
_MAX_RUNTIME_TREE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_STDLIB_TREE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RUNTIME_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RUNTIME_ENTRIES = 250_000
_MAX_RECORD_BYTES = 32 * 1024 * 1024
_MAX_SEALED_MANIFEST_BYTES = 128 * 1024 * 1024
_TREATMENT_MODULE_PATH = "ava_extensions/identity/relationship_guard_treatment.py"
_RUST_BUILDER_DOCKERFILE_PATH = "deploy/docker/Dockerfile.rust-builder"
_EVALUATION_MANIFEST_PATH = "ava_extensions/evals/relationship/data/manifest.v3.json"
_TREATMENT_NAME = "RELATIONSHIP_GUARD_TREATMENT"
_BASELINE_TREATMENT = "shadow-baseline-only-v1"
_CANDIDATE_TREATMENT = "runtime-enforced-v1"
_CURRENT_LINK = Path("/var/lib/ava/current")
_SEALED_RELEASE_ROOT = Path("/var/lib/ava/releases")
_DEPLOYED_CONFIG_PATH = Path("/home/avalon/.openjarvis/config.toml")
_GIT_EXECUTABLE = Path("/usr/bin/git")
_UV_EXECUTABLE = Path("/usr/local/bin/ava-uv")
_PROCESS_EXECUTABLE = Path(sys.executable).expanduser().absolute()
_PROCESS_STDLIB_ROOT = Path(sysconfig.get_path("stdlib")).expanduser().absolute()
_PROCESS_EUID = os.geteuid()
_TRUSTED_RUNTIME_UID = 0
_TRUSTED_RUNTIME_GID = 0
_PYTHON_RUNTIME_FORMAT = "ava.python-runtime/v1"
_PYTHON_LOCK_PATH = "uv.lock"
_PYTHON_RUNTIME_SOURCE_PATH = "deploy/runtime/ava-python-runtime.v1.json"
_PYTHON_REQUIREMENTS_PATH = "deploy/runtime/ava-runtime-requirements.v1.txt"
_PYTHON_WHEELHOUSE_MANIFEST_PATH = "deploy/runtime/ava-runtime-wheelhouse.v1.json"
_PYTHON_WHEELHOUSE_PATH = "deploy/runtime/wheels"
_SEALED_WHEELHOUSE_PATH = ".ava-artifacts/python-wheelhouse"
_FRONTEND_ARCHIVE_PATH = ".ava-artifacts/frontend-static.tar"
_FRONTEND_BUILD_ATTESTATION_PATH = ".ava-artifacts/frontend-build-attestation.json"
_FRONTEND_INSTALL_PATH = "src/openjarvis/server/static"
_FRONTEND_BUILD_ATTESTATION_SCHEMA = "ava.frontend.build-attestation/v1"
_FRONTEND_BUILDER_DOCKERFILE_PATH = "deploy/docker/Dockerfile.frontend-builder"
_FRONTEND_BUILDER_DOCKERFILE_SHA256 = (
    "e69c746eee045bafc46d4951ab9522bd39ab13f2f830e61640a7ad010a10a58e"
)
_FRONTEND_BUILDER_BASE_IMAGE = (
    "node:22.23.0-slim@sha256:"
    "d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2"
)
_FRONTEND_BUILDER_PLATFORM = "linux/amd64"
_FRONTEND_NODE_VERSION = "22.23.0"
_FRONTEND_NPM_VERSION = "10.9.8"
_SOURCE_MAP_FORMAT = "ava-source-map-v1"
_RUNTIME_MAP_FORMAT = "ava-runtime-map-v1"
_FILES_MAP_FORMAT = "ava-files-map-v1"
_RESERVED_SOURCE_TOP_LEVEL = frozenset(
    {
        ".ava-artifacts",
        ".ava-building",
        ".ava-files-manifest.jsonl",
        ".ava-ready",
        ".ava-release",
        ".ava-runtime-manifest.jsonl",
        ".ava-seal.json",
        ".ava-source-manifest.jsonl",
        ".python",
        ".venv",
    }
)
_PYTHON_RUNTIME_ARCHIVE_NAME = (
    "cpython-3.12.13+20260510-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
)
_DOCOPT_WHEEL_PATH = "deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl"
_DOCOPT_WHEEL_SHA256 = (
    "sha256:6d6eabf5974d0b72899f74ecb6ae84f0d436ca0f7b3037ffc7b9a8a0790a6813"
)
_CRITICAL_IMPORT_PATHS = {
    "anthropic": "anthropic/__init__.py",
    "cryptography": "cryptography/__init__.py",
    "httpcore": "httpcore/__init__.py",
    "httpx": "httpx/__init__.py",
}
_CRITICAL_DISTRIBUTIONS = frozenset(_CRITICAL_IMPORT_PATHS)
_UV_SYNC_ARGUMENTS = (
    "--no-config",
    "pip",
    "sync",
    f"<release>/{_PYTHON_REQUIREMENTS_PATH}",
    "--python=<release>/.venv/bin/python",
    "--require-hashes",
    "--no-build",
    "--offline",
    "--link-mode=copy",
    "--no-index",
    "--no-cache",
    "--find-links=<release>/.ava-artifacts/python-wheelhouse",
)
_PYTHON_IDENTITY_PROBE = (
    "import json,sys;"
    "print(json.dumps({'base_prefix':sys.base_prefix,'executable':sys.executable,"
    "'version':'.'.join(map(str,sys.version_info[:3]))},sort_keys=True))"
)
_SEALED_RUNTIME_RECIPE = {
    "format": "ava-runtime-recipe-v1",
    "python_probe_sha256": hashlib.sha256(
        _PYTHON_IDENTITY_PROBE.encode("ascii")
    ).hexdigest(),
    "python_version": "3.12.13",
    "rust_install_flags": [
        "--no-config",
        "pip",
        "install",
        "--python=<release>/.venv/bin/python",
        "--no-deps",
        "--no-build",
        "--offline",
        "--link-mode=copy",
        "--no-index",
        "--no-cache",
        "--find-links=<release>/.ava-artifacts",
        "<pinned-rust-name-version>",
    ],
    "sync_flags": list(_UV_SYNC_ARGUMENTS),
    "venv_flags": ["-I", "-m", "venv", "--copies", "--without-pip"],
}
_RUST_ATTESTATION_KEYS = (
    "format",
    "attestation_type",
    "signature",
    "git_sha",
    "rust_tree_sha256",
    "wheel_sha256",
    "wheel_filename",
    "builder_image_id",
    "builder_dockerfile_sha256",
    "builder_platform",
    "builder_python_image",
    "builder_rust_image",
    "python_version",
    "rust_version",
    "maturin_version",
    "wheel_compatibility",
)


@dataclass(frozen=True, slots=True)
class ReleaseAttestationResult:
    """Non-secret metadata needed to pin the generated document externally."""

    output_path: Path
    sha256: str
    git_sha: str


@dataclass(frozen=True, slots=True)
class CausalPairResult:
    """Non-secret metadata needed to pin one generated causal pair."""

    output_path: Path
    sha256: str
    baseline_git_sha: str
    candidate_git_sha: str


@dataclass(frozen=True, slots=True)
class WheelArtifact:
    """Exact identity and payload map recovered from one immutable wheel."""

    filename: str
    name: str
    version: str
    sha256: str
    payload_sha256: str
    dist_info_root: str
    control_contents: dict[str, bytes]
    modes: dict[str, str]
    record: dict[str, tuple[str, int]]


def _strict_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    allow_empty: bool = False,
) -> bytes:
    candidate = path.expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("source d'attestation introuvable") from exc
    if resolved != candidate:
        raise ContractError("source d'attestation liee ou indirecte")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractError("source d'attestation illisible") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > max_bytes
            or (before.st_size == 0 and not allow_empty)
        ):
            raise ContractError("source d'attestation non reguliere ou hors taille")
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
            or after.st_size != before.st_size
            or after.st_mode != before.st_mode
        ):
            raise ContractError("source d'attestation modifiee pendant la lecture")
        return payload
    except OSError as exc:
        raise ContractError("source d'attestation illisible") from exc
    finally:
        os.close(descriptor)


def _writable_by_effective_user(metadata: os.stat_result) -> bool:
    if os.geteuid() == metadata.st_uid:
        return bool(metadata.st_mode & stat.S_IWUSR)
    if metadata.st_gid in {os.getegid(), *os.getgroups()}:
        return bool(metadata.st_mode & stat.S_IWGRP)
    return bool(metadata.st_mode & stat.S_IWOTH)


def _trusted_sealed_metadata(path: Path, *, directory: bool) -> os.stat_result:
    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_type(metadata.st_mode)
            or metadata.st_uid != _TRUSTED_RUNTIME_UID
            or metadata.st_gid != _TRUSTED_RUNTIME_GID
            or metadata.st_mode & 0o222
            or stat.S_IMODE(metadata.st_mode)
            not in ({0o555} if directory else {0o444, 0o555})
            or (not directory and metadata.st_nlink != 1)
            or candidate.resolve(strict=True) != candidate
        ):
            raise ContractError("runtime Python non scelle ou indirect")
    except OSError as exc:
        raise ContractError("runtime Python scelle indisponible") from exc
    return metadata


def _trusted_authority_directory(path: Path) -> os.stat_result:
    """Validate a root-owned container which may need owner-write to publish."""

    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _TRUSTED_RUNTIME_UID
            or metadata.st_gid != _TRUSTED_RUNTIME_GID
            or metadata.st_mode & 0o022
            or stat.S_IMODE(metadata.st_mode) not in {0o555, 0o755}
            or candidate.resolve(strict=True) != candidate
        ):
            raise ContractError("racine authoritative mutable ou indirecte")
    except OSError as exc:
        raise ContractError("racine authoritative indisponible") from exc
    return metadata


def _verify_sealed_release_root(release_root: Path) -> tuple[int, int]:
    root = release_root.expanduser().absolute()
    expected_parent = _SEALED_RELEASE_ROOT.expanduser().absolute()
    if root.parent != expected_parent or _PROCESS_EUID == _TRUSTED_RUNTIME_UID:
        raise ContractError("release hors racine scellee autoritaire")
    if expected_parent == Path("/var/lib/ava/releases"):
        for ancestor in (
            Path("/var"),
            Path("/var/lib"),
            Path("/var/lib/ava"),
        ):
            _trusted_authority_directory(ancestor)
    _trusted_authority_directory(expected_parent)
    metadata = _trusted_sealed_metadata(root, directory=True)
    return metadata.st_dev, metadata.st_ino


def _strict_sealed_digest(path: Path, *, max_bytes: int) -> tuple[str, int, int]:
    candidate = path.expanduser().absolute()
    before = _trusted_sealed_metadata(candidate, directory=False)
    if before.st_size < 0 or before.st_size > max_bytes or before.st_nlink != 1:
        raise ContractError("fichier runtime Python hors taille ou lie physiquement")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractError("fichier runtime Python illisible") from exc
    digest = hashlib.sha256()
    observed_size = 0
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_mode != before.st_mode
            or opened.st_size != before.st_size
            or opened.st_uid != before.st_uid
            or opened.st_gid != before.st_gid
            or opened.st_nlink != before.st_nlink
        ):
            raise ContractError("fichier runtime Python remplace avant lecture")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > max_bytes:
                raise ContractError("fichier runtime Python hors taille")
            digest.update(chunk)
        after = candidate.lstat()
        if (
            observed_size != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_mode != before.st_mode
            or after.st_size != before.st_size
            or after.st_uid != before.st_uid
            or after.st_gid != before.st_gid
            or after.st_nlink != before.st_nlink
        ):
            raise ContractError("fichier runtime Python modifie pendant lecture")
    except OSError as exc:
        raise ContractError("fichier runtime Python illisible") from exc
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}", observed_size, stat.S_IMODE(before.st_mode)


def _sealed_tree_map(
    root: Path,
    *,
    max_bytes: int,
    reject_site_artifacts: bool,
) -> tuple[str, int, int, dict[str, dict[str, Any]]]:
    """Hash one root-owned import tree without following links or trusting pyc."""

    tree_root = root.expanduser().absolute()
    _trusted_sealed_metadata(tree_root, directory=True)
    entries: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    total_size = 0
    pending: list[tuple[Path, PurePosixPath]] = [(tree_root, PurePosixPath())]
    while pending:
        directory, relative_directory = pending.pop()
        _trusted_sealed_metadata(directory, directory=True)
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: os.fsencode(item.name)
            )
        except OSError as exc:
            raise ContractError("arbre runtime Python illisible") from exc
        for child in children:
            try:
                name = child.name
                if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                    raise ContractError("chemin runtime Python invalide")
                relative = relative_directory / name
                relative_text = relative.as_posix()
                if len(relative_text.encode("utf-8")) > _MAX_ARCHIVE_PATH_BYTES:
                    raise ContractError("chemin runtime Python hors taille")
                child_path = directory / name
                metadata = child.stat(follow_symlinks=False)
            except (OSError, UnicodeError) as exc:
                raise ContractError("entree runtime Python invalide") from exc
            if stat.S_ISDIR(metadata.st_mode):
                if reject_site_artifacts and name == "__pycache__":
                    raise ContractError("bytecode site-packages interdit")
                trusted = _trusted_sealed_metadata(child_path, directory=True)
                if (
                    trusted.st_dev != metadata.st_dev
                    or trusted.st_ino != metadata.st_ino
                    or trusted.st_mode != metadata.st_mode
                    or trusted.st_uid != metadata.st_uid
                    or trusted.st_gid != metadata.st_gid
                ):
                    raise ContractError("repertoire runtime Python remplace")
                pending.append((child_path, relative))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ContractError("lien ou type special dans runtime Python")
            if reject_site_artifacts and (
                relative.suffix == ".pyc" or relative.suffix == ".pth"
            ):
                raise ContractError("pyc ou pth interdit dans site-packages scelle")
            digest, size, mode = _strict_sealed_digest(
                child_path, max_bytes=_MAX_RUNTIME_FILE_BYTES
            )
            total_size += size
            if total_size > max_bytes:
                raise ContractError("arbre runtime Python hors taille")
            entry = {
                "mode": f"{mode:04o}",
                "path": relative_text,
                "sha256": digest,
                "size": size,
            }
            entries.append(entry)
            by_path[relative_text] = entry
            if len(entries) > _MAX_RUNTIME_ENTRIES:
                raise ContractError("arbre runtime Python contient trop d'entrees")
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return (
        sha256_bytes(canonical_json_bytes(entries)),
        len(entries),
        total_size,
        by_path,
    )


def _python_runtime_source_contract(payload: bytes) -> dict[str, Any]:
    """Validate the Git-owned pin for the standalone CPython distribution."""

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("pin source du runtime Python invalide") from exc
    if type(document) is not dict or set(document) != {
        "archive",
        "build",
        "implementation",
        "platform",
        "schema",
        "version",
    }:
        raise ContractError("pin source du runtime Python incomplet")
    archive = document["archive"]
    if type(archive) is not dict or set(archive) != {
        "member_count",
        "regular_file_bytes",
        "regular_file_count",
        "sealed_relative_path",
        "sha256",
        "size",
        "symlink_count",
        "url",
    }:
        raise ContractError("archive source du runtime Python incomplete")
    expected_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if (
        document["schema"] != "ava.python-runtime-source/v1"
        or document["implementation"] != "cpython"
        or document["version"] != expected_version
        or document["platform"] != "x86_64-unknown-linux-gnu"
        or type(document["build"]) is not str
        or re.fullmatch(r"[0-9]{8}", document["build"]) is None
        or archive["sealed_relative_path"] != ".ava-artifacts/python-runtime.tar.gz"
        or type(archive["sha256"]) is not str
        or _SHA256_RE.fullmatch(archive["sha256"]) is None
        or type(archive["url"]) is not str
        or re.fullmatch(
            r"https://releases\.astral\.sh/[A-Za-z0-9%._/+\-]+\.tar\.gz",
            archive["url"],
        )
        is None
    ):
        raise ContractError("identite source du runtime Python invalide")
    for key in (
        "member_count",
        "regular_file_bytes",
        "regular_file_count",
        "size",
        "symlink_count",
    ):
        if type(archive[key]) is not int or archive[key] <= 0:
            raise ContractError(f"archive runtime Python {key} invalide")
    if (
        archive["member_count"]
        != archive["regular_file_count"] + archive["symlink_count"]
        or archive["member_count"] > _MAX_ARCHIVE_MEMBERS
        or archive["regular_file_bytes"] > _MAX_STDLIB_TREE_BYTES
        or archive["size"] > _MAX_ARCHIVE_BYTES
        or archive["regular_file_bytes"] > archive["size"] * _MAX_COMPRESSION_RATIO
    ):
        raise ContractError("cardinalite archive runtime Python invalide")
    return document


def _reject_runtime_manifest_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ContractError(f"manifeste wheelhouse: cle JSON dupliquee: {key}")
        document[key] = value
    return document


def _reject_runtime_manifest_nonfinite(value: str) -> None:
    raise ContractError(f"manifeste wheelhouse: constante JSON interdite: {value}")


def _runtime_wheelhouse_manifest_contract(
    payload: bytes,
    *,
    requirements_payload: bytes,
    lock_payload: bytes,
) -> dict[str, str]:
    try:
        document = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_reject_runtime_manifest_duplicate_keys,
            parse_constant=_reject_runtime_manifest_nonfinite,
        )
    except ContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("manifeste wheelhouse JSON invalide") from exc
    if (
        type(document) is not dict
        or payload != canonical_json_bytes(document) + b"\n"
        or set(document)
        != {
            "entries",
            "requirements_sha256",
            "schema_version",
            "target",
            "uv_lock_sha256",
        }
        or document["schema_version"] != "ava.runtime-wheelhouse/v1"
        or document["target"]
        != {
            "implementation": "cpython",
            "platform": "linux_x86_64",
            "python_version": "3.12.13",
        }
        or document["requirements_sha256"] != sha256_bytes(requirements_payload)
        or document["uv_lock_sha256"] != sha256_bytes(lock_payload)
    ):
        raise ContractError(
            "manifeste wheelhouse non canonique ou divergent des sources runtime"
        )
    entries = document["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= _MAX_WHEELHOUSE_FILES:
        raise ContractError("manifeste wheelhouse vide ou hors borne")
    result: dict[str, str] = {}
    casefolded: set[str] = set()
    digests: set[str] = set()
    for index, entry in enumerate(entries):
        if (
            type(entry) is not dict
            or set(entry) != {"filename", "sha256"}
            or type(entry.get("filename")) is not str
            or _WHEEL_RE.fullmatch(entry["filename"]) is None
            or type(entry.get("sha256")) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise ContractError(f"manifeste wheelhouse entries[{index}] invalide")
        filename = entry["filename"]
        digest = entry["sha256"]
        if filename in result or filename.casefold() in casefolded or digest in digests:
            raise ContractError("manifeste wheelhouse contient un doublon")
        result[filename] = digest
        casefolded.add(filename.casefold())
        digests.add(digest)
    if list(result) != sorted(result, key=lambda value: value.encode("ascii")):
        raise ContractError("manifeste wheelhouse non trie")
    return result


def _python_archive_materialized_map(
    payload: bytes,
    *,
    source_contract: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    """Project a pinned CPython tarball after safe symlink materialization."""

    archive_contract = source_contract["archive"]
    if (
        len(payload) != archive_contract["size"]
        or sha256_bytes(payload) != f"sha256:{archive_contract['sha256']}"
    ):
        raise ContractError("archive runtime Python divergente du pin source")
    regular: dict[str, tuple[int, bytes]] = {}
    links: dict[str, str] = {}
    seen: dict[str, str] = {}
    regular_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= _MAX_ARCHIVE_MEMBERS:
                    raise ContractError("archive runtime Python trop volumineuse")
                raw_name = (
                    member.name.removesuffix("/") if member.isdir() else member.name
                )
                name = _safe_archive_name(raw_name, f"python_runtime.archive[{index}]")
                if name != "python" and not name.startswith("python/"):
                    raise ContractError("archive runtime Python hors prefixe")
                if name in seen:
                    raise ContractError("chemin duplique dans archive runtime Python")
                parents = PurePosixPath(name).parents
                if any(
                    seen.get(parent.as_posix()) not in {None, "directory"}
                    for parent in parents
                    if parent.as_posix() != "."
                ):
                    raise ContractError("archive runtime Python sous non-repertoire")
                if not member.isdir() and any(
                    existing.startswith(f"{name}/") for existing in seen
                ):
                    raise ContractError("archive runtime Python remplace un repertoire")
                if member.isfile():
                    if name == "python":
                        raise ContractError("archive runtime Python hors prefixe")
                    if (
                        member.size < 0
                        or member.size > _MAX_ARCHIVE_MEMBER_BYTES
                        or bool(member.sparse)
                    ):
                        raise ContractError("membre runtime Python hors taille")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ContractError("membre runtime Python illisible")
                    content = extracted.read(member.size + 1)
                    if len(content) != member.size:
                        raise ContractError("membre runtime Python tronque")
                    regular_bytes += member.size
                    if regular_bytes > _MAX_STDLIB_TREE_BYTES:
                        raise ContractError("runtime Python decompresse hors taille")
                    regular[name] = (stat.S_IMODE(member.mode), content)
                    seen[name] = "file"
                elif member.issym():
                    if name == "python":
                        raise ContractError("archive runtime Python hors prefixe")
                    target = member.linkname
                    if (
                        not target
                        or target.startswith("/")
                        or "\\" in target
                        or "\x00" in target
                    ):
                        raise ContractError("symlink runtime Python invalide")
                    resolved_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), target)
                    )
                    if resolved_target == "python" or not resolved_target.startswith(
                        "python/"
                    ):
                        raise ContractError("symlink runtime Python hors racine")
                    links[name] = resolved_target
                    seen[name] = "link"
                elif member.isdir():
                    if member.size != 0 or member.linkname:
                        raise ContractError("repertoire runtime Python avec payload")
                    seen[name] = "directory"
                else:
                    raise ContractError("type special dans archive runtime Python")
    except (tarfile.TarError, OSError) as exc:
        raise ContractError("archive runtime Python invalide") from exc
    if (
        len(regular) != archive_contract["regular_file_count"]
        or len(links) != archive_contract["symlink_count"]
        or len(regular) + len(links) != archive_contract["member_count"]
        or regular_bytes != archive_contract["regular_file_bytes"]
    ):
        raise ContractError("archive runtime Python et compteurs divergent")

    def resolve_regular(path: str) -> tuple[int, bytes]:
        visited: set[str] = set()
        current = path
        while current in links:
            if current in visited:
                raise ContractError("cycle de symlink runtime Python")
            visited.add(current)
            current = links[current]
        target = regular.get(current)
        if target is None:
            raise ContractError("symlink runtime Python non regulier")
        return target

    entries: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    materialized_size = 0
    for full_path in sorted({*regular, *links}, key=lambda item: item.encode("utf-8")):
        mode, content = regular.get(full_path) or resolve_regular(full_path)
        sealed_mode = 0o555 if mode & 0o111 else 0o444
        relative = full_path.removeprefix("python/")
        materialized_size += len(content)
        if materialized_size > _MAX_RUNTIME_TREE_BYTES:
            raise ContractError("projection runtime Python hors taille")
        contents[relative] = content
        entries.append(
            {
                "mode": f"{sealed_mode:04o}",
                "path": relative,
                "sha256": sha256_bytes(content),
                "size": len(content),
            }
        )
    return sha256_bytes(canonical_json_bytes(entries)), entries, contents


def _normalised_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _wheel_metadata_identity(payload: bytes) -> tuple[str, str]:
    """Read unique Name/Version fields from the RFC-822 header block only."""

    if b"\x00" in payload or b"\r" in payload.replace(b"\r\n", b""):
        raise ContractError("wheel avec METADATA aux separateurs invalides")
    header_block = payload.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0]
    names = [
        line.removeprefix(b"Name: ")
        for line in header_block.splitlines()
        if line.startswith(b"Name: ")
    ]
    versions = [
        line.removeprefix(b"Version: ")
        for line in header_block.splitlines()
        if line.startswith(b"Version: ")
    ]
    if len(names) != 1 or len(versions) != 1:
        raise ContractError("wheel avec METADATA incomplet ou ambigu")
    try:
        raw_name = names[0].decode("ascii")
        version = versions[0].decode("ascii")
    except UnicodeError as exc:
        raise ContractError("wheel avec METADATA non ASCII") from exc
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw_name) is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version) is None
    ):
        raise ContractError("wheel avec identite METADATA invalide")
    return _normalised_distribution_name(raw_name), version


def _wheel_filename_tags(filename: str) -> frozenset[tuple[str, str, str]]:
    """Expand and close the PEP 425 tags carried by one wheel filename."""

    if _WHEEL_RE.fullmatch(filename) is None:
        raise ContractError("nom de wheel non canonique")
    try:
        _prefix, python_field, abi_field, platform_field = filename.removesuffix(
            ".whl"
        ).rsplit("-", 3)
    except ValueError as exc:
        raise ContractError("nom de wheel sans tags fermes") from exc
    tag_component = re.compile(r"[A-Za-z0-9_]+")
    fields = [field.split(".") for field in (python_field, abi_field, platform_field)]
    if any(
        not values
        or len(values) != len(set(values))
        or any(tag_component.fullmatch(value) is None for value in values)
        for values in fields
    ):
        raise ContractError("tags compresses de wheel non canoniques")
    python_tags, abi_tags, platform_tags = fields
    return frozenset(
        (python_tag, abi_tag, platform_tag)
        for python_tag in python_tags
        for abi_tag in abi_tags
        for platform_tag in platform_tags
    )


def _wheel_tag_compatible(tag: tuple[str, str, str]) -> bool:
    python_tag, abi_tag, platform_tag = tag
    if python_tag in {"py3", "py312"}:
        python_abi_compatible = abi_tag == "none"
    elif python_tag == "cp312":
        python_abi_compatible = abi_tag in {"abi3", "cp312", "none"}
    else:
        stable_abi = re.fullmatch(r"cp3([0-9]{1,2})", python_tag)
        python_abi_compatible = (
            stable_abi is not None
            and 2 <= int(stable_abi.group(1)) <= _TARGET_CPYTHON_MINOR
            and abi_tag == "abi3"
        )
    if not python_abi_compatible:
        return False
    if platform_tag == "any":
        return abi_tag == "none"
    if platform_tag == "linux_x86_64":
        return True
    if platform_tag in {
        "manylinux1_x86_64",
        "manylinux2010_x86_64",
        "manylinux2014_x86_64",
    }:
        return True
    versioned = re.fullmatch(r"manylinux_([0-9]+)_([0-9]+)_x86_64", platform_tag)
    return (
        versioned is not None
        and (int(versioned.group(1)), int(versioned.group(2))) <= _TARGET_GLIBC
        and int(versioned.group(1)) == 2
        and int(versioned.group(2)) >= 5
    )


def _validate_wheel_tags(
    filename: str,
    wheel_payload: bytes,
) -> None:
    filename_tags = _wheel_filename_tags(filename)
    if not any(_wheel_tag_compatible(tag) for tag in filename_tags):
        raise ContractError("wheel incompatible avec CPython 3.12 Linux glibc x86_64")
    if b"\x00" in wheel_payload or b"\r" in wheel_payload.replace(b"\r\n", b""):
        raise ContractError("WHEEL contient des separateurs invalides")
    header_block = wheel_payload.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0]
    lines = header_block.splitlines()
    versions = [
        line.removeprefix(b"Wheel-Version: ")
        for line in lines
        if line.startswith(b"Wheel-Version: ")
    ]
    purelib = [
        line.removeprefix(b"Root-Is-Purelib: ")
        for line in lines
        if line.startswith(b"Root-Is-Purelib: ")
    ]
    raw_tags = [
        line.removeprefix(b"Tag: ") for line in lines if line.startswith(b"Tag: ")
    ]
    try:
        decoded_tags = [raw_tag.decode("ascii") for raw_tag in raw_tags]
    except UnicodeError as exc:
        raise ContractError("WHEEL contient un tag non ASCII") from exc
    wheel_tags: set[tuple[str, str, str]] = set()
    expanded_tag_count = 0
    for decoded_tag in decoded_tags:
        parts = decoded_tag.split("-")
        if len(parts) != 3:
            raise ContractError("WHEEL contient un tag non canonique")
        fields = [field.split(".") for field in parts]
        if any(
            not values
            or len(values) != len(set(values))
            or any(re.fullmatch(r"[A-Za-z0-9_]+", value) is None for value in values)
            for values in fields
        ):
            raise ContractError("WHEEL contient un tag non canonique")
        python_tags, abi_tags, platform_tags = fields
        expanded_tag_count += len(python_tags) * len(abi_tags) * len(platform_tags)
        wheel_tags.update(
            (python_tag, abi_tag, platform_tag)
            for python_tag in python_tags
            for abi_tag in abi_tags
            for platform_tag in platform_tags
        )
    if (
        versions != [b"1.0"]
        or len(purelib) != 1
        or purelib[0] not in {b"true", b"false"}
        or not decoded_tags
        or len(decoded_tags) != len(set(decoded_tags))
        or expanded_tag_count != len(wheel_tags)
        or frozenset(wheel_tags) != filename_tags
    ):
        raise ContractError("WHEEL et tags du filename divergent")


def _locked_distributions(
    lock_payload: bytes,
) -> tuple[
    dict[str, set[str]],
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], dict[str, str]],
]:
    try:
        document = tomllib.loads(lock_payload.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError("uv.lock runtime invalide") from exc
    packages = document.get("package")
    if type(packages) is not list:
        raise ContractError("uv.lock runtime sans packages")
    result: dict[str, set[str]] = {}
    allowed_hashes: dict[tuple[str, str], set[str]] = {}
    wheel_hashes: dict[tuple[str, str], dict[str, str]] = {}
    local_sources: set[tuple[str, str]] = set()
    for index, package in enumerate(packages):
        if type(package) is not dict:
            raise ContractError(f"uv.lock.package[{index}] invalide")
        name = package.get("name")
        version = package.get("version")
        source_descriptor = package.get("source")
        if type(name) is not str or not name or type(source_descriptor) is not dict:
            raise ContractError(f"uv.lock.package[{index}] incomplet")
        if len(source_descriptor) != 1:
            raise ContractError(f"uv.lock.package[{index}].source invalide")
        source_kind = next(iter(source_descriptor))
        if source_kind != "registry":
            local_sources.add((_normalised_distribution_name(name), source_kind))
            continue
        if type(name) is not str or type(version) is not str or not name or not version:
            raise ContractError(f"uv.lock.package[{index}] incomplet")
        result.setdefault(_normalised_distribution_name(name), set()).add(version)
        package_key = (_normalised_distribution_name(name), version)
        if package_key in allowed_hashes:
            raise ContractError("uv.lock contient un package registry duplique")
        package_allowed = allowed_hashes.setdefault(package_key, set())
        source = package.get("sdist")
        if source is not None:
            if (
                type(source) is not dict
                or type(source.get("hash")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", source["hash"]) is None
            ):
                raise ContractError(f"uv.lock.package[{index}].sdist invalide")
            package_allowed.add(source["hash"])
        wheels = package.get("wheels", [])
        if type(wheels) is not list:
            raise ContractError(f"uv.lock.package[{index}].wheels invalide")
        package_wheels = wheel_hashes.setdefault(package_key, {})
        for wheel in wheels:
            if (
                type(wheel) is not dict
                or type(wheel.get("hash")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", wheel["hash"]) is None
                or type(wheel.get("url")) is not str
            ):
                raise ContractError(f"uv.lock.package[{index}].wheel invalide")
            filename = _wheel_filename_from_url(wheel["url"])
            if filename in package_wheels:
                raise ContractError("uv.lock contient un nom de wheel duplique")
            package_allowed.add(wheel["hash"])
            package_wheels[filename] = wheel["hash"]
    if local_sources != {("openjarvis", "editable"), ("openjarvis-rust", "directory")}:
        raise ContractError("uv.lock contient une source locale non autorisee")
    return result, allowed_hashes, wheel_hashes


def _wheel_filename_from_url(value: str) -> str:
    try:
        path = urllib.parse.urlsplit(value).path
        encoded = path.rsplit("/", 1)[-1].encode("ascii")
        decoded_bytes = urllib.parse.unquote_to_bytes(encoded)
        decoded = decoded_bytes.decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ContractError("uv.lock contient une URL de wheel invalide") from exc
    if (
        not decoded
        or any(character in decoded_bytes for character in (b"/", b"\\", b"\x00", b"%"))
        or urllib.parse.quote_from_bytes(decoded_bytes, safe="")
        != encoded.decode("ascii")
        or _WHEEL_RE.fullmatch(decoded) is None
    ):
        raise ContractError("uv.lock contient un nom de wheel non canonique")
    return decoded


def _runtime_source_wheel_pins(
    source_contents: dict[str, bytes],
) -> dict[str, tuple[str, WheelArtifact]]:
    prefix = f"{_PYTHON_WHEELHOUSE_PATH}/"
    paths = sorted(path for path in source_contents if path.startswith(prefix))
    if not paths or len(paths) > _MAX_WHEELHOUSE_FILES:
        raise ContractError("wheelhouse runtime source divergent")
    result: dict[str, tuple[str, WheelArtifact]] = {}
    identities: set[tuple[str, str]] = set()
    for path in paths:
        filename = path.removeprefix(prefix)
        if "/" in filename or _WHEEL_RE.fullmatch(filename) is None:
            raise ContractError("wheelhouse runtime source divergent")
        payload = source_contents[path]
        artifact = _wheel_artifact(payload, filename=filename)
        identity = (artifact.name, artifact.version)
        if filename in result or identity in identities:
            raise ContractError("wheelhouse runtime source duplique")
        identities.add(identity)
        result[filename] = (sha256_bytes(payload), artifact)
    return result


def _runtime_requirements_contract(
    payload: bytes,
    *,
    lock_payload: bytes,
    source_contents: dict[str, bytes],
    expected_pins: dict[str, str] | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, set[str]],
    str,
    str,
    dict[str, tuple[str, WheelArtifact]],
    dict[tuple[str, str], dict[str, str]],
]:
    """Close requirements against every lock artifact and local wheel pin."""

    try:
        text = payload.decode("ascii")
    except UnicodeError as exc:
        raise ContractError("requirements runtime non ASCII") from exc
    if "\r" in text or "\x00" in text:
        raise ContractError("requirements runtime contient un separateur interdit")
    logical = re.sub(r"\\\n[ \t]*", " ", text)
    if "\\" in logical:
        raise ContractError("requirements runtime contient une continuation invalide")

    locked, allowed_hashes, wheel_hashes = _locked_distributions(lock_payload)
    source_pins = _runtime_source_wheel_pins(source_contents)
    if expected_pins is not None:
        if set(source_pins) != set(expected_pins):
            raise ContractError("pins de wheels runtime et Git divergent")
        for filename, expected in expected_pins.items():
            observed = source_pins[filename][0]
            if observed != f"sha256:{expected}":
                raise ContractError("pin de wheel runtime et Git divergent")
    local_by_identity: dict[tuple[str, str], tuple[str, str]] = {}
    for filename, (digest, artifact) in source_pins.items():
        identity = (artifact.name, artifact.version)
        if identity in local_by_identity:
            raise ContractError("pins de wheels runtime avec identite dupliquee")
        local_by_identity[identity] = (filename, digest)

    requirement_pattern = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)=="
        r"([A-Za-z0-9][A-Za-z0-9.!+_-]*)"
        r"(?:\s*;\s*([A-Za-z0-9_ .,'\"<>=!()~+\-]+))?$"
    )
    details: dict[tuple[str, str], dict[str, Any]] = {}
    versions: dict[str, set[str]] = {}
    projection: list[dict[str, Any]] = []
    for index, raw_line in enumerate(logical.splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        segments = line.split(" --hash=sha256:")
        if len(segments) < 2:
            raise ContractError(f"requirements runtime ligne {index} sans hash")
        requirement_text = segments[0].strip()
        raw_hashes = [value.strip() for value in segments[1:]]
        if any(_SHA256_RE.fullmatch(value) is None for value in raw_hashes):
            raise ContractError(f"requirements runtime ligne {index} hash invalide")
        hashes = {f"sha256:{value}" for value in raw_hashes}
        if len(hashes) != len(raw_hashes):
            raise ContractError("requirements runtime contient un hash duplique")
        match = requirement_pattern.fullmatch(requirement_text)
        if match is None:
            raise ContractError(f"requirements runtime ligne {index} invalide")
        name = _normalised_distribution_name(match.group(1))
        version = match.group(2)
        marker = match.group(3) or ""
        pair = (name, version)
        if pair in details or version not in locked.get(name, set()):
            raise ContractError("requirements runtime divergent de uv.lock")
        accepted = set(allowed_hashes.get(pair, set()))
        local = local_by_identity.get(pair)
        if local is not None:
            accepted.add(local[1])
        installable = set(wheel_hashes.get(pair, {}).values())
        if local is not None:
            installable.add(local[1])
        if hashes != accepted or not hashes.intersection(installable):
            raise ContractError("requirements runtime sans wheel hash-lockee exacte")
        details[pair] = {"hashes": frozenset(hashes), "marker": marker}
        versions.setdefault(name, set()).add(version)
        projection.append(
            {
                "hashes": sorted(hashes),
                "marker": marker,
                "name": name,
                "version": version,
            }
        )
    if not details or set(local_by_identity) - set(details):
        raise ContractError("requirements runtime vide ou pin local absent")
    projection.sort(key=lambda item: (item["name"], item["version"], item["marker"]))
    pin_projection = [
        {"path": f"{_PYTHON_WHEELHOUSE_PATH}/{filename}", "sha256": digest}
        for filename, (digest, _artifact) in sorted(source_pins.items())
    ]
    return (
        details,
        versions,
        sha256_bytes(canonical_json_bytes(projection)),
        sha256_bytes(canonical_json_bytes(pin_projection)),
        source_pins,
        wheel_hashes,
    )


def _hashed_requirements(
    payload: bytes,
    *,
    lock_payload: bytes,
    source_contents: dict[str, bytes],
) -> tuple[dict[str, set[str]], str, str]:
    """Validate the exact hash-locked, wheel-only install input from Git."""

    pinned_wheel = source_contents.get(_DOCOPT_WHEEL_PATH)
    if pinned_wheel is None or sha256_bytes(pinned_wheel) != _DOCOPT_WHEEL_SHA256:
        raise ContractError("wheelhouse runtime source divergent")
    _details, versions, requirements_sha256, source_pins_sha256, _pins, _wheels = (
        _runtime_requirements_contract(
            payload,
            lock_payload=lock_payload,
            source_contents=source_contents,
            expected_pins={
                Path(_DOCOPT_WHEEL_PATH).name: _DOCOPT_WHEEL_SHA256.removeprefix(
                    "sha256:"
                )
            },
        )
    )
    return versions, requirements_sha256, source_pins_sha256


def _validate_site_packages_records(
    site_root: Path,
    entries: dict[str, dict[str, Any]],
    artifacts: dict[tuple[str, str], WheelArtifact],
) -> tuple[dict[str, str], list[str], str]:
    """Bind installed RECORDs and files to the exact selected raw wheels."""

    metadata_paths = sorted(
        path
        for path in entries
        if len(PurePosixPath(path).parts) == 2
        and PurePosixPath(path).parts[0].endswith(".dist-info")
        and PurePosixPath(path).parts[1] == "METADATA"
    )
    record_paths = sorted(
        path
        for path in entries
        if len(PurePosixPath(path).parts) == 2
        and PurePosixPath(path).parts[0].endswith(".dist-info")
        and PurePosixPath(path).parts[1] == "RECORD"
    )
    metadata_roots = {path.removesuffix("/METADATA") for path in metadata_paths}
    record_roots = {path.removesuffix("/RECORD") for path in record_paths}
    if not metadata_roots or metadata_roots != record_roots:
        raise ContractError("METADATA/RECORD site-packages incomplets")
    installed: dict[str, str] = {}
    covered_paths: set[str] = set()
    removed_pth_paths: set[str] = set()
    canonical_record_digests: dict[str, str] = {}
    venv_root = site_root.parents[2]
    projected_external: set[str] = set()
    projected_scripts: set[str] = set()
    projected_data: set[str] = set()
    venv_baseline = {
        "bin/python",
        "bin/python3",
        "bin/python3.12",
    }
    for dist_root in sorted(metadata_roots):
        metadata_payload = _strict_regular_bytes(
            site_root / dist_root / "METADATA", max_bytes=_MAX_RECORD_BYTES
        )
        name, version = _wheel_metadata_identity(metadata_payload)
        identity = (name, version)
        artifact = artifacts.get(identity)
        if (
            name in installed
            or artifact is None
            or artifact.dist_info_root != dist_root
        ):
            raise ContractError(
                "distribution installee divergente des wheels selectionnees"
            )
        if (
            metadata_payload
            != artifact.control_contents[f"{artifact.dist_info_root}/METADATA"]
        ):
            raise ContractError("METADATA installe divergent de la wheel epinglee")
        installed[name] = version

        record_payload = _strict_regular_bytes(
            site_root / dist_root / "RECORD", max_bytes=_MAX_RECORD_BYTES
        )
        record_relative = f"{dist_root}/RECORD"
        expected_rows: dict[str, tuple[str, int]] = {}
        canonical_rows: dict[str, tuple[str, int]] = {}

        def add_expected(
            installed_record_path: str,
            *,
            digest: str,
            size: int,
            canonical_digest: str | None = None,
        ) -> None:
            if installed_record_path in expected_rows:
                raise ContractError("projection de wheel installee dupliquee")
            expected_rows[installed_record_path] = (digest, size)
            canonical_rows[installed_record_path] = (
                digest if canonical_digest is None else canonical_digest,
                size,
            )

        def validate_site_file(
            relative: str,
            *,
            digest: str,
            size: int,
            mode: str,
        ) -> None:
            if relative in covered_paths:
                raise ContractError(
                    "fichier site-packages revendique par plusieurs wheels"
                )
            entry = entries.get(relative)
            if (
                entry is None
                or entry["sha256"] != digest
                or entry["size"] != size
                or entry["mode"] != mode
            ):
                raise ContractError("fichier installe divergent de la wheel epinglee")
            covered_paths.add(relative)

        def validate_external_file(
            relative: str,
            *,
            digest: str,
            size: int,
            mode: str,
        ) -> None:
            if relative in projected_external or relative in venv_baseline:
                raise ContractError("projection externe de wheel dupliquee")
            observed_digest, observed_size, observed_mode = _strict_sealed_digest(
                venv_root / relative,
                max_bytes=_MAX_RUNTIME_FILE_BYTES,
            )
            if (
                observed_digest != digest
                or observed_size != size
                or f"{observed_mode:04o}" != mode
            ):
                raise ContractError("fichier externe divergent de la wheel epinglee")
            projected_external.add(relative)

        raw_record_path = f"{artifact.dist_info_root}/RECORD"
        for raw_path, (raw_digest, raw_size) in artifact.record.items():
            if raw_path == raw_record_path:
                continue
            zone, installed_path = _wheel_destination(
                raw_path,
                dist_info_root=artifact.dist_info_root,
            )
            raw_mode = artifact.modes[raw_path]
            if zone == "site" and installed_path.endswith(".pth"):
                if (
                    "/" in installed_path
                    or raw_size <= 0
                    or installed_path in entries
                    or installed_path in removed_pth_paths
                ):
                    raise ContractError("pth supprime non direct ou encore installe")
                add_expected(installed_path, digest=raw_digest, size=raw_size)
                removed_pth_paths.add(installed_path)
                continue
            if zone == "site":
                validate_site_file(
                    installed_path,
                    digest=raw_digest,
                    size=raw_size,
                    mode=raw_mode,
                )
                add_expected(installed_path, digest=raw_digest, size=raw_size)
                continue
            if zone == "bin":
                raw_content = artifact.control_contents[raw_path]
                installed_content, canonical_content = _data_script_contents(
                    raw_content,
                    venv_root=venv_root,
                )
                external_relative = f"bin/{installed_path}"
                validate_external_file(
                    external_relative,
                    digest=sha256_bytes(installed_content),
                    size=len(installed_content),
                    mode="0555",
                )
                projected_scripts.add(external_relative)
                add_expected(
                    f"../../../{external_relative}",
                    digest=sha256_bytes(installed_content),
                    size=len(installed_content),
                    canonical_digest=sha256_bytes(canonical_content),
                )
                continue
            if zone == "data":
                external_relative = installed_path
                validate_external_file(
                    external_relative,
                    digest=raw_digest,
                    size=raw_size,
                    mode=raw_mode,
                )
                projected_data.add(external_relative)
                add_expected(
                    f"../../../{external_relative}",
                    digest=raw_digest,
                    size=raw_size,
                )
                continue
            raise AssertionError("zone de wheel non geree")

        entry_points = artifact.control_contents.get(
            f"{artifact.dist_info_root}/entry_points.txt"
        )
        for script_name, target in _console_entry_points(entry_points).items():
            actual_script, canonical_script = _console_script_contents(
                target,
                venv_root=venv_root,
            )
            external_relative = f"bin/{script_name}"
            validate_external_file(
                external_relative,
                digest=sha256_bytes(actual_script),
                size=len(actual_script),
                mode="0555",
            )
            projected_scripts.add(external_relative)
            add_expected(
                f"../../../{external_relative}",
                digest=sha256_bytes(actual_script),
                size=len(actual_script),
                canonical_digest=sha256_bytes(canonical_script),
            )

        for generated_path, generated_content in (
            (f"{dist_root}/INSTALLER", b"uv"),
            (f"{dist_root}/REQUESTED", b""),
        ):
            validate_site_file(
                generated_path,
                digest=sha256_bytes(generated_content),
                size=len(generated_content),
                mode="0444",
            )
            add_expected(
                generated_path,
                digest=sha256_bytes(generated_content),
                size=len(generated_content),
            )

        record_entry = entries.get(record_relative)
        if record_entry is None or record_entry["mode"] != "0444":
            raise ContractError("RECORD installe absent ou executable")
        expected_record_rows = [
            (path, _record_encoded_digest(digest), str(size))
            for path, (digest, size) in sorted(
                expected_rows.items(), key=lambda item: item[0].encode("utf-8")
            )
        ] + [(record_relative, "", "")]
        if record_payload != _record_payload(expected_record_rows):
            raise ContractError("RECORD installe non canonique")
        canonical_record_rows = [
            (path, _record_encoded_digest(digest), str(size))
            for path, (digest, size) in sorted(
                canonical_rows.items(), key=lambda item: item[0].encode("utf-8")
            )
        ] + [(record_relative, "", "")]
        canonical_record_digests[record_relative] = sha256_bytes(
            _record_payload(canonical_record_rows)
        )
        covered_paths.add(record_relative)

    if not _CRITICAL_DISTRIBUTIONS.issubset(installed):
        raise ContractError("distributions Anthropic/HTTP critiques absentes")
    if {(name, version) for name, version in installed.items()} != set(artifacts):
        raise ContractError("set installe divergent du wheelhouse et de Rust")
    uncovered = set(entries) - covered_paths
    if uncovered:
        raise ContractError("site-packages contient des fichiers hors RECORD")
    _validate_external_wheel_projection(
        venv_root,
        projected_scripts=projected_scripts,
        projected_data=projected_data,
        venv_baseline=venv_baseline,
    )
    canonical_entries: list[dict[str, Any]] = []
    for path in sorted(entries, key=lambda item: item.encode("utf-8")):
        entry = dict(entries[path])
        if path in canonical_record_digests:
            entry["sha256"] = canonical_record_digests[path]
        canonical_entries.append(entry)
    canonical_site_map = sha256_bytes(canonical_json_bytes(canonical_entries))
    return installed, sorted(removed_pth_paths), canonical_site_map


def _wheel_destination(
    raw_path: str,
    *,
    dist_info_root: str,
) -> tuple[str, str]:
    parts = PurePosixPath(raw_path).parts
    if not parts or not parts[0].endswith(".data"):
        return "site", raw_path
    expected_data_root = f"{dist_info_root.removesuffix('.dist-info')}.data"
    if parts[0] != expected_data_root or len(parts) < 3:
        raise ContractError("wheel contient une racine .data divergente")
    scheme = parts[1]
    tail = PurePosixPath(*parts[2:]).as_posix()
    if scheme in {"purelib", "platlib"}:
        return "site", tail
    if scheme == "scripts":
        if len(parts) != 3 or _SAFE_RUNTIME_FILENAME_RE.fullmatch(parts[2]) is None:
            raise ContractError("wheel contient un script .data non canonique")
        return "bin", parts[2]
    if scheme == "data":
        if len(parts) < 4 or parts[2] != "share":
            raise ContractError("wheel contient une zone .data/data hors share")
        return "data", tail
    raise ContractError("wheel contient une zone .data non attestable")


def _data_script_contents(
    raw: bytes,
    *,
    venv_root: Path,
) -> tuple[bytes, bytes]:
    first_line, separator, remainder = raw.partition(b"\n")
    if first_line in {b"#!python", b"#!pythonw"} and separator:
        actual = f"#!{venv_root / 'bin/python'}\n".encode("utf-8") + remainder
        canonical = b"#!<release>/.venv/bin/python\n" + remainder
        return actual, canonical
    if first_line.startswith(b"#!python"):
        raise ContractError("shebang de script .data ambigu")
    return raw, raw


def _console_entry_points(payload: bytes | None) -> dict[str, str]:
    if payload is None:
        return {}
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(payload.decode("utf-8"))
        if parser.defaults():
            raise ContractError("entry_points contient des defaults ambigus")
        if parser.has_section("gui_scripts") and dict(parser.items("gui_scripts")):
            raise ContractError("entry_points contient des gui_scripts non attestes")
        values = (
            dict(parser.items("console_scripts"))
            if parser.has_section("console_scripts")
            else {}
        )
    except (UnicodeError, configparser.Error) as exc:
        raise ContractError("entry_points de wheel invalide") from exc
    if any(_SAFE_RUNTIME_FILENAME_RE.fullmatch(name) is None for name in values):
        raise ContractError("nom de console script non canonique")
    return values


def _console_script_contents(
    target: str,
    *,
    venv_root: Path,
) -> tuple[bytes, bytes]:
    module, separator, function = target.partition(":")
    if (
        separator != ":"
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) is None
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", function) is None
    ):
        raise ContractError("cible de console script non canonique")
    imported = function.split(".", 1)[0]
    body = (
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"from {module} import {imported}\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        f"    sys.exit({function}())\n"
    )
    actual = f"#!{venv_root / 'bin/python'}\n{body}".encode("utf-8")
    canonical = f"#!<release>/.venv/bin/python\n{body}".encode("utf-8")
    return actual, canonical


def _validate_external_wheel_projection(
    venv_root: Path,
    *,
    projected_scripts: set[str],
    projected_data: set[str],
    venv_baseline: set[str],
) -> None:
    bin_rows = _sealed_tree_rows(venv_root / "bin")
    observed_bin: set[str] = set()
    for row in bin_rows:
        relative = f"bin/{row['path']}"
        expected_mode = (
            "0555"
            if relative in projected_scripts
            or relative in {"bin/python", "bin/python3", "bin/python3.12"}
            else "0444"
        )
        if (
            row["type"] != "file"
            or relative not in projected_scripts | venv_baseline
            or row["mode"] != expected_mode
        ):
            raise ContractError(".venv/bin contient un script sans wheel")
        observed_bin.add(relative)
    if observed_bin != projected_scripts | venv_baseline:
        raise ContractError(".venv/bin incomplet pour la recette scellee")

    share_root = venv_root / "share"
    try:
        share_metadata = share_root.lstat()
    except FileNotFoundError:
        if projected_data:
            raise ContractError("donnees de wheel absentes de .venv/share")
        return
    except OSError as exc:
        raise ContractError(".venv/share illisible") from exc
    if not projected_data or not stat.S_ISDIR(share_metadata.st_mode):
        raise ContractError(".venv/share sans provenance de wheel")
    share_rows = _sealed_tree_rows(share_root)
    expected_paths = {path.removeprefix("share/") for path in projected_data}
    expected_paths.update(
        parent.as_posix()
        for path in tuple(expected_paths)
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    )
    if {str(row["path"]) for row in share_rows} != expected_paths:
        raise ContractError(".venv/share contient des donnees sans wheel")


def _record_encoded_digest(digest: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ContractError("empreinte RECORD interne invalide")
    encoded = base64.urlsafe_b64encode(
        bytes.fromhex(digest.removeprefix("sha256:"))
    ).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _record_payload(rows: list[tuple[str, str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _sealed_wheelhouse(
    release_root: Path,
    *,
    requirements: dict[tuple[str, str], dict[str, Any]],
    source_pins: dict[str, tuple[str, WheelArtifact]],
    locked_wheels: dict[tuple[str, str], dict[str, str]],
    wheelhouse_manifest: dict[str, str],
) -> tuple[dict[tuple[str, str], WheelArtifact], str]:
    wheelhouse = release_root / _SEALED_WHEELHOUSE_PATH
    _trusted_sealed_metadata(wheelhouse, directory=True)
    try:
        children = sorted(
            os.scandir(wheelhouse), key=lambda item: os.fsencode(item.name)
        )
    except OSError as exc:
        raise ContractError("wheelhouse scelle illisible") from exc
    if [child.name for child in children] != list(wheelhouse_manifest):
        raise ContractError("wheelhouse scelle divergent du manifeste exact")
    artifacts: dict[tuple[str, str], WheelArtifact] = {}
    by_filename: dict[str, WheelArtifact] = {}
    projection: list[dict[str, str]] = []
    retained_control_bytes = sum(
        len(content)
        for _digest, artifact in source_pins.values()
        for content in artifact.control_contents.values()
    )
    for child in children:
        filename = child.name
        if _WHEEL_RE.fullmatch(filename) is None:
            raise ContractError("wheelhouse scelle contient un nom invalide")
        path = wheelhouse / filename
        metadata = _trusted_sealed_metadata(path, directory=False)
        if metadata.st_size <= 0 or metadata.st_size > _MAX_ARCHIVE_BYTES:
            raise ContractError("wheel scellee hors taille")
        payload = _strict_regular_bytes(path, max_bytes=_MAX_ARCHIVE_BYTES)
        artifact = _wheel_artifact(payload, filename=filename)
        if artifact.sha256 != wheelhouse_manifest[filename]:
            raise ContractError(
                "wheelhouse scelle contient un SHA divergent du manifeste"
            )
        retained_control_bytes += sum(
            len(content) for content in artifact.control_contents.values()
        )
        if retained_control_bytes > _MAX_WHEELHOUSE_CONTROL_BYTES:
            raise ContractError("metadata du wheelhouse hors taille")
        identity = (artifact.name, artifact.version)
        if identity in artifacts or filename in by_filename:
            raise ContractError("wheelhouse scelle contient un doublon")
        requirement = requirements.get(identity)
        source_pin = source_pins.get(filename)
        locked_digest = locked_wheels.get(identity, {}).get(filename)
        if (
            requirement is None
            or artifact.sha256 not in requirement["hashes"]
            or (
                locked_digest != artifact.sha256
                and (source_pin is None or source_pin[0] != artifact.sha256)
            )
        ):
            raise ContractError("wheelhouse sort des requirements, uv.lock ou pins Git")
        artifacts[identity] = artifact
        by_filename[filename] = artifact
        projection.append(
            {
                "filename": filename,
                "name": artifact.name,
                "payload_sha256": artifact.payload_sha256,
                "sha256": artifact.sha256,
                "version": artifact.version,
            }
        )
    for filename, (digest, source_artifact) in source_pins.items():
        observed = by_filename.get(filename)
        if (
            observed is None
            or observed.sha256 != digest
            or (observed.name, observed.version)
            != (source_artifact.name, source_artifact.version)
        ):
            raise ContractError("wheel Git absente ou divergente du wheelhouse scelle")
    selected_requirements = {
        identity
        for identity, requirement in requirements.items()
        if _marker_applies(str(requirement["marker"]))
    }
    if set(artifacts) != selected_requirements:
        raise ContractError("wheelhouse divergent des markers Linux CPython 3.12")
    names = [name for name, _version in artifacts]
    if len(names) != len(set(names)):
        raise ContractError("wheelhouse selectionne plusieurs versions d'un package")
    projection.sort(key=lambda item: item["filename"].encode("utf-8"))
    return artifacts, sha256_bytes(canonical_json_bytes(projection))


def _marker_applies(marker: str) -> bool:
    """Evaluate the closed marker grammar used by the pinned uv export."""

    if not marker:
        return True
    environment = {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_full_version": "3.12.13",
        "python_version": "3.12",
        "sys_platform": "linux",
    }
    version_names = {"python_full_version", "python_version"}

    def numeric_version(value: str) -> tuple[int, int, int]:
        version_pattern = (
            r"(?:0|[1-9][0-9]{0,2})\."
            r"(?:0|[1-9][0-9]{0,2})"
            r"(?:\.(?:0|[1-9][0-9]{0,2}))?"
        )
        if re.fullmatch(version_pattern, value) is None:
            raise ContractError("version de marker non canonique")
        parts = [int(part) for part in value.split(".")]
        if len(parts) == 2:
            return parts[0], parts[1], 0
        return parts[0], parts[1], parts[2]

    try:
        expression = ast.parse(marker, mode="eval")
    except SyntaxError as exc:
        raise ContractError("marker de requirement invalide") from exc

    def evaluate(node: ast.AST) -> str | bool:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [evaluate(value) for value in node.values]
            if any(type(value) is not bool for value in values):
                raise ContractError("marker de requirement booleen invalide")
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == len(node.comparators) == 1
        ):
            left = evaluate(node.left)
            right = evaluate(node.comparators[0])
            if type(left) is not str or type(right) is not str:
                raise ContractError("comparaison de marker invalide")
            operator = node.ops[0]
            compares_version = any(
                isinstance(operand, ast.Name) and operand.id in version_names
                for operand in (node.left, node.comparators[0])
            )
            if compares_version:
                if isinstance(operator, (ast.In, ast.NotIn)):
                    raise ContractError("operateur de version de marker invalide")
                compared_left: str | tuple[int, int, int] = numeric_version(left)
                compared_right: str | tuple[int, int, int] = numeric_version(right)
            else:
                compared_left = left
                compared_right = right
            if isinstance(operator, ast.Eq):
                return compared_left == compared_right
            if isinstance(operator, ast.NotEq):
                return compared_left != compared_right
            if isinstance(operator, ast.Lt):
                return compared_left < compared_right
            if isinstance(operator, ast.LtE):
                return compared_left <= compared_right
            if isinstance(operator, ast.Gt):
                return compared_left > compared_right
            if isinstance(operator, ast.GtE):
                return compared_left >= compared_right
            if isinstance(operator, ast.In):
                return compared_left in compared_right
            if isinstance(operator, ast.NotIn):
                return compared_left not in compared_right
        raise ContractError("syntaxe de marker non fermee")

    result = evaluate(expression)
    if type(result) is not bool:
        raise ContractError("marker de requirement non booleen")
    return result


def _sealed_tree_rows(
    root: Path,
    *,
    prefixes: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Recalculate the sealer's exhaustive row format from trusted metadata."""

    tree_root = root.expanduser().absolute()
    _trusted_sealed_metadata(tree_root, directory=True)
    rows: list[dict[str, Any]] = []
    pending = [tree_root]
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: os.fsencode(item.name)
            )
        except OSError as exc:
            raise ContractError("arbre scelle illisible") from exc
        for child in children:
            path = directory / child.name
            relative = path.relative_to(tree_root).as_posix()
            if len(relative.encode("utf-8")) > _MAX_ARCHIVE_PATH_BYTES:
                raise ContractError("chemin scelle hors taille")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ContractError("entree scellee illisible") from exc
            if stat.S_ISDIR(metadata.st_mode):
                trusted = _trusted_sealed_metadata(path, directory=True)
                if (trusted.st_dev, trusted.st_ino, trusted.st_mode) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                ):
                    raise ContractError("repertoire scelle remplace")
                rows.append({"mode": "0555", "path": relative, "type": "directory"})
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                digest, size, mode = _strict_sealed_digest(
                    path, max_bytes=_MAX_RUNTIME_FILE_BYTES
                )
                total_bytes += size
                if total_bytes > _MAX_RUNTIME_TREE_BYTES:
                    raise ContractError("arbre scelle hors taille")
                rows.append(
                    {
                        "mode": f"{mode:04o}",
                        "path": relative,
                        "sha256": digest.removeprefix("sha256:"),
                        "size": size,
                        "type": "file",
                    }
                )
            else:
                raise ContractError("arbre scelle contient un lien ou type special")
            if len(rows) > _MAX_RUNTIME_ENTRIES:
                raise ContractError("arbre scelle contient trop d'entrees")
    if prefixes is not None:
        rows = [
            row
            for row in rows
            if any(
                row["path"] == prefix or str(row["path"]).startswith(f"{prefix}/")
                for prefix in prefixes
            )
        ]
    return sorted(rows, key=lambda row: Path(str(row["path"])))


def _sealed_jsonl_rows(
    path: Path,
    *,
    expected_format: str,
) -> tuple[bytes, list[dict[str, Any]]]:
    payload = _strict_regular_bytes(path, max_bytes=_MAX_SEALED_MANIFEST_BYTES)
    metadata = _trusted_sealed_metadata(path, directory=False)
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise ContractError("manifeste scelle non 0444")
    try:
        lines = payload.decode("ascii").splitlines()
        documents = [json.loads(line) for line in lines]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("manifeste JSONL scelle invalide") from exc
    if not documents or documents[0] != {"format": expected_format}:
        raise ContractError("format de manifeste JSONL divergent")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in documents[1:]:
        if type(row) is not dict or row.get("type") not in {"directory", "file"}:
            raise ContractError("ligne de manifeste JSONL invalide")
        path_value = row.get("path")
        if (
            type(path_value) is not str
            or _safe_archive_name(path_value, "manifest.path") != path_value
        ):
            raise ContractError("chemin de manifeste JSONL invalide")
        if path_value in seen:
            raise ContractError("manifeste JSONL duplique")
        seen.add(path_value)
        if row["type"] == "directory":
            if set(row) != {"mode", "path", "type"} or row["mode"] != "0555":
                raise ContractError("repertoire de manifeste JSONL invalide")
        elif (
            set(row) != {"mode", "path", "sha256", "size", "type"}
            or row["mode"] not in {"0444", "0555"}
            or type(row["sha256"]) is not str
            or _SHA256_RE.fullmatch(row["sha256"]) is None
            or type(row["size"]) is not int
            or row["size"] < 0
        ):
            raise ContractError("fichier de manifeste JSONL invalide")
        rows.append(row)
    canonical = b"".join(
        canonical_json_bytes(document) + b"\n" for document in documents
    )
    if canonical != payload:
        raise ContractError("manifeste JSONL non canonique")
    return payload, rows


def _archive_extraction_rows(
    payload: bytes,
    *,
    source_archive: bool,
    git_sha: str | None = None,
    reproducible_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Recalculate exactly the rows emitted by the root-owned tar extractor."""

    rows: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ContractError("archive scellee vide ou hors taille")
            _validate_tar_zero_tail(payload, members)
            for index, member in enumerate(members):
                raw = member.name.removesuffix("/") if member.isdir() else member.name
                name = _safe_archive_name(raw, f"archive.extract[{index}]")
                if name in seen:
                    raise ContractError("archive scellee contient un doublon")
                if (
                    source_archive
                    and PurePosixPath(name).parts[0] in _RESERVED_SOURCE_TOP_LEVEL
                ):
                    raise ContractError("archive source contient un chemin reserve")
                allowed_pax = {"comment": git_sha} if git_sha is not None else {}
                if member.pax_headers != allowed_pax:
                    raise ContractError("archive scellee contient des PAX divergents")
                if reproducible_metadata and (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or member.mtime != 0
                ):
                    raise ContractError("archive frontend avec metadata non canoniques")
                for parent in PurePosixPath(name).parents:
                    if (
                        parent.as_posix() != "."
                        and seen.get(parent.as_posix()) == "file"
                    ):
                        raise ContractError(
                            "archive scellee place une entree sous un fichier"
                        )
                if not member.isdir() and any(
                    path.startswith(f"{name}/") for path in seen
                ):
                    raise ContractError("archive scellee remplace un repertoire")
                if member.isdir():
                    if reproducible_metadata and (
                        member.mode != 0o755 or member.size != 0 or member.linkname
                    ):
                        raise ContractError("repertoire frontend non canonique")
                    seen[name] = "directory"
                    rows.append({"mode": "0555", "path": name, "type": "directory"})
                    continue
                if not member.isreg():
                    raise ContractError(
                        "archive scellee contient un lien ou type special"
                    )
                if reproducible_metadata and (
                    member.mode not in {0o644, 0o755} or member.linkname
                ):
                    raise ContractError("fichier frontend non canonique")
                if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise ContractError("membre d'archive scellee hors taille")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ContractError("membre d'archive scellee illisible")
                content = extracted.read(_MAX_ARCHIVE_MEMBER_BYTES + 1)
                if len(content) != member.size:
                    raise ContractError("membre d'archive scellee tronque")
                total += len(content)
                if total > _MAX_RUNTIME_TREE_BYTES:
                    raise ContractError("archive scellee decompressee hors taille")
                mode = "0555" if member.mode & 0o111 else "0444"
                rows.append(
                    {
                        "mode": mode,
                        "path": name,
                        "sha256": sha256_bytes(content).removeprefix("sha256:"),
                        "size": len(content),
                        "type": "file",
                    }
                )
                seen[name] = "file"
    except (tarfile.TarError, UnicodeError, OSError) as exc:
        raise ContractError("archive scellee invalide") from exc
    return sorted(rows, key=lambda row: str(row["path"]))


def _validate_tar_zero_tail(
    payload: bytes,
    members: Sequence[tarfile.TarInfo],
) -> None:
    for member in members:
        if (
            member.offset < 0
            or member.offset % 512
            or member.offset_data != member.offset + 512
        ):
            raise ContractError("archive tar hors structure USTAR")
        header = payload[member.offset : member.offset + 512]
        if (
            len(header) != 512
            or header[257:263] != b"ustar\0"
            or header[263:265] != b"00"
        ):
            raise ContractError("archive tar hors structure USTAR")
    payload_end = max(
        member.offset_data + ((member.size + 511) // 512) * 512 for member in members
    )
    tail = payload[payload_end:]
    if len(tail) < 1024:
        raise ContractError("archive tar sans marqueur de fin canonique")
    if any(tail):
        raise ContractError("archive tar avec queue non NUL")
    if len(payload) % 512:
        raise ContractError("archive tar avec queue non alignee")


def _canonical_ascii_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _reject_frontend_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ContractError(
                f"attestation build frontend: cle JSON dupliquee: {key}"
            )
        document[key] = value
    return document


def _reject_frontend_nonfinite(value: str) -> None:
    raise ContractError(
        f"attestation build frontend: constante JSON interdite: {value}"
    )


def _validate_frontend_builder_dockerfile(payload: bytes) -> None:
    if (
        sha256_bytes(payload).removeprefix("sha256:")
        != _FRONTEND_BUILDER_DOCKERFILE_SHA256
    ):
        raise ContractError("Dockerfile frontend divergent du pin exact")
    try:
        text = payload.decode("ascii")
    except UnicodeError as exc:
        raise ContractError("Dockerfile frontend non ASCII") from exc
    expected_from = f"FROM {_FRONTEND_BUILDER_BASE_IMAGE} AS builder"
    from_lines = [
        line.strip() for line in text.splitlines() if line.lstrip().startswith("FROM ")
    ]
    required_fragments = (
        "COPY frontend/package.json frontend/package-lock.json frontend/",
        "npm ci --prefix frontend --ignore-scripts --no-audit --no-fund",
        "COPY frontend/ frontend/",
        "RUN --network=none env -i",
        "SOURCE_DATE_EPOCH=0",
        "--format=ustar --mode='a=rX,u+w'",
        f'test "$node_version" = v{_FRONTEND_NODE_VERSION};',
        f'test "$npm_version" = {_FRONTEND_NPM_VERSION};',
    )
    if from_lines != [expected_from, "FROM scratch AS artifact"] or any(
        fragment not in text for fragment in required_fragments
    ):
        raise ContractError("recette Docker frontend divergente du contrat ferme")


def _frontend_build_evidence(
    *,
    source_archive_payload: bytes,
    source_contents: dict[str, bytes],
    frontend_archive_payload: bytes,
    build_attestation_payload: bytes,
    git_sha: str,
) -> dict[str, str]:
    try:
        raw = build_attestation_payload.decode("ascii")
        document = json.loads(
            raw,
            object_pairs_hook=_reject_frontend_duplicate_keys,
            parse_constant=_reject_frontend_nonfinite,
        )
    except ContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("attestation build frontend JSON invalide") from exc
    if (
        type(document) is not dict
        or build_attestation_payload != _canonical_ascii_json_bytes(document) + b"\n"
    ):
        raise ContractError("attestation build frontend non canonique")

    source_rows = _archive_extraction_rows(
        source_archive_payload,
        source_archive=True,
        git_sha=git_sha,
    )
    frontend_source_rows = [
        row for row in source_rows if str(row.get("path", "")).startswith("frontend/")
    ]
    if not frontend_source_rows:
        raise ContractError("archive source sans sous-arbre frontend attestable")
    frontend_source_rows.sort(key=lambda row: str(row["path"]))
    frontend_rows = _archive_extraction_rows(
        frontend_archive_payload,
        source_archive=False,
        reproducible_metadata=True,
    )
    if not any(
        row["path"] == "index.html" and row["type"] == "file" and int(row["size"]) > 0
        for row in frontend_rows
    ):
        raise ContractError("frontend atteste sans index.html")

    try:
        package_json = source_contents["frontend/package.json"]
        package_lock = source_contents["frontend/package-lock.json"]
        dockerfile = source_contents[_FRONTEND_BUILDER_DOCKERFILE_PATH]
    except KeyError as exc:
        raise ContractError("source frontend ou Dockerfile attestable absent") from exc
    _validate_frontend_builder_dockerfile(dockerfile)
    builder = {
        "base_image": _FRONTEND_BUILDER_BASE_IMAGE,
        "dockerfile_path": _FRONTEND_BUILDER_DOCKERFILE_PATH,
        "dockerfile_sha256": sha256_bytes(dockerfile),
        "node_version": _FRONTEND_NODE_VERSION,
        "npm_version": _FRONTEND_NPM_VERSION,
        "platform": _FRONTEND_BUILDER_PLATFORM,
    }
    source_map_sha256 = sha256_bytes(_canonical_ascii_json_bytes(frontend_source_rows))
    archive_map_sha256 = sha256_bytes(_canonical_ascii_json_bytes(frontend_rows))
    expected = {
        "builder": builder,
        "frontend_source_map_sha256": source_map_sha256,
        "git_sha": git_sha,
        "output": {
            "archive_map_sha256": archive_map_sha256,
            "archive_path": "frontend-static.tar",
            "archive_sha256": sha256_bytes(frontend_archive_payload),
            "build_count": 2,
        },
        "package_json_sha256": sha256_bytes(package_json),
        "package_lock_sha256": sha256_bytes(package_lock),
        "schema_version": _FRONTEND_BUILD_ATTESTATION_SCHEMA,
        "source_archive_sha256": sha256_bytes(source_archive_payload),
    }
    if document != expected:
        raise ContractError(
            "attestation build frontend et artefacts recalcules divergent"
        )
    builder_recipe = {
        "builder": builder,
        "format": "ava.frontend.builder-recipe/v1",
    }
    return {
        "archive_map_sha256": archive_map_sha256,
        "archive_sha256": sha256_bytes(frontend_archive_payload),
        "build_attestation_sha256": sha256_bytes(build_attestation_payload),
        "builder_recipe_sha256": sha256_bytes(
            _canonical_ascii_json_bytes(builder_recipe)
        ),
        "source_map_sha256": source_map_sha256,
    }


def _verify_sealed_manifests(release_root: Path, seal: dict[str, Any]) -> None:
    source_payload, source_rows = _sealed_jsonl_rows(
        release_root / ".ava-source-manifest.jsonl",
        expected_format=_SOURCE_MAP_FORMAT,
    )
    runtime_payload, runtime_rows = _sealed_jsonl_rows(
        release_root / ".ava-runtime-manifest.jsonl",
        expected_format=_RUNTIME_MAP_FORMAT,
    )
    files_payload, files_rows = _sealed_jsonl_rows(
        release_root / ".ava-files-manifest.jsonl",
        expected_format=_FILES_MAP_FORMAT,
    )
    if (
        sha256_bytes(source_payload).removeprefix("sha256:")
        != seal["source_manifest_sha256"]
        or sha256_bytes(runtime_payload).removeprefix("sha256:")
        != seal["runtime_manifest_sha256"]
        or sha256_bytes(files_payload).removeprefix("sha256:")
        != seal["files_manifest_sha256"]
    ):
        raise ContractError("sceau et manifestes JSONL divergent")

    source_archive = _strict_regular_bytes(
        release_root / ".ava-artifacts/source-tree.tar", max_bytes=_MAX_ARCHIVE_BYTES
    )
    frontend_archive = _strict_regular_bytes(
        release_root / _FRONTEND_ARCHIVE_PATH, max_bytes=_MAX_ARCHIVE_BYTES
    )
    expected_source = _archive_extraction_rows(
        source_archive, source_archive=True, git_sha=release_root.name
    )
    frontend_rows = _archive_extraction_rows(
        frontend_archive, source_archive=False, reproducible_metadata=True
    )
    if not any(
        row["path"] == "index.html" and row["type"] == "file" and row["size"] > 0
        for row in frontend_rows
    ):
        raise ContractError("frontend scelle sans index.html")
    expected_source.append(
        {"mode": "0555", "path": _FRONTEND_INSTALL_PATH, "type": "directory"}
    )
    expected_source.extend(
        {**row, "path": f"{_FRONTEND_INSTALL_PATH}/{row['path']}"}
        for row in frontend_rows
    )
    expected_source.sort(key=lambda row: str(row["path"]))
    if source_rows != expected_source:
        raise ContractError("source-manifest divergent des archives source/frontend")

    actual_rows = _sealed_tree_rows(release_root)
    actual_by_path = {str(row["path"]): row for row in actual_rows}
    if any(actual_by_path.get(str(row["path"])) != row for row in expected_source):
        raise ContractError("arbre extrait divergent du source-manifest")
    expected_runtime = [
        row
        for row in actual_rows
        if str(row["path"]) in {".python", ".venv"}
        or str(row["path"]).startswith((".python/", ".venv/"))
    ]
    if runtime_rows != expected_runtime:
        raise ContractError("runtime-manifest divergent de .python/.venv")
    expected_files = [
        row
        for row in actual_rows
        if row["path"] not in {".ava-files-manifest.jsonl", ".ava-seal.json"}
    ]
    if files_rows != expected_files:
        raise ContractError("files-manifest non exhaustif ou divergent")


def _uv_installer_evidence() -> tuple[dict[str, Any], str]:
    executable = _UV_EXECUTABLE.expanduser().absolute()
    digest, _size, mode = _strict_sealed_digest(executable, max_bytes=256 * 1024 * 1024)
    if not mode & 0o111:
        raise ContractError("binaire uv non executable")
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("version uv indisponible") from exc
    try:
        version_output = completed.stdout.decode("ascii").strip()
    except UnicodeError as exc:
        raise ContractError("version uv invalide") from exc
    match = re.fullmatch(r"uv ([0-9]+\.[0-9]+\.[0-9]+)(?: .*)?", version_output)
    if completed.returncode != 0 or match is None:
        raise ContractError("version uv invalide")
    return (
        {
            "path": "/usr/local/bin/ava-uv",
            "sha256": digest,
            "version": match.group(1),
            "arguments": list(_UV_SYNC_ARGUMENTS),
        },
        version_output,
    )


def _verify_sealed_runtime_recipe(release_root: Path) -> dict[str, Any]:
    """Bind runtime evidence to the root-owned sealer's exact build recipe."""

    seal_path = release_root / ".ava-seal.json"
    payload = _strict_regular_bytes(seal_path, max_bytes=_MAX_CONFIG_BYTES)
    _trusted_sealed_metadata(seal_path, directory=False)
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("sceau du runtime Python invalide") from exc
    recipe_sha256 = sha256_bytes(
        canonical_json_bytes(_SEALED_RUNTIME_RECIPE)
    ).removeprefix("sha256:")
    input_keys = {
        "evolutions_sha256",
        "frontend_build_attestation_sha256",
        "frontend_static_sha256",
        "git_sha",
        "python_runtime_archive",
        "python_runtime_sha256",
        "python_runtime_source_sha256",
        "release_manifest_sha256",
        "runtime_requirements_sha256",
        "runtime_wheelhouse_manifest_sha256",
        "runtime_wheel_pins",
        "rust_attestation_sha256",
        "rust_tree_sha256",
        "rust_wheel_sha256",
        "source_tree_sha256",
        "uv_lock_sha256",
        "uv_sha256",
        "uv_version",
    }
    inputs = document.get("inputs") if type(document) is dict else None
    runtime_pins = inputs.get("runtime_wheel_pins") if type(inputs) is dict else None
    pins_valid = type(runtime_pins) is list and bool(runtime_pins)
    pin_names: list[str] = []
    if pins_valid:
        for item in runtime_pins:
            if (
                type(item) is not dict
                or set(item) != {"filename", "sha256"}
                or type(item["filename"]) is not str
                or _WHEEL_RE.fullmatch(item["filename"]) is None
                or type(item["sha256"]) is not str
                or _SHA256_RE.fullmatch(item["sha256"]) is None
            ):
                pins_valid = False
                break
            pin_names.append(item["filename"])
        if pin_names != sorted(pin_names) or len(pin_names) != len(set(pin_names)):
            pins_valid = False
    if (
        type(document) is not dict
        or set(document)
        != {
            "files_manifest_sha256",
            "format",
            "git_sha",
            "inputs",
            "package_set_sha256",
            "runtime_manifest_sha256",
            "runtime_recipe",
            "runtime_recipe_sha256",
            "source_manifest_sha256",
        }
        or document["format"] != "ava-sealed-release-v1"
        or document["git_sha"] != release_root.name
        or type(inputs) is not dict
        or set(inputs) != input_keys
        or inputs["git_sha"] != release_root.name
        or inputs["python_runtime_archive"] != _PYTHON_RUNTIME_ARCHIVE_NAME
        or type(inputs["uv_version"]) is not str
        or re.fullmatch(
            r"uv [0-9]+\.[0-9]+\.[0-9]+(?: \([^\n]{1,160}\))?",
            inputs["uv_version"],
        )
        is None
        or any(
            type(inputs[key]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", inputs[key]) is None
            for key in input_keys
            - {"git_sha", "python_runtime_archive", "runtime_wheel_pins", "uv_version"}
        )
        or not pins_valid
        or document["runtime_recipe"] != _SEALED_RUNTIME_RECIPE
        or document["runtime_recipe_sha256"] != recipe_sha256
        or any(
            type(document[key]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", document[key]) is None
            for key in (
                "files_manifest_sha256",
                "package_set_sha256",
                "runtime_manifest_sha256",
                "runtime_recipe_sha256",
                "source_manifest_sha256",
            )
        )
    ):
        raise ContractError("recette scellee du runtime Python divergente")
    if payload != canonical_json_bytes(document) + b"\n":
        raise ContractError("sceau du runtime Python non canonique")

    manifest_payload = _strict_regular_bytes(
        release_root / ".ava-release", max_bytes=_MAX_MANIFEST_BYTES
    )
    manifest = _parse_release_manifest(manifest_payload)
    if (
        manifest["git_sha"] != release_root.name
        or sha256_bytes(manifest_payload).removeprefix("sha256:")
        != inputs["release_manifest_sha256"]
        or manifest["source_tree_sha256"] != inputs["source_tree_sha256"]
        or manifest["rust_tree_sha256"] != inputs["rust_tree_sha256"]
        or manifest["wheel_sha256"] != inputs["rust_wheel_sha256"]
        or manifest["attestation_sha256"] != inputs["rust_attestation_sha256"]
        or manifest["evolutions_sha256"] != inputs["evolutions_sha256"]
    ):
        raise ContractError("sceau et manifeste de release divergent")

    digest_paths = {
        "evolutions_sha256": (".ava-artifacts/evolutions-v1.json", _MAX_CONFIG_BYTES),
        "frontend_build_attestation_sha256": (
            _FRONTEND_BUILD_ATTESTATION_PATH,
            _MAX_CONFIG_BYTES,
        ),
        "frontend_static_sha256": (_FRONTEND_ARCHIVE_PATH, _MAX_ARCHIVE_BYTES),
        "python_runtime_sha256": (
            ".ava-artifacts/python-runtime.tar.gz",
            _MAX_ARCHIVE_BYTES,
        ),
        "python_runtime_source_sha256": (
            _PYTHON_RUNTIME_SOURCE_PATH,
            _MAX_CONFIG_BYTES,
        ),
        "runtime_requirements_sha256": (
            _PYTHON_REQUIREMENTS_PATH,
            _MAX_CONFIG_BYTES,
        ),
        "runtime_wheelhouse_manifest_sha256": (
            _PYTHON_WHEELHOUSE_MANIFEST_PATH,
            _MAX_CONFIG_BYTES,
        ),
        "rust_attestation_sha256": (
            f".ava-artifacts/{manifest['wheel_filename']}.attestation",
            _MAX_CONFIG_BYTES,
        ),
        "rust_tree_sha256": (".ava-artifacts/rust-tree.tar", _MAX_ARCHIVE_BYTES),
        "rust_wheel_sha256": (
            f".ava-artifacts/{manifest['wheel_filename']}",
            _MAX_ARCHIVE_BYTES,
        ),
        "source_tree_sha256": (".ava-artifacts/source-tree.tar", _MAX_ARCHIVE_BYTES),
        "uv_lock_sha256": (_PYTHON_LOCK_PATH, _MAX_LOCK_BYTES),
    }
    for key, (relative, maximum) in digest_paths.items():
        digest, _size, _mode = _strict_sealed_digest(
            release_root / relative, max_bytes=maximum
        )
        if digest != f"sha256:{inputs[key]}":
            raise ContractError(f"sceau et {key} divergent")
    for item in runtime_pins:
        digest, _size, _mode = _strict_sealed_digest(
            release_root / _PYTHON_WHEELHOUSE_PATH / item["filename"],
            max_bytes=_MAX_ARCHIVE_BYTES,
        )
        if digest != f"sha256:{item['sha256']}":
            raise ContractError("pin de wheel runtime et source Git divergent")
    _verify_sealed_manifests(release_root, document)
    return document


def _parse_pyvenv(payload: bytes, *, release_root: Path) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ContractError("pyvenv.cfg non UTF-8") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip() or key.strip() in values:
            raise ContractError("pyvenv.cfg invalide")
        values[key.strip()] = value.strip()
    python = release_root / ".python/bin/python3.12"
    expected = {
        "home": str(release_root / ".python/bin"),
        "include-system-site-packages": "false",
        "version": _SEALED_RUNTIME_RECIPE["python_version"],
        "executable": str(python),
        "command": (
            f"{python} -m venv --copies --without-pip {release_root / '.venv'}"
        ),
    }
    if values != expected:
        raise ContractError("pyvenv.cfg divergent de la recette scellee")
    canonical: dict[str, str] = {}
    root_text = str(release_root)
    for key, value in values.items():
        canonical_value = value.replace(root_text, "<release>")
        canonical[key] = canonical_value
    return canonical


def _python_runtime_evidence(
    release_root: Path,
    source_contents: dict[str, bytes],
) -> dict[str, Any]:
    """Derive the exact no-site runtime that can execute the causal shadow."""

    root_identity = _verify_sealed_release_root(release_root)
    seal_document = _verify_sealed_runtime_recipe(release_root)
    seal_inputs = seal_document["inputs"]
    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    runtime_source_payload = source_contents.get(_PYTHON_RUNTIME_SOURCE_PATH)
    if runtime_source_payload is None:
        raise ContractError("pin source du runtime Python absent de l'archive")
    installed_runtime_source = _strict_regular_bytes(
        release_root / _PYTHON_RUNTIME_SOURCE_PATH,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    if installed_runtime_source != runtime_source_payload:
        raise ContractError("pin source du runtime Python installe divergent")
    if seal_inputs["python_runtime_source_sha256"] != sha256_bytes(
        runtime_source_payload
    ).removeprefix("sha256:"):
        raise ContractError("sceau et pin source du runtime Python divergent")
    runtime_source_contract = _python_runtime_source_contract(runtime_source_payload)
    runtime_archive_relative = runtime_source_contract["archive"][
        "sealed_relative_path"
    ]
    runtime_archive_path = release_root / runtime_archive_relative
    archive_metadata = _trusted_sealed_metadata(runtime_archive_path, directory=False)
    if (
        archive_metadata.st_nlink != 1
        or stat.S_IMODE(archive_metadata.st_mode) != 0o444
    ):
        raise ContractError("archive runtime Python non scellee en 0444")
    runtime_archive_payload = _strict_regular_bytes(
        runtime_archive_path,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    if seal_inputs["python_runtime_sha256"] != sha256_bytes(
        runtime_archive_payload
    ).removeprefix("sha256:"):
        raise ContractError("sceau et archive du runtime Python divergent")
    (
        runtime_archive_map_sha256,
        runtime_archive_entries,
        runtime_archive_contents,
    ) = _python_archive_materialized_map(
        runtime_archive_payload,
        source_contract=runtime_source_contract,
    )
    python_install_root = release_root / ".python"
    (
        python_install_map_sha256,
        python_install_entry_count,
        python_install_total_size,
        _python_install_entries,
    ) = _sealed_tree_map(
        python_install_root,
        max_bytes=_MAX_RUNTIME_TREE_BYTES,
        reject_site_artifacts=False,
    )
    expected_install_size = sum(entry["size"] for entry in runtime_archive_entries)
    if (
        python_install_map_sha256 != runtime_archive_map_sha256
        or python_install_entry_count != len(runtime_archive_entries)
        or python_install_total_size != expected_install_size
    ):
        raise ContractError("runtime Python installe divergent de l'archive source")

    executable_path = release_root / ".venv" / "bin" / "python"
    try:
        process_executable = _PROCESS_EXECUTABLE.resolve(strict=True)
        expected_executable = executable_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("interpreteur Python scelle indisponible") from exc
    if process_executable != expected_executable:
        raise ContractError("attestation executee par un autre interpreteur")
    executable_sha256, _executable_size, executable_mode = _strict_sealed_digest(
        executable_path, max_bytes=256 * 1024 * 1024
    )
    if not executable_mode & 0o111:
        raise ContractError("interpreteur Python non executable")
    executable_prefix = _strict_regular_bytes(
        executable_path, max_bytes=256 * 1024 * 1024
    )[:4]
    if executable_prefix != b"\x7fELF":
        raise ContractError("interpreteur Python non ELF")
    source_executable = runtime_archive_contents.get("bin/python3.12")
    if source_executable is None or executable_sha256 != sha256_bytes(
        source_executable
    ):
        raise ContractError("interpreteur Python divergent de l'archive source")

    pyvenv_path = release_root / ".venv" / "pyvenv.cfg"
    pyvenv_payload = _strict_regular_bytes(pyvenv_path, max_bytes=64 * 1024)
    _trusted_sealed_metadata(pyvenv_path, directory=False)
    pyvenv_contract = _parse_pyvenv(pyvenv_payload, release_root=release_root)

    lock_payload = source_contents.get(_PYTHON_LOCK_PATH)
    if lock_payload is None:
        raise ContractError("uv.lock absent de l'archive source")
    installed_lock = _strict_regular_bytes(
        release_root / _PYTHON_LOCK_PATH, max_bytes=_MAX_LOCK_BYTES
    )
    if installed_lock != lock_payload:
        raise ContractError("uv.lock installe divergent de l'archive source")
    if seal_inputs["uv_lock_sha256"] != sha256_bytes(lock_payload).removeprefix(
        "sha256:"
    ):
        raise ContractError("sceau et uv.lock divergent")

    requirements_payload = source_contents.get(_PYTHON_REQUIREMENTS_PATH)
    if requirements_payload is None:
        raise ContractError("requirements runtime absentes de l'archive source")
    installed_requirements = _strict_regular_bytes(
        release_root / _PYTHON_REQUIREMENTS_PATH,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    if installed_requirements != requirements_payload:
        raise ContractError("requirements runtime installees divergentes")
    if seal_inputs["runtime_requirements_sha256"] != sha256_bytes(
        requirements_payload
    ).removeprefix("sha256:"):
        raise ContractError("sceau et requirements runtime divergents")
    wheelhouse_manifest_payload = source_contents.get(_PYTHON_WHEELHOUSE_MANIFEST_PATH)
    if wheelhouse_manifest_payload is None:
        raise ContractError("manifeste wheelhouse absent de l'archive source")
    installed_wheelhouse_manifest = _strict_regular_bytes(
        release_root / _PYTHON_WHEELHOUSE_MANIFEST_PATH,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    if installed_wheelhouse_manifest != wheelhouse_manifest_payload:
        raise ContractError("manifeste wheelhouse installe divergent de la source")
    if seal_inputs["runtime_wheelhouse_manifest_sha256"] != sha256_bytes(
        wheelhouse_manifest_payload
    ).removeprefix("sha256:"):
        raise ContractError("sceau et manifeste wheelhouse divergent")
    wheelhouse_manifest = _runtime_wheelhouse_manifest_contract(
        wheelhouse_manifest_payload,
        requirements_payload=requirements_payload,
        lock_payload=lock_payload,
    )
    expected_pins = {
        item["filename"]: item["sha256"] for item in seal_inputs["runtime_wheel_pins"]
    }
    (
        requirement_details,
        _requirement_versions,
        requirements_set_sha256,
        _source_pins_sha256,
        source_pins,
        locked_wheels,
    ) = _runtime_requirements_contract(
        requirements_payload,
        lock_payload=lock_payload,
        source_contents=source_contents,
        expected_pins=expected_pins,
    )
    wheelhouse_artifacts, wheelhouse_map_sha256 = _sealed_wheelhouse(
        release_root,
        requirements=requirement_details,
        source_pins=source_pins,
        locked_wheels=locked_wheels,
        wheelhouse_manifest=wheelhouse_manifest,
    )
    release_manifest = _parse_release_manifest(
        _strict_regular_bytes(
            release_root / ".ava-release", max_bytes=_MAX_MANIFEST_BYTES
        )
    )
    rust_wheel_payload = _strict_regular_bytes(
        release_root / ".ava-artifacts" / release_manifest["wheel_filename"],
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    rust_artifact = _wheel_artifact(
        rust_wheel_payload, filename=release_manifest["wheel_filename"]
    )
    rust_identity = (rust_artifact.name, rust_artifact.version)
    if (
        rust_artifact.sha256 != f"sha256:{seal_inputs['rust_wheel_sha256']}"
        or rust_identity in wheelhouse_artifacts
        or rust_identity in requirement_details
    ):
        raise ContractError("wheel Rust divergente ou confondue avec le wheelhouse")
    selected_artifacts = {**wheelhouse_artifacts, rust_identity: rust_artifact}

    stdlib_root = _PROCESS_STDLIB_ROOT.expanduser().absolute()
    try:
        stdlib_root = stdlib_root.resolve(strict=True)
    except OSError as exc:
        raise ContractError("stdlib Python scellee indisponible") from exc
    expected_stdlib_root = python_install_root / (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}"
    )
    if stdlib_root != expected_stdlib_root:
        raise ContractError("stdlib Python hors archive source scellee")
    stdlib_map, stdlib_count, stdlib_size, _stdlib_entries = _sealed_tree_map(
        stdlib_root,
        max_bytes=_MAX_STDLIB_TREE_BYTES,
        reject_site_artifacts=False,
    )
    site_relative = Path(
        f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    site_root = release_root / site_relative
    _raw_site_map, site_count, site_size, site_entries = _sealed_tree_map(
        site_root,
        max_bytes=_MAX_RUNTIME_TREE_BYTES,
        reject_site_artifacts=True,
    )
    (
        installed_distributions,
        removed_pth_paths,
        site_map,
    ) = _validate_site_packages_records(site_root, site_entries, selected_artifacts)
    expected_removed_pth = sorted(
        path
        for artifact in selected_artifacts.values()
        for path in artifact.record
        if path.endswith(".pth") and "/" not in path
    )
    if removed_pth_paths != expected_removed_pth or any(
        path.endswith(".pth") and "/" in path
        for artifact in selected_artifacts.values()
        for path in artifact.record
    ):
        raise ContractError("set de pth supprimees divergent des wheels")
    package_set = sorted([list(identity) for identity in selected_artifacts])
    package_set_sha256 = sha256_bytes(
        json.dumps(package_set, separators=(",", ":")).encode("ascii")
    ).removeprefix("sha256:")
    if package_set_sha256 != seal_document["package_set_sha256"]:
        raise ContractError("package_set scelle divergent des wheels installees")
    critical_imports: dict[str, dict[str, str]] = {}
    for module_name, relative_path in _CRITICAL_IMPORT_PATHS.items():
        entry = site_entries.get(relative_path)
        if entry is None:
            raise ContractError("module runtime critique absent de site-packages")
        critical_imports[module_name] = {
            "path": relative_path,
            "sha256": entry["sha256"],
        }
    installer, installer_version_output = _uv_installer_evidence()
    if (
        installer["sha256"] != f"sha256:{seal_inputs['uv_sha256']}"
        or installer_version_output != seal_inputs["uv_version"]
    ):
        raise ContractError("sceau et installateur uv divergent")
    evidence = {
        "format": _PYTHON_RUNTIME_FORMAT,
        "implementation": sys.implementation.name,
        "python_version": version,
        "executable_path": executable_path.relative_to(release_root).as_posix(),
        "executable_sha256": executable_sha256,
        "runtime_source_path": _PYTHON_RUNTIME_SOURCE_PATH,
        "runtime_source_sha256": sha256_bytes(runtime_source_payload),
        "runtime_archive_path": runtime_archive_relative,
        "runtime_archive_sha256": sha256_bytes(runtime_archive_payload),
        "runtime_archive_map_sha256": runtime_archive_map_sha256,
        "python_install_path": ".python",
        "python_install_map_sha256": python_install_map_sha256,
        "python_install_entry_count": python_install_entry_count,
        "python_install_total_size": python_install_total_size,
        "pyvenv_path": pyvenv_path.relative_to(release_root).as_posix(),
        "pyvenv_contract_sha256": sha256_bytes(canonical_json_bytes(pyvenv_contract)),
        "stdlib_path": stdlib_root.relative_to(release_root).as_posix(),
        "stdlib_map_sha256": stdlib_map,
        "stdlib_entry_count": stdlib_count,
        "stdlib_total_size": stdlib_size,
        "site_packages_path": site_relative.as_posix(),
        "site_packages_map_sha256": site_map,
        "site_packages_entry_count": site_count,
        "site_packages_total_size": site_size,
        "lock_path": _PYTHON_LOCK_PATH,
        "lock_sha256": sha256_bytes(lock_payload),
        "requirements_path": _PYTHON_REQUIREMENTS_PATH,
        "requirements_sha256": sha256_bytes(requirements_payload),
        "requirements_set_sha256": requirements_set_sha256,
        "wheelhouse_manifest_path": _PYTHON_WHEELHOUSE_MANIFEST_PATH,
        "wheelhouse_manifest_sha256": sha256_bytes(wheelhouse_manifest_payload),
        "wheelhouse_path": _SEALED_WHEELHOUSE_PATH,
        "wheelhouse_map_sha256": wheelhouse_map_sha256,
        "critical_imports": critical_imports,
        "removed_pth_count": len(removed_pth_paths),
        "removed_pth_set_sha256": sha256_bytes(canonical_json_bytes(removed_pth_paths)),
        "distribution_set_sha256": sha256_bytes(
            json.dumps(package_set, separators=(",", ":")).encode("ascii")
        ),
        "installer": installer,
        "site_initialization": False,
        "bytecode_allowed": False,
    }
    if _verify_sealed_release_root(release_root) != root_identity:
        raise ContractError("racine scellee remplacee pendant attestation runtime")
    return evidence


def _release_root(value: str | Path) -> tuple[Path, Path]:
    requested = Path(value).expanduser().absolute()
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ContractError("release Ava introuvable") from exc
    if (
        resolved != requested
        or not resolved.is_dir()
        or _GIT_SHA_RE.fullmatch(resolved.name) is None
        or resolved.parent != _SEALED_RELEASE_ROOT.expanduser().absolute()
    ):
        raise ContractError("release Ava non immutable ou mal nommee")
    _verify_sealed_release_root(resolved)
    module_path = _MODULE_PATH.resolve(strict=True)
    if not module_path.is_relative_to(resolved):
        raise ContractError("generateur execute hors de la release attestee")
    return requested, resolved


def _parse_release_manifest(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ContractError("manifeste de release non UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != len(_MANIFEST_KEYS):
        raise ContractError("manifeste de release incomplet")
    items: list[tuple[str, str]] = []
    for line in lines:
        if "=" not in line:
            raise ContractError("manifeste de release invalide")
        key, value = line.split("=", 1)
        if not key or not value:
            raise ContractError("manifeste de release invalide")
        items.append((key, value))
    if tuple(key for key, _value in items) != _MANIFEST_KEYS:
        raise ContractError("ordre ou champs du manifeste de release invalides")
    manifest = dict(items)
    if manifest["format"] != "ava-release-v1":
        raise ContractError("format de release Ava non supporte")
    if _GIT_SHA_RE.fullmatch(manifest["git_sha"]) is None:
        raise ContractError("SHA Git de release invalide")
    for key in (
        "source_tree_sha256",
        "rust_tree_sha256",
        "wheel_sha256",
        "attestation_sha256",
        "evolutions_sha256",
    ):
        if _SHA256_RE.fullmatch(manifest[key]) is None:
            raise ContractError("empreinte du manifeste de release invalide")
    if _WHEEL_RE.fullmatch(manifest["wheel_filename"]) is None:
        raise ContractError("wheel du manifeste de release invalide")
    return manifest


def _prefixed_sha256(value: str, label: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ContractError(f"{label}: empreinte invalide")
    return f"sha256:{value}"


def _safe_archive_name(value: str, label: str) -> str:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ContractError(f"{label}: nom invalide")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(f"{label}: chemin non canonique")
    canonical = path.as_posix()
    if (
        canonical != value.rstrip("/")
        or len(canonical.encode("utf-8")) > _MAX_ARCHIVE_PATH_BYTES
    ):
        raise ContractError(f"{label}: chemin non canonique")
    return canonical


def _source_archive_map(
    payload: bytes,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Return a bounded content map for a Git tar, rejecting special entries."""

    entries: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    seen: set[str] = set()
    total_bytes = 0
    observed_members: list[tarfile.TarInfo] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            member_count = 0
            for index, member in enumerate(archive):
                member_count += 1
                if member_count > _MAX_ARCHIVE_MEMBERS:
                    raise ContractError("archive source vide ou trop volumineuse")
                observed_members.append(member)
                name = _safe_archive_name(member.name, f"archive.source[{index}]")
                if name in seen:
                    raise ContractError("archive source contient un chemin duplique")
                seen.add(name)
                if member.isdir():
                    continue
                if member.isreg():
                    if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                        raise ContractError(
                            "archive source contient un membre hors taille"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ContractError(
                            "archive source contient un fichier illisible"
                        )
                    content = extracted.read(_MAX_ARCHIVE_MEMBER_BYTES + 1)
                    if len(content) != member.size:
                        raise ContractError(
                            "archive source contient un fichier tronque"
                        )
                    mode = "100755" if member.mode & 0o111 else "100644"
                else:
                    raise ContractError(
                        "archive source contient un lien ou type special"
                    )
                total_bytes += len(content)
                if total_bytes > _MAX_ARCHIVE_BYTES:
                    raise ContractError("archive source decompressee hors taille")
                contents[name] = content
                entries.append(
                    {
                        "mode": mode,
                        "path": name,
                        "sha256": sha256_bytes(content),
                        "size": len(content),
                    }
                )
            if member_count == 0:
                raise ContractError("archive source vide ou trop volumineuse")
            _validate_tar_zero_tail(payload, observed_members)
    except (tarfile.TarError, UnicodeError, OSError) as exc:
        raise ContractError("archive source invalide") from exc
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return entries, contents


def _source_archive_map_sha256(payload: bytes) -> tuple[str, dict[str, bytes]]:
    entries, contents = _source_archive_map(payload)
    return sha256_bytes(canonical_json_bytes(entries)), contents


def _verify_installed_source_tree(
    release_root: Path,
    entries: list[dict[str, Any]],
    contents: dict[str, bytes],
) -> str:
    """Bind every Git source entry to the exact installed release bytes."""

    if len(entries) != len(contents):
        raise ContractError("carte source installee incomplete")
    release_root = release_root.expanduser().absolute()
    try:
        if release_root.resolve(strict=True) != release_root:
            raise ContractError("racine de release source liee ou indirecte")
    except OSError as exc:
        raise ContractError("racine de release source introuvable") from exc
    expected_paths = {entry["path"] for entry in entries}
    for path in tuple(expected_paths):
        expected_paths.update(
            parent.as_posix()
            for parent in PurePosixPath(path).parents
            if parent.as_posix() != "."
        )
    observed_paths: set[str] = set()
    frontend_ancestors = {
        parent.as_posix()
        for parent in PurePosixPath(_FRONTEND_INSTALL_PATH).parents
        if parent.as_posix() != "."
    }
    pending = [release_root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: os.fsencode(item.name)
            )
        except OSError as exc:
            raise ContractError("arbre source installe illisible") from exc
        for child in children:
            path = directory / child.name
            relative = path.relative_to(release_root).as_posix()
            top_level = PurePosixPath(relative).parts[0]
            if top_level in _RESERVED_SOURCE_TOP_LEVEL:
                continue
            if relative == _FRONTEND_INSTALL_PATH or relative.startswith(
                f"{_FRONTEND_INSTALL_PATH}/"
            ):
                continue
            if relative not in frontend_ancestors or relative in expected_paths:
                observed_paths.add(relative)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ContractError("entree source installee illisible") from exc
            try:
                if stat.S_ISDIR(metadata.st_mode):
                    _trusted_sealed_metadata(path, directory=True)
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    _trusted_sealed_metadata(path, directory=False)
                else:
                    raise ContractError(
                        "source installee contient un lien ou type special"
                    )
            except ContractError as exc:
                raise ContractError("source installee mutable ou indirecte") from exc
    if observed_paths != expected_paths:
        raise ContractError("arbre source installe contient un extra ou un manque")

    checked_directories: set[Path] = set()
    for entry in entries:
        path = entry["path"]
        expected = contents.get(path)
        if expected is None:
            raise ContractError("carte source installee divergente")
        installed_path = release_root / path
        directory = installed_path.parent
        while directory not in checked_directories:
            try:
                metadata = directory.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != _TRUSTED_RUNTIME_UID
                    or metadata.st_gid != _TRUSTED_RUNTIME_GID
                    or metadata.st_mode & 0o222
                    or stat.S_IMODE(metadata.st_mode) != 0o555
                    or directory.resolve(strict=True) != directory
                ):
                    raise ContractError(
                        "repertoire source installe modifiable ou indirect"
                    )
            except OSError as exc:
                raise ContractError("repertoire source installe invalide") from exc
            checked_directories.add(directory)
            if directory == release_root:
                break
            if release_root not in directory.parents:
                raise ContractError("source installee hors racine de release")
            directory = directory.parent
        try:
            before = installed_path.lstat()
        except OSError as exc:
            raise ContractError("source installee introuvable") from exc
        installed = _strict_regular_bytes(
            installed_path,
            max_bytes=max(1, len(expected)),
            allow_empty=True,
        )
        try:
            after = installed_path.lstat()
            resolved = installed_path.resolve(strict=True)
        except OSError as exc:
            raise ContractError("source installee instable") from exc
        installed_mode = stat.S_IMODE(after.st_mode)
        expected_executable = entry["mode"] == "100755"
        if (
            installed != expected
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_size != after.st_size
            or before.st_uid != after.st_uid
            or before.st_gid != after.st_gid
            or before.st_nlink != after.st_nlink
            or after.st_uid != _TRUSTED_RUNTIME_UID
            or after.st_gid != _TRUSTED_RUNTIME_GID
            or after.st_nlink != 1
            or after.st_mode & 0o222
            or resolved != installed_path
            or installed_mode != (0o555 if expected_executable else 0o444)
        ):
            raise ContractError("source installee et archive de release divergentes")
    return sha256_bytes(canonical_json_bytes(entries))


def _treatment_from_module(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
        module = ast.parse(text, filename=_TREATMENT_MODULE_PATH)
    except (UnicodeError, SyntaxError) as exc:
        raise ContractError("module treatment invalide") from exc
    values: list[Any] = []
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == _TREATMENT_NAME
                for target in targets
            ):
                values.append(ast.literal_eval(node.value))
    if len(values) != 1 or values[0] not in {
        _BASELINE_TREATMENT,
        _CANDIDATE_TREATMENT,
    }:
        raise ContractError("module treatment absent, duplique ou inconnu")
    return str(values[0])


def _wheel_payload_sha256(payload: bytes) -> str:
    """Canonicalise only safe ZIP payload entries, including RECORD."""

    entries: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    expanded = 0
    record_count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as wheel:
            infos = wheel.infolist()
            if not infos or len(infos) > _MAX_WHEEL_MEMBERS:
                raise ContractError("wheel vide ou trop volumineuse")
            for index, info in enumerate(infos):
                raw_name = (
                    info.filename.removesuffix("/") if info.is_dir() else info.filename
                )
                name = _safe_archive_name(raw_name, f"wheel[{index}]")
                if name in seen:
                    raise ContractError("wheel contient un chemin duplique")
                parents = PurePosixPath(name).parents
                if any(
                    seen.get(parent.as_posix()) not in {None, "directory"}
                    for parent in parents
                    if parent.as_posix() != "."
                ) or (
                    not info.is_dir()
                    and any(path.startswith(f"{name}/") for path in seen)
                ):
                    raise ContractError("wheel contient un parent non repertoire")
                if info.flag_bits & 0x1:
                    raise ContractError("wheel contient une entree chiffree")
                if info.file_size < 0 or info.file_size > _MAX_WHEEL_MEMBER_BYTES:
                    raise ContractError("wheel contient une entree hors taille")
                if (
                    info.file_size
                    and info.compress_size == 0
                    or info.compress_size
                    and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise ContractError(
                        "wheel contient un ratio de compression interdit"
                    )
                parts = PurePosixPath(name).parts
                if (
                    parts
                    and parts[0].endswith(".data")
                    and (
                        len(parts) < 2
                        or parts[1] not in {"data", "platlib", "purelib", "scripts"}
                    )
                ):
                    raise ContractError("wheel contient une zone .data non attestable")
                raw_mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    if raw_mode and not stat.S_ISDIR(raw_mode):
                        raise ContractError("wheel contient un repertoire ambigu")
                    seen[name] = "directory"
                    continue
                file_type = stat.S_IFMT(raw_mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise ContractError("wheel contient un type special")
                digest = hashlib.sha256()
                size = 0
                with wheel.open(info, mode="r") as source:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        expanded += len(chunk)
                        if (
                            size > info.file_size
                            or expanded > _MAX_WHEEL_EXPANDED_BYTES
                        ):
                            raise ContractError("wheel decompressee hors taille")
                        digest.update(chunk)
                if size != info.file_size:
                    raise ContractError("wheel contient une entree tronquee")
                if name.endswith(".pth") and "/" in name:
                    raise ContractError("wheel contient un pth imbrique interdit")
                if name.endswith(".pth") and not name.count("/") and not size:
                    raise ContractError("wheel contient un pth direct vide")
                seen[name] = "file"
                path_parts = PurePosixPath(name).parts
                if (
                    len(path_parts) == 2
                    and path_parts[0].endswith(".dist-info")
                    and path_parts[1] == "RECORD"
                ):
                    record_count += 1
                entries.append(
                    {
                        "mode": "100755" if raw_mode & 0o111 else "100644",
                        "path": name,
                        "sha256": f"sha256:{digest.hexdigest()}",
                        "size": size,
                    }
                )
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, OSError) as exc:
        raise ContractError("wheel ZIP invalide") from exc
    if record_count != 1:
        raise ContractError("wheel doit contenir exactement un RECORD")
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return sha256_bytes(canonical_json_bytes(entries))


def _record_rows(payload: bytes, *, label: str) -> dict[str, tuple[str, int]]:
    try:
        decoded_payload = payload.decode("utf-8")
        if (
            not payload
            or len(payload) > _MAX_RECORD_BYTES
            or not payload.endswith(b"\n")
            or "\r" in decoded_payload.replace("\r\n", "")
        ):
            raise ContractError(f"{label}: RECORD contient un CR nu")
        rows = list(csv.reader(io.StringIO(decoded_payload, newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ContractError(f"{label}: RECORD invalide") from exc
    if not rows or len(rows) > _MAX_RUNTIME_ENTRIES:
        raise ContractError(f"{label}: RECORD vide ou hors taille")
    result: dict[str, tuple[str, int]] = {}
    for row in rows:
        if len(row) != 3 or not row[0] or "\\" in row[0] or "\x00" in row[0]:
            raise ContractError(f"{label}: entree RECORD invalide")
        path = _safe_archive_name(row[0], f"{label}.record")
        if path in result:
            raise ContractError(f"{label}: entree RECORD dupliquee")
        if not row[1] and not row[2]:
            result[path] = ("", -1)
            continue
        try:
            algorithm, encoded = row[1].split("=", 1)
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            size = int(row[2])
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{label}: empreinte RECORD invalide") from exc
        if (
            algorithm != "sha256"
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded) is None
            or len(decoded) != 32
            or size < 0
            or str(size) != row[2]
            or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != encoded
        ):
            raise ContractError(f"{label}: empreinte RECORD invalide")
        result[path] = (f"sha256:{decoded.hex()}", size)
    return result


def _wheel_artifact(payload: bytes, *, filename: str) -> WheelArtifact:
    """Parse a raw wheel and bind its closed RECORD to every ZIP payload byte."""

    if _WHEEL_RE.fullmatch(filename) is None:
        raise ContractError("nom de wheel non canonique")
    observed: dict[str, tuple[str, int]] = {}
    control_contents: dict[str, bytes] = {}
    modes: dict[str, str] = {}
    payload_entries: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    expanded = 0
    control_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as wheel:
            infos = wheel.infolist()
            if not infos or len(infos) > _MAX_WHEEL_MEMBERS:
                raise ContractError("wheel vide ou trop volumineuse")
            for index, info in enumerate(infos):
                raw_name = (
                    info.filename.removesuffix("/") if info.is_dir() else info.filename
                )
                name = _safe_archive_name(raw_name, f"wheel[{index}]")
                if name in seen:
                    raise ContractError("wheel contient un chemin duplique")
                parents = PurePosixPath(name).parents
                if any(
                    seen.get(parent.as_posix()) not in {None, "directory"}
                    for parent in parents
                    if parent.as_posix() != "."
                ) or (
                    not info.is_dir()
                    and any(path.startswith(f"{name}/") for path in seen)
                ):
                    raise ContractError("wheel contient un parent non repertoire")
                parts = PurePosixPath(name).parts
                if (
                    parts
                    and parts[0].endswith(".data")
                    and (
                        len(parts) < 2
                        or parts[1] not in {"data", "platlib", "purelib", "scripts"}
                    )
                ):
                    raise ContractError("wheel contient une zone .data non attestable")
                raw_mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    if raw_mode and not stat.S_ISDIR(raw_mode):
                        raise ContractError("wheel contient un repertoire ambigu")
                    seen[name] = "directory"
                    continue
                if (
                    stat.S_IFMT(raw_mode) not in {0, stat.S_IFREG}
                    or info.flag_bits & 0x1
                ):
                    raise ContractError("wheel contient un type special ou chiffre")
                if info.file_size < 0 or info.file_size > _MAX_WHEEL_MEMBER_BYTES:
                    raise ContractError("wheel contient une entree hors taille")
                if name.endswith(".pth") and "/" in name:
                    raise ContractError("wheel contient un pth imbrique interdit")
                if (
                    info.file_size
                    and info.compress_size == 0
                    or info.compress_size
                    and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise ContractError(
                        "wheel contient un ratio de compression interdit"
                    )
                top_level_dist_info_control = (
                    len(parts) == 2
                    and parts[0].endswith(".dist-info")
                    and parts[1] in {"METADATA", "RECORD", "WHEEL", "entry_points.txt"}
                )
                data_script = (
                    len(parts) >= 2
                    and parts[0].endswith(".data")
                    and parts[1] == "scripts"
                )
                retain_content = top_level_dist_info_control or data_script
                if data_script and info.file_size > _MAX_WHEEL_SCRIPT_BYTES:
                    raise ContractError("script .data de wheel hors taille")
                digest = hashlib.sha256()
                size = 0
                chunks: list[bytes] = []
                with wheel.open(info, mode="r") as source:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        expanded += len(chunk)
                        if (
                            size > info.file_size
                            or expanded > _MAX_WHEEL_EXPANDED_BYTES
                        ):
                            raise ContractError("wheel decompressee hors taille")
                        digest.update(chunk)
                        if retain_content:
                            chunks.append(chunk)
                if size != info.file_size:
                    raise ContractError("wheel contient une entree tronquee")
                if name.endswith(".pth") and not name.count("/") and not size:
                    raise ContractError("wheel contient un pth direct vide")
                seen[name] = "file"
                prefixed_digest = f"sha256:{digest.hexdigest()}"
                observed[name] = (prefixed_digest, size)
                mode = "0555" if raw_mode & 0o111 else "0444"
                modes[name] = mode
                payload_entries.append(
                    {
                        "mode": "100755" if raw_mode & 0o111 else "100644",
                        "path": name,
                        "sha256": prefixed_digest,
                        "size": size,
                    }
                )
                if retain_content:
                    content = b"".join(chunks)
                    control_bytes += len(content)
                    if control_bytes > _MAX_WHEEL_CONTROL_BYTES:
                        raise ContractError("metadata de wheel hors taille")
                    control_contents[name] = content
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, OSError) as exc:
        raise ContractError("wheel ZIP invalide") from exc

    metadata_paths = sorted(
        path
        for path in observed
        if len(PurePosixPath(path).parts) == 2
        and PurePosixPath(path).parts[0].endswith(".dist-info")
        and PurePosixPath(path).parts[1] == "METADATA"
    )
    record_paths = sorted(
        path
        for path in observed
        if len(PurePosixPath(path).parts) == 2
        and PurePosixPath(path).parts[0].endswith(".dist-info")
        and PurePosixPath(path).parts[1] == "RECORD"
    )
    if len(metadata_paths) != 1 or len(record_paths) != 1:
        raise ContractError("wheel sans METADATA/RECORD unique")
    dist_info_root = metadata_paths[0].removesuffix("/METADATA")
    if record_paths[0] != f"{dist_info_root}/RECORD":
        raise ContractError("wheel avec racines METADATA/RECORD divergentes")
    metadata = control_contents[metadata_paths[0]]
    name, version = _wheel_metadata_identity(metadata)
    wheel_control_path = f"{dist_info_root}/WHEEL"
    wheel_control = control_contents.get(wheel_control_path)
    if wheel_control is None:
        raise ContractError("wheel sans metadata WHEEL top-level")
    _validate_wheel_tags(filename, wheel_control)
    dist_info_base = dist_info_root.removesuffix(".dist-info")
    filename_parts = filename.removesuffix(".whl").split("-")
    if (
        len(filename_parts) < 5
        or _normalised_distribution_name(filename_parts[0]) != name
        or filename_parts[1] != version
        or "-" not in dist_info_base
        or _normalised_distribution_name(dist_info_base.rsplit("-", 1)[0]) != name
        or dist_info_base.rsplit("-", 1)[1] != version
    ):
        raise ContractError("nom de wheel divergent de METADATA")
    reserved = {
        f"{dist_info_root}/INSTALLER",
        f"{dist_info_root}/REQUESTED",
        f"{dist_info_root}/direct_url.json",
        f"{dist_info_root}/uv_cache.json",
    }
    if set(observed).intersection(reserved):
        raise ContractError("wheel brute contient une metadata generee reservee")
    projected: set[tuple[str, str]] = set()
    for path in observed:
        destination = _wheel_destination(path, dist_info_root=dist_info_root)
        if destination in projected:
            raise ContractError("wheel contient une projection installee dupliquee")
        projected.add(destination)
    record_payload = control_contents.pop(record_paths[0])
    record = _record_rows(record_payload, label=filename)
    if set(record) != set(observed):
        raise ContractError("RECORD brut ne couvre pas exactement la wheel")
    for path, observed_entry in observed.items():
        digest, size = record[path]
        if path == record_paths[0]:
            if digest or size != -1:
                raise ContractError("RECORD brut doit omettre sa propre empreinte")
        elif (digest, size) != observed_entry:
            raise ContractError("RECORD brut et payload de wheel divergent")
    payload_entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return WheelArtifact(
        filename=filename,
        name=name,
        version=version,
        sha256=sha256_bytes(payload),
        payload_sha256=sha256_bytes(canonical_json_bytes(payload_entries)),
        dist_info_root=dist_info_root,
        control_contents=control_contents,
        modes=modes,
        record=record,
    )


def _parse_rust_attestation(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ContractError("attestation Rust non UTF-8") from exc
    if len(lines) != len(_RUST_ATTESTATION_KEYS):
        raise ContractError("attestation Rust incomplete")
    values: dict[str, str] = {}
    for expected_key, line in zip(_RUST_ATTESTATION_KEYS, lines, strict=True):
        key, separator, value = line.partition("=")
        if separator != "=" or key != expected_key or not value or key in values:
            raise ContractError("attestation Rust invalide")
        values[key] = value
    if (
        values["format"] != "ava-rust-wheel-attestation-v1"
        or values["attestation_type"] != "unsigned-checksum-manifest"
        or values["signature"] != "none"
        or _GIT_SHA_RE.fullmatch(values["git_sha"]) is None
        or _SHA256_RE.fullmatch(values["rust_tree_sha256"]) is None
        or _SHA256_RE.fullmatch(values["wheel_sha256"]) is None
        or _WHEEL_RE.fullmatch(values["wheel_filename"]) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", values["builder_image_id"]) is None
        or _SHA256_RE.fullmatch(values["builder_dockerfile_sha256"]) is None
    ):
        raise ContractError("attestation Rust hors contrat")
    return values


def _rust_builder(values: dict[str, str]) -> dict[str, str]:
    return {
        "attestation_type": values["attestation_type"],
        "signature": values["signature"],
        "builder_image_id": values["builder_image_id"],
        "builder_dockerfile_sha256": _prefixed_sha256(
            values["builder_dockerfile_sha256"], "builder_dockerfile_sha256"
        ),
        "builder_platform": values["builder_platform"],
        "builder_python_image": values["builder_python_image"],
        "builder_rust_image": values["builder_rust_image"],
        "python_version": values["python_version"],
        "rust_version": values["rust_version"],
        "maturin_version": values["maturin_version"],
        "wheel_compatibility": values["wheel_compatibility"],
    }


def _builder_recipe_sha256_from_source(
    builder: dict[str, Any], source_contents: dict[str, bytes]
) -> str:
    """Recalculate one recipe from attested fields and its exact Dockerfile."""

    dockerfile_payload = source_contents.get(_RUST_BUILDER_DOCKERFILE_PATH)
    if dockerfile_payload is None:
        raise ContractError("Dockerfile du builder absent de l'archive source")
    dockerfile_sha256 = sha256_bytes(dockerfile_payload)
    if builder.get("builder_dockerfile_sha256") != dockerfile_sha256:
        raise ContractError("Dockerfile du builder et attestation Rust divergents")
    return rust_builder_recipe_sha256(builder)


def _current_target() -> Path:
    current = _CURRENT_LINK.expanduser().absolute()
    authority_before = _trusted_authority_directory(current.parent)
    try:
        metadata = current.lstat()
        target = current.resolve(strict=True)
        authority_after = _trusted_authority_directory(current.parent)
        current_after = current.lstat()
        target_after = current.resolve(strict=True)
    except OSError as exc:
        raise ContractError("pointeur current indisponible") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _TRUSTED_RUNTIME_UID
        or metadata.st_gid != _TRUSTED_RUNTIME_GID
        or metadata.st_nlink != 1
        or not target.is_dir()
        or (authority_before.st_dev, authority_before.st_ino)
        != (authority_after.st_dev, authority_after.st_ino)
        or (metadata.st_dev, metadata.st_ino)
        != (current_after.st_dev, current_after.st_ino)
        or target_after != target
    ):
        raise ContractError("pointeur current non canonique")
    return target


def _deployment_state(release_root: Path, treatment: str) -> str:
    target = _current_target()
    if treatment == _BASELINE_TREATMENT:
        if target == release_root:
            raise ContractError("baseline shadow ne peut pas etre current")
        return "prepared_noncurrent"
    if treatment == _CANDIDATE_TREATMENT:
        if target != release_root:
            raise ContractError("candidate shadow doit etre current")
        return "active_current"
    raise ContractError("treatment de release inconnu")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"configuration {label} absente")
    return value


def _optional_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key, "")
    if type(value) is not str:
        raise ContractError(f"configuration {label} invalide")
    return value.strip()


def _configured_anthropic_engine(payload: bytes) -> tuple[str, str, str]:
    try:
        decoded = payload.decode("utf-8")
        config = tomllib.loads(decoded)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError("configuration Ava TOML invalide") from exc
    intelligence = _mapping(config.get("intelligence"), "intelligence")
    server_value = config.get("server", {})
    server = _mapping(server_value, "server")
    server_model = _optional_string(server, "model", "server.model")
    default_model = _optional_string(
        intelligence,
        "default_model",
        "intelligence.default_model",
    )
    model = server_model or default_model
    provider = _optional_string(
        intelligence,
        "provider",
        "intelligence.provider",
    )
    preferred_engine = _optional_string(
        intelligence,
        "preferred_engine",
        "intelligence.preferred_engine",
    )
    if provider != "anthropic" or _MODEL_RE.fullmatch(model) is None:
        raise ContractError("configuration Ava non Anthropic ou modele invalide")
    if preferred_engine not in {"", "cloud"}:
        raise ContractError("moteur configure incompatible avec CloudEngine")
    return provider, model, "cloud"


def _private_output_directory(value: str | Path) -> Path:
    candidate = Path(value).expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ContractError("repertoire de sortie indisponible") from exc
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ContractError("repertoire de sortie non prive ou indirect")
    return resolved


def _write_private_content_addressed(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _strict_regular_bytes(path, max_bytes=len(payload))
        if existing != payload or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ContractError("conflit d'attestation de release")
        return
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short attestation write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def generate_release_attestation(
    *,
    release_root: str | Path,
    config_path: str | Path,
    evaluation_manifest_sha256: str,
    output_directory: str | Path,
) -> ReleaseAttestationResult:
    """Derive and publish one immutable, non-secret release attestation."""

    if re.fullmatch(r"sha256:[0-9a-f]{64}", evaluation_manifest_sha256) is None:
        raise ContractError("empreinte du manifeste d'evaluation invalide")
    requested_config = Path(config_path).expanduser().absolute()
    expected_config = _DEPLOYED_CONFIG_PATH.expanduser().absolute()
    if requested_config != expected_config:
        raise ContractError("configuration Ava hors chemin runtime canonique")
    requested_root, resolved_root = _release_root(release_root)
    evaluation_manifest_path = resolved_root / _EVALUATION_MANIFEST_PATH
    evaluation_manifest_payload = _strict_regular_bytes(
        evaluation_manifest_path, max_bytes=_MAX_CONFIG_BYTES
    )
    evaluation_manifest_digest = sha256_bytes(evaluation_manifest_payload)
    if evaluation_manifest_digest != evaluation_manifest_sha256:
        raise ContractError("manifeste d'evaluation et pin GitOps divergents")
    evaluation_suite = load_suite(evaluation_manifest_path)
    if (
        evaluation_suite.manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
        or evaluation_suite.manifest_sha256 != evaluation_manifest_digest
    ):
        raise ContractError("contrat du manifeste d'evaluation invalide")
    ready_path = resolved_root / ".ava-ready"
    manifest_path = resolved_root / ".ava-release"
    ready_payload = _strict_regular_bytes(ready_path, max_bytes=1024)
    manifest_payload = _strict_regular_bytes(
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest = _parse_release_manifest(manifest_payload)
    if manifest["git_sha"] != resolved_root.name:
        raise ContractError("release et manifeste Git divergents")
    if ready_payload != f"{manifest['git_sha']}\n".encode("ascii"):
        raise ContractError("marqueur ready et manifeste Git divergents")
    artifact_root = resolved_root / ".ava-artifacts"
    source_archive_payload = _strict_regular_bytes(
        artifact_root / "source-tree.tar", max_bytes=_MAX_ARCHIVE_BYTES
    )
    frontend_archive_payload = _strict_regular_bytes(
        resolved_root / _FRONTEND_ARCHIVE_PATH,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    frontend_build_attestation_payload = _strict_regular_bytes(
        resolved_root / _FRONTEND_BUILD_ATTESTATION_PATH,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    rust_archive_payload = _strict_regular_bytes(
        artifact_root / "rust-tree.tar", max_bytes=_MAX_ARCHIVE_BYTES
    )
    wheel_payload = _strict_regular_bytes(
        artifact_root / manifest["wheel_filename"], max_bytes=_MAX_ARCHIVE_BYTES
    )
    rust_attestation_payload = _strict_regular_bytes(
        artifact_root / f"{manifest['wheel_filename']}.attestation",
        max_bytes=_MAX_CONFIG_BYTES,
    )
    if sha256_bytes(source_archive_payload) != _prefixed_sha256(
        manifest["source_tree_sha256"], "source_tree_sha256"
    ):
        raise ContractError("archive source et manifeste de release divergents")
    if sha256_bytes(rust_archive_payload) != _prefixed_sha256(
        manifest["rust_tree_sha256"], "rust_tree_sha256"
    ):
        raise ContractError("archive Rust et manifeste de release divergents")
    if sha256_bytes(wheel_payload) != _prefixed_sha256(
        manifest["wheel_sha256"], "wheel_sha256"
    ):
        raise ContractError("wheel et manifeste de release divergents")
    if sha256_bytes(rust_attestation_payload) != _prefixed_sha256(
        manifest["attestation_sha256"], "attestation_sha256"
    ):
        raise ContractError("attestation Rust et manifeste de release divergents")
    source_entries, source_contents = _source_archive_map(source_archive_payload)
    source_map_sha256 = sha256_bytes(canonical_json_bytes(source_entries))
    if (
        _verify_installed_source_tree(resolved_root, source_entries, source_contents)
        != source_map_sha256
    ):
        raise ContractError("carte source installee divergente")
    rust_map_sha256, _rust_contents = _source_archive_map_sha256(rust_archive_payload)
    treatment_payload = source_contents.get(_TREATMENT_MODULE_PATH)
    if treatment_payload is None:
        raise ContractError("module treatment absent de l'archive source")
    treatment = _treatment_from_module(treatment_payload)
    if source_contents.get(_EVALUATION_MANIFEST_PATH) != evaluation_manifest_payload:
        raise ContractError("manifeste d'evaluation et archive source divergents")
    frontend = _frontend_build_evidence(
        source_archive_payload=source_archive_payload,
        source_contents=source_contents,
        frontend_archive_payload=frontend_archive_payload,
        build_attestation_payload=frontend_build_attestation_payload,
        git_sha=manifest["git_sha"],
    )
    treatment_path = resolved_root / _TREATMENT_MODULE_PATH
    installed_treatment = _strict_regular_bytes(
        treatment_path, max_bytes=_MAX_CONFIG_BYTES
    )
    if (
        installed_treatment != treatment_payload
        or stat.S_IMODE(treatment_path.stat().st_mode) != 0o444
    ):
        raise ContractError("module treatment installe divergent ou modifiable")
    treatment_module_sha256 = sha256_bytes(treatment_payload)
    rust_values = _parse_rust_attestation(rust_attestation_payload)
    if (
        rust_values["git_sha"] != manifest["git_sha"]
        or rust_values["rust_tree_sha256"] != manifest["rust_tree_sha256"]
        or rust_values["wheel_sha256"] != manifest["wheel_sha256"]
        or rust_values["wheel_filename"] != manifest["wheel_filename"]
    ):
        raise ContractError("attestation Rust et release divergentes")
    rust_builder = _rust_builder(rust_values)
    # This is deliberately recalculated from the archive bytes even though the
    # recipe digest itself is emitted only by the later causal-pair generator.
    _builder_recipe_sha256_from_source(rust_builder, source_contents)
    wheel_payload_sha256 = _wheel_payload_sha256(wheel_payload)
    python_runtime = _python_runtime_evidence(resolved_root, source_contents)
    if python_runtime["python_version"] != rust_builder["python_version"]:
        raise ContractError("runtime Python et builder Rust divergents")
    deployment_state = _deployment_state(resolved_root, treatment)
    config_payload = _strict_regular_bytes(
        requested_config,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    provider, model, adapter = _configured_anthropic_engine(config_payload)
    # Detect an atomic `ava-current` switch while the evidence was being read.
    if (
        requested_root.resolve(strict=True) != resolved_root
        or _deployment_state(resolved_root, treatment) != deployment_state
        or _strict_regular_bytes(evaluation_manifest_path, max_bytes=_MAX_CONFIG_BYTES)
        != evaluation_manifest_payload
        or _strict_regular_bytes(ready_path, max_bytes=1024) != ready_payload
        or _strict_regular_bytes(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        != manifest_payload
        or _strict_regular_bytes(
            resolved_root / _FRONTEND_ARCHIVE_PATH,
            max_bytes=_MAX_ARCHIVE_BYTES,
        )
        != frontend_archive_payload
        or _strict_regular_bytes(
            resolved_root / _FRONTEND_BUILD_ATTESTATION_PATH,
            max_bytes=_MAX_CONFIG_BYTES,
        )
        != frontend_build_attestation_payload
        or _strict_regular_bytes(treatment_path, max_bytes=_MAX_CONFIG_BYTES)
        != installed_treatment
        or _strict_regular_bytes(requested_config, max_bytes=_MAX_CONFIG_BYTES)
        != config_payload
        or _python_runtime_evidence(resolved_root, source_contents) != python_runtime
    ):
        raise ContractError("release active basculee pendant l'attestation")
    revalidated_suite = load_suite(evaluation_manifest_path)
    if revalidated_suite.manifest_sha256 != evaluation_manifest_digest:
        raise ContractError("contrat d'evaluation modifie pendant l'attestation")
    if (
        _verify_installed_source_tree(resolved_root, source_entries, source_contents)
        != source_map_sha256
    ):
        raise ContractError("source installee modifiee pendant l'attestation")

    config_sha256 = sha256_bytes(config_payload)
    manifest_sha256 = sha256_bytes(manifest_payload)
    document = {
        "artifact": {
            "evaluation_manifest_sha256": evaluation_manifest_digest,
            "frontend": frontend,
            "manifest_sha256": manifest_sha256,
            "python_runtime": python_runtime,
            "rust_attestation_sha256": sha256_bytes(rust_attestation_payload),
            "rust_builder": rust_builder,
            "rust_archive_map_sha256": rust_map_sha256,
            "rust_tree_sha256": _prefixed_sha256(
                manifest["rust_tree_sha256"], "rust_tree_sha256"
            ),
            "source_archive_map_sha256": source_map_sha256,
            "source_tree_sha256": _prefixed_sha256(
                manifest["source_tree_sha256"], "source_tree_sha256"
            ),
            "wheel_filename": manifest["wheel_filename"],
            "wheel_payload_sha256": wheel_payload_sha256,
            "wheel_sha256": _prefixed_sha256(manifest["wheel_sha256"], "wheel_sha256"),
        },
        "attestation_id": (
            "ava-release-shadow-"
            f"{manifest['git_sha'][:12]}-{config_sha256.removeprefix('sha256:')[:8]}-"
            f"{evaluation_manifest_digest.removeprefix('sha256:')[:8]}"
        ),
        "canonical_knowledge": False,
        "engine": {
            "adapter": adapter,
            "config_sha256": config_sha256,
            "model": model,
            "provider": provider,
            # The provider response must return this exact identifier on every call.
            "revision": model,
        },
        "release": {
            "deployment_state": deployment_state,
            "git_sha": manifest["git_sha"],
            "repository": "repo://ava",
            "treatment": treatment,
            "treatment_module_path": _TREATMENT_MODULE_PATH,
            "treatment_module_sha256": treatment_module_sha256,
        },
        "schema_version": RELEASE_ATTESTATION_SCHEMA_VERSION,
    }
    payload = canonical_json_bytes(document) + b"\n"
    digest = sha256_bytes(payload)
    output_root = _private_output_directory(output_directory)
    output_path = output_root / (
        "ava-release-attestation-"
        f"{manifest['git_sha']}-{digest.removeprefix('sha256:')}.json"
    )
    _write_private_content_addressed(output_path, payload)
    load_release_attestation(output_path, expected_sha256=digest)
    return ReleaseAttestationResult(
        output_path=output_path,
        sha256=digest,
        git_sha=manifest["git_sha"],
    )


def _verify_attested_source_archive(
    source_archive_path: str | Path,
    attestation: Any,
) -> tuple[bytes, list[dict[str, Any]], dict[str, bytes]]:
    """Recalculate one copied source archive without requiring a deployed tree."""

    document = attestation.document
    release = document["release"]
    artifact = document["artifact"]
    source_payload = _strict_regular_bytes(
        Path(source_archive_path), max_bytes=_MAX_ARCHIVE_BYTES
    )
    entries, contents = _source_archive_map(source_payload)
    if (
        sha256_bytes(source_payload) != artifact["source_tree_sha256"]
        or sha256_bytes(canonical_json_bytes(entries))
        != artifact["source_archive_map_sha256"]
    ):
        raise ContractError("archive source copiee et attestation divergentes")
    treatment_payload = contents.get(_TREATMENT_MODULE_PATH)
    if (
        treatment_payload is None
        or _treatment_from_module(treatment_payload) != release["treatment"]
        or sha256_bytes(treatment_payload) != release["treatment_module_sha256"]
    ):
        raise ContractError("module treatment et attestation divergents")
    return source_payload, entries, contents


def _repository_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser().absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("depot Git du causal pair introuvable") from exc
    if resolved != candidate or not resolved.is_dir():
        raise ContractError("depot Git du causal pair non canonique")
    try:
        top_level = Path(
            _git_command(resolved, "rev-parse", "--show-toplevel")
            .decode("utf-8", errors="strict")
            .strip()
        ).absolute()
    except UnicodeError as exc:
        raise ContractError("racine Git du causal pair invalide") from exc
    if top_level != resolved:
        raise ContractError("depot Git du causal pair n'est pas sa racine")
    return resolved


def _git_environment() -> dict[str, str]:
    """Bind Git plumbing to ``-C <repository>`` and literal object identities."""

    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _trusted_git_executable() -> Path:
    candidate = _GIT_EXECUTABLE.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        if (
            candidate.resolve(strict=True) != candidate
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise ContractError("executable Git du causal pair non fiable")
    except OSError as exc:
        raise ContractError("executable Git du causal pair indisponible") from exc
    return candidate


def _git_command(repository: Path, *arguments: str) -> bytes:
    git_executable = _trusted_git_executable()
    try:
        completed = subprocess.run(
            [str(git_executable), "-C", str(repository), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("preuve Git du causal pair indisponible") from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_ARCHIVE_BYTES:
        raise ContractError("preuve Git du causal pair invalide")
    return completed.stdout


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ContractError("preuve des blobs Git tronquee")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _git_tree(
    repository: Path, git_sha: str
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Read the complete recursive Git tree and every exact regular blob.

    ``git archive`` is not an authority for the changed-path proof because
    ``export-ignore`` and ``export-subst`` attributes can hide or rewrite tree
    entries.  The pair generator therefore reads the full tree and blobs, then
    compares their canonical map with the separately supplied release archive.
    """

    raw_tree = _git_command(repository, "ls-tree", "-rz", "--full-tree", git_sha)
    if not raw_tree.endswith(b"\0"):
        raise ContractError("preuve Git du causal pair tronquee")
    raw_records = raw_tree[:-1].split(b"\0")
    if not raw_records or len(raw_records) > _MAX_ARCHIVE_MEMBERS:
        raise ContractError("arbre Git vide ou trop volumineux")

    blobs: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index, record in enumerate(raw_records):
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = header.split(b" ")
            path = raw_path.decode("utf-8", errors="strict")
            mode = raw_mode.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
            oid = raw_oid.decode("ascii", errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise ContractError("entree de l'arbre Git invalide") from exc
        path = _safe_archive_name(path, f"git.tree[{index}]")
        if path in seen:
            raise ContractError("arbre Git contient un chemin duplique")
        seen.add(path)
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None
        ):
            raise ContractError("arbre Git contient un lien ou type special")
        blobs.append((path, mode, oid))

    try:
        process = subprocess.Popen(
            [
                str(_trusted_git_executable()),
                "-C",
                str(repository),
                "cat-file",
                "--batch",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=_git_environment(),
        )
    except OSError as exc:
        raise ContractError("lecture des blobs Git indisponible") from exc
    entries: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    total_bytes = 0
    try:
        if process.stdin is None or process.stdout is None:
            raise ContractError("lecture des blobs Git indisponible")
        for path, mode, oid in blobs:
            process.stdin.write(f"{oid}\n".encode("ascii"))
            process.stdin.flush()
            response_header = process.stdout.readline(256)
            if not response_header.endswith(b"\n") or len(response_header) >= 256:
                raise ContractError("entete de blob Git invalide")
            try:
                returned_oid, raw_type, raw_size = response_header[:-1].split(b" ")
                size = int(raw_size.decode("ascii", errors="strict"))
            except (ValueError, UnicodeError) as exc:
                raise ContractError("entete de blob Git invalide") from exc
            if (
                returned_oid.decode("ascii", errors="strict") != oid
                or raw_type != b"blob"
                or size < 0
                or size > _MAX_ARCHIVE_MEMBER_BYTES
            ):
                raise ContractError("blob Git hors contrat")
            total_bytes += size
            if total_bytes > _MAX_ARCHIVE_BYTES:
                raise ContractError("arbre Git decompresse hors taille")
            content = _read_exact(process.stdout, size)
            if process.stdout.read(1) != b"\n":
                raise ContractError("separateur de blob Git invalide")
            contents[path] = content
            entries.append(
                {
                    "mode": mode,
                    "path": path,
                    "sha256": sha256_bytes(content),
                    "size": size,
                }
            )
        process.stdin.close()
        if process.wait(timeout=120) != 0:
            raise ContractError("lecture des blobs Git invalide")
        if process.stdout.read(1) != b"":
            raise ContractError("sortie Git supplementaire inattendue")
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ContractError("lecture des blobs Git invalide") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return entries, contents


def _raw_commit_parents(repository: Path, git_sha: str) -> list[str]:
    """Read literal commit headers, bypassing replace refs and legacy grafts."""

    payload = _git_command(repository, "cat-file", "commit", git_sha)
    if len(payload) > 1024 * 1024:
        raise ContractError("objet commit du causal pair hors taille")
    headers, separator, _message = payload.partition(b"\n\n")
    if not separator or b"\0" in headers:
        raise ContractError("objet commit du causal pair invalide")
    parents: list[str] = []
    for line in headers.splitlines():
        if not line.startswith(b"parent "):
            continue
        raw_parent = line.removeprefix(b"parent ")
        try:
            parent = raw_parent.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise ContractError("parent brut du causal pair invalide") from exc
        if re.fullmatch(r"[0-9a-f]{40}", parent) is None:
            raise ContractError("parent brut du causal pair invalide")
        parents.append(parent)
    return parents


def _pair_side(
    attestation: Any, *, source_contents: dict[str, bytes]
) -> dict[str, Any]:
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
        "builder_recipe_sha256": _builder_recipe_sha256_from_source(
            artifact["rust_builder"], source_contents
        ),
        "python_runtime_sha256": python_runtime_sha256(artifact["python_runtime"]),
    }


def generate_causal_pair(
    *,
    repository_root: str | Path,
    baseline_source_archive_path: str | Path,
    candidate_source_archive_path: str | Path,
    baseline_attestation_path: str | Path,
    baseline_attestation_sha256: str,
    candidate_attestation_path: str | Path,
    candidate_attestation_sha256: str,
    evaluation_manifest_sha256: str,
    output_directory: str | Path,
) -> CausalPairResult:
    """Verify the direct-child marker-only experiment and publish its proof."""

    if re.fullmatch(r"sha256:[0-9a-f]{64}", evaluation_manifest_sha256) is None:
        raise ContractError("empreinte du manifeste d'evaluation invalide")
    baseline = load_release_attestation(
        baseline_attestation_path, expected_sha256=baseline_attestation_sha256
    )
    candidate = load_release_attestation(
        candidate_attestation_path, expected_sha256=candidate_attestation_sha256
    )
    if (
        baseline.document["schema_version"] != RELEASE_ATTESTATION_SCHEMA_VERSION
        or candidate.document["schema_version"] != RELEASE_ATTESTATION_SCHEMA_VERSION
    ):
        raise ContractError("causal pair exige deux attestations release v2")
    baseline_release = baseline.document["release"]
    candidate_release = candidate.document["release"]
    if (
        baseline_release["treatment"] != _BASELINE_TREATMENT
        or candidate_release["treatment"] != _CANDIDATE_TREATMENT
        or baseline_release["repository"] != candidate_release["repository"]
        or baseline.document["engine"] != candidate.document["engine"]
        or baseline.document["artifact"]["wheel_filename"]
        != candidate.document["artifact"]["wheel_filename"]
    ):
        raise ContractError("causal pair: releases A/B ou moteurs invalides")
    if (
        baseline.document["artifact"]["evaluation_manifest_sha256"]
        != evaluation_manifest_sha256
        or candidate.document["artifact"]["evaluation_manifest_sha256"]
        != evaluation_manifest_sha256
    ):
        raise ContractError("causal pair: manifeste et attestations divergents")
    _baseline_archive_payload, baseline_archive_entries, baseline_archive_contents = (
        _verify_attested_source_archive(baseline_source_archive_path, baseline)
    )
    (
        _candidate_archive_payload,
        candidate_archive_entries,
        candidate_archive_contents,
    ) = _verify_attested_source_archive(candidate_source_archive_path, candidate)

    repository = _repository_root(repository_root)
    if (
        _git_command(repository, "rev-parse", "--is-shallow-repository").strip()
        != b"false"
    ):
        raise ContractError("causal pair refuse un depot Git shallow")
    if _raw_commit_parents(repository, candidate_release["git_sha"]) != [
        baseline_release["git_sha"]
    ]:
        raise ContractError("candidate B ne possede pas A comme parent unique")
    baseline_git_entries, baseline_git_contents = _git_tree(
        repository, baseline_release["git_sha"]
    )
    candidate_git_entries, candidate_git_contents = _git_tree(
        repository, candidate_release["git_sha"]
    )
    if (
        baseline_git_entries != baseline_archive_entries
        or candidate_git_entries != candidate_archive_entries
    ):
        raise ContractError("archives source de release et commits Git divergents")
    baseline_by_path = {entry["path"]: entry for entry in baseline_git_entries}
    candidate_by_path = {entry["path"]: entry for entry in candidate_git_entries}
    if set(baseline_by_path) != set(candidate_by_path):
        raise ContractError("causal pair modifie la liste des chemins")
    changed_paths = [
        path
        for path in sorted(baseline_by_path, key=lambda item: item.encode("utf-8"))
        if baseline_by_path[path] != candidate_by_path[path]
    ]
    if changed_paths != [_TREATMENT_MODULE_PATH]:
        raise ContractError("causal pair n'est pas un diff marker-only")
    before_entry = baseline_by_path[_TREATMENT_MODULE_PATH]
    after_entry = candidate_by_path[_TREATMENT_MODULE_PATH]
    before = baseline_git_contents[_TREATMENT_MODULE_PATH]
    after = candidate_git_contents[_TREATMENT_MODULE_PATH]
    old_literal = _BASELINE_TREATMENT.encode("utf-8")
    new_literal = _CANDIDATE_TREATMENT.encode("utf-8")
    if (
        before_entry["mode"] != "100644"
        or after_entry["mode"] != "100644"
        or before.count(old_literal) != 1
        or new_literal in before
        or after != before.replace(old_literal, new_literal, 1)
        or after.count(new_literal) != 1
        or old_literal in after
        or _treatment_from_module(before) != _BASELINE_TREATMENT
        or _treatment_from_module(after) != _CANDIDATE_TREATMENT
    ):
        raise ContractError("causal pair ne remplace pas le literal unique attendu")

    baseline_side = _pair_side(baseline, source_contents=baseline_archive_contents)
    candidate_side = _pair_side(candidate, source_contents=candidate_archive_contents)
    for key in (
        "rust_archive_map_sha256",
        "wheel_payload_sha256",
        "builder_recipe_sha256",
        "python_runtime_sha256",
    ):
        if baseline_side[key] != candidate_side[key]:
            raise ContractError(f"causal pair: equivalence {key} non prouvee")
    frontend_equivalence_keys = (
        "archive_sha256",
        "archive_map_sha256",
        "source_map_sha256",
        "builder_recipe_sha256",
    )
    for key in frontend_equivalence_keys:
        if baseline_side["frontend"][key] != candidate_side["frontend"][key]:
            raise ContractError(f"causal pair: equivalence frontend.{key} non prouvee")
    baseline_map_sha256 = sha256_bytes(canonical_json_bytes(baseline_git_entries))
    candidate_map_sha256 = sha256_bytes(canonical_json_bytes(candidate_git_entries))
    document = {
        "baseline": baseline_side,
        "candidate": candidate_side,
        "canonical_knowledge": False,
        "equivalence": {
            **{
                key: baseline_side[key]
                for key in (
                    "rust_archive_map_sha256",
                    "wheel_payload_sha256",
                    "builder_recipe_sha256",
                    "python_runtime_sha256",
                )
            },
            "frontend": {
                key: baseline_side["frontend"][key] for key in frontend_equivalence_keys
            },
        },
        "evaluation_manifest_sha256": evaluation_manifest_sha256,
        "isolation": {
            "baseline_archive_map_sha256": baseline_map_sha256,
            "baseline_git_tree_map_sha256": baseline_map_sha256,
            "baseline_literal": _BASELINE_TREATMENT,
            "candidate_archive_map_sha256": candidate_map_sha256,
            "candidate_git_tree_map_sha256": candidate_map_sha256,
            "candidate_literal": _CANDIDATE_TREATMENT,
            "candidate_parent_count": 1,
            "candidate_parent_git_sha": baseline_release["git_sha"],
            "changed_paths": changed_paths,
            "total_entry_count": len(baseline_git_entries),
            "treatment_module_mode": "100644",
            "treatment_module_path": _TREATMENT_MODULE_PATH,
            "unchanged_entry_count": len(baseline_git_entries) - 1,
        },
        "model_output_causality_claimed": False,
        "pair_id": (
            "relationship-causal-pair-"
            f"{baseline_release['git_sha'][:12]}-{candidate_release['git_sha'][:12]}"
        ),
        "repository": baseline_release["repository"],
        "schema_version": CAUSAL_PAIR_SCHEMA_VERSION,
    }
    payload = canonical_json_bytes(document) + b"\n"
    digest = sha256_bytes(payload)
    output_root = _private_output_directory(output_directory)
    output_path = output_root / (
        "ava-relationship-causal-pair-"
        f"{baseline_release['git_sha']}-{candidate_release['git_sha']}-"
        f"{digest.removeprefix('sha256:')}.json"
    )
    _write_private_content_addressed(output_path, payload)
    load_causal_pair(
        output_path,
        expected_sha256=digest,
        baseline_attestation=baseline,
        candidate_attestation=candidate,
        expected_manifest_sha256=evaluation_manifest_sha256,
    )
    return CausalPairResult(
        output_path=output_path,
        sha256=digest,
        baseline_git_sha=baseline_release["git_sha"],
        candidate_git_sha=candidate_release["git_sha"],
    )


def _release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive an Ava relationship-shadow release attestation"
    )
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--evaluation-manifest-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def _causal_pair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and publish one Ava relationship causal pair"
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--baseline-source-archive", required=True)
    parser.add_argument("--baseline-release-attestation", required=True)
    parser.add_argument("--baseline-release-attestation-sha256", required=True)
    parser.add_argument("--candidate-source-archive", required=True)
    parser.add_argument("--candidate-release-attestation", required=True)
    parser.add_argument("--candidate-release-attestation-sha256", required=True)
    parser.add_argument("--evaluation-manifest-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    causal_pair_mode = bool(arguments and arguments[0] == "causal-pair")
    parser = _causal_pair_parser() if causal_pair_mode else _release_parser()
    args = parser.parse_args(arguments[1:] if causal_pair_mode else arguments)
    try:
        if causal_pair_mode:
            result = generate_causal_pair(
                repository_root=args.repository_root,
                baseline_source_archive_path=args.baseline_source_archive,
                candidate_source_archive_path=args.candidate_source_archive,
                baseline_attestation_path=args.baseline_release_attestation,
                baseline_attestation_sha256=(args.baseline_release_attestation_sha256),
                candidate_attestation_path=args.candidate_release_attestation,
                candidate_attestation_sha256=(
                    args.candidate_release_attestation_sha256
                ),
                evaluation_manifest_sha256=args.evaluation_manifest_sha256,
                output_directory=args.output_dir,
            )
        else:
            result = generate_release_attestation(
                release_root=args.release_root,
                config_path=args.config,
                evaluation_manifest_sha256=args.evaluation_manifest_sha256,
                output_directory=args.output_dir,
            )
    except (ContractError, OSError, ValueError):
        failure = (
            "causal pair generation failed"
            if causal_pair_mode
            else "release attestation generation failed"
        )
        print(failure, file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"path": str(result.output_path), "sha256": result.sha256},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())


__all__ = [
    "CausalPairResult",
    "ReleaseAttestationResult",
    "generate_causal_pair",
    "generate_release_attestation",
    "main",
]
