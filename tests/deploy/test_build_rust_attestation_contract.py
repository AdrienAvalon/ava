"""Static contracts for the VM-compatible, attested Rust wheel builder."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-rust-attested.sh"
DOCKERFILE = ROOT / "deploy/docker/Dockerfile.rust-builder"

PYTHON_IMAGE = (
    "python:3.12.13-slim-bookworm@"
    "sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf"
)
RUST_IMAGE = (
    "rust:1.88.0-bookworm@"
    "sha256:4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0"
)


def test_builder_shell_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_builder_uses_only_pinned_bookworm_toolchains() -> None:
    dockerfile = DOCKERFILE.read_text()
    script = SCRIPT.read_text()

    assert f"FROM {PYTHON_IMAGE} AS python-runtime" in dockerfile
    assert f"FROM {RUST_IMAGE} AS builder" in dockerfile
    assert f"PYTHON_IMAGE='{PYTHON_IMAGE}'" in script
    assert f"RUST_IMAGE='{RUST_IMAGE}'" in script
    assert "maturin==1.14.1 --hash=sha256:" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "rustc 1\\.88\\.0" in dockerfile
    assert "platform.python_version()" in dockerfile
    assert '--platform "$TARGET_PLATFORM"' in script
    assert "TARGET_PLATFORM='linux/amd64'" in script

    for mutable_base in (
        "FROM python:3.12 AS",
        "FROM python:3.12-slim AS",
        "FROM rust:1.88 AS",
        "FROM debian:bookworm AS",
    ):
        assert mutable_base not in dockerfile


def test_builder_tests_locks_and_imports_the_wheel_in_isolation() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert re.search(r"cargo test\s+\\\n\s+--manifest-path rust/Cargo.toml", dockerfile)
    assert "--workspace" in dockerfile
    assert dockerfile.count("--locked") >= 2
    assert "maturin build" in dockerfile
    assert "--compatibility manylinux_2_36" in dockerfile
    assert "python3 -m venv /tmp/verify-venv" in dockerfile
    assert "/tmp/verify-venv/bin/pip install --no-deps --no-index" in dockerfile
    assert "/tmp/verify-venv/bin/python -I" in dockerfile
    assert "import openjarvis_rust" in dockerfile
    assert dockerfile.index("cargo test") < dockerfile.index("maturin build")
    assert dockerfile.index("maturin build") < dockerfile.index(
        "import openjarvis_rust"
    )


def test_script_builds_an_exact_commit_without_laptop_compilation() -> None:
    source = SCRIPT.read_text()

    assert 'git -C "$ROOT" diff --quiet' in source
    assert 'git -C "$ROOT" diff --cached --quiet' in source
    assert 'git -C "$ROOT" ls-files --others --exclude-standard' in source
    assert (
        'git -C "$ROOT" archive --format=tar "$GIT_SHA" "$DOCKERFILE_REL" rust'
        in source
    )
    assert '"$DOCKER_BIN" build' in source
    assert '"$DOCKER_BIN" image inspect' in source
    assert '"$DOCKER_BIN" cp "$CONTAINER_ID:/artifacts/."' in source
    assert "cargo test" not in source
    assert "maturin build" not in source
    assert "AVA_CARGO_BIN" not in source
    assert "AVA_MATURIN_BIN" not in source
    assert "AVA_RUST_PYTHON" not in source


def test_attestation_is_an_honest_unsigned_checksum_manifest() -> None:
    source = SCRIPT.read_text()
    required_fields = (
        "format=ava-rust-wheel-attestation-v1",
        "attestation_type=unsigned-checksum-manifest",
        "signature=none",
        "git_sha=",
        "rust_tree_sha256=",
        "wheel_sha256=",
        "wheel_filename=",
        "builder_image_id=",
        "builder_dockerfile_sha256=",
        "builder_platform=",
        "builder_python_image=",
        "builder_rust_image=",
        "python_version=",
        "rust_version=",
        "maturin_version=",
        "wheel_compatibility=manylinux_2_36_x86_64",
    )

    for field in required_fields:
        assert field in source
    assert 'git -C "$ROOT" archive --format=tar "$GIT_SHA" rust' in source
    assert re.search(r"WHEEL_SHA256=.*sha256sum", source)
    assert '[[ ! -e "$DESTINATION" && ! -e "$ATTESTATION" ]]' in source
    assert 'mv -- "$STAGED_WHEEL" "$DESTINATION"' in source
    assert 'mv -- "$STAGED_ATTESTATION" "$ATTESTATION"' in source


def test_builder_context_and_script_do_not_publish_or_embed_credentials() -> None:
    combined = f"{DOCKERFILE.read_text()}\n{SCRIPT.read_text()}".lower()

    for forbidden in (
        "git push",
        "--secret",
        "token=",
        "password=",
        "authorization:",
        "curl ",
        "wget ",
    ):
        assert forbidden not in combined


def test_builder_removes_its_process_scoped_image() -> None:
    source = SCRIPT.read_text()

    assert 'IMAGE_REF="ava-rust-builder:${GIT_SHA}-$$"' in source
    assert '"$DOCKER_BIN" image rm -f "$IMAGE_REF"' in source
