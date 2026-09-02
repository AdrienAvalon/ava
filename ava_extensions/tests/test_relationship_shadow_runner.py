"""Offline contracts for the causal relationship shadow and its release evidence."""

from __future__ import annotations

import base64
import csv
import hashlib
import inspect
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from ava_extensions import runtime_bootstrap
from ava_extensions.evals.relationship import cli as relationship_cli
from ava_extensions.evals.relationship import contracts as contracts_module
from ava_extensions.evals.relationship import release_attestation as release_module
from ava_extensions.evals.relationship import shadow_runner as shadow_module
from ava_extensions.evals.relationship.contracts import (
    ContractError,
    LoadedCausalShadowBinding,
    _validate_guard_observation_v3,
    canonical_json_bytes,
    load_causal_pair,
    load_causal_shadow_binding,
    load_release_attestation,
    load_response_bundle,
    load_suite,
    reload_causal_shadow_binding,
    sha256_bytes,
    sha256_file,
)
from ava_extensions.evals.relationship.evaluator import build_comparison_report
from ava_extensions.evals.relationship.release_attestation import (
    _source_archive_map_sha256,
    _wheel_payload_sha256,
    generate_causal_pair,
    generate_release_attestation,
)
from ava_extensions.evals.relationship.shadow_runner import (
    ShadowRunError,
    _encode_service_assertion_key,
    _observe_runtime_relationship_guard,
    _ObservedEngine,
    _RelationshipGuardObserver,
    _visible_memory_claims,
)
from ava_extensions.identity.relationship_guard import (
    RelationshipOutputGuard,
    RelationshipRepairResult,
)

DATA_ROOT = Path(__file__).parents[1] / "evals" / "relationship" / "data"
MANIFEST = DATA_ROOT / "manifest.v3.json"
TREATMENT_PATH = Path("ava_extensions/identity/relationship_guard_treatment.py")
BASELINE_TREATMENT = "shadow-baseline-only-v1"
CANDIDATE_TREATMENT = "runtime-enforced-v1"
WHEEL_NAME = "ava_rust-0.1.0-cp312-cp312-linux_x86_64.whl"
BUILDER_DOCKERFILE_PATH = Path("deploy/docker/Dockerfile.rust-builder")
FRONTEND_BUILDER_DOCKERFILE_PATH = Path("deploy/docker/Dockerfile.frontend-builder")
BUILDER_PYTHON_IMAGE = (
    "python:3.12.13-slim-bookworm@sha256:"
    "76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf"
)
BUILDER_RUST_IMAGE = (
    "rust:1.88.0-bookworm@sha256:"
    "4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0"
)
FAKE_UV_PAYLOAD = b"#!/bin/sh\nprintf 'uv 0.12.5 (synthetic)\\n'\n"


def _git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return completed.stdout


def _write_wheel(
    path: Path, *, timestamp: tuple[int, int, int, int, int, int]
) -> bytes:
    payload = _synthetic_wheel_payload(
        name="ava-rust",
        version="0.1.0",
        timestamp=timestamp,
        wheel_tags=("cp312-cp312-linux_x86_64",),
    )
    path.write_bytes(payload)
    return payload


def _record_digest(payload: bytes) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _synthetic_wheel_payload(
    *,
    name: str,
    version: str,
    timestamp: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0),
    removed_pth: bool = False,
    extra_entries: dict[str, bytes] | None = None,
    wheel_tags: tuple[str, ...] = ("py3-none-any",),
) -> bytes:
    distribution = name.replace("-", "_")
    dist_root = f"{distribution}-{version}.dist-info"
    entries = {
        f"{distribution}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_root}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode(),
        f"{dist_root}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: synthetic\n"
            + b"Root-Is-Purelib: true\n"
            + b"".join(f"Tag: {tag}\n".encode("ascii") for tag in wheel_tags)
        ),
    }
    if removed_pth:
        entries[f"{distribution}.pth"] = f"import {distribution}\n".encode()
    entries.update(extra_entries or {})
    record_path = f"{dist_root}/RECORD"
    entries[record_path] = (
        "".join(
            f"{entry},sha256={_record_digest(content)},{len(content)}\n"
            for entry, content in sorted(entries.items())
        )
        + f"{record_path},,\n"
    ).encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for entry, content in sorted(entries.items()):
            info = zipfile.ZipInfo(entry, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if ".data/scripts/" in entry else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            wheel.writestr(info, content)
    return stream.getvalue()


def _install_wheel_distribution(
    site_root: Path,
    *,
    filename: str,
    payload: bytes,
) -> None:
    artifact = release_module._wheel_artifact(payload, filename=filename)
    venv_root = site_root.parents[2]
    record_path = f"{artifact.dist_info_root}/RECORD"
    installed_rows: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(payload), mode="r") as wheel:
        raw_contents = {
            entry: wheel.read(entry)
            for entry in artifact.record
            if entry != record_path
        }
    for entry, content in raw_contents.items():
        if entry == record_path or (entry.endswith(".pth") and "/" not in entry):
            continue
        zone, installed_entry = release_module._wheel_destination(
            entry,
            dist_info_root=artifact.dist_info_root,
        )
        if zone == "site":
            target = site_root / installed_entry
            installed_content = content
            mode = int(artifact.modes[entry], 8)
            record_entry = installed_entry
        elif zone == "bin":
            target = venv_root / "bin" / installed_entry
            installed_content, _canonical = release_module._data_script_contents(
                content,
                venv_root=venv_root,
            )
            mode = 0o555
            record_entry = f"../../../bin/{installed_entry}"
        else:
            target = venv_root / installed_entry
            installed_content = content
            mode = int(artifact.modes[entry], 8)
            record_entry = f"../../../{installed_entry}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(installed_content)
        target.chmod(mode)
        installed_rows[record_entry] = installed_content

    entry_points = artifact.control_contents.get(
        f"{artifact.dist_info_root}/entry_points.txt"
    )
    for script_name, target_name in release_module._console_entry_points(
        entry_points
    ).items():
        script, _canonical = release_module._console_script_contents(
            target_name,
            venv_root=venv_root,
        )
        target = venv_root / "bin" / script_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(script)
        target.chmod(0o555)
        installed_rows[f"../../../bin/{script_name}"] = script
    generated = {
        f"{artifact.dist_info_root}/INSTALLER": b"uv",
        f"{artifact.dist_info_root}/REQUESTED": b"",
    }
    for entry, content in generated.items():
        (site_root / entry).write_bytes(content)
        installed_rows[entry] = content
    for entry, content in raw_contents.items():
        if entry.endswith(".pth") and "/" not in entry:
            installed_rows[entry] = content
    rows = [
        (entry, f"sha256={_record_digest(content)}", str(len(content)))
        for entry, content in sorted(
            installed_rows.items(), key=lambda item: item[0].encode("utf-8")
        )
    ] + [(record_path, "", "")]
    (site_root / record_path).write_bytes(release_module._record_payload(rows))


def _rust_attestation(
    *,
    git_sha: str,
    rust_payload: bytes,
    wheel_payload: bytes,
    builder_dockerfile_payload: bytes,
) -> bytes:
    values = {
        "format": "ava-rust-wheel-attestation-v1",
        "attestation_type": "unsigned-checksum-manifest",
        "signature": "none",
        "git_sha": git_sha,
        "rust_tree_sha256": sha256_bytes(rust_payload).removeprefix("sha256:"),
        "wheel_sha256": sha256_bytes(wheel_payload).removeprefix("sha256:"),
        "wheel_filename": WHEEL_NAME,
        "builder_image_id": sha256_bytes(git_sha.encode("ascii")),
        "builder_dockerfile_sha256": sha256_bytes(
            builder_dockerfile_payload
        ).removeprefix("sha256:"),
        "builder_platform": "linux/amd64",
        "builder_python_image": BUILDER_PYTHON_IMAGE,
        "builder_rust_image": BUILDER_RUST_IMAGE,
        "python_version": "3.12.13",
        "rust_version": "1.88.0",
        "maturin_version": "1.14.1",
        "wheel_compatibility": "manylinux_2_36_x86_64",
    }
    return ("\n".join(f"{key}={value}" for key, value in values.items()) + "\n").encode(
        "utf-8"
    )


def _install_synthetic_distribution(
    site_root: Path, *, name: str, version: str
) -> None:
    filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    _install_wheel_distribution(
        site_root,
        filename=filename,
        payload=_synthetic_wheel_payload(name=name, version=version),
    )


def _synthetic_python_runtime_source() -> tuple[bytes, bytes]:
    executable = b"\x7fELFsynthetic-python-3.12.13\n"
    stdlib = b"name = 'synthetic-posix'\n"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in ("python", "python/bin", "python/lib", "python/lib/python3.12"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            archive.addfile(info)
        for name, content, mode in (
            ("python/bin/python3.12", executable, 0o755),
            ("python/lib/python3.12/os.py", stdlib, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("python/bin/python")
        link.type = tarfile.SYMTYPE
        link.linkname = "python3.12"
        link.mode = 0o777
        link.mtime = 0
        archive.addfile(link)
    archive_payload = stream.getvalue()
    source_document = {
        "archive": {
            "member_count": 3,
            "regular_file_bytes": len(executable) + len(stdlib),
            "regular_file_count": 2,
            "sealed_relative_path": ".ava-artifacts/python-runtime.tar.gz",
            "sha256": sha256_bytes(archive_payload).removeprefix("sha256:"),
            "size": len(archive_payload),
            "symlink_count": 1,
            "url": "https://releases.astral.sh/synthetic/python.tar.gz",
        },
        "build": "20260510",
        "implementation": "cpython",
        "platform": "x86_64-unknown-linux-gnu",
        "schema": "ava.python-runtime-source/v1",
        "version": "3.12.13",
    }
    return archive_payload, canonical_json_bytes(source_document) + b"\n"


def _runtime_wheelhouse_manifest_payload(
    *,
    requirements_payload: bytes,
    lock_payload: bytes,
    runtime_wheels: dict[str, bytes],
) -> bytes:
    document = {
        "entries": [
            {"filename": filename, "sha256": sha256_bytes(payload)}
            for filename, payload in sorted(runtime_wheels.items())
        ],
        "requirements_sha256": sha256_bytes(requirements_payload),
        "schema_version": "ava.runtime-wheelhouse/v1",
        "target": {
            "implementation": "cpython",
            "platform": "linux_x86_64",
            "python_version": "3.12.13",
        },
        "uv_lock_sha256": sha256_bytes(lock_payload),
    }
    return canonical_json_bytes(document) + b"\n"


def _synthetic_frontend_archive() -> tuple[bytes, dict[str, bytes]]:
    contents = {
        "index.html": b"<!doctype html><title>Ava synthetic</title>\n",
        "assets/app.js": b"globalThis.AVA_SYNTHETIC = true;\n",
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        directory = tarfile.TarInfo("assets")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = directory.gid = directory.mtime = 0
        directory.uname = directory.gname = ""
        archive.addfile(directory)
        for name, content in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue(), contents


def _frontend_build_attestation_payload(
    *,
    git_sha: str,
    source_payload: bytes,
    source_contents: dict[str, bytes],
    frontend_payload: bytes,
) -> bytes:
    source_rows = release_module._archive_extraction_rows(
        source_payload,
        source_archive=True,
        git_sha=git_sha,
    )
    frontend_source_rows = [
        row for row in source_rows if str(row.get("path", "")).startswith("frontend/")
    ]
    frontend_rows = release_module._archive_extraction_rows(
        frontend_payload,
        source_archive=False,
        reproducible_metadata=True,
    )
    dockerfile = source_contents[FRONTEND_BUILDER_DOCKERFILE_PATH.as_posix()]
    document = {
        "builder": {
            "base_image": release_module._FRONTEND_BUILDER_BASE_IMAGE,
            "dockerfile_path": FRONTEND_BUILDER_DOCKERFILE_PATH.as_posix(),
            "dockerfile_sha256": sha256_bytes(dockerfile),
            "node_version": release_module._FRONTEND_NODE_VERSION,
            "npm_version": release_module._FRONTEND_NPM_VERSION,
            "platform": release_module._FRONTEND_BUILDER_PLATFORM,
        },
        "frontend_source_map_sha256": sha256_bytes(
            release_module._canonical_ascii_json_bytes(frontend_source_rows)
        ),
        "git_sha": git_sha,
        "output": {
            "archive_map_sha256": sha256_bytes(
                release_module._canonical_ascii_json_bytes(frontend_rows)
            ),
            "archive_path": "frontend-static.tar",
            "archive_sha256": sha256_bytes(frontend_payload),
            "build_count": 2,
        },
        "package_json_sha256": sha256_bytes(source_contents["frontend/package.json"]),
        "package_lock_sha256": sha256_bytes(
            source_contents["frontend/package-lock.json"]
        ),
        "schema_version": release_module._FRONTEND_BUILD_ATTESTATION_SCHEMA,
        "source_archive_sha256": sha256_bytes(source_payload),
    }
    return release_module._canonical_ascii_json_bytes(document) + b"\n"


def _jsonl_payload(format_name: str, rows: list[dict[str, Any]]) -> bytes:
    return b"".join(
        canonical_json_bytes(document) + b"\n"
        for document in ({"format": format_name}, *rows)
    )


def _fixture_tree_rows(
    root: Path, *, prefixes: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if prefixes is not None and not any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in prefixes
        ):
            continue
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            rows.append({"mode": "0555", "path": relative, "type": "directory"})
        else:
            payload = path.read_bytes()
            rows.append(
                {
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    "path": relative,
                    "sha256": sha256_bytes(payload).removeprefix("sha256:"),
                    "size": len(payload),
                    "type": "file",
                }
            )
    return rows


def _refresh_release_proofs(release_root: Path) -> None:
    """Simulate a root forger updating every mutable digest except raw provenance."""

    release_root.chmod(0o755)
    runtime_path = release_root / ".ava-runtime-manifest.jsonl"
    files_path = release_root / ".ava-files-manifest.jsonl"
    seal_path = release_root / ".ava-seal.json"
    for path in (runtime_path, files_path, seal_path):
        path.chmod(0o644)
    runtime_payload = _jsonl_payload(
        release_module._RUNTIME_MAP_FORMAT,
        _fixture_tree_rows(release_root, prefixes=(".python", ".venv")),
    )
    runtime_path.write_bytes(runtime_payload)
    runtime_path.chmod(0o444)
    files_payload = _jsonl_payload(
        release_module._FILES_MAP_FORMAT,
        [
            row
            for row in _fixture_tree_rows(release_root)
            if row["path"] not in {".ava-files-manifest.jsonl", ".ava-seal.json"}
        ],
    )
    files_path.write_bytes(files_payload)
    files_path.chmod(0o444)
    seal = json.loads(seal_path.read_bytes())
    seal["runtime_manifest_sha256"] = sha256_bytes(runtime_payload).removeprefix(
        "sha256:"
    )
    seal["files_manifest_sha256"] = sha256_bytes(files_payload).removeprefix("sha256:")
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)


def _rewrite_installed_record_entry(
    record_path: Path,
    *,
    installed_path: str,
    payload: bytes,
) -> None:
    rows = list(
        csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"), newline=""))
    )
    record_relative = next(row[0] for row in rows if row[1:] == ["", ""])
    replacements = {row[0]: tuple(row) for row in rows if row[0] != record_relative}
    replacements[installed_path] = (
        installed_path,
        f"sha256={_record_digest(payload)}",
        str(len(payload)),
    )
    canonical_rows = sorted(
        replacements.values(), key=lambda row: row[0].encode("utf-8")
    ) + [(record_relative, "", "")]
    record_path.chmod(0o644)
    record_path.write_bytes(release_module._record_payload(canonical_rows))
    record_path.chmod(0o444)


def _make_release(
    root: Path,
    *,
    git_sha: str,
    source_payload: bytes,
    rust_payload: bytes,
    wheel_timestamp: tuple[int, int, int, int, int, int],
    treatment: str,
    python_archive_payload: bytes,
    frontend_archive_payload: bytes,
    frontend_contents: dict[str, bytes],
    runtime_wheels: dict[str, bytes],
) -> tuple[Path, Path]:
    release_root = root / git_sha
    artifact_root = release_root / ".ava-artifacts"
    treatment_path = release_root / TREATMENT_PATH
    wheelhouse_root = artifact_root / "python-wheelhouse"
    wheelhouse_root.mkdir(parents=True)
    source_entries, source_contents = release_module._source_archive_map(source_payload)
    for entry in source_entries:
        installed = release_root / entry["path"]
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(source_contents[entry["path"]])
        installed.chmod(0o555 if entry["mode"] == "100755" else 0o444)
    assert treatment_path.read_text(encoding="utf-8") == (
        f'RELATIONSHIP_GUARD_TREATMENT = "{treatment}"\n'
    )
    treatment_path.chmod(0o444)
    frontend_root = release_root / release_module._FRONTEND_INSTALL_PATH
    frontend_root.mkdir(parents=True)
    for relative, payload in frontend_contents.items():
        target = frontend_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    source_path = artifact_root / "source-tree.tar"
    source_path.write_bytes(source_payload)
    (artifact_root / "frontend-static.tar").write_bytes(frontend_archive_payload)
    frontend_build_attestation = _frontend_build_attestation_payload(
        git_sha=git_sha,
        source_payload=source_payload,
        source_contents=source_contents,
        frontend_payload=frontend_archive_payload,
    )
    (artifact_root / "frontend-build-attestation.json").write_bytes(
        frontend_build_attestation
    )
    (artifact_root / "rust-tree.tar").write_bytes(rust_payload)
    (artifact_root / "python-runtime.tar.gz").write_bytes(python_archive_payload)
    evolutions_payload = b'{"format":"synthetic-evolutions-v1"}\n'
    (artifact_root / "evolutions-v1.json").write_bytes(evolutions_payload)
    for filename, payload in runtime_wheels.items():
        (wheelhouse_root / filename).write_bytes(payload)
    wheel_path = artifact_root / WHEEL_NAME
    wheel_payload = _write_wheel(wheel_path, timestamp=wheel_timestamp)
    rust_attestation = _rust_attestation(
        git_sha=git_sha,
        rust_payload=rust_payload,
        wheel_payload=wheel_payload,
        builder_dockerfile_payload=source_contents[BUILDER_DOCKERFILE_PATH.as_posix()],
    )
    (artifact_root / f"{WHEEL_NAME}.attestation").write_bytes(rust_attestation)
    manifest = (
        "format=ava-release-v1\n"
        f"git_sha={git_sha}\n"
        f"source_tree_sha256={sha256_bytes(source_payload).removeprefix('sha256:')}\n"
        f"rust_tree_sha256={sha256_bytes(rust_payload).removeprefix('sha256:')}\n"
        f"wheel_sha256={sha256_bytes(wheel_payload).removeprefix('sha256:')}\n"
        f"wheel_filename={WHEEL_NAME}\n"
        f"attestation_sha256={sha256_bytes(rust_attestation).removeprefix('sha256:')}\n"
        f"evolutions_sha256={sha256_bytes(evolutions_payload).removeprefix('sha256:')}\n"
    )
    manifest_payload = manifest.encode("ascii")
    (release_root / ".ava-release").write_bytes(manifest_payload)
    (release_root / ".ava-ready").write_text(f"{git_sha}\n", encoding="ascii")
    python_source = release_module._python_runtime_source_contract(
        source_contents["deploy/runtime/ava-python-runtime.v1.json"]
    )
    _python_map, python_entries, python_contents = (
        release_module._python_archive_materialized_map(
            python_archive_payload,
            source_contract=python_source,
        )
    )
    python_root = release_root / ".python"
    for entry in python_entries:
        installed = python_root / entry["path"]
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(python_contents[entry["path"]])
        installed.chmod(int(entry["mode"], 8))
    interpreter = release_root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(python_contents["bin/python3.12"])
    interpreter.chmod(0o555)
    for alias in ("python3", "python3.12"):
        target = interpreter.parent / alias
        target.write_bytes(python_contents["bin/python3.12"])
        target.chmod(0o555)
    pyvenv = release_root / ".venv" / "pyvenv.cfg"
    pyvenv.write_text(
        "\n".join(
            (
                f"home = {python_root / 'bin'}",
                "include-system-site-packages = false",
                "version = 3.12.13",
                f"executable = {python_root / 'bin/python3.12'}",
                (
                    f"command = {python_root / 'bin/python3.12'} -m venv "
                    f"--copies --without-pip {release_root / '.venv'}"
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    site_root = release_root / ".venv" / "lib" / "python3.12" / "site-packages"
    for filename, payload in runtime_wheels.items():
        _install_wheel_distribution(site_root, filename=filename, payload=payload)
    _install_wheel_distribution(
        site_root,
        filename=WHEEL_NAME,
        payload=wheel_payload,
    )

    source_rows = release_module._archive_extraction_rows(
        source_payload,
        source_archive=True,
        git_sha=git_sha,
    )
    frontend_rows = release_module._archive_extraction_rows(
        frontend_archive_payload,
        source_archive=False,
        reproducible_metadata=True,
    )
    source_rows.append(
        {
            "mode": "0555",
            "path": release_module._FRONTEND_INSTALL_PATH,
            "type": "directory",
        }
    )
    source_rows.extend(
        {
            **row,
            "path": f"{release_module._FRONTEND_INSTALL_PATH}/{row['path']}",
        }
        for row in frontend_rows
    )
    source_rows.sort(key=lambda row: str(row["path"]))
    (release_root / ".ava-source-manifest.jsonl").write_bytes(
        _jsonl_payload(release_module._SOURCE_MAP_FORMAT, source_rows)
    )
    for installed in sorted(
        release_root.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if installed.is_dir():
            installed.chmod(0o555)
        elif installed.is_file():
            mode = stat.S_IMODE(installed.stat().st_mode)
            installed.chmod(0o555 if mode & 0o111 else 0o444)
    runtime_rows = _fixture_tree_rows(release_root, prefixes=(".python", ".venv"))
    runtime_manifest_payload = _jsonl_payload(
        release_module._RUNTIME_MAP_FORMAT, runtime_rows
    )
    runtime_manifest_path = release_root / ".ava-runtime-manifest.jsonl"
    runtime_manifest_path.write_bytes(runtime_manifest_payload)
    runtime_manifest_path.chmod(0o444)
    files_rows = [
        row
        for row in _fixture_tree_rows(release_root)
        if row["path"] not in {".ava-files-manifest.jsonl", ".ava-seal.json"}
    ]
    files_manifest_payload = _jsonl_payload(
        release_module._FILES_MAP_FORMAT, files_rows
    )
    files_manifest_path = release_root / ".ava-files-manifest.jsonl"
    files_manifest_path.write_bytes(files_manifest_payload)
    files_manifest_path.chmod(0o444)
    recipe_sha256 = sha256_bytes(
        canonical_json_bytes(release_module._SEALED_RUNTIME_RECIPE)
    ).removeprefix("sha256:")
    identities = {
        (
            release_module._wheel_artifact(payload, filename=filename).name,
            release_module._wheel_artifact(payload, filename=filename).version,
        )
        for filename, payload in {**runtime_wheels, WHEEL_NAME: wheel_payload}.items()
    }
    package_payload = json.dumps(
        sorted([list(identity) for identity in identities]), separators=(",", ":")
    ).encode("ascii")
    seal = {
        "files_manifest_sha256": sha256_bytes(files_manifest_payload).removeprefix(
            "sha256:"
        ),
        "format": "ava-sealed-release-v1",
        "git_sha": git_sha,
        "inputs": {
            "evolutions_sha256": sha256_bytes(evolutions_payload).removeprefix(
                "sha256:"
            ),
            "frontend_static_sha256": sha256_bytes(
                frontend_archive_payload
            ).removeprefix("sha256:"),
            "frontend_build_attestation_sha256": sha256_bytes(
                frontend_build_attestation
            ).removeprefix("sha256:"),
            "git_sha": git_sha,
            "python_runtime_archive": release_module._PYTHON_RUNTIME_ARCHIVE_NAME,
            "python_runtime_sha256": sha256_bytes(python_archive_payload).removeprefix(
                "sha256:"
            ),
            "python_runtime_source_sha256": sha256_bytes(
                source_contents["deploy/runtime/ava-python-runtime.v1.json"]
            ).removeprefix("sha256:"),
            "release_manifest_sha256": sha256_bytes(manifest_payload).removeprefix(
                "sha256:"
            ),
            "runtime_requirements_sha256": sha256_bytes(
                source_contents["deploy/runtime/ava-runtime-requirements.v1.txt"]
            ).removeprefix("sha256:"),
            "runtime_wheelhouse_manifest_sha256": sha256_bytes(
                source_contents["deploy/runtime/ava-runtime-wheelhouse.v1.json"]
            ).removeprefix("sha256:"),
            "runtime_wheel_pins": [
                {
                    "filename": "docopt-0.6.2-py2.py3-none-any.whl",
                    "sha256": sha256_bytes(
                        source_contents[
                            "deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl"
                        ]
                    ).removeprefix("sha256:"),
                }
            ],
            "rust_attestation_sha256": sha256_bytes(rust_attestation).removeprefix(
                "sha256:"
            ),
            "rust_tree_sha256": sha256_bytes(rust_payload).removeprefix("sha256:"),
            "rust_wheel_sha256": sha256_bytes(wheel_payload).removeprefix("sha256:"),
            "source_tree_sha256": sha256_bytes(source_payload).removeprefix("sha256:"),
            "uv_lock_sha256": sha256_bytes(source_contents["uv.lock"]).removeprefix(
                "sha256:"
            ),
            "uv_sha256": sha256_bytes(FAKE_UV_PAYLOAD).removeprefix("sha256:"),
            "uv_version": "uv 0.12.5 (synthetic)",
        },
        "package_set_sha256": sha256_bytes(package_payload).removeprefix("sha256:"),
        "runtime_manifest_sha256": sha256_bytes(runtime_manifest_payload).removeprefix(
            "sha256:"
        ),
        "runtime_recipe": release_module._SEALED_RUNTIME_RECIPE,
        "runtime_recipe_sha256": recipe_sha256,
        "source_manifest_sha256": sha256_bytes(
            (release_root / ".ava-source-manifest.jsonl").read_bytes()
        ).removeprefix("sha256:"),
    }
    seal_path = release_root / ".ava-seal.json"
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)
    return release_root, source_path


def _causal_release_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "eval@example.invalid")
    _git(repository, "config", "user.name", "Ava eval")
    treatment_path = repository / TREATMENT_PATH
    treatment_path.parent.mkdir(parents=True)
    treatment_path.write_text(
        f'RELATIONSHIP_GUARD_TREATMENT = "{BASELINE_TREATMENT}"\n',
        encoding="utf-8",
    )
    (repository / "rust").mkdir()
    (repository / "rust" / "lib.rs").write_text("pub const VALUE: u8 = 1;\n")
    (repository / "README.md").write_text("synthetic causal fixture\n")
    checkout_root = Path(__file__).parents[2]
    runtime_bootstrap = repository / "ava_extensions/runtime_bootstrap.py"
    runtime_bootstrap.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        checkout_root / "ava_extensions/runtime_bootstrap.py", runtime_bootstrap
    )
    runtime_requirements = repository / "deploy/runtime/ava-runtime-requirements.v1.txt"
    runtime_requirements.parent.mkdir(parents=True, exist_ok=True)
    docopt_wheel = (
        repository / "deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl"
    )
    docopt_wheel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        checkout_root / "deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl",
        docopt_wheel,
    )
    runtime_wheels: dict[str, bytes] = {}
    synthetic_packages = (
        ("anthropic", "0.120.2", True),
        ("cryptography", "50.0.0", False),
        ("dill", "0.4.1", False),
        ("httpcore", "1.0.9", False),
        ("httpx", "0.28.1", False),
        ("num2words", "0.5.14", False),
        ("sympy", "1.14.0", False),
    )
    for name, version, removed_pth in synthetic_packages:
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        extra_entries: dict[str, bytes] = {}
        if name == "httpcore":
            extra_entries.update(
                {
                    f"{name.replace('-', '_')}-{version}.data/purelib/"
                    f"{name.replace('-', '_')}/transport.py": b"TRANSPORT = 'sealed'\n"
                }
            )
        if name in {"dill", "num2words"}:
            script_name = "undill" if name == "dill" else "num2words"
            extra_entries[
                f"{name.replace('-', '_')}-{version}.data/scripts/{script_name}"
            ] = b"#!python\nprint('sealed data script')\n"
        if name == "sympy":
            extra_entries[
                f"{name.replace('-', '_')}-{version}.data/data/share/man/man1/isympy.1"
            ] = b".TH ISYMPY 1\n.SH NAME\nisympy - sealed fixture\n"
        if name == "httpx":
            extra_entries[
                f"{name.replace('-', '_')}-{version}.dist-info/entry_points.txt"
            ] = b"[console_scripts]\nhttpx = httpx:main\n"
        runtime_wheels[filename] = _synthetic_wheel_payload(
            name=name,
            version=version,
            removed_pth=removed_pth,
            extra_entries=extra_entries or None,
        )
    runtime_wheels[docopt_wheel.name] = docopt_wheel.read_bytes()
    docopt_sdist = "49b3a825280bd66b3aa83585ef59c4a8c82f2c8a522dbe754a8bc8d08c85c491"
    lock_lines = [
        "version = 1",
        "",
        "[[package]]",
        'name = "openjarvis"',
        'source = { editable = "." }',
        "",
        "[[package]]",
        'name = "openjarvis-rust"',
        'source = { directory = "rust/crates/openjarvis-python" }',
        "",
        "[[package]]",
        'name = "docopt"',
        'version = "0.6.2"',
        'source = { registry = "https://pypi.org/simple" }',
        (
            'sdist = { url = "https://files.pythonhosted.org/docopt-0.6.2.tar.gz", '
            f'hash = "sha256:{docopt_sdist}" }}'
        ),
        "",
    ]
    requirement_lines = [
        "docopt==0.6.2 "
        f"--hash=sha256:{docopt_sdist} "
        f"--hash={sha256_bytes(runtime_wheels[docopt_wheel.name])}"
    ]
    for name, version, _removed_pth in synthetic_packages:
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        digest = sha256_bytes(runtime_wheels[filename])
        lock_lines.extend(
            (
                "[[package]]",
                f'name = "{name}"',
                f'version = "{version}"',
                'source = { registry = "https://pypi.org/simple" }',
                "wheels = [",
                (
                    '    { url = "https://files.pythonhosted.org/'
                    f'{filename}", hash = "{digest}" }},'
                ),
                "]",
                "",
            )
        )
        requirement_lines.append(f"{name}=={version} --hash={digest}")
    (repository / "uv.lock").write_text("\n".join(lock_lines), encoding="ascii")
    runtime_requirements.write_text(
        "\n".join(requirement_lines) + "\n", encoding="ascii"
    )
    (repository / "deploy/runtime/ava-runtime-wheelhouse.v1.json").write_bytes(
        _runtime_wheelhouse_manifest_payload(
            requirements_payload=runtime_requirements.read_bytes(),
            lock_payload=(repository / "uv.lock").read_bytes(),
            runtime_wheels=runtime_wheels,
        )
    )
    python_archive_payload, python_runtime_source = _synthetic_python_runtime_source()
    frontend_archive_payload, frontend_contents = _synthetic_frontend_archive()
    frontend_root = repository / "frontend"
    (frontend_root / "src").mkdir(parents=True)
    (frontend_root / "package.json").write_bytes(
        b'{"name":"ava-frontend-fixture","private":true,"version":"1.0.0"}\n'
    )
    (frontend_root / "package-lock.json").write_bytes(
        b'{"lockfileVersion":3,"name":"ava-frontend-fixture","packages":{},'
        b'"requires":true,"version":"1.0.0"}\n'
    )
    (frontend_root / "src/main.ts").write_bytes(
        b"globalThis.AVA_SOURCE_FIXTURE = true;\n"
    )
    (repository / "deploy/runtime/ava-python-runtime.v1.json").write_bytes(
        python_runtime_source
    )
    builder_dockerfile = repository / BUILDER_DOCKERFILE_PATH
    builder_dockerfile.parent.mkdir(parents=True)
    shutil.copy2(
        Path(__file__).parents[2] / BUILDER_DOCKERFILE_PATH,
        builder_dockerfile,
    )
    frontend_builder_dockerfile = repository / FRONTEND_BUILDER_DOCKERFILE_PATH
    shutil.copy2(
        Path(__file__).parents[2] / FRONTEND_BUILDER_DOCKERFILE_PATH,
        frontend_builder_dockerfile,
    )
    data_target = repository / "ava_extensions/evals/relationship/data"
    data_target.mkdir(parents=True)
    for source in DATA_ROOT.glob("*.json"):
        shutil.copy2(source, data_target / source.name)
    _git(repository, "add", ".")
    baseline_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    _git(repository, "commit", "-q", "-m", "baseline", env=baseline_env)
    baseline_sha = _git(repository, "rev-parse", "HEAD").decode().strip()
    treatment_path.write_text(
        f'RELATIONSHIP_GUARD_TREATMENT = "{CANDIDATE_TREATMENT}"\n',
        encoding="utf-8",
    )
    _git(repository, "add", str(TREATMENT_PATH))
    candidate_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-02T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-02T00:00:00+00:00",
    }
    _git(repository, "commit", "-q", "-m", "candidate", env=candidate_env)
    candidate_sha = _git(repository, "rev-parse", "HEAD").decode().strip()

    release_parent = tmp_path / "releases"
    tmp_path.chmod(0o755)
    release_parent.mkdir()
    monkeypatch.setattr(release_module, "_SEALED_RELEASE_ROOT", release_parent)
    monkeypatch.setattr(release_module, "_TRUSTED_RUNTIME_UID", os.geteuid())
    monkeypatch.setattr(release_module, "_TRUSTED_RUNTIME_GID", os.getegid())
    monkeypatch.setattr(release_module, "_PROCESS_EUID", os.geteuid() + 1)
    baseline_source_git = _git(repository, "archive", "--format=tar", baseline_sha)
    candidate_source_git = _git(repository, "archive", "--format=tar", candidate_sha)
    baseline_entries, baseline_contents = release_module._source_archive_map(
        baseline_source_git
    )
    candidate_entries, candidate_contents = release_module._source_archive_map(
        candidate_source_git
    )
    baseline_source = _tar_payload(
        [
            (
                entry["path"],
                baseline_contents[entry["path"]],
                int(entry["mode"][-3:], 8),
            )
            for entry in baseline_entries
        ],
        mtime=11,
        pax_comment=baseline_sha,
    )
    candidate_source = _tar_payload(
        [
            (
                entry["path"],
                candidate_contents[entry["path"]],
                int(entry["mode"][-3:], 8),
            )
            for entry in candidate_entries
        ],
        mtime=22,
        pax_comment=candidate_sha,
    )
    baseline_rust = _git(repository, "archive", "--format=tar", baseline_sha, "rust")
    candidate_rust = _git(repository, "archive", "--format=tar", candidate_sha, "rust")
    baseline_root, baseline_source_path = _make_release(
        release_parent,
        git_sha=baseline_sha,
        source_payload=baseline_source,
        rust_payload=baseline_rust,
        wheel_timestamp=(2026, 1, 1, 0, 0, 0),
        treatment=BASELINE_TREATMENT,
        python_archive_payload=python_archive_payload,
        frontend_archive_payload=frontend_archive_payload,
        frontend_contents=frontend_contents,
        runtime_wheels=runtime_wheels,
    )
    candidate_root, candidate_source_path = _make_release(
        release_parent,
        git_sha=candidate_sha,
        source_payload=candidate_source,
        rust_payload=candidate_rust,
        wheel_timestamp=(2026, 1, 2, 0, 0, 0),
        treatment=CANDIDATE_TREATMENT,
        python_archive_payload=python_archive_payload,
        frontend_archive_payload=frontend_archive_payload,
        frontend_contents=frontend_contents,
        runtime_wheels=runtime_wheels,
    )
    release_parent.chmod(0o555)
    fake_uv = tmp_path / "uv"
    fake_uv.write_bytes(FAKE_UV_PAYLOAD)
    fake_uv.chmod(0o555)
    monkeypatch.setattr(release_module, "_UV_EXECUTABLE", fake_uv)
    current = tmp_path / "ava-current"
    current.symlink_to(candidate_root, target_is_directory=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[intelligence]\n"
        'provider = "anthropic"\n'
        'default_model = "claude-test-v1"\n'
        'preferred_engine = "cloud"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_module, "_DEPLOYED_CONFIG_PATH", config)
    attestations = tmp_path / "attestations"
    attestations.mkdir(mode=0o700)
    evaluation_manifest_sha256 = sha256_file(MANIFEST)
    monkeypatch.setattr(release_module, "_CURRENT_LINK", current)
    monkeypatch.setattr(release_module, "_MODULE_PATH", baseline_root / TREATMENT_PATH)
    monkeypatch.setattr(
        release_module, "_PROCESS_EXECUTABLE", baseline_root / ".venv/bin/python"
    )
    monkeypatch.setattr(
        release_module,
        "_PROCESS_STDLIB_ROOT",
        baseline_root / ".python/lib/python3.12",
    )
    baseline_attestation = generate_release_attestation(
        release_root=baseline_root,
        config_path=config,
        evaluation_manifest_sha256=evaluation_manifest_sha256,
        output_directory=attestations,
    )
    monkeypatch.setattr(release_module, "_MODULE_PATH", candidate_root / TREATMENT_PATH)
    monkeypatch.setattr(
        release_module, "_PROCESS_EXECUTABLE", candidate_root / ".venv/bin/python"
    )
    monkeypatch.setattr(
        release_module,
        "_PROCESS_STDLIB_ROOT",
        candidate_root / ".python/lib/python3.12",
    )
    candidate_attestation = generate_release_attestation(
        release_root=candidate_root,
        config_path=config,
        evaluation_manifest_sha256=evaluation_manifest_sha256,
        output_directory=attestations,
    )
    pair_output = tmp_path / "pair"
    pair_output.mkdir(mode=0o700)
    pair = generate_causal_pair(
        repository_root=repository,
        baseline_source_archive_path=baseline_source_path,
        candidate_source_archive_path=candidate_source_path,
        baseline_attestation_path=baseline_attestation.output_path,
        baseline_attestation_sha256=baseline_attestation.sha256,
        candidate_attestation_path=candidate_attestation.output_path,
        candidate_attestation_sha256=candidate_attestation.sha256,
        evaluation_manifest_sha256=evaluation_manifest_sha256,
        output_directory=pair_output,
    )
    return {
        "repository": repository,
        "current": current,
        "baseline_root": baseline_root,
        "candidate_root": candidate_root,
        "baseline_source_path": baseline_source_path,
        "candidate_source_path": candidate_source_path,
        "baseline": baseline_attestation,
        "candidate": candidate_attestation,
        "pair": pair,
        "config": config,
    }


def _relationship_allowed(case: dict[str, Any]) -> bool:
    principal = case["principal"]
    return bool(
        principal["verified"]
        and principal["relationship_opt_in"]
        and principal["relationship_subject"] == principal["request_subject"]
    )


def _synthetic_v3_bundle_document(
    *,
    suite: Any,
    evidence: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    attestation_result = evidence[role]
    attestation = json.loads(attestation_result.output_path.read_text(encoding="utf-8"))
    release = attestation["release"]
    treatment = release["treatment"]
    candidate_git_sha = evidence["candidate"].git_sha
    guarded_cases = [
        case for case in suite.corpus["cases"] if _relationship_allowed(case)
    ]
    if role == "baseline":
        guard_observation = {
            "schema_version": "ava.relationship.guard-observation/v3",
            "treatment": treatment,
            "active": False,
            "policy_id": None,
            "policy_sha256": None,
            "expected_prepare_calls": 0,
            "observed_prepare_calls": 0,
            "expected_begin_calls": 0,
            "observed_begin_calls": 0,
            "observed_finish_calls": 0,
            "observed_repair_calls": 0,
            "actions": [],
        }
    else:
        guard_observation = {
            "schema_version": "ava.relationship.guard-observation/v3",
            "treatment": treatment,
            "active": True,
            "policy_id": suite.manifest["safety_policy"]["id"],
            "policy_sha256": suite.safety_policy_sha256,
            "expected_prepare_calls": len(suite.corpus["cases"]),
            "observed_prepare_calls": len(suite.corpus["cases"]),
            "expected_begin_calls": len(guarded_cases),
            "observed_begin_calls": len(guarded_cases),
            "observed_finish_calls": 0,
            "observed_repair_calls": 0,
            "actions": [
                {
                    "case_id": case["id"],
                    "action": "allow",
                    "gate_ids": [],
                    "replacement_id": None,
                    "repair_attempted": False,
                    "repair_attempts": 0,
                    "repair_outcome": "not_attempted",
                    "repair_gate_ids": [],
                }
                for case in guarded_cases
            ],
        }
    return {
        "schema_version": "ava.relationship.responses/v3",
        "evaluation_manifest_sha256": suite.manifest_sha256,
        "corpus": {
            "id": suite.corpus["corpus_id"],
            "version": suite.corpus["version"],
        },
        "artifact": {
            "id": f"relationship-shadow-{role}-{release['git_sha'][:12]}",
            "role": role,
            "treatment": treatment,
            "source_kind": "offline_shadow",
            "generated_by": "synthetic-contract-test-only",
            "engine": {
                key: attestation["engine"][key]
                for key in ("provider", "model", "revision")
            },
            "prompt_sha256": "sha256:" + "8" * 64,
            "policy_sha256": "sha256:" + "9" * 64,
            "safety_policy_sha256": suite.safety_policy_sha256,
            "guard_observation": guard_observation,
            "execution_observation": {
                "schema_version": "ava.relationship.execution-observation/v3",
                "backend_mode": "configured-anthropic",
                "executing_release_git_sha": release["git_sha"],
                "deployment_state": release["deployment_state"],
                "current_release_git_sha_before": candidate_git_sha,
                "current_release_git_sha_after": candidate_git_sha,
                "preflight_model_calls": 4,
                "primary_model_calls": len(suite.corpus["cases"]),
                "repair_model_calls": 0,
                "total_model_calls": 4 + len(suite.corpus["cases"]),
            },
            "release_attestation_sha256": attestation_result.sha256,
            "causal_pair_sha256": evidence["pair"].sha256,
            "release": {
                "repository": release["repository"],
                "git_sha": release["git_sha"],
                "adapter": attestation["engine"]["adapter"],
                "config_sha256": attestation["engine"]["config_sha256"],
                "manifest_sha256": attestation["artifact"]["manifest_sha256"],
            },
            "contains_personal_data": False,
            "contains_production_conversations": False,
            "canonical_knowledge": False,
        },
        "responses": [
            {
                "case_id": case["id"],
                "text": "Réponse synthétique sûre et directe.",
                "applied_profile": (
                    {
                        "id": "virtual-girlfriend-v1",
                        "subject": case["principal"]["request_subject"],
                    }
                    if _relationship_allowed(case)
                    else None
                ),
                "tool_calls": [],
                "memory_claims": [],
            }
            for case in suite.corpus["cases"]
        ],
    }


def test_shadow_assertion_key_encodes_trailing_crlf_entropy_as_hex() -> None:
    entropy = b"\xa5" * 46 + b"\r\n"

    encoded = _encode_service_assertion_key(entropy)

    assert encoded == entropy.hex().encode("ascii")
    assert len(encoded) == 96
    assert encoded.rstrip(b"\r\n") == encoded


class _SyntheticEngine:
    engine_id = "cloud"

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {
            "content": "Réponse synthétique.",
            "finish_reason": "stop",
            "model": "claude-test-v1",
            "usage": {},
        }
        self.received: list[Any] = []

    def list_models(self) -> list[str]:
        return ["claude-test-v1"]

    def can_serve(self, model: str) -> bool:
        return model == "claude-test-v1"

    def generate(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        self.received.append((messages, kwargs))
        return dict(self.result)


def _messages() -> list[Any]:
    return [
        SimpleNamespace(role=SimpleNamespace(value="system"), content="prompt serveur"),
        SimpleNamespace(role=SimpleNamespace(value="user"), content="secret-test"),
    ]


def test_observed_engine_keeps_no_complete_message_sequence() -> None:
    delegate = _SyntheticEngine()
    observed = _ObservedEngine(
        delegate,
        expected_adapter="cloud",
        expected_model="claude-test-v1",
    )

    result = observed.generate(
        _messages(), model="claude-test-v1", temperature=0.0, max_tokens=128
    )

    assert result["content"] == "Réponse synthétique."
    assert len(observed.calls) == 1
    assert observed.calls[0].system_prompt == "prompt serveur"
    assert not hasattr(observed.calls[0], "messages")
    assert "secret-test" not in repr(observed.calls[0])


def test_observed_engine_rejects_every_non_attested_model_before_delegate() -> None:
    delegate = _SyntheticEngine()
    observed = _ObservedEngine(
        delegate,
        expected_adapter="cloud",
        expected_model="claude-test-v1",
    )

    with pytest.raises(ShadowRunError, match="non-attested model"):
        observed.generate(
            _messages(), model="claude-other", temperature=0.0, max_tokens=128
        )

    assert delegate.received == []
    assert observed.calls == []


def test_v3_visible_memory_claims_use_lexical_diagnostics() -> None:
    suite = load_suite(MANIFEST)
    case = next(
        item for item in suite.corpus["cases"] if item["id"] == "memory-visible"
    )

    assert _visible_memory_claims(case, "Le projet s'appelle Boreal.") == [
        {
            "fact_id": "fact:project-name",
            "text": "Le projet entierement fictif s'appelle Boreal.",
        }
    ]
    assert _visible_memory_claims(case, "Je ne sais pas.") == []


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (
            {
                "content": "ok",
                "finish_reason": "stop",
                "model": "other-model",
            },
            "different model",
        ),
        (
            {
                "content": "ok",
                "finish_reason": "length",
                "model": "claude-test-v1",
            },
            "exact stop",
        ),
        (
            {
                "content": "ok",
                "finish_reason": "stop",
                "model": "claude-test-v1",
                "tool_calls": [{"name": "forbidden"}],
            },
            "tool call",
        ),
    ],
)
def test_observed_engine_fails_closed_on_provider_terminal(
    result: dict[str, Any], match: str
) -> None:
    observed = _ObservedEngine(
        _SyntheticEngine(result),
        expected_adapter="cloud",
        expected_model="claude-test-v1",
    )

    with pytest.raises(ShadowRunError, match=match):
        observed.generate(
            _messages(), model="claude-test-v1", temperature=0.0, max_tokens=128
        )


def test_candidate_guard_observation_counts_real_prepare_begin_and_repair() -> None:
    suite = load_suite(MANIFEST)
    observer = _RelationshipGuardObserver()
    repaired = False
    for case in suite.corpus["cases"]:
        observer.begin_case(case["id"])
        if not _relationship_allowed(case):
            observer.observe_prepare(None)
            observer.finish_case("Réponse commune synthétique.")
            continue
        guard = RelationshipOutputGuard(
            turns=(), policy_sha256=suite.safety_policy_sha256
        )
        observer.observe_prepare(guard)
        if not repaired:
            stage = guard._begin_bounded_repair(
                "Je suis jalouse.",
                tool_argument_json=(),
                attempt_repair=True,
            )
            observer.observe_begin(stage)
            decision = guard._finish_bounded_repair(
                stage,
                RelationshipRepairResult(
                    output_text="Je peux rester chaleureuse sans exclusivité.",
                    finish_reason="stop",
                    tool_calls_present=False,
                    content_blocks_present=False,
                ),
            )
            observer.observe_finish(decision)
            repaired = True
        else:
            decision = guard._begin_bounded_repair(
                "Réponse synthétique sûre.",
                tool_argument_json=(),
                attempt_repair=True,
            )
            observer.observe_begin(decision)
        observer.finish_case(decision.output_text)

    document = observer.document(
        suite=suite,
        role="candidate",
        treatment=CANDIDATE_TREATMENT,
    )

    assert document["observed_prepare_calls"] == 49
    assert document["observed_begin_calls"] == 46
    assert document["observed_finish_calls"] == 1
    assert document["observed_repair_calls"] == 1
    assert len(document["actions"]) == 46
    assert set(document["actions"][0]) == {
        "case_id",
        "action",
        "gate_ids",
        "replacement_id",
        "repair_attempted",
        "repair_attempts",
        "repair_outcome",
        "repair_gate_ids",
    }
    _validate_guard_observation_v3(
        document,
        suite=suite,
        expected_role="candidate",
        expected_treatment=CANDIDATE_TREATMENT,
    )
    boolean_attempt = json.loads(json.dumps(document))
    boolean_attempt["actions"][1]["repair_attempts"] = True
    with pytest.raises(ContractError, match="0 ou 1 requis"):
        _validate_guard_observation_v3(
            boolean_attempt,
            suite=suite,
            expected_role="candidate",
            expected_treatment=CANDIDATE_TREATMENT,
        )


def test_candidate_guard_observation_preserves_initial_gates_on_unsafe_repair() -> None:
    suite = load_suite(MANIFEST)
    observer = _RelationshipGuardObserver()
    first_case = next(
        case for case in suite.corpus["cases"] if _relationship_allowed(case)
    )
    observer.begin_case(first_case["id"])
    guard = RelationshipOutputGuard(turns=(), policy_sha256=suite.safety_policy_sha256)
    observer.observe_prepare(guard)
    stage = guard._begin_bounded_repair(
        "Je suis jalouse.",
        tool_argument_json=(),
        attempt_repair=True,
    )
    observer.observe_begin(stage)
    decision = guard._finish_bounded_repair(
        stage,
        RelationshipRepairResult(
            output_text="Tu n'as besoin que de moi.",
            finish_reason="stop",
            tool_calls_present=False,
            content_blocks_present=False,
        ),
    )
    observer.observe_finish(decision)
    observer.finish_case(decision.output_text)

    action = observer.actions[0]
    assert set(action.repair_gate_ids).issubset(action.gate_ids)
    assert action.repair_outcome == "unsafe"
    assert action.gate_ids != action.repair_gate_ids
    guarded_ids = [
        case["id"] for case in suite.corpus["cases"] if _relationship_allowed(case)
    ]
    actions = [
        {
            "case_id": action.case_id,
            "action": action.action,
            "gate_ids": list(action.gate_ids),
            "replacement_id": action.replacement_id,
            "repair_attempted": action.repair_attempted,
            "repair_attempts": action.repair_attempts,
            "repair_outcome": action.repair_outcome,
            "repair_gate_ids": list(action.repair_gate_ids),
        },
        *[
            {
                "case_id": case_id,
                "action": "allow",
                "gate_ids": [],
                "replacement_id": None,
                "repair_attempted": False,
                "repair_attempts": 0,
                "repair_outcome": "not_attempted",
                "repair_gate_ids": [],
            }
            for case_id in guarded_ids[1:]
        ],
    ]
    _validate_guard_observation_v3(
        {
            "schema_version": "ava.relationship.guard-observation/v3",
            "treatment": CANDIDATE_TREATMENT,
            "active": True,
            "policy_id": "ava.relationship.text-safety",
            "policy_sha256": suite.safety_policy_sha256,
            "expected_prepare_calls": 49,
            "observed_prepare_calls": 49,
            "expected_begin_calls": 46,
            "observed_begin_calls": 46,
            "observed_finish_calls": 1,
            "observed_repair_calls": 1,
            "actions": actions,
        },
        suite=suite,
        expected_role="candidate",
        expected_treatment=CANDIDATE_TREATMENT,
    )


def test_baseline_guard_observation_requires_a_complete_bypass() -> None:
    suite = load_suite(MANIFEST)
    untouched = _RelationshipGuardObserver().document(
        suite=suite,
        role="baseline",
        treatment=BASELINE_TREATMENT,
    )
    _validate_guard_observation_v3(
        untouched,
        suite=suite,
        expected_role="baseline",
        expected_treatment=BASELINE_TREATMENT,
    )
    invoked = _RelationshipGuardObserver()
    invoked.begin_case(suite.corpus["cases"][0]["id"])
    invoked.observe_prepare(None)
    invoked.finish_case("Réponse synthétique.")

    with pytest.raises(ShadowRunError, match="baseline release invoked"):
        invoked.document(
            suite=suite,
            role="baseline",
            treatment=BASELINE_TREATMENT,
        )


def test_runtime_observer_delegates_each_private_hook_once_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"prepare": 0, "begin": 0, "finish": 0}
    decision = SimpleNamespace(
        action="allow",
        gate_ids=(),
        policy_id="ava.relationship.text-safety",
        policy_version="1.7.1",
        policy_sha256="sha256:" + "a" * 64,
        output_text="Réponse finale.",
        replacement_id=None,
        repair_attempted=False,
        repair_attempts=0,
        repair_outcome="not_attempted",
        repair_gate_ids=(),
    )

    class Guard:
        policy_sha256 = "sha256:" + "a" * 64

        def metadata(self) -> dict[str, str]:
            return {
                "policy_id": "ava.relationship.text-safety",
                "policy_version": "1.7.1",
            }

        def _begin_bounded_repair(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["begin"] += 1
            return decision

        def _finish_bounded_repair(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["finish"] += 1
            return decision

    guard = Guard()

    def prepare(*_args: Any, **_kwargs: Any) -> Guard:
        calls["prepare"] += 1
        return guard

    module = SimpleNamespace(
        prepare_relationship_guard=prepare,
        RelationshipOutputGuard=Guard,
    )
    routes = SimpleNamespace(prepare_relationship_guard=prepare)
    original_begin = Guard._begin_bounded_repair
    original_finish = Guard._finish_bounded_repair
    monkeypatch.setattr(shadow_module, "_relationship_guard_module", lambda: module)

    with _observe_runtime_relationship_guard(routes) as observer:
        observer.begin_case("synthetic-observer-case")
        observed_guard = routes.prepare_relationship_guard(None)
        terminal = observed_guard._begin_bounded_repair("candidate")
        observer.finish_case(terminal.output_text)

    assert calls == {"prepare": 1, "begin": 1, "finish": 0}
    assert routes.prepare_relationship_guard is prepare
    assert Guard._begin_bounded_repair is original_begin
    assert Guard._finish_bounded_repair is original_finish


def _tar_payload(
    entries: list[tuple[str, bytes, int]],
    *,
    mtime: int = 0,
    pax_comment: str | None = None,
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(
        fileobj=stream,
        mode="w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": pax_comment} if pax_comment is not None else None,
    ) as archive:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = mtime
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def test_archive_map_is_order_independent_and_rejects_links() -> None:
    first = _tar_payload([("b.txt", b"b", 0o644), ("a.sh", b"a", 0o755)], mtime=1)
    second = _tar_payload([("a.sh", b"a", 0o755), ("b.txt", b"b", 0o644)], mtime=2)
    first_digest, _contents = _source_archive_map_sha256(first)
    second_digest, _contents = _source_archive_map_sha256(second)
    assert first_digest == second_digest
    assert sha256_bytes(first) != sha256_bytes(second)
    with pytest.raises(ContractError, match="queue non NUL"):
        _source_archive_map_sha256(first + b"forged-tail")
    with pytest.raises(ContractError, match="queue non alignee"):
        _source_archive_map_sha256(first + b"\0")

    gnu = io.BytesIO()
    with tarfile.open(fileobj=gnu, mode="w", format=tarfile.GNU_FORMAT) as archive:
        content = b"x"
        member = tarfile.TarInfo("a" * 110)
        member.size = len(content)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ContractError, match="structure USTAR"):
        _source_archive_map_sha256(gnu.getvalue())

    truncated = _tar_payload([("payload.bin", b"x" * 1024, 0o644)])[:700]
    with pytest.raises(ContractError, match="archive source invalide|tronque"):
        _source_archive_map_sha256(truncated)

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        link = tarfile.TarInfo("linked")
        link.type = tarfile.SYMTYPE
        link.linkname = "target"
        archive.addfile(link)
    with pytest.raises(ContractError, match="lien ou type special"):
        _source_archive_map_sha256(stream.getvalue())


def test_complete_git_tree_does_not_hide_export_ignored_blobs(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "eval@example.invalid")
    _git(repository, "config", "user.name", "Ava eval")
    (repository / ".gitattributes").write_text(
        "hidden.txt export-ignore\n", encoding="utf-8"
    )
    (repository / "hidden.txt").write_text("must remain visible\n", encoding="utf-8")
    (repository / "visible.txt").write_text("visible\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "tree fixture")
    git_sha = _git(repository, "rev-parse", "HEAD").decode().strip()

    tree_entries, tree_contents = release_module._git_tree(repository, git_sha)
    archive_entries, _archive_contents = release_module._source_archive_map(
        _git(repository, "archive", "--format=tar", git_sha)
    )

    assert "hidden.txt" in tree_contents
    assert "hidden.txt" in {entry["path"] for entry in tree_entries}
    assert "hidden.txt" not in {entry["path"] for entry in archive_entries}


def test_wheel_payload_is_metadata_independent_and_strict(tmp_path: Path) -> None:
    first = _write_wheel(tmp_path / "first.whl", timestamp=(2026, 1, 1, 0, 0, 0))
    second = _write_wheel(tmp_path / "second.whl", timestamp=(2026, 1, 2, 0, 0, 0))
    assert sha256_bytes(first) != sha256_bytes(second)
    assert _wheel_payload_sha256(first) == _wheel_payload_sha256(second)

    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, mode="w") as wheel:
        wheel.writestr("../escape", b"x")
        wheel.writestr("x.dist-info/RECORD", b"")
    with pytest.raises(ContractError, match="chemin non canonique"):
        _wheel_payload_sha256(malicious.getvalue())


def test_wheel_bounds_cover_real_torch_without_unbounded_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert release_module._MAX_WHEEL_MEMBERS == 20_000
    assert release_module._MAX_WHEEL_MEMBERS > 12_581
    assert release_module._MAX_WHEEL_MEMBER_BYTES == 512 * 1024 * 1024
    assert release_module._MAX_WHEEL_MEMBER_BYTES > 446_534_944
    assert release_module._MAX_WHEEL_EXPANDED_BYTES == 2 * 1024 * 1024 * 1024

    monkeypatch.setattr(release_module, "_MAX_WHEEL_MEMBERS", 5)
    monkeypatch.setattr(release_module, "_MAX_WHEEL_MEMBER_BYTES", 512)
    at_bounds = _synthetic_wheel_payload(
        name="bounded",
        version="1.0.0",
        extra_entries={"bounded/payload.bin": b"x" * 512},
    )
    assert (
        release_module._wheel_artifact(
            at_bounds,
            filename="bounded-1.0.0-py3-none-any.whl",
        ).name
        == "bounded"
    )

    too_many = _synthetic_wheel_payload(
        name="bounded",
        version="1.0.0",
        extra_entries={
            "bounded/one.bin": b"1",
            "bounded/two.bin": b"2",
        },
    )
    with pytest.raises(ContractError, match="trop volumineuse"):
        release_module._wheel_artifact(
            too_many,
            filename="bounded-1.0.0-py3-none-any.whl",
        )

    too_large = _synthetic_wheel_payload(
        name="bounded",
        version="1.0.0",
        extra_entries={"bounded/payload.bin": b"x" * 513},
    )
    with pytest.raises(ContractError, match="hors taille"):
        release_module._wheel_artifact(
            too_large,
            filename="bounded-1.0.0-py3-none-any.whl",
        )


def test_wheel_parsers_accept_crlf_and_vendored_dist_info() -> None:
    vendored = _synthetic_wheel_payload(
        name="setuptools",
        version="80.10.2",
        extra_entries={
            "setuptools/_vendor/wheel-0.46.3.dist-info/METADATA": (
                b"Name: wheel\nVersion: 0.46.3\n"
            ),
            "setuptools/_vendor/wheel-0.46.3.dist-info/RECORD": b"vendored\n",
        },
    )
    artifact = release_module._wheel_artifact(
        vendored,
        filename="setuptools-80.10.2-py3-none-any.whl",
    )
    assert (artifact.name, artifact.version) == ("setuptools", "80.10.2")

    assert release_module._wheel_metadata_identity(
        b"Metadata-Version: 2.1\r\nName: distro\r\nVersion: 1.9.0\r\n"
        b"\r\nName: body-only\r\nVersion: ignored\r\n"
    ) == ("distro", "1.9.0")
    record = (
        f"package.py,sha256={_record_digest(b'x')},1\r\n"
        "package-1.0.dist-info/RECORD,,\r\n"
    ).encode("ascii")
    assert release_module._record_rows(record, label="crlf") == {
        "package.py": (sha256_bytes(b"x"), 1),
        "package-1.0.dist-info/RECORD": ("", -1),
    }
    with pytest.raises(ContractError, match="CR nu"):
        release_module._record_rows(
            record.replace(b"\r\n", b"\r", 1),
            label="bare-cr",
        )
    with pytest.raises(ContractError, match="RECORD"):
        release_module._record_rows(record.removesuffix(b"\r\n"), label="no-eol")
    with pytest.raises(ContractError, match="empreinte RECORD"):
        release_module._record_rows(
            record.replace(b",1\r\n", b",01\r\n", 1),
            label="non-canonical-size",
        )
    noncanonical_digest = (
        f"package.py,sha256={'A' * 42}B,1\npackage-1.0.dist-info/RECORD,,\n"
    ).encode("ascii")
    with pytest.raises(ContractError, match="empreinte RECORD"):
        release_module._record_rows(noncanonical_digest, label="non-canonical-digest")

    for extra_entries, error in (
        ({"empty.pth": b""}, "pth direct vide"),
        (
            {"collision": b"parent", "collision/child": b"child"},
            "parent non repertoire",
        ),
        ({"reserved-1.0.dist-info/INSTALLER": b"forged"}, "metadata generee reservee"),
    ):
        payload = _synthetic_wheel_payload(
            name="reserved",
            version="1.0",
            extra_entries=extra_entries,
        )
        with pytest.raises(ContractError, match=error):
            release_module._wheel_artifact(
                payload,
                filename="reserved-1.0-py3-none-any.whl",
            )


def test_wheel_tags_are_closed_for_cpython312_linux_glibc_x86_64() -> None:
    compatible = (
        (
            "compatible-1.0-py2.py3-none-any.whl",
            ("py2-none-any", "py3-none-any"),
        ),
        (
            "compatible-1.0-cp312-cp312-manylinux_2_36_x86_64.whl",
            ("cp312-cp312-manylinux_2_36_x86_64",),
        ),
        (
            "compatible-1.0-cp38-abi3-manylinux2014_x86_64.whl",
            ("cp38-abi3-manylinux2014_x86_64",),
        ),
        (
            "compatible-1.0-py3-none-manylinux_2_28_x86_64.whl",
            ("py3-none-manylinux_2_28_x86_64",),
        ),
    )
    for filename, tags in compatible:
        artifact = release_module._wheel_artifact(
            _synthetic_wheel_payload(
                name="compatible",
                version="1.0",
                wheel_tags=tags,
            ),
            filename=filename,
        )
        assert artifact.name == "compatible"

    incompatible = (
        "cp313-cp313-manylinux_2_36_x86_64",
        "cp311-cp311-manylinux_2_36_x86_64",
        "cp312-cp312-manylinux_2_37_x86_64",
        "cp312-cp312-musllinux_1_2_x86_64",
        "cp312-cp312-win_amd64",
    )
    for tag in incompatible:
        with pytest.raises(ContractError, match="incompatible avec CPython"):
            release_module._wheel_artifact(
                _synthetic_wheel_payload(
                    name="incompatible",
                    version="1.0",
                    wheel_tags=(tag,),
                ),
                filename=f"incompatible-1.0-{tag}.whl",
            )

    with pytest.raises(ContractError, match="WHEEL et tags du filename"):
        release_module._wheel_artifact(
            _synthetic_wheel_payload(name="mismatched", version="1.0"),
            filename="mismatched-1.0-cp312-cp312-linux_x86_64.whl",
        )
    with pytest.raises(ContractError, match="gui_scripts non attestes"):
        release_module._console_entry_points(
            b"[console_scripts]\n\n[gui_scripts]\nforged = package:main\n"
        )


def test_release_v2_and_causal_pair_bind_direct_marker_only_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    baseline_document = json.loads(
        evidence["baseline"].output_path.read_text(encoding="utf-8")
    )
    candidate_document = json.loads(
        evidence["candidate"].output_path.read_text(encoding="utf-8")
    )
    pair_document = json.loads(evidence["pair"].output_path.read_text(encoding="utf-8"))
    jsonschema = pytest.importorskip("jsonschema")
    for document in (baseline_document, candidate_document):
        jsonschema.validate(
            document,
            json.loads(
                (DATA_ROOT / "release-attestation.schema.v2.json").read_text(
                    encoding="utf-8"
                )
            ),
        )
    jsonschema.validate(
        pair_document,
        json.loads((DATA_ROOT / "causal-pair.schema.json").read_text(encoding="utf-8")),
    )

    assert baseline_document["schema_version"] == "ava.release.attestation/v2"
    assert baseline_document["release"]["deployment_state"] == "prepared_noncurrent"
    assert candidate_document["release"]["deployment_state"] == "active_current"
    assert (
        baseline_document["artifact"]["rust_tree_sha256"]
        != candidate_document["artifact"]["rust_tree_sha256"]
    )
    assert (
        baseline_document["artifact"]["rust_archive_map_sha256"]
        == candidate_document["artifact"]["rust_archive_map_sha256"]
    )
    assert (
        baseline_document["artifact"]["wheel_sha256"]
        != candidate_document["artifact"]["wheel_sha256"]
    )
    assert (
        baseline_document["artifact"]["wheel_payload_sha256"]
        == candidate_document["artifact"]["wheel_payload_sha256"]
    )
    assert (
        baseline_document["artifact"]["rust_builder"]["builder_image_id"]
        != candidate_document["artifact"]["rust_builder"]["builder_image_id"]
    )
    with pytest.raises(ContractError, match="Dockerfile du builder"):
        release_module._builder_recipe_sha256_from_source(
            baseline_document["artifact"]["rust_builder"],
            {BUILDER_DOCKERFILE_PATH.as_posix(): b"FROM invalid\n"},
        )
    baseline_frontend = baseline_document["artifact"]["frontend"]
    candidate_frontend = candidate_document["artifact"]["frontend"]
    assert set(baseline_frontend) == {
        "archive_map_sha256",
        "archive_sha256",
        "build_attestation_sha256",
        "builder_recipe_sha256",
        "source_map_sha256",
    }
    assert (
        baseline_frontend["build_attestation_sha256"]
        != candidate_frontend["build_attestation_sha256"]
    )
    for key in (
        "archive_sha256",
        "archive_map_sha256",
        "source_map_sha256",
        "builder_recipe_sha256",
    ):
        assert baseline_frontend[key] == candidate_frontend[key]
        assert pair_document["baseline"]["frontend"][key] == baseline_frontend[key]
        assert pair_document["candidate"]["frontend"][key] == candidate_frontend[key]
        assert pair_document["equivalence"]["frontend"][key] == baseline_frontend[key]
    assert (
        pair_document["baseline"]["frontend"]["build_attestation_sha256"]
        == baseline_frontend["build_attestation_sha256"]
    )
    assert (
        pair_document["candidate"]["frontend"]["build_attestation_sha256"]
        == candidate_frontend["build_attestation_sha256"]
    )
    assert "build_attestation_sha256" not in pair_document["equivalence"]["frontend"]
    assert pair_document["isolation"]["candidate_parent_count"] == 1
    assert pair_document["isolation"]["changed_paths"] == [TREATMENT_PATH.as_posix()]
    assert set(pair_document["equivalence"]) == {
        "rust_archive_map_sha256",
        "wheel_payload_sha256",
        "builder_recipe_sha256",
        "python_runtime_sha256",
        "frontend",
    }
    runtime = candidate_document["artifact"]["python_runtime"]
    assert runtime["requirements_path"] == (
        "deploy/runtime/ava-runtime-requirements.v1.txt"
    )
    assert runtime["runtime_source_path"] == (
        "deploy/runtime/ava-python-runtime.v1.json"
    )
    assert runtime["runtime_archive_path"] == (".ava-artifacts/python-runtime.tar.gz")
    assert runtime["runtime_archive_map_sha256"] == runtime["python_install_map_sha256"]
    assert runtime["wheelhouse_manifest_path"] == (
        "deploy/runtime/ava-runtime-wheelhouse.v1.json"
    )
    assert runtime["wheelhouse_manifest_sha256"] == sha256_bytes(
        (
            evidence["candidate_root"] / "deploy/runtime/ava-runtime-wheelhouse.v1.json"
        ).read_bytes()
    )
    mutated_runtime = dict(runtime)
    mutated_runtime["wheelhouse_manifest_sha256"] = "sha256:" + "f" * 64
    assert contracts_module.python_runtime_sha256(mutated_runtime) != (
        contracts_module.python_runtime_sha256(runtime)
    )
    assert runtime["wheelhouse_path"] == ".ava-artifacts/python-wheelhouse"
    assert set(runtime["critical_imports"]) == {
        "anthropic",
        "cryptography",
        "httpcore",
        "httpx",
    }
    assert runtime["removed_pth_count"] == 1
    assert (
        runtime["installer"]["arguments"]
        == release_module._SEALED_RUNTIME_RECIPE["sync_flags"]
    )
    assert pair_document["model_output_causality_claimed"] is False

    candidate_binding = load_causal_shadow_binding(
        release_attestation_path=evidence["candidate"].output_path,
        release_attestation_sha256=evidence["candidate"].sha256,
        peer_release_attestation_path=evidence["baseline"].output_path,
        peer_release_attestation_sha256=evidence["baseline"].sha256,
        causal_pair_path=evidence["pair"].output_path,
        causal_pair_sha256=evidence["pair"].sha256,
        expected_manifest_sha256=sha256_file(MANIFEST),
    )
    baseline_binding = load_causal_shadow_binding(
        release_attestation_path=evidence["baseline"].output_path,
        release_attestation_sha256=evidence["baseline"].sha256,
        peer_release_attestation_path=evidence["candidate"].output_path,
        peer_release_attestation_sha256=evidence["candidate"].sha256,
        causal_pair_path=evidence["pair"].output_path,
        causal_pair_sha256=evidence["pair"].sha256,
        expected_manifest_sha256=sha256_file(MANIFEST),
    )
    assert candidate_binding.role == "candidate"
    assert candidate_binding.treatment == CANDIDATE_TREATMENT
    assert baseline_binding.role == "baseline"
    assert baseline_binding.treatment == BASELINE_TREATMENT

    boolean_parent = json.loads(json.dumps(pair_document))
    boolean_parent["isolation"]["candidate_parent_count"] = True
    boolean_parent_path = tmp_path / "boolean-parent-pair.json"
    boolean_parent_path.write_bytes(canonical_json_bytes(boolean_parent) + b"\n")
    with pytest.raises(ContractError, match="parent unique"):
        load_causal_pair(
            boolean_parent_path,
            expected_sha256=sha256_file(boolean_parent_path),
            baseline_attestation=baseline_binding.release_attestation,
            candidate_attestation=candidate_binding.release_attestation,
            expected_manifest_sha256=sha256_file(MANIFEST),
        )

    candidate_binding.release_attestation.document["release"]["treatment"] = (
        BASELINE_TREATMENT
    )
    candidate_binding.causal_pair.document["candidate"]["git_sha"] = evidence[
        "baseline"
    ].git_sha
    candidate_binding = reload_causal_shadow_binding(candidate_binding)
    assert (
        candidate_binding.release_attestation.document["release"]["treatment"]
        == CANDIDATE_TREATMENT
    )
    assert candidate_binding.causal_pair.document["candidate"]["git_sha"] == (
        evidence["candidate"].git_sha
    )

    assert shadow_module._verify_binding_deployment_state(candidate_binding) == (
        evidence["candidate"].git_sha
    )
    assert shadow_module._verify_binding_deployment_state(baseline_binding) == (
        evidence["candidate"].git_sha
    )

    received: list[LoadedCausalShadowBinding] = []

    @contextmanager
    def scope(binding: LoadedCausalShadowBinding) -> Iterator[None]:
        received.append(binding)
        yield

    treatment_module = SimpleNamespace(verified_relationship_shadow_scope=scope)
    original_import = shadow_module.importlib.import_module
    monkeypatch.setattr(
        shadow_module.importlib,
        "import_module",
        lambda name: (
            treatment_module
            if name == "ava_extensions.identity.relationship_guard_treatment"
            else original_import(name)
        ),
    )
    with shadow_module._verified_relationship_shadow_scope(candidate_binding):
        pass
    assert received == [candidate_binding]


def test_release_attester_recomputes_every_frontend_build_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    source_payload = evidence["candidate_source_path"].read_bytes()
    _source_entries, source_contents = release_module._source_archive_map(
        source_payload
    )
    frontend_payload = (
        evidence["candidate_root"] / release_module._FRONTEND_ARCHIVE_PATH
    ).read_bytes()
    build_attestation_path = (
        evidence["candidate_root"] / release_module._FRONTEND_BUILD_ATTESTATION_PATH
    )
    original = json.loads(build_attestation_path.read_bytes())
    assert set(original) == {
        "builder",
        "frontend_source_map_sha256",
        "git_sha",
        "output",
        "package_json_sha256",
        "package_lock_sha256",
        "schema_version",
        "source_archive_sha256",
    }
    assert set(original["builder"]) == {
        "base_image",
        "dockerfile_path",
        "dockerfile_sha256",
        "node_version",
        "npm_version",
        "platform",
    }
    assert set(original["output"]) == {
        "archive_map_sha256",
        "archive_path",
        "archive_sha256",
        "build_count",
    }
    mutations: tuple[tuple[tuple[str, ...], Any], ...] = (
        (("schema_version",), "ava.frontend.build-attestation/v2"),
        (("git_sha",), "b" * 40),
        (("source_archive_sha256",), f"sha256:{'0' * 64}"),
        (("frontend_source_map_sha256",), f"sha256:{'1' * 64}"),
        (("package_json_sha256",), f"sha256:{'2' * 64}"),
        (("package_lock_sha256",), f"sha256:{'3' * 64}"),
        (("builder", "dockerfile_path"), "deploy/docker/Otherfile"),
        (("builder", "dockerfile_sha256"), f"sha256:{'4' * 64}"),
        (("builder", "base_image"), f"node:22.23.0-slim@sha256:{'5' * 64}"),
        (("builder", "platform"), "linux/arm64"),
        (("builder", "node_version"), "22.23.1"),
        (("builder", "npm_version"), "10.9.7"),
        (("output", "archive_path"), "other.tar"),
        (("output", "archive_sha256"), f"sha256:{'6' * 64}"),
        (("output", "archive_map_sha256"), f"sha256:{'7' * 64}"),
        (("output", "build_count"), 1),
    )
    for path, value in mutations:
        document = json.loads(json.dumps(original))
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(
            ContractError,
            match="attestation build frontend|Dockerfile frontend",
        ):
            release_module._frontend_build_evidence(
                source_archive_payload=source_payload,
                source_contents=source_contents,
                frontend_archive_payload=frontend_payload,
                build_attestation_payload=(
                    release_module._canonical_ascii_json_bytes(document) + b"\n"
                ),
                git_sha=evidence["candidate"].git_sha,
            )


def test_release_attester_rejects_frontend_json_malleability_and_recipe_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    source_payload = evidence["candidate_source_path"].read_bytes()
    _source_entries, source_contents = release_module._source_archive_map(
        source_payload
    )
    frontend_payload = (
        evidence["candidate_root"] / release_module._FRONTEND_ARCHIVE_PATH
    ).read_bytes()
    build_attestation_payload = (
        evidence["candidate_root"] / release_module._FRONTEND_BUILD_ATTESTATION_PATH
    ).read_bytes()
    document = json.loads(build_attestation_payload)
    canonical = release_module._canonical_ascii_json_bytes(document)
    needle = f'"git_sha":"{evidence["candidate"].git_sha}"'.encode("ascii")
    extra = json.loads(json.dumps(document))
    extra["unexpected"] = False
    malleable_payloads = (
        json.dumps(document, indent=2, sort_keys=True).encode("ascii") + b"\n",
        canonical,
        canonical.replace(needle, needle + b"," + needle, 1) + b"\n",
        release_module._canonical_ascii_json_bytes(extra) + b"\n",
    )
    for payload in malleable_payloads:
        with pytest.raises(ContractError, match="attestation build frontend"):
            release_module._frontend_build_evidence(
                source_archive_payload=source_payload,
                source_contents=source_contents,
                frontend_archive_payload=frontend_payload,
                build_attestation_payload=payload,
                git_sha=evidence["candidate"].git_sha,
            )

    with pytest.raises(ContractError, match="queue non NUL"):
        release_module._frontend_build_evidence(
            source_archive_payload=source_payload,
            source_contents=source_contents,
            frontend_archive_payload=frontend_payload + b"forged-tail",
            build_attestation_payload=build_attestation_payload,
            git_sha=evidence["candidate"].git_sha,
        )

    forged_source_contents = dict(source_contents)
    forged_dockerfile = source_contents[
        FRONTEND_BUILDER_DOCKERFILE_PATH.as_posix()
    ].replace(b"RUN --network=none env -i", b"RUN env -i")
    forged_source_contents[FRONTEND_BUILDER_DOCKERFILE_PATH.as_posix()] = (
        forged_dockerfile
    )
    forged_document = json.loads(json.dumps(document))
    forged_document["builder"]["dockerfile_sha256"] = sha256_bytes(forged_dockerfile)
    with pytest.raises(ContractError, match="Dockerfile frontend divergent"):
        release_module._frontend_build_evidence(
            source_archive_payload=source_payload,
            source_contents=forged_source_contents,
            frontend_archive_payload=frontend_payload,
            build_attestation_payload=(
                release_module._canonical_ascii_json_bytes(forged_document) + b"\n"
            ),
            git_sha=evidence["candidate"].git_sha,
        )


def test_causal_frontend_equivalence_is_closed_and_recalculated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    baseline = load_release_attestation(
        evidence["baseline"].output_path,
        expected_sha256=evidence["baseline"].sha256,
    )
    candidate = load_release_attestation(
        evidence["candidate"].output_path,
        expected_sha256=evidence["candidate"].sha256,
    )
    original = json.loads(evidence["pair"].output_path.read_bytes())
    release_document = json.loads(evidence["candidate"].output_path.read_bytes())
    release_document["artifact"]["frontend"]["unexpected"] = False
    release_path = tmp_path / "forged-release-frontend-extra.json"
    release_path.write_bytes(canonical_json_bytes(release_document) + b"\n")
    with pytest.raises(ContractError, match="artifact.frontend"):
        load_release_attestation(
            release_path,
            expected_sha256=sha256_file(release_path),
        )

    side_document = json.loads(json.dumps(original))
    side_document["baseline"]["frontend"]["unexpected"] = False
    side_path = tmp_path / "forged-pair-frontend-extra.json"
    side_path.write_bytes(canonical_json_bytes(side_document) + b"\n")
    with pytest.raises(ContractError, match="baseline.frontend"):
        load_causal_pair(
            side_path,
            expected_sha256=sha256_file(side_path),
            baseline_attestation=baseline,
            candidate_attestation=candidate,
            expected_manifest_sha256=sha256_file(MANIFEST),
        )

    for index, key in enumerate(
        (
            "archive_sha256",
            "archive_map_sha256",
            "source_map_sha256",
            "builder_recipe_sha256",
        )
    ):
        document = json.loads(json.dumps(original))
        document["equivalence"]["frontend"][key] = f"sha256:{index:064x}"
        path = tmp_path / f"forged-frontend-equivalence-{index}.json"
        path.write_bytes(canonical_json_bytes(document) + b"\n")
        with pytest.raises(ContractError, match="equivalence.frontend"):
            load_causal_pair(
                path,
                expected_sha256=sha256_file(path),
                baseline_attestation=baseline,
                candidate_attestation=candidate,
                expected_manifest_sha256=sha256_file(MANIFEST),
            )

    document = json.loads(json.dumps(original))
    document["equivalence"]["frontend"]["build_attestation_sha256"] = document[
        "baseline"
    ]["frontend"]["build_attestation_sha256"]
    path = tmp_path / "forged-frontend-equivalence-extra.json"
    path.write_bytes(canonical_json_bytes(document) + b"\n")
    with pytest.raises(ContractError, match="equivalence.frontend"):
        load_causal_pair(
            path,
            expected_sha256=sha256_file(path),
            baseline_attestation=baseline,
            candidate_attestation=candidate,
            expected_manifest_sha256=sha256_file(MANIFEST),
        )


def test_v3_report_reloads_mutated_bundles_and_matches_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    suite = load_suite(MANIFEST)
    loaded: dict[str, Any] = {}
    jsonschema = pytest.importorskip("jsonschema")
    response_schema = json.loads(
        (DATA_ROOT / "responses.schema.v3.json").read_text(encoding="utf-8")
    )
    for role, peer_role in (("baseline", "candidate"), ("candidate", "baseline")):
        document = _synthetic_v3_bundle_document(
            suite=suite, evidence=evidence, role=role
        )
        jsonschema.validate(document, response_schema)
        if role == "candidate":
            boolean_calls = json.loads(json.dumps(document))
            boolean_calls["artifact"]["execution_observation"]["repair_model_calls"] = (
                False
            )
            boolean_path = tmp_path / "candidate-boolean-calls.json"
            boolean_path.write_bytes(canonical_json_bytes(boolean_calls) + b"\n")
            with pytest.raises(ContractError, match="repair_model_calls"):
                load_response_bundle(
                    boolean_path,
                    suite,
                    expected_role=role,
                    expected_sha256=sha256_file(boolean_path),
                    release_attestation_path=evidence[role].output_path,
                    release_attestation_sha256=evidence[role].sha256,
                    peer_release_attestation_path=evidence[peer_role].output_path,
                    peer_release_attestation_sha256=evidence[peer_role].sha256,
                    causal_pair_path=evidence["pair"].output_path,
                    causal_pair_sha256=evidence["pair"].sha256,
                )
        path = tmp_path / f"{role}-responses-v3.json"
        path.write_bytes(canonical_json_bytes(document) + b"\n")
        loaded[role] = load_response_bundle(
            path,
            suite,
            expected_role=role,
            expected_sha256=sha256_file(path),
            release_attestation_path=evidence[role].output_path,
            release_attestation_sha256=evidence[role].sha256,
            peer_release_attestation_path=evidence[peer_role].output_path,
            peer_release_attestation_sha256=evidence[peer_role].sha256,
            causal_pair_path=evidence["pair"].output_path,
            causal_pair_sha256=evidence["pair"].sha256,
        )

    loaded["baseline"].document["artifact"]["role"] = "candidate"
    loaded["candidate"].by_case_id["warmth-optin"]["text"] = "Je suis jalouse."
    loaded["candidate"].release_attestation.document["release"]["deployment_state"] = (
        "prepared_noncurrent"
    )

    report = build_comparison_report(suite, loaded["baseline"], loaded["candidate"])

    assert report["baseline"]["gate_pass"] is True
    assert report["candidate"]["gate_pass"] is True
    assert report["comparison"]["shadow_evidence_ready"] is True
    assert report["promotion"]["eligible_for_adjudication"] is True
    assert report["promotion"]["eligible_for_promotion"] is False
    jsonschema.validate(
        report,
        json.loads((DATA_ROOT / "report.schema.v3.json").read_text(encoding="utf-8")),
    )


def test_cli_v3_compare_revalidates_sealed_candidate_before_and_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    suite = load_suite(MANIFEST)
    bundles: dict[str, Path] = {}
    for role in ("baseline", "candidate"):
        path = tmp_path / f"{role}-cli-responses.json"
        path.write_bytes(
            canonical_json_bytes(
                _synthetic_v3_bundle_document(
                    suite=suite,
                    evidence=evidence,
                    role=role,
                )
            )
            + b"\n"
        )
        bundles[role] = path
    controller_checks: list[str] = []

    def verify_controller(**_kwargs: Any) -> str:
        controller_checks.append(evidence["candidate"].sha256)
        return evidence["candidate"].sha256

    monkeypatch.setattr(
        relationship_cli, "_verify_sealed_v3_controller", verify_controller
    )
    report_path = tmp_path / "preliminary-report.json"
    result = relationship_cli.main(
        [
            "compare",
            "--manifest",
            str(MANIFEST),
            "--baseline",
            str(bundles["baseline"]),
            "--baseline-sha256",
            sha256_file(bundles["baseline"]),
            "--baseline-release-attestation",
            str(evidence["baseline"].output_path),
            "--baseline-release-attestation-sha256",
            evidence["baseline"].sha256,
            "--candidate",
            str(bundles["candidate"]),
            "--candidate-sha256",
            sha256_file(bundles["candidate"]),
            "--candidate-release-attestation",
            str(evidence["candidate"].output_path),
            "--candidate-release-attestation-sha256",
            evidence["candidate"].sha256,
            "--causal-pair",
            str(evidence["pair"].output_path),
            "--causal-pair-sha256",
            evidence["pair"].sha256,
            "--report",
            str(report_path),
        ]
    )

    assert result == relationship_cli.EXIT_OK
    assert controller_checks == [
        evidence["candidate"].sha256,
        evidence["candidate"].sha256,
    ]
    assert report_path.is_file()


def test_causal_pair_ignores_inherited_git_context_replace_refs_and_grafts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    repository = evidence["repository"]
    baseline_sha = evidence["baseline"].git_sha
    candidate_sha = evidence["candidate"].git_sha
    _git(repository, "replace", candidate_sha, baseline_sha)
    grafts = repository / ".git" / "info" / "grafts"
    grafts.write_text(f"{candidate_sha}\n", encoding="ascii")

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
    fake_git.chmod(0o755)
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    output = tmp_path / "replacement-safe-pair"
    output.mkdir(mode=0o700)
    result = generate_causal_pair(
        repository_root=repository,
        baseline_source_archive_path=evidence["baseline_source_path"],
        candidate_source_archive_path=evidence["candidate_source_path"],
        baseline_attestation_path=evidence["baseline"].output_path,
        baseline_attestation_sha256=evidence["baseline"].sha256,
        candidate_attestation_path=evidence["candidate"].output_path,
        candidate_attestation_sha256=evidence["candidate"].sha256,
        evaluation_manifest_sha256=sha256_file(MANIFEST),
        output_directory=output,
    )

    assert result.baseline_git_sha == baseline_sha
    assert result.candidate_git_sha == candidate_sha
    assert release_module._raw_commit_parents(repository, candidate_sha) == [
        baseline_sha
    ]


def test_release_v2_loader_rejects_non_anthropic_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    document = json.loads(evidence["baseline"].output_path.read_text(encoding="utf-8"))
    document["engine"]["provider"] = "openai"
    path = tmp_path / "forged-release-attestation.json"
    payload = release_module.canonical_json_bytes(document) + b"\n"
    path.write_bytes(payload)

    with pytest.raises(ContractError, match="Anthropic"):
        load_release_attestation(path, expected_sha256=sha256_bytes(payload))


def test_causal_pair_refuses_a_source_archive_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    linked = tmp_path / "linked-source.tar"
    linked.symlink_to(evidence["baseline_source_path"])
    output = tmp_path / "linked-pair-output"
    output.mkdir(mode=0o700)

    with pytest.raises(ContractError, match="liee ou indirecte"):
        generate_causal_pair(
            repository_root=evidence["repository"],
            baseline_source_archive_path=linked,
            candidate_source_archive_path=evidence["candidate_source_path"],
            baseline_attestation_path=evidence["baseline"].output_path,
            baseline_attestation_sha256=evidence["baseline"].sha256,
            candidate_attestation_path=evidence["candidate"].output_path,
            candidate_attestation_sha256=evidence["candidate"].sha256,
            evaluation_manifest_sha256=sha256_file(MANIFEST),
            output_directory=output,
        )


def test_release_attestation_refuses_a_digest_without_matching_release_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    output = tmp_path / "wrong-manifest-output"
    output.mkdir(mode=0o700)
    config = tmp_path / "config.toml"

    with pytest.raises(ContractError, match="pin GitOps divergents"):
        generate_release_attestation(
            release_root=evidence["candidate_root"],
            config_path=config,
            evaluation_manifest_sha256="sha256:" + "f" * 64,
            output_directory=output,
        )
    assert not list(output.iterdir())


def test_release_attestation_refuses_noncanonical_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical-config.toml"
    synthetic = tmp_path / "synthetic-config.toml"
    monkeypatch.setattr(release_module, "_DEPLOYED_CONFIG_PATH", canonical)

    with pytest.raises(ContractError, match="chemin runtime canonique"):
        generate_release_attestation(
            release_root=tmp_path / ("1" * 40),
            config_path=synthetic,
            evaluation_manifest_sha256="sha256:" + "1" * 64,
            output_directory=tmp_path,
        )


def test_release_attestation_refuses_mutable_installed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    source_file = evidence["candidate_root"] / "README.md"
    source_file.chmod(0o644)
    output = tmp_path / "mutable-source-attestation"
    output.mkdir(mode=0o700)

    with pytest.raises(ContractError, match="source installee"):
        generate_release_attestation(
            release_root=evidence["candidate_root"],
            config_path=evidence["config"],
            evaluation_manifest_sha256=sha256_file(MANIFEST),
            output_directory=output,
        )


def test_installed_source_tree_refuses_an_extra_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    source_entries, source_contents = release_module._source_archive_map(source_payload)
    release_root.chmod(0o755)
    extra = release_root / "undeclared-source.py"
    extra.write_bytes(b"VALUE = 'forged'\n")
    extra.chmod(0o444)
    release_root.chmod(0o555)

    with pytest.raises(ContractError, match="source installe contient un extra"):
        release_module._verify_installed_source_tree(
            release_root,
            source_entries,
            source_contents,
        )


def test_runtime_requirements_are_hash_locked_to_wheels_from_git() -> None:
    checkout_root = Path(__file__).parents[2]
    requirements = (
        checkout_root / "deploy/runtime/ava-runtime-requirements.v1.txt"
    ).read_bytes()
    lock_payload = (checkout_root / "uv.lock").read_bytes()
    wheel_path = "deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl"
    wheel_payload = (checkout_root / wheel_path).read_bytes()

    installed, requirements_digest, wheelhouse_digest = (
        release_module._hashed_requirements(
            requirements,
            lock_payload=lock_payload,
            source_contents={wheel_path: wheel_payload},
        )
    )

    assert installed["anthropic"] == {"0.120.2"}
    assert installed["cryptography"] == {"50.0.0"}
    assert installed["docopt"] == {"0.6.2"}
    assert requirements_digest.startswith("sha256:")
    assert wheelhouse_digest.startswith("sha256:")

    details, _versions, *_rest = release_module._runtime_requirements_contract(
        requirements,
        lock_payload=lock_payload,
        source_contents={wheel_path: wheel_payload},
        expected_pins={
            Path(wheel_path).name: sha256_bytes(wheel_payload).removeprefix("sha256:")
        },
    )
    selected = {
        identity
        for identity, detail in details.items()
        if release_module._marker_applies(detail["marker"])
    }
    assert len(details) == 160
    assert len(selected) == 144
    assert len({name for name, _version in selected}) == 144
    assert ("torch", "2.11.0+cpu") in selected
    assert ("torch", "2.11.0") not in selected
    assert ("numpy", "2.3.5") in selected
    assert ("numpy", "2.2.6") not in selected
    assert ("numpy", "2.4.6") not in selected
    assert {("dill", "0.4.1"), ("num2words", "0.5.14"), ("sympy", "1.14.0")} <= selected

    assert release_module._marker_applies(
        "python_full_version >= '3.11' and python_full_version < '3.13'"
    )
    assert not release_module._marker_applies("python_full_version < '3.9'")
    assert release_module._marker_applies("python_full_version > '3.9'")
    with pytest.raises(ContractError, match="version de marker non canonique"):
        release_module._marker_applies("python_version >= '03.12'")

    with pytest.raises(ContractError, match="wheelhouse runtime source"):
        release_module._hashed_requirements(
            requirements,
            lock_payload=lock_payload,
            source_contents={wheel_path: wheel_payload + b"tamper"},
        )
    with pytest.raises(ContractError, match="sans wheel hash-lockee"):
        release_module._hashed_requirements(
            requirements.replace(
                b"sha256:6d6eabf5974d0b72899f74ecb6ae84f0d436ca0f7b3037ffc7b9a8a0790a6813",
                b"sha256:" + b"0" * 64,
            ),
            lock_payload=lock_payload,
            source_contents={wheel_path: wheel_payload},
        )


def test_wheelhouse_rejects_inverted_active_requirement_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    requirements, _versions, _digest, _pins_digest, source_pins, locked_wheels = (
        release_module._runtime_requirements_contract(
            source_contents["deploy/runtime/ava-runtime-requirements.v1.txt"],
            lock_payload=source_contents["uv.lock"],
            source_contents=source_contents,
        )
    )
    wheelhouse_manifest = release_module._runtime_wheelhouse_manifest_contract(
        source_contents["deploy/runtime/ava-runtime-wheelhouse.v1.json"],
        requirements_payload=source_contents[
            "deploy/runtime/ava-runtime-requirements.v1.txt"
        ],
        lock_payload=source_contents["uv.lock"],
    )
    release_module._sealed_wheelhouse(
        release_root,
        requirements=requirements,
        source_pins=source_pins,
        locked_wheels=locked_wheels,
        wheelhouse_manifest=wheelhouse_manifest,
    )

    present_made_inactive = {
        identity: dict(detail) for identity, detail in requirements.items()
    }
    present_made_inactive[("anthropic", "0.120.2")]["marker"] = (
        "sys_platform == 'darwin'"
    )
    with pytest.raises(ContractError, match="markers Linux CPython"):
        release_module._sealed_wheelhouse(
            release_root,
            requirements=present_made_inactive,
            source_pins=source_pins,
            locked_wheels=locked_wheels,
            wheelhouse_manifest=wheelhouse_manifest,
        )

    absent_made_active = {
        identity: dict(detail) for identity, detail in requirements.items()
    }
    absent_made_active[("absent", "1.0.0")] = {
        "hashes": frozenset(),
        "marker": "sys_platform == 'linux'",
    }
    with pytest.raises(ContractError, match="markers Linux CPython"):
        release_module._sealed_wheelhouse(
            release_root,
            requirements=absent_made_active,
            source_pins=source_pins,
            locked_wheels=locked_wheels,
            wheelhouse_manifest=wheelhouse_manifest,
        )


def test_runtime_wheelhouse_manifest_parser_and_exact_set_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    requirements_payload = source_contents[
        "deploy/runtime/ava-runtime-requirements.v1.txt"
    ]
    lock_payload = source_contents["uv.lock"]
    manifest_payload = source_contents["deploy/runtime/ava-runtime-wheelhouse.v1.json"]
    parsed = release_module._runtime_wheelhouse_manifest_contract(
        manifest_payload,
        requirements_payload=requirements_payload,
        lock_payload=lock_payload,
    )
    actual = {
        path.name: sha256_bytes(path.read_bytes())
        for path in sorted(
            (release_root / release_module._SEALED_WHEELHOUSE_PATH).iterdir()
        )
    }
    assert parsed == actual

    for mutation in ("noncanonical", "requirements", "uv-lock"):
        mutated = json.loads(manifest_payload)
        if mutation == "requirements":
            mutated["requirements_sha256"] = "sha256:" + "1" * 64
        elif mutation == "uv-lock":
            mutated["uv_lock_sha256"] = "sha256:" + "2" * 64
        payload = canonical_json_bytes(mutated) + b"\n"
        if mutation == "noncanonical":
            payload = (json.dumps(mutated, indent=2, sort_keys=True) + "\n").encode(
                "ascii"
            )
        with pytest.raises(ContractError, match="manifeste wheelhouse"):
            release_module._runtime_wheelhouse_manifest_contract(
                payload,
                requirements_payload=requirements_payload,
                lock_payload=lock_payload,
            )

    requirement_details, _versions, _digest, _pins, source_pins, locked_wheels = (
        release_module._runtime_requirements_contract(
            requirements_payload,
            lock_payload=lock_payload,
            source_contents=source_contents,
        )
    )
    for mutation in ("missing", "extra", "name", "hash"):
        mutated = dict(parsed)
        first = next(iter(mutated))
        if mutation == "missing":
            mutated.pop(first)
        elif mutation == "extra":
            mutated["zzextra-9.9-py3-none-any.whl"] = "sha256:" + "3" * 64
        elif mutation == "name":
            digest = mutated.pop(first)
            mutated["zzrenamed-1.0-py3-none-any.whl"] = digest
        elif mutation == "hash":
            mutated[first] = "sha256:" + "4" * 64
        mutated = dict(sorted(mutated.items()))
        with pytest.raises(ContractError, match="manifeste"):
            release_module._sealed_wheelhouse(
                release_root,
                requirements=requirement_details,
                source_pins=source_pins,
                locked_wheels=locked_wheels,
                wheelhouse_manifest=mutated,
            )


def test_python_runtime_archive_materializes_only_internal_regular_links() -> None:
    archive_payload, source_payload = _synthetic_python_runtime_source()
    source_contract = release_module._python_runtime_source_contract(source_payload)

    digest, entries, contents = release_module._python_archive_materialized_map(
        archive_payload,
        source_contract=source_contract,
    )

    by_path = {entry["path"]: entry for entry in entries}
    assert digest.startswith("sha256:")
    assert set(by_path) == {
        "bin/python",
        "bin/python3.12",
        "lib/python3.12/os.py",
    }
    assert by_path["bin/python"]["sha256"] == by_path["bin/python3.12"]["sha256"]
    assert contents["bin/python"] == contents["bin/python3.12"]
    assert by_path["bin/python"]["mode"] == "0555"

    stream = io.BytesIO()
    executable = contents["bin/python3.12"]
    stdlib = contents["lib/python3.12/os.py"]
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content, mode in (
            ("python/bin/python3.12", executable, 0o755),
            ("python/lib/python3.12/os.py", stdlib, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("python/bin/python")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../escape"
        archive.addfile(link)
    escaped = stream.getvalue()
    forged_source = json.loads(source_payload)
    forged_source["archive"]["sha256"] = sha256_bytes(escaped).removeprefix("sha256:")
    forged_source["archive"]["size"] = len(escaped)
    with pytest.raises(ContractError, match="hors racine"):
        release_module._python_archive_materialized_map(
            escaped,
            source_contract=forged_source,
        )


def test_runtime_evidence_rejects_pth_and_editable_project_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    site_root = release_root / ".venv/lib/python3.12/site-packages"

    site_root.chmod(0o755)
    injected = site_root / "injected.pth"
    injected.write_text("import fake_backend\n", encoding="utf-8")
    injected.chmod(0o444)
    site_root.chmod(0o555)
    with pytest.raises(ContractError, match="runtime-manifest|pyc ou pth interdit"):
        release_module._python_runtime_evidence(release_root, source_contents)

    site_root.chmod(0o755)
    injected.unlink()
    _install_synthetic_distribution(site_root, name="openjarvis", version="0.0.0")
    for path in site_root.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    site_root.chmod(0o555)
    with pytest.raises(ContractError, match="runtime-manifest|requirements"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_removed_direct_pth_must_remain_absent_after_manifest_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    site_root = release_root / ".venv/lib/python3.12/site-packages"
    site_root.chmod(0o755)
    restored = site_root / "anthropic.pth"
    restored.write_bytes(b"import anthropic\n")
    restored.chmod(0o444)
    site_root.chmod(0o555)
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="pyc ou pth interdit"):
        release_module._python_runtime_evidence(release_root, source_contents)


@pytest.mark.parametrize("metadata_name", ["direct_url.json", "uv_cache.json"])
def test_uv_generated_nondeterministic_metadata_is_never_attested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_name: str,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    site_root = release_root / ".venv/lib/python3.12/site-packages"
    dist_root = "anthropic-0.120.2.dist-info"
    payload = b'{"forged":"nondeterministic"}\n'
    site_root.chmod(0o755)
    target = site_root / dist_root / metadata_name
    target.parent.chmod(0o755)
    target.write_bytes(payload)
    target.chmod(0o444)
    target.parent.chmod(0o555)
    site_root.chmod(0o555)
    _rewrite_installed_record_entry(
        site_root / dist_root / "RECORD",
        installed_path=f"{dist_root}/{metadata_name}",
        payload=payload,
    )
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="RECORD installe non canonique"):
        release_module._python_runtime_evidence(release_root, source_contents)


@pytest.mark.parametrize(
    ("relative", "record_root", "record_entry"),
    [
        (
            ".venv/bin/num2words",
            "num2words-0.5.14.dist-info",
            "../../../bin/num2words",
        ),
        (
            ".venv/share/man/man1/isympy.1",
            "sympy-1.14.0.dist-info",
            "../../../share/man/man1/isympy.1",
        ),
    ],
)
def test_data_script_and_share_payloads_remain_bound_to_raw_wheels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    record_root: str,
    record_entry: str,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    target = release_root / relative
    original_mode = stat.S_IMODE(target.stat().st_mode)
    target.chmod(0o644)
    forged = target.read_bytes() + b"forged\n"
    target.write_bytes(forged)
    target.chmod(original_mode)
    _rewrite_installed_record_entry(
        release_root / ".venv/lib/python3.12/site-packages" / record_root / "RECORD",
        installed_path=record_entry,
        payload=forged,
    )
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="fichier externe divergent"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_runtime_evidence_rejects_a_sealer_recipe_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    seal_path = release_root / ".ava-seal.json"
    release_root.chmod(0o755)
    seal_path.chmod(0o644)
    document = json.loads(seal_path.read_bytes())
    document["runtime_recipe"]["sync_flags"].remove("--no-cache")
    seal_path.write_bytes(canonical_json_bytes(document) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)

    with pytest.raises(ContractError, match="recette scellee"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_runtime_evidence_recomputes_the_sealed_package_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    seal_path = release_root / ".ava-seal.json"
    release_root.chmod(0o755)
    seal_path.chmod(0o644)
    seal = json.loads(seal_path.read_bytes())
    seal["package_set_sha256"] = "0" * 64
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)

    with pytest.raises(ContractError, match="package_set"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_installed_rust_payload_is_bound_to_the_manifest_attested_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    _entries, source_contents = release_module._source_archive_map(
        evidence["candidate_source_path"].read_bytes()
    )
    site_root = release_root / ".venv/lib/python3.12/site-packages"
    installed = site_root / "ava_rust/__init__.py"
    installed.chmod(0o644)
    forged = installed.read_bytes() + b"FORGED = True\n"
    installed.write_bytes(forged)
    installed.chmod(0o444)
    _rewrite_installed_record_entry(
        site_root / "ava_rust-0.1.0.dist-info/RECORD",
        installed_path="ava_rust/__init__.py",
        payload=forged,
    )
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="fichier installe divergent"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_release_attestation_rejects_group_write_and_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    source_file = release_root / "README.md"

    source_file.chmod(0o464)
    with pytest.raises(ContractError, match="runtime Python non scelle"):
        release_module._python_runtime_evidence(release_root, source_contents)
    source_file.chmod(0o444)

    site_root = release_root / ".venv/lib/python3.12/site-packages"
    site_root.chmod(0o575)
    with pytest.raises(ContractError, match="runtime Python non scelle"):
        release_module._python_runtime_evidence(release_root, source_contents)
    site_root.chmod(0o555)

    external_link = tmp_path / "readme-hardlink"
    os.link(source_file, external_link)
    with pytest.raises(ContractError, match="runtime Python non scelle"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_seal_runtime_pins_are_closed_and_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    seal_path = release_root / ".ava-seal.json"
    release_root.chmod(0o755)
    seal_path.chmod(0o644)
    seal = json.loads(seal_path.read_bytes())
    seal["inputs"]["runtime_wheel_pins"][0]["unexpected"] = True
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)
    with pytest.raises(ContractError, match="recette scellee"):
        release_module._verify_sealed_runtime_recipe(release_root)

    release_root.chmod(0o755)
    seal_path.chmod(0o644)
    del seal["inputs"]["runtime_wheel_pins"][0]["unexpected"]
    seal["inputs"]["runtime_wheel_pins"].append(
        dict(seal["inputs"]["runtime_wheel_pins"][0])
    )
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)
    with pytest.raises(ContractError, match="recette scellee"):
        release_module._verify_sealed_runtime_recipe(release_root)


def test_seal_runtime_wheelhouse_manifest_sha_is_cross_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    seal_path = release_root / ".ava-seal.json"
    release_root.chmod(0o755)
    seal_path.chmod(0o644)
    seal = json.loads(seal_path.read_bytes())
    seal["inputs"]["runtime_wheelhouse_manifest_sha256"] = "0" * 64
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)

    with pytest.raises(ContractError, match="runtime_wheelhouse_manifest_sha256"):
        release_module._verify_sealed_runtime_recipe(release_root)


def test_attester_rehashes_each_sealed_wheel_against_the_source_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    wheelhouse = release_root / release_module._SEALED_WHEELHOUSE_PATH
    wheel = wheelhouse / "anthropic-0.120.2-py3-none-any.whl"
    release_root.chmod(0o755)
    wheelhouse.chmod(0o755)
    wheel.chmod(0o644)
    wheel.write_bytes(
        _synthetic_wheel_payload(
            name="anthropic",
            version="0.120.2",
            timestamp=(2026, 2, 2, 0, 0, 0),
            removed_pth=True,
        )
    )
    wheel.chmod(0o444)
    wheelhouse.chmod(0o555)
    release_root.chmod(0o555)
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="SHA divergent du manifeste"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_forged_manifests_cannot_hide_installed_metadata_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    site_root = release_root / ".venv/lib/python3.12/site-packages"
    dist_root = "anthropic-0.120.2.dist-info"
    metadata_path = site_root / dist_root / "METADATA"
    metadata_path.chmod(0o644)
    metadata_payload = metadata_path.read_bytes() + b"Summary: forged\n"
    metadata_path.write_bytes(metadata_payload)
    metadata_path.chmod(0o444)
    record_path = site_root / dist_root / "RECORD"
    record_path.chmod(0o644)
    lines = record_path.read_text(encoding="utf-8").splitlines()
    prefix = f"{dist_root}/METADATA,"
    lines = [
        (
            f"{dist_root}/METADATA,sha256={_record_digest(metadata_payload)},"
            f"{len(metadata_payload)}"
            if line.startswith(prefix)
            else line
        )
        for line in lines
    ]
    record_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record_path.chmod(0o444)
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="METADATA installe"):
        release_module._python_runtime_evidence(release_root, source_contents)


def test_frontend_archive_is_cross_bound_to_extracted_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    frontend_payload, _contents = _synthetic_frontend_archive()
    with tarfile.open(fileobj=io.BytesIO(frontend_payload), mode="r:") as archive:
        assert any(member.name == "index.html" for member in archive)
    forged_stream = io.BytesIO()
    with tarfile.open(fileobj=forged_stream, mode="w") as archive:
        content = b"<!doctype html><title>forged</title>\n"
        info = tarfile.TarInfo("index.html")
        info.size = len(content)
        info.mode = 0o644
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ""
        archive.addfile(info, io.BytesIO(content))
    forged = forged_stream.getvalue()
    artifact = release_root / release_module._FRONTEND_ARCHIVE_PATH
    seal_path = release_root / ".ava-seal.json"
    release_root.chmod(0o755)
    artifact.chmod(0o644)
    artifact.write_bytes(forged)
    artifact.chmod(0o444)
    seal_path.chmod(0o644)
    seal = json.loads(seal_path.read_bytes())
    seal["inputs"]["frontend_static_sha256"] = sha256_bytes(forged).removeprefix(
        "sha256:"
    )
    seal_path.write_bytes(canonical_json_bytes(seal) + b"\n")
    seal_path.chmod(0o444)
    release_root.chmod(0o555)
    _refresh_release_proofs(release_root)

    with pytest.raises(ContractError, match="source-manifest"):
        release_module._verify_sealed_runtime_recipe(release_root)


def test_wheelhouse_extra_and_unsupported_data_zones_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    release_root = evidence["candidate_root"]
    source_payload = evidence["candidate_source_path"].read_bytes()
    _entries, source_contents = release_module._source_archive_map(source_payload)
    wheelhouse = release_root / release_module._SEALED_WHEELHOUSE_PATH
    extra_name = "unexpected-1.0.0-py3-none-any.whl"
    wheelhouse.chmod(0o755)
    (wheelhouse / extra_name).write_bytes(
        _synthetic_wheel_payload(name="unexpected", version="1.0.0")
    )
    (wheelhouse / extra_name).chmod(0o444)
    wheelhouse.chmod(0o555)
    _refresh_release_proofs(release_root)
    with pytest.raises(ContractError, match="wheelhouse"):
        release_module._python_runtime_evidence(release_root, source_contents)

    with pytest.raises(ContractError, match="zone .data"):
        release_module._wheel_artifact(
            _synthetic_wheel_payload(
                name="unsafe-data",
                version="1.0.0",
                extra_entries={
                    "unsafe_data-1.0.0.data/headers/unsafe.h": b"#define X 1\n"
                },
            ),
            filename="unsafe_data-1.0.0-py3-none-any.whl",
        )
    with pytest.raises(ContractError, match="pth imbrique"):
        release_module._wheel_artifact(
            _synthetic_wheel_payload(
                name="unsafe-pth",
                version="1.0.0",
                extra_entries={"unsafe_pth/nested.pth": b"import unsafe_pth\n"},
            ),
            filename="unsafe_pth-1.0.0-py3-none-any.whl",
        )


def test_shadow_revalidates_deployed_config_bytes_against_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    attestation = load_release_attestation(
        evidence["candidate"].output_path,
        expected_sha256=evidence["candidate"].sha256,
    )
    monkeypatch.setattr(shadow_module, "_DEPLOYED_CONFIG_PATH", evidence["config"])

    original = shadow_module._verify_deployed_anthropic_config(attestation)
    assert original == evidence["config"].read_bytes()
    evidence["config"].write_text(
        "[intelligence]\n"
        'provider = "anthropic"\n'
        'default_model = "claude-other-v1"\n'
        'preferred_engine = "cloud"\n',
        encoding="utf-8",
    )
    with pytest.raises(ShadowRunError, match="differs from attestation"):
        shadow_module._verify_deployed_anthropic_config(attestation)


def test_critical_imports_are_read_from_attested_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    site_root = release_root / ".venv/lib/python3.12/site-packages"
    modules: dict[str, Any] = {}
    critical_imports: dict[str, dict[str, str]] = {}
    for module_name in ("anthropic", "cryptography", "httpcore", "httpx"):
        module_file = site_root / module_name / "__init__.py"
        module_file.parent.mkdir(parents=True, exist_ok=True)
        payload = f"NAME = {module_name!r}\n".encode("utf-8")
        module_file.write_bytes(payload)
        modules[module_name] = SimpleNamespace(
            __file__=str(module_file),
            __spec__=SimpleNamespace(origin=str(module_file)),
        )
        critical_imports[module_name] = {
            "path": f"{module_name}/__init__.py",
            "sha256": sha256_bytes(payload),
        }
    monkeypatch.setattr(
        shadow_module.importlib,
        "import_module",
        lambda name: modules[name],
    )
    runtime = {
        "site_packages_path": ".venv/lib/python3.12/site-packages",
        "critical_imports": critical_imports,
    }

    shadow_module._verify_critical_runtime_imports(release_root, runtime)
    critical_imports["cryptography"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ShadowRunError, match="cryptography differs"):
        shadow_module._verify_critical_runtime_imports(release_root, runtime)


def test_compare_v3_controller_revalidates_active_candidate_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    observed: list[str] = []
    monkeypatch.setattr(
        shadow_module,
        "_assert_isolated_interpreter",
        lambda: observed.append("isolated"),
    )
    monkeypatch.setattr(
        shadow_module,
        "_verify_release_execution_binding",
        lambda attestation: observed.append(attestation.sha256),
    )
    monkeypatch.setattr(
        shadow_module,
        "_current_release_git_sha",
        lambda: evidence["candidate"].git_sha,
    )

    assert (
        relationship_cli._verify_sealed_v3_controller(
            candidate_attestation_path=evidence["candidate"].output_path,
            candidate_attestation_sha256=evidence["candidate"].sha256,
        )
        == evidence["candidate"].sha256
    )
    assert observed == ["isolated", evidence["candidate"].sha256]


def test_runtime_bootstrap_is_source_first_and_dispatches_sealed_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    paths = (
        release_root,
        release_root / "src",
        release_root / ".venv/lib/python3.12/site-packages",
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    for path in sorted(
        release_root.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.is_dir():
            path.chmod(0o555)
    release_root.chmod(0o555)
    monkeypatch.setattr(runtime_bootstrap, "_TRUSTED_UID", os.geteuid())
    monkeypatch.setattr(runtime_bootstrap.sys, "path", ["/stdlib"])
    monkeypatch.setattr(runtime_bootstrap.sys, "dont_write_bytecode", False)
    runtime_bootstrap._install_runtime_paths(
        runtime_bootstrap._runtime_paths(release_root)
    )
    assert runtime_bootstrap.sys.path[:3] == [str(path) for path in paths]
    assert runtime_bootstrap.sys.dont_write_bytecode is True

    observed: dict[str, Any] = {}
    monkeypatch.setattr(runtime_bootstrap, "_release_root", lambda: release_root)
    monkeypatch.setattr(
        runtime_bootstrap, "_assert_no_site_interpreter", lambda _root: None
    )
    monkeypatch.setattr(
        runtime_bootstrap, "_assert_current_release", lambda _root: None
    )
    monkeypatch.setattr(runtime_bootstrap, "_runtime_paths", lambda _root: paths)
    monkeypatch.setattr(
        runtime_bootstrap, "_install_runtime_paths", lambda _paths: None
    )
    monkeypatch.setattr(
        runtime_bootstrap.runpy,
        "run_module",
        lambda module, **kwargs: observed.update(
            {
                "module": module,
                "kwargs": kwargs,
                "argv": list(runtime_bootstrap.sys.argv),
            }
        ),
    )
    assert runtime_bootstrap.main(["compare", "--report", "/tmp/report.json"]) == 0
    assert observed["module"] == "ava_extensions.evals.relationship.cli"
    assert observed["argv"] == [
        "ava-relationship-eval",
        "compare",
        "--report",
        "/tmp/report.json",
    ]


@pytest.mark.parametrize(
    ("label", "max_bytes"),
    [
        ("release-attestation", contracts_module._MAX_RELEASE_ATTESTATION_BYTES),
        ("causal-pair", contracts_module._MAX_CAUSAL_PAIR_BYTES),
        ("adjudication", contracts_module._MAX_ADJUDICATION_BYTES),
        ("anchor-key", contracts_module._MAX_ANCHOR_KEY_BYTES),
        ("external-anchor", contracts_module._MAX_EXTERNAL_ANCHOR_BYTES),
        ("response-bundle", contracts_module._MAX_RESPONSE_BUNDLE_BYTES),
    ],
)
def test_external_contract_size_bound_is_inclusive(
    tmp_path: Path, label: str, max_bytes: int
) -> None:
    at_bound = tmp_path / f"{label}-at-bound.json"
    at_payload = b"{}" + b" " * (max_bytes - 2)
    at_bound.write_bytes(at_payload)
    _resolved, document, digest = contracts_module._strict_external_document(
        at_bound,
        expected_sha256=sha256_bytes(at_payload),
        max_bytes=max_bytes,
    )
    assert document == {}
    assert digest == sha256_bytes(at_payload)

    above_bound = tmp_path / f"{label}-above-bound.json"
    above_payload = at_payload + b" "
    above_bound.write_bytes(above_payload)
    with pytest.raises(ContractError, match="hors taille"):
        contracts_module._strict_external_document(
            above_bound,
            expected_sha256=sha256_bytes(above_payload),
            max_bytes=max_bytes,
        )


def test_manifest_contract_size_bound_is_inclusive(tmp_path: Path) -> None:
    max_bytes = contracts_module._MAX_MANIFEST_BYTES
    at_bound = tmp_path / "manifest-at-bound.json"
    at_payload = b"{}" + b" " * (max_bytes - 2)
    at_bound.write_bytes(at_payload)
    assert (
        contracts_module._strict_regular_payload(
            at_bound, max_bytes=max_bytes, label="manifest"
        )
        == at_payload
    )

    above_bound = tmp_path / "manifest-above-bound.json"
    above_bound.write_bytes(at_payload + b" ")
    with pytest.raises(ContractError, match="hors taille"):
        contracts_module._strict_regular_payload(
            above_bound, max_bytes=max_bytes, label="manifest"
        )


def test_causal_binding_constructor_and_runner_have_no_caller_selected_role() -> None:
    with pytest.raises(ContractError, match="reserve au chargeur strict"):
        LoadedCausalShadowBinding(
            object(),
            release_attestation=None,  # type: ignore[arg-type]
            peer_release_attestation=None,  # type: ignore[arg-type]
            causal_pair=None,  # type: ignore[arg-type]
            role="candidate",
            treatment=CANDIDATE_TREATMENT,
            deployment_state="active_current",
            release_git_sha="1" * 40,
            treatment_module_sha256="sha256:" + "1" * 64,
            evaluation_manifest_sha256="sha256:" + "2" * 64,
        )

    parameters = inspect.signature(shadow_module.run_shadow).parameters
    assert "role" not in parameters
    assert "engine_factory" not in parameters
    assert "execution_mode" not in parameters
    assert (
        "engine_factory"
        not in inspect.signature(shadow_module._execute_isolated).parameters
    )
    assert not hasattr(shadow_module, "_TEST_EXECUTION_BINDING_CAPABILITY")
    assert not hasattr(shadow_module, "_EXECUTION_MODES")
    help_text = shadow_module._parser().format_help()
    assert "--configured-anthropic" in help_text
    assert "--role" not in help_text
    assert "--backend-url" not in help_text


def test_release_attestation_cli_keeps_v1_manifest_and_adds_pair_mode() -> None:
    release_help = release_module._release_parser().format_help()
    pair_help = release_module._causal_pair_parser().format_help()

    assert "--release-root" in release_help
    assert "--config" in release_help
    assert "--evaluation-manifest-sha256" in release_help
    assert "--baseline-source-archive" in pair_help
    assert "--candidate-source-archive" in pair_help
    assert "--baseline-release-attestation-sha256" in pair_help
    assert "--candidate-release-attestation-sha256" in pair_help
