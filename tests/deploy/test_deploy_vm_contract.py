"""Offline regression contracts for Ava's immutable VM delivery path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-vm.sh"
FAKE_DOWNLOADED_WHEEL = b"fake downloaded wheel\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def _rust_tree_hash(repo: Path, commit: str) -> str:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit, "rust"],
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(archive).hexdigest()


def _make_collectable_releases(releases: Path, *, count: int = 3) -> list[Path]:
    """Create ordered, ready-looking stale releases eligible for retention."""

    releases.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    oldest_timestamp = time.time() - 1_000
    for index in range(count):
        release = releases / f"{index + 1:040x}"
        release.mkdir()
        (release / ".ava-ready").write_text(f"{release.name}\n")
        (release / ".ava-release").write_text("format=ava-release-v1\n")
        timestamp = oldest_timestamp + index
        os.utime(release, (timestamp, timestamp))
        created.append(release)
    return created


class DeploymentFixture(NamedTuple):
    repo: Path
    remote: Path
    legacy: Path
    staging: Path
    releases: Path
    current: Path
    wheel: Path
    attestation: Path
    fake_bin: Path
    environment: dict[str, str]
    commit: str
    ssh_marker: Path
    build_log: Path
    restart_count: Path
    service_state: Path
    deadman_state: Path
    ssh_cutoff: Path
    fake_deadman: Path
    runtime_policy_count: Path


@pytest.fixture(autouse=True)
def _restore_deployment_fixture_permissions(tmp_path: Path):
    """Make sealed fake releases removable by pytest after every test.

    The fake remote deliberately removes owner write bits to exercise the real
    immutable-release contract. Pytest cannot clean its numbered temp tree until
    the owning test process restores those bits.
    """

    yield
    for directory, _directories, filenames in os.walk(tmp_path, topdown=False):
        root = Path(directory)
        for filename in filenames:
            path = root / filename
            if path.is_symlink():
                continue
            try:
                path.chmod(path.stat().st_mode | 0o600)
            except FileNotFoundError:
                pass
        if not root.is_symlink():
            try:
                root.chmod(root.stat().st_mode | 0o700)
            except FileNotFoundError:
                pass


def _make_deployment_fixture(
    tmp_path: Path,
    *,
    relationship_treatment: str = "runtime-enforced-v1",
    runtime_manifest_mutation: str | None = None,
) -> DeploymentFixture:
    repo = tmp_path / "ava"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    (repo / "rust" / "crates").mkdir(parents=True)
    (repo / "rust" / "crates" / "lib.rs").write_text("pub fn ava() {}\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text('{"scripts":{"build":"true"}}\n')
    (repo / "frontend" / "package-lock.json").write_text(
        '{"lockfileVersion":3,"name":"ava-deploy-fixture","packages":{},"requires":true}\n'
    )
    (repo / "frontend" / "index.html").write_text("<html>tracked context</html>\n")
    (repo / "tests" / "deployment").mkdir(parents=True)
    (repo / "tests" / "deployment" / "test_packaging.py").write_text(
        "def test_packaging_fixture():\n    assert True\n"
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname='ava-deploy-fixture'\nversion='1.0.0'\n"
    )
    (repo / "uv.lock").write_text(
        "version = 1\nrevision = 3\nrequires-python = '>=3.12'\n"
    )
    builder_dockerfile = repo / "deploy" / "docker" / "Dockerfile.rust-builder"
    builder_dockerfile.parent.mkdir(parents=True)
    builder_dockerfile.write_text("FROM scratch\n")
    shutil.copy2(
        ROOT / "deploy/docker/Dockerfile.frontend-builder",
        repo / "deploy/docker/Dockerfile.frontend-builder",
    )
    runtime_root = repo / "deploy" / "runtime"
    (runtime_root / "wheels").mkdir(parents=True)
    for relative in (
        "ava-python-runtime.v1.json",
        "ava-runtime-requirements.v1.txt",
        "wheels/docopt-0.6.2-py2.py3-none-any.whl",
    ):
        shutil.copy2(ROOT / "deploy" / "runtime" / relative, runtime_root / relative)
    runtime_manifest = {
        "entries": sorted(
            (
                {
                    "filename": "dependency-1.0-py3-none-any.whl",
                    "sha256": "sha256:"
                    + hashlib.sha256(FAKE_DOWNLOADED_WHEEL).hexdigest(),
                },
                {
                    "filename": "docopt-0.6.2-py2.py3-none-any.whl",
                    "sha256": "sha256:"
                    + hashlib.sha256(
                        (
                            runtime_root / "wheels/docopt-0.6.2-py2.py3-none-any.whl"
                        ).read_bytes()
                    ).hexdigest(),
                },
            ),
            key=lambda entry: entry["filename"],
        ),
        "requirements_sha256": "sha256:"
        + hashlib.sha256(
            (runtime_root / "ava-runtime-requirements.v1.txt").read_bytes()
        ).hexdigest(),
        "schema_version": "ava.runtime-wheelhouse/v1",
        "target": {
            "implementation": "cpython",
            "platform": "linux_x86_64",
            "python_version": "3.12.13",
        },
        "uv_lock_sha256": "sha256:"
        + hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
    }
    if runtime_manifest_mutation == "requirements":
        runtime_manifest["requirements_sha256"] = "sha256:" + "0" * 64
    elif runtime_manifest_mutation == "uv-lock":
        runtime_manifest["uv_lock_sha256"] = "sha256:" + "0" * 64
    elif runtime_manifest_mutation not in {None, "noncanonical"}:
        raise AssertionError(
            f"unknown runtime manifest mutation: {runtime_manifest_mutation}"
        )
    runtime_manifest_path = runtime_root / "ava-runtime-wheelhouse.v1.json"
    if runtime_manifest_mutation == "noncanonical":
        runtime_manifest_path.write_text(
            json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
    else:
        runtime_manifest_path.write_bytes(
            json.dumps(
                runtime_manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    extension_root = repo / "ava_extensions"
    (extension_root / "identity").mkdir(parents=True)
    shutil.copy2(ROOT / "ava_extensions" / "runtime_bootstrap.py", extension_root)
    treatment_path = ROOT / "ava_extensions/identity/relationship_guard_treatment.py"
    treatment = treatment_path.read_text()
    treatment_assignment = next(
        line
        for line in treatment.splitlines()
        if line.startswith("RELATIONSHIP_GUARD_TREATMENT = ")
    )
    treatment = treatment.replace(
        treatment_assignment,
        f'RELATIONSHIP_GUARD_TREATMENT = "{relationship_treatment}"',
        1,
    )
    (extension_root / "identity/relationship_guard_treatment.py").write_text(treatment)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Ava deployment test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "test fixture")
    commit = _git(repo, "rev-parse", "HEAD")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = artifacts / "openjarvis_rust-1.0.0-cp312-cp312-manylinux_2_36_x86_64.whl"
    wheel.write_bytes(b"fake but content-addressed rust wheel\n")
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    attestation = wheel.with_name(f"{wheel.name}.attestation")
    attestation.write_text(
        "\n".join(
            (
                "format=ava-rust-wheel-attestation-v1",
                "attestation_type=unsigned-checksum-manifest",
                "signature=none",
                f"git_sha={commit}",
                f"rust_tree_sha256={_rust_tree_hash(repo, commit)}",
                f"wheel_sha256={wheel_hash}",
                f"wheel_filename={wheel.name}",
                f"builder_image_id=sha256:{'0' * 64}",
                "builder_dockerfile_sha256="
                + hashlib.sha256(builder_dockerfile.read_bytes()).hexdigest(),
                "builder_platform=linux/amd64",
                "builder_python_image=python:3.12.13-slim-bookworm@sha256:"
                "76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf",
                "builder_rust_image=rust:1.88.0-bookworm@sha256:"
                "4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0",
                "python_version=3.12.13",
                "rust_version=1.88.0",
                "maturin_version=1.14.1",
                "wheel_compatibility=manylinux_2_36_x86_64",
                "",
            )
        )
    )

    remote = tmp_path / "remote"
    staging = remote / "staging"
    legacy_sha = "809ade530fedc53424f6fe93a22320d129237e62"
    legacy = staging / legacy_sha
    releases = remote / "releases"
    current = remote / "current"
    releases.mkdir(parents=True)
    (legacy / ".venv").mkdir(parents=True)
    (legacy / ".venv" / "sentinel").write_text("legacy environment\n")
    (legacy / ".venv/bin").mkdir()
    legacy_jarvis = legacy / ".venv/bin/jarvis"
    legacy_jarvis.write_text("#!/bin/sh\nexit 0\n")
    legacy_jarvis.chmod(0o755)
    legacy_python = legacy / ".venv/bin/python"
    legacy_python.write_text("#!/bin/sh\nexit 0\n")
    legacy_python.chmod(0o755)
    (legacy / "src/openjarvis/server/static").mkdir(parents=True)
    (legacy / "src/openjarvis/server/static/index.html").write_text(
        "<html>legacy</html>\n"
    )
    (legacy / "frontend" / "dist").mkdir(parents=True)
    (legacy / "frontend" / "dist" / "sentinel").write_text("legacy frontend\n")
    (legacy / ".ava-ready").write_text(f"{legacy_sha}\n")
    (legacy / ".ava-release").write_text(
        "\n".join(
            (
                "format=ava-release-v1",
                f"git_sha={legacy_sha}",
                f"source_tree_sha256={'1' * 64}",
                f"rust_tree_sha256={'2' * 64}",
                f"wheel_sha256={'3' * 64}",
                "wheel_filename=openjarvis_rust-1.0.0-cp312-cp312-manylinux_2_36_x86_64.whl",
                f"attestation_sha256={'4' * 64}",
                f"evolutions_sha256={'5' * 64}",
                "",
            )
        )
    )
    for legacy_path in (legacy, *legacy.rglob("*")):
        if legacy_path.is_dir() or legacy_path.stat().st_mode & 0o111:
            legacy_path.chmod(0o555)
        else:
            legacy_path.chmod(0o444)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_marker = tmp_path / "ssh-was-called"
    build_log = tmp_path / "build.log"
    restart_count = tmp_path / "restart-count"
    service_state = tmp_path / "service-state"
    service_state.write_text("active\n")
    active_target = tmp_path / "active-target"
    active_target.write_text(f"{legacy}\n")
    relay_ca = tmp_path / "relay-ca.crt"
    relay_ca.write_text("test-only public CA\n")
    python_runtime_archive = tmp_path / "python-runtime.tar.gz"
    python_runtime_archive.write_bytes(b"fake pinned Python 3.12 runtime\n")
    python_runtime_sha256 = hashlib.sha256(
        python_runtime_archive.read_bytes()
    ).hexdigest()
    relationship_policy = tmp_path / "relationship-policy.json"
    relationship_policy.write_text('{"bindings":[],"enabled":false,"version":1}\n')
    deadman_state = tmp_path / "deadman-state"
    ssh_cutoff = tmp_path / "ssh-cutoff"
    runtime_policy_count = tmp_path / "runtime-policy-count"
    python_stub = tmp_path / "python-stub"
    _write_executable(
        python_stub,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"config.agent.system_prompt_path == ''"* ]]; then
  count=0
  [[ ! -f "$FAKE_RUNTIME_POLICY_COUNT" ]] || count=$(cat "$FAKE_RUNTIME_POLICY_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_RUNTIME_POLICY_COUNT"
  if [[ -n "${FAKE_RUNTIME_POLICY_FAIL_AT:-}" \
    && "$count" -eq "$FAKE_RUNTIME_POLICY_FAIL_AT" ]]; then
    exit 93
  fi
fi
exit 0
""",
    )

    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
touch "$FAKE_SSH_MARKER"
last="${!#}"
printf '%s\n' "$last" >> "$FAKE_SSH_LOG"
if [[ "$last" == *"# ava-wheelhouse-prefill-v1"* ]]; then
  if [[ -n "${FAKE_UV_FAIL:-}" ]]; then
    printf 'failed-build-must-be-preserved\\0\n' \
      > "$AVA_STAGING_ROOT/${FAKE_GIT_SHA}/.cleanup-preserve.bin"
    exit 41
  fi
  wheelhouse="$AVA_STAGING_ROOT/${FAKE_GIT_SHA}/.ava-artifacts/python-wheelhouse"
  printf 'fake downloaded wheel\n' \
    > "$wheelhouse/dependency-1.0-py3-none-any.whl"
  case "${FAKE_WHEELHOUSE_MUTATION:-}" in
    '') ;;
    missing) rm -f -- "$wheelhouse/dependency-1.0-py3-none-any.whl" ;;
    extra) printf 'unexpected wheel\n' > "$wheelhouse/extra-9.9-py3-none-any.whl" ;;
    name) mv -- "$wheelhouse/dependency-1.0-py3-none-any.whl" \
      "$wheelhouse/renamed-1.0-py3-none-any.whl" ;;
    hash) printf 'tamper\n' >> "$wheelhouse/dependency-1.0-py3-none-any.whl" ;;
    *) exit 99 ;;
  esac
  rm -rf -- "$AVA_STAGING_ROOT/${FAKE_GIT_SHA}/.python-wheel-downloader"
  rm -f -- "$AVA_STAGING_ROOT/${FAKE_GIT_SHA}/.ava-artifacts/.runtime-requirements.tmp"
  exit 0
fi
if [[ "$last" == *"AVA_MAIN_PID=4242 /usr/bin/python3"* ]]; then
  [[ -z "${FAKE_BAD_MAIN_ARGV:-}" ]] || exit 97
  exit 0
fi
signal_deployer() {
  target=$PPID
  while [[ "$target" -gt 1 ]]; do
    command_line=$(tr '\0' ' ' < "/proc/$target/cmdline" 2>/dev/null || true)
    if [[ "$command_line" == *"scripts/deploy-vm.sh"* ]]; then
      kill -TERM "$target"
      return
    fi
    target=$(awk '{print $4}' "/proc/$target/stat")
  done
  exit 98
}
if [[ -f "$FAKE_SSH_CUTOFF" ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_LOCK_PREFLIGHT_STATUS:-}" \
  && "$last" == set\\ -eu* \
  && "$last" == *"# ava-lock-control-v1"* ]]; then
  bash -c "$last" >/dev/null
  exit "$FAKE_AMBIGUOUS_LOCK_PREFLIGHT_STATUS"
fi
if [[ -n "${FAKE_LOCK_CREATE_NEVER_STARTS:-}" \
  && "$last" == set\\ -eu* \
  && "$last" == *"# ava-lock-publish-v2"* ]]; then
  if [[ -n "${FAKE_AMBIGUOUS_LOCK_CREATE_SIGNAL:-}" ]]; then
    signal_deployer
  fi
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_LOCK_CREATE_STATUS:-}" \
  && "$last" == set\\ -eu* \
  && "$last" == *"# ava-lock-publish-v2"* ]]; then
  if [[ -n "${FAKE_DELAYED_LOCK_CREATE:-}" ]]; then
    (
      trap '' HUP INT TERM
      set +e
      bash -c "$last"
      remote_status=$?
      printf '%s\n' "$remote_status" > "$FAKE_LOCK_CREATE_STATUS"
      touch "$FAKE_LOCK_CREATE_FINISHED"
      exit "$remote_status"
    ) >/dev/null 2>&1 &
    paused=0
    for ((attempt = 0; attempt < 500; attempt++)); do
      if [[ -f "$FAKE_LOCK_CREATE_PAUSED" ]]; then
        paused=1
        break
      fi
      sleep 0.01
    done
    [[ "$paused" -eq 1 ]] || exit 96
    (
      sleep "${FAKE_LOCK_CREATE_PAUSE_SECONDS:-0.25}"
      touch "$FAKE_LOCK_CREATE_RESUME"
    ) >/dev/null 2>&1 &
  else
    bash -c "$last"
  fi
  if [[ -n "${FAKE_AMBIGUOUS_LOCK_CREATE_SIGNAL:-}" ]]; then
    signal_deployer
  fi
  exit "$FAKE_AMBIGUOUS_LOCK_CREATE_STATUS"
fi
if [[ -n "${FAKE_SIGNAL_AFTER_LOCK_CREATE:-}" \
  && "$last" == *"# ava-lock-publish-v2"* ]]; then
  bash -c "$last"
  lock="$AVA_RELEASE_ROOT/.deploy-lock"
  owner=$(/usr/bin/python3 -c \
    "import os,sys; sys.stdout.write(os.getxattr(
sys.argv[1], 'user.ava_deploy_token').decode('ascii'))" \
    "$lock")
  entries=$(find "$lock" -mindepth 1 -maxdepth 1 -print | wc -l)
  printf '%s\n%s\n' "$owner" "$entries" > "$FAKE_PUBLISHED_LOCK_SNAPSHOT"
  signal_deployer
  exit 0
fi
if [[ -n "${FAKE_SIGNAL_AFTER_RELEASE_CREATE:-}" \
  && "$last" == *"mkdir -- $AVA_RELEASE_ROOT/"* \
  && "$last" == *".ava-building"* ]]; then
  bash -c "$last"
  signal_deployer
  exit 0
fi
if [[ -n "${FAKE_AMBIGUOUS_FIRST_LOCK_RELEASE:-}" \
  && "$last" == *"# ava-lock-release-v2"* ]]; then
  count=0
  [[ ! -f "$FAKE_LOCK_RELEASE_COUNT" ]] || count=$(cat "$FAKE_LOCK_RELEASE_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_LOCK_RELEASE_COUNT"
  if [[ "$count" -eq 1 ]]; then
    exit 255
  fi
fi
if [[ -n "${FAKE_SIGNAL_AFTER_LOCK_RELEASE:-}" \
  && "$last" == *"# ava-lock-release-v2"* ]]; then
  bash -c "$last"
  signal_deployer
  exit 0
fi
if [[ -n "${FAKE_AMBIGUOUS_RELEASE_EXISTS_PROBE:-}" \
  && "$last" == "test -e $AVA_RELEASE_ROOT/"* ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_RELEASE_READY_PROBE:-}" \
  && "$last" == "test -f $AVA_RELEASE_ROOT/"*"/.ava-ready" ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_RELEASE_EXISTS_PROBE:-}" \
  && -n "${FAKE_AMBIGUOUS_RELEASE_READY_PROBE:-}" \
  && "$last" == *"# ava-release-state-v1"* ]]; then
  bash -c "$last" >/dev/null
  exit 255
fi
if [[ -n "${FAKE_SWITCH_CURRENT_AFTER_RELEASE_INSPECTION:-}" \
  && "$last" == *"# ava-release-state-v1"* ]]; then
  release_state=$(bash -c "$last")
  temporary="${AVA_CURRENT_LINK}.race.$$"
  ln -s -- "$FAKE_SWITCH_CURRENT_AFTER_RELEASE_INSPECTION" "$temporary"
  mv -Tf -- "$temporary" "$AVA_CURRENT_LINK"
  printf '%s' "$release_state"
  exit 0
fi
if [[ -n "${FAKE_AMBIGUOUS_CURRENT_LINK_PROBE:-}" \
  && -n "${FAKE_AMBIGUOUS_CURRENT_EXISTS_PROBE:-}" \
  && "$last" == *"# ava-current-state-v1"* ]]; then
  bash -c "$last" >/dev/null
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_CURRENT_LINK_PROBE:-}" \
  && "$last" == "test -L $AVA_CURRENT_LINK" ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_CURRENT_EXISTS_PROBE:-}" \
  && "$last" == "test -e $AVA_CURRENT_LINK" ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_CLEANUP_CURRENT_PROBE:-}" \
  && "$last" == *"# ava-cleanup-release-v1"* ]]; then
  exit 255
fi
if [[ -n "${FAKE_AMBIGUOUS_CLEANUP_CURRENT_PROBE:-}" \
  && "$last" == "readlink -f -- $AVA_CURRENT_LINK" ]]; then
  exit 255
fi
if [[ -n "${FAKE_CORRUPT_WHEEL_TRANSFER:-}" && "$last" == "cat > "*".whl" ]]; then
  bash -c "$last"
  destination=${last#cat > }
  printf 'corrupted in transit\n' >> "$destination"
  exit 0
fi
if [[ -n "${FAKE_FAIL_SWITCH_TARGET:-}" ]]; then
  if [[ "$last" == *"ln -s -- $FAKE_FAIL_SWITCH_TARGET "* ]]; then
    bash -c "$last"
    exit 77
  fi
fi
if [[ -n "${FAKE_PERSISTENT_CUTOFF_TARGET:-}" ]]; then
  if [[ "$last" == *"ln -s -- $FAKE_PERSISTENT_CUTOFF_TARGET "* ]]; then
    bash -c "$last"
    touch "$FAKE_SSH_CUTOFF"
    exit 255
  fi
fi
exec bash -c "$last"
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'uv|%s|%s\n' "$PWD" "$*" >> "$FAKE_BUILD_LOG"
case "${1:-}" in
  sync)
    if [[ -n "${FAKE_UV_FAIL:-}" ]]; then
      printf 'failed-build-must-be-preserved\\0\n' > .cleanup-preserve.bin
      exit 41
    fi
    mkdir -p .venv/bin
    cp "$FAKE_PYTHON_STUB" .venv/bin/python
    chmod 0755 .venv/bin/python
    cp "$FAKE_PYTHON_STUB" .venv/bin/jarvis
    chmod 0755 .venv/bin/jarvis
    ;;
  pip)
    ;;
  *)
    exit 42
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
set -euo pipefail
touch "$FAKE_HOST_NPM_CALLED"
exit 99
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker|%s|%s\n' "$PWD" "$*" >> "$FAKE_BUILD_LOG"
[[ "${1:-}" == build ]]
shift
pull=0
no_cache=0
platform=''
dockerfile=''
output=''
context=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull) pull=1; shift ;;
    --no-cache) no_cache=1; shift ;;
    --platform) platform=$2; shift 2 ;;
    --file) dockerfile=$2; shift 2 ;;
    --output) output=$2; shift 2 ;;
    -*) exit 91 ;;
    *)
      [[ -z "$context" && $# -eq 1 ]]
      context=$1
      shift
      ;;
  esac
done
[[ "$pull" -eq 1 && "$no_cache" -eq 1 && "$platform" == linux/amd64 ]]
[[ "$output" == type=local,dest=* ]]
destination=${output#type=local,dest=}
[[ -d "$destination" && -d "$context/frontend" ]]
[[ ! -e "$context/.git" && ! -L "$context/.git" ]]
[[ -f "$context/frontend/package.json" && -f "$context/frontend/package-lock.json" ]]
grep -qx '<html>tracked context</html>' "$context/frontend/index.html"
[[ "$dockerfile" == "$context/deploy/docker/Dockerfile.frontend-builder" ]]
expected_base='node:22.23.0-slim@sha256:'
expected_base+='d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2'
grep -Fqx "FROM $expected_base AS builder" "$dockerfile"
grep -Fq 'npm ci --prefix frontend --ignore-scripts --no-audit --no-fund' "$dockerfile"
grep -Fq 'RUN --network=none env -i' "$dockerfile"
grep -Fq 'test "$npm_version" = 10.9.8' "$dockerfile"
count=0
[[ ! -f "$FAKE_DOCKER_COUNT" ]] || count=$(cat "$FAKE_DOCKER_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_DOCKER_COUNT"
/usr/bin/python3 - \
  "$destination/frontend-static.tar" \
  "$destination/frontend-toolchain.json" \
  "$count" \
  "${FAKE_DOCKER_DIVERGE_OUTPUT:-}" \
  "${FAKE_DOCKER_DIVERGE_TOOLCHAIN:-}" \
  "${FAKE_DOCKER_NONZERO_TAIL:-}" <<'PY'
import io
import json
import sys
import tarfile
from pathlib import Path

archive_path = Path(sys.argv[1])
toolchain_path = Path(sys.argv[2])
build_number = int(sys.argv[3])
diverge_output = bool(sys.argv[4]) and build_number == 2
diverge_toolchain = bool(sys.argv[5]) and build_number == 2
nonzero_tail = bool(sys.argv[6])
entries = (
    ("assets", None),
    ("assets/app.js", b"asset divergent\\n" if diverge_output else b"asset\\n"),
    ("index.html", b"<html>Ava release</html>\\n"),
)
with tarfile.open(archive_path, mode="w", format=tarfile.USTAR_FORMAT) as archive:
    for name, payload in entries:
        member = tarfile.TarInfo(name)
        member.uid = member.gid = member.mtime = 0
        member.uname = member.gname = ""
        if payload is None:
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        else:
            member.mode = 0o644
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
if nonzero_tail:
    archive_payload = bytearray(archive_path.read_bytes())
    archive_payload[-1] = 0x58
    archive_path.write_bytes(archive_payload)
document = {
    "node_version": "22.23.0",
    "npm_version": "10.9.7" if diverge_toolchain else "10.9.8",
}
toolchain_path.write_text(
    json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    + "\\n",
    encoding="ascii",
)
PY
""",
    )
    fake_deadman = fake_bin / "fake-deadman"
    _write_executable(
        fake_deadman,
        """#!/usr/bin/env bash
set -euo pipefail
command=${1:-}
shift || true
case "$command" in
  probe)
    printf 'ready\n'
    ;;
  inspect-current)
    if [[ -n "${FAKE_AMBIGUOUS_CURRENT_EXISTS_PROBE:-}" \
      || -n "${FAKE_AMBIGUOUS_CURRENT_LINK_PROBE:-}" ]]; then
      exit 255
    fi
    if [[ ! -e "$AVA_CURRENT_LINK" && ! -L "$AVA_CURRENT_LINK" ]]; then
      active=''
      [[ ! -f "$FAKE_ACTIVE_TARGET" ]] || active=$(cat "$FAKE_ACTIVE_TARGET")
      if [[ "$active" == "$AVA_LEGACY_RELEASE" \
        && -d "$AVA_LEGACY_RELEASE" \
        && "$(systemctl is-active openjarvis.service || true)" == active ]]; then
        printf 'legacy:%s\n' "$FAKE_LEGACY_SHA"
      else
        printf 'absent\n'
      fi
    else
      target=$(readlink -f -- "$AVA_CURRENT_LINK")
      if [[ "$target" == "$AVA_LEGACY_RELEASE" ]]; then
        printf 'legacy:%s\n' "$FAKE_LEGACY_SHA"
      elif [[ "$target" == "$AVA_AUTHORITATIVE_RELEASE_ROOT/"* \
        && "${target##*/}" =~ ^[0-9a-f]{40}$ ]]; then
        printf 'sealed:%s\n' "${target##*/}"
      else
        exit 70
      fi
    fi
    ;;
  arm)
    previous=''
    candidate=''
    expectation=''
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --candidate) candidate=$2; shift 2 ;;
        --expect-current) expectation=$2; shift 2 ;;
        --ttl) shift 2 ;;
        *) exit 71 ;;
      esac
    done
    [[ -n "$candidate" && -n "$expectation" && ! -e "$FAKE_DEADMAN_STATE" ]]
    lock="$AVA_RELEASE_ROOT/.deploy-lock"
    [[ -d "$lock" && ! -L "$lock" ]]
    [[ "$(stat -c %a "$lock")" == 700 ]]
    [[ "$(stat -c %u "$lock")" == "$(id -u)" ]]
    [[ -z "$(find "$lock" -mindepth 1 -maxdepth 1 -print -quit)" ]]
    [[ ! -e "$lock/identity-owner.json" && ! -L "$lock/identity-owner.json" ]]
    if [[ -n "${FAKE_ARM_EXPECTATION_MISMATCH:-}" ]]; then
      actual=absent
    else
      actual=$($0 inspect-current)
    fi
    [[ "$actual" == "$expectation" ]]
    case "$expectation" in
      absent) previous='<none>' ;;
      legacy:*) previous="$AVA_LEGACY_RELEASE" ;;
      sealed:*) previous="$AVA_AUTHORITATIVE_RELEASE_ROOT/${expectation#sealed:}" ;;
      *) exit 71 ;;
    esac
    printf '%s\n%s\n' "$previous" "$candidate" > "$FAKE_DEADMAN_STATE"
    printf 'armed\n'
    ;;
  activate)
    candidate=$2
    mapfile -t state < "$FAKE_DEADMAN_STATE"
    [[ "${state[1]}" == "$candidate" ]]
    temporary="${AVA_CURRENT_LINK}.activate.$$"
    ln -s -- "$candidate" "$temporary"
    mv -Tf -- "$temporary" "$AVA_CURRENT_LINK"
    printf '%s\n' "$candidate" > "$FAKE_ACTIVE_TARGET"
    if [[ -n "${FAKE_FAIL_SWITCH_TARGET:-}" \
      && "$candidate" == "$FAKE_FAIL_SWITCH_TARGET" ]]; then
      exit 77
    fi
    if [[ -n "${FAKE_PERSISTENT_CUTOFF_TARGET:-}" \
      && "$candidate" == "$FAKE_PERSISTENT_CUTOFF_TARGET" ]]; then
      touch "$FAKE_SSH_CUTOFF"
      exit 255
    fi
    printf 'activated\n'
    ;;
  confirm)
    candidate=$2
    if [[ ! -e "$FAKE_DEADMAN_STATE" ]]; then
      printf 'not-armed\n'
      exit 0
    fi
    mapfile -t state < "$FAKE_DEADMAN_STATE"
    [[ "${state[1]}" == "$candidate" ]]
    [[ "$(readlink -f -- "$AVA_CURRENT_LINK")" == "$candidate" ]]
    rm -f -- "$FAKE_DEADMAN_STATE"
    printf 'confirmed\n'
    ;;
  cancel)
    candidate=$2
    if [[ ! -e "$FAKE_DEADMAN_STATE" ]]; then
      printf 'not-armed\n'
      exit 0
    fi
    mapfile -t state < "$FAKE_DEADMAN_STATE"
    [[ "${state[1]}" == "$candidate" ]]
    if [[ "${state[0]}" == '<none>' ]]; then
      [[ ! -e "$AVA_CURRENT_LINK" ]]
      [[ "$(systemctl is-active openjarvis.service || true)" =~ ^(failed|inactive)$ ]]
    elif [[ "${state[0]}" == "$AVA_LEGACY_RELEASE" ]]; then
      [[ ! -e "$AVA_CURRENT_LINK" && ! -L "$AVA_CURRENT_LINK" ]]
    else
      [[ "$(readlink -f -- "$AVA_CURRENT_LINK")" == "${state[0]}" ]]
    fi
    rm -f -- "$FAKE_DEADMAN_STATE"
    printf 'cancelled\n'
    ;;
  reclaim-lock)
    [[ "${1:-}" == --stale-after && "${2:-}" =~ ^[0-9]+$ ]] || exit 73
    lock="$AVA_RELEASE_ROOT/.deploy-lock"
    if [[ -e "$FAKE_DEADMAN_STATE" ]]; then
      exit 74
    fi
    if [[ ! -d "$lock" ]]; then
      printf 'not-locked\n'
      exit 0
    fi
    now=$(date +%s)
    modified=$(stat -c %Y "$lock")
    if (( now - modified < $2 )); then
      printf 'fresh\n'
      exit 0
    fi
    rmdir -- "$lock"
    printf 'reclaimed\n'
    ;;
  check|rollback)
    [[ -e "$FAKE_DEADMAN_STATE" ]] || { printf 'not-armed\n'; exit 0; }
    mapfile -t state < "$FAKE_DEADMAN_STATE"
    if [[ "${state[0]}" == '<none>' ]]; then
      systemctl stop openjarvis.service
      rm -f -- "$AVA_CURRENT_LINK"
      : > "$FAKE_ACTIVE_TARGET"
      result='bootstrap-stopped'
    elif [[ "${state[0]}" == "$AVA_LEGACY_RELEASE" ]]; then
      rm -f -- "$AVA_CURRENT_LINK"
      : > "$FAKE_ACTIVE_TARGET"
      systemctl stop openjarvis.service openjarvis-relay.service \
        openjarvis-relay-tls.service
      result='legacy-stopped'
    else
      temporary="${AVA_CURRENT_LINK}.deadman.$$"
      ln -s -- "${state[0]}" "$temporary"
      mv -Tf -- "$temporary" "$AVA_CURRENT_LINK"
      printf '%s\n' "${state[0]}" > "$FAKE_ACTIVE_TARGET"
      systemctl restart openjarvis.service
      result='rolled-back'
    fi
    if [[ -n "${FAKE_DEADMAN_ROLLBACK_FAIL:-}" && "$command" == rollback ]]; then
      exit 75
    fi
    rm -f -- "$FAKE_DEADMAN_STATE"
    rmdir -- "$AVA_RELEASE_ROOT/.deploy-lock"
    printf '%s\n' "$result"
    ;;
  *) exit 72 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == -n ]]; then
  shift
fi
if [[ "${1:-}" == "/usr/local/libexec/avalon/ava-deploy-deadman.py" ]]; then
  shift
  exec "$FAKE_DEADMAN" "$@"
fi
if [[ "${1:-}" == "/usr/local/libexec/avalon/ava-release-seal.py" ]]; then
  shift
  [[ "${1:-}" == seal ]] || exit 81
  printf '%s\t' "$@" >> "$FAKE_SEAL_LOG"
  printf '\n' >> "$FAKE_SEAL_LOG"
  shift
  git_sha=''
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --git-sha) git_sha=$2; shift 2 ;;
      --runtime-wheel-pin) shift 2 ;;
      --release-manifest-sha256|--source-tree-sha256) shift 2 ;;
      --frontend-static-sha256) shift 2 ;;
      --frontend-build-attestation-sha256) shift 2 ;;
      --rust-tree-sha256|--rust-wheel-sha256|--rust-attestation-sha256) shift 2 ;;
      --evolutions-sha256|--uv-lock-sha256|--runtime-requirements-sha256) shift 2 ;;
      --runtime-wheelhouse-manifest-sha256) shift 2 ;;
      --python-runtime-archive|--python-runtime-source-sha256) shift 2 ;;
      --python-runtime-sha256|--uv-sha256|--uv-version) shift 2 ;;
      *) exit 82 ;;
    esac
  done
  [[ "$git_sha" =~ ^[0-9a-f]{40}$ ]]
  staging="$AVA_STAGING_ROOT/$git_sha"
  target="$AVA_AUTHORITATIVE_RELEASE_ROOT/$git_sha"
  if [[ -d "$target" ]]; then
    if find "$target" -xdev -perm /0222 -print -quit | grep -q .; then
      exit 83
    fi
    target_attestation=$(find "$target/.ava-artifacts" -maxdepth 1 -type f \
      -name '*.attestation' -print -quit)
    staging_attestation=$(find "$staging/.ava-artifacts" -maxdepth 1 -type f \
      -name '*.attestation' -print -quit)
    [[ -n "$target_attestation" && -n "$staging_attestation" ]]
    cmp -- "$target_attestation" "$staging_attestation"
    printf 'already-sealed\n'
    exit 0
  fi
  mkdir -p -- "$target" "$target/src/openjarvis/server/static" "$target/.venv/bin"
  tar -xf "$staging/.ava-artifacts/source-tree.tar" -C "$target"
  tar -xf "$staging/.ava-artifacts/frontend-static.tar" \
    -C "$target/src/openjarvis/server/static"
  cp -a -- "$staging/.ava-artifacts" "$target/.ava-artifacts"
  cp -- "$staging/.ava-release" "$target/.ava-release"
  cp -- "$staging/.ava-ready" "$target/.ava-ready"
  cp -- "$FAKE_PYTHON_STUB" "$target/.venv/bin/python"
  chmod 0755 "$target/.venv/bin/python"
  printf '{"format":"ava-sealed-release-v1","git_sha":"%s"}\n' \
    "$git_sha" > "$target/.ava-seal.json"
  chmod -R a-w -- "$target"
  printf 'sealed\n'
  exit 0
fi
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  restart)
    if [[ "${2:-}" == openjarvis || "${2:-}" == openjarvis.service ]]; then
      count=0
      [[ ! -f "$FAKE_RESTART_COUNT" ]] || count=$(cat "$FAKE_RESTART_COUNT")
      printf '%s\n' "$((count + 1))" > "$FAKE_RESTART_COUNT"
    fi
    printf 'active\n' > "$FAKE_SERVICE_STATE"
    ;;
  stop)
    printf 'inactive\n' > "$FAKE_SERVICE_STATE"
    ;;
  is-active)
    cat "$FAKE_SERVICE_STATE"
    ;;
  is-enabled)
    ;;
  show)
    printf '4242\n'
    ;;
  *)
    exit 43
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "readlink",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-f" && "${2:-}" == "--" && "${3:-}" == /proc/*/cwd ]]; then
  if [[ -e "$AVA_CURRENT_LINK" || -L "$AVA_CURRENT_LINK" ]]; then
    current_target=$(/usr/bin/readlink -f -- "$AVA_CURRENT_LINK")
  else
    current_target=$(cat "$FAKE_ACTIVE_TARGET")
  fi
  if [[ -n "${FAKE_MAIN_CWD_OVERRIDE:-}" \
    && ( -z "${FAKE_MAIN_CWD_OVERRIDE_TARGET:-}" \
      || "$current_target" == "$FAKE_MAIN_CWD_OVERRIDE_TARGET" ) ]]; then
    printf '%s\n' "$FAKE_MAIN_CWD_OVERRIDE"
  else
    printf '%s\n' "$current_target"
  fi
  exit
fi
if [[ "${1:-}" == "-f" && "${2:-}" == "--" && "${3:-}" == /proc/*/exe ]]; then
  if [[ -e "$AVA_CURRENT_LINK" || -L "$AVA_CURRENT_LINK" ]]; then
    current_target=$(/usr/bin/readlink -f -- "$AVA_CURRENT_LINK")
  else
    current_target=$(cat "$FAKE_ACTIVE_TARGET")
  fi
  if [[ -n "${FAKE_MAIN_EXE_OVERRIDE:-}" \
    && ( -z "${FAKE_MAIN_EXE_OVERRIDE_TARGET:-}" \
      || "$current_target" == "$FAKE_MAIN_EXE_OVERRIDE_TARGET" ) ]]; then
    printf '%s\n' "$FAKE_MAIN_EXE_OVERRIDE"
  else
    exec /usr/bin/readlink -f -- "$current_target/.venv/bin/python"
  fi
fi
exec /usr/bin/readlink "$@"
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
count=0
[[ ! -f "$FAKE_CURL_COUNT" ]] || count=$(cat "$FAKE_CURL_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_CURL_COUNT"
if [[ -n "${FAKE_FAIL_FIRST_HEALTH:-}" && "$count" -eq 1 ]]; then
  printf '503'
else
  printf '200'
fi
""",
    )
    _write_executable(
        fake_bin / "mkdir",
        """#!/usr/bin/env bash
set -euo pipefail
/usr/bin/mkdir "$@"
""",
    )
    _write_executable(
        fake_bin / "rmdir",
        """#!/usr/bin/env bash
set -euo pipefail
target=${!#}
if [[ -n "${FAKE_FAIL_FIRST_LOCK_RMDIR:-}" \
  && "$target" == "$AVA_RELEASE_ROOT/.deploy-lock" ]]; then
  count=0
  [[ ! -f "$FAKE_LOCK_RMDIR_COUNT" ]] || count=$(cat "$FAKE_LOCK_RMDIR_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_LOCK_RMDIR_COUNT"
  if [[ "$count" -eq 1 ]]; then
    /usr/bin/python3 -c \
      "import os,sys; value=os.getxattr(sys.argv[1], "\
"'user.ava_deploy_token'); sys.stdout.write(value.decode('ascii'))" \
      "$target" > "$FAKE_LOCK_OWNER_BEFORE_RMDIR"
    exit 99
  fi
fi
exec /usr/bin/rmdir "$@"
""",
    )
    _write_executable(
        fake_bin / "chmod",
        """#!/usr/bin/env bash
set -euo pipefail
target=${!#}
if [[ -n "${FAKE_SWITCH_CURRENT_DURING_CLEANUP:-}" \
  && "${1:-}" == "-R" && "${2:-}" == "u+w" && "${3:-}" == "--" \
  && "$target" == "$FAKE_SWITCH_CURRENT_DURING_CLEANUP" ]]; then
  temporary="${AVA_CURRENT_LINK}.race.$$"
  ln -s -- "$FAKE_SWITCH_CURRENT_DURING_CLEANUP" "$temporary"
  mv -Tf -- "$temporary" "$AVA_CURRENT_LINK"
fi
if [[ -n "${FAKE_FAIL_GC_CHMOD_TARGET:-}" \
  && "${1:-}" == "-R" && "${2:-}" == "u+w" && "${3:-}" == "--" \
  && "$target" == "$FAKE_FAIL_GC_CHMOD_TARGET" ]]; then
  printf 'fake GC chmod failure: %s\n' "$target" >&2
  exit 97
fi
exec /usr/bin/chmod "$@"
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AVA_JUMP": "fake-jump",
            "AVA_VM": "fake-vm",
            "AVA_RACINE": str(legacy),
            "AVA_LEGACY_RELEASE": str(legacy),
            "AVA_LEGACY_GIT_SHA": legacy_sha,
            "AVA_STAGING_ROOT": str(staging),
            "AVA_RELEASE_ROOT": str(staging),
            "AVA_AUTHORITATIVE_RELEASE_ROOT": str(releases),
            "AVA_CURRENT_LINK": str(current),
            "AVA_RUST_WHEEL": str(wheel),
            "AVA_RUST_ATTESTATION": str(attestation),
            "AVA_UV": str(fake_bin / "uv"),
            "AVA_SSH_BIN": str(fake_bin / "ssh"),
            "AVA_HEALTH_DELAY_SECONDS": "0",
            "AVA_RELAY_CA": str(relay_ca),
            "AVA_RELAY_TLS_NAME": "ava-relay.test",
            "AVA_PYTHON_RUNTIME_ARCHIVE": str(python_runtime_archive),
            "AVA_PYTHON_RUNTIME_SHA256": python_runtime_sha256,
            "AVA_RELATIONSHIP_POLICY_FILE": str(relationship_policy),
            "FAKE_SSH_MARKER": str(ssh_marker),
            "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
            "FAKE_BUILD_LOG": str(build_log),
            "FAKE_DOCKER_COUNT": str(tmp_path / "docker-count"),
            "FAKE_HOST_NPM_CALLED": str(tmp_path / "host-npm-called"),
            "FAKE_RESTART_COUNT": str(restart_count),
            "FAKE_SERVICE_STATE": str(service_state),
            "FAKE_ACTIVE_TARGET": str(active_target),
            "FAKE_DEADMAN": str(fake_deadman),
            "FAKE_DEADMAN_STATE": str(deadman_state),
            "FAKE_SSH_CUTOFF": str(ssh_cutoff),
            "FAKE_CURL_COUNT": str(tmp_path / "curl-count"),
            "FAKE_PYTHON_STUB": str(python_stub),
            "FAKE_RUNTIME_POLICY_COUNT": str(runtime_policy_count),
            "FAKE_GIT_SHA": commit,
            "FAKE_LEGACY_SHA": legacy_sha,
            "FAKE_SEAL_LOG": str(tmp_path / "seal.log"),
            "FAKE_LOCK_CREATE_PAUSED": str(tmp_path / "lock-create-paused"),
            "FAKE_LOCK_CREATE_RESUME": str(tmp_path / "lock-create-resume"),
            "FAKE_LOCK_CREATE_FINISHED": str(tmp_path / "lock-create-finished"),
            "FAKE_LOCK_CREATE_STATUS": str(tmp_path / "lock-create-status"),
            "FAKE_PUBLISHED_LOCK_SNAPSHOT": str(tmp_path / "published-lock-snapshot"),
            "FAKE_LOCK_RELEASE_COUNT": str(tmp_path / "lock-release-count"),
            "FAKE_LOCK_RMDIR_COUNT": str(tmp_path / "lock-rmdir-count"),
            "FAKE_LOCK_OWNER_BEFORE_RMDIR": str(tmp_path / "lock-owner-before-rmdir"),
        }
    )
    return DeploymentFixture(
        repo=repo,
        remote=remote,
        legacy=legacy,
        staging=staging,
        releases=releases,
        current=current,
        wheel=wheel,
        attestation=attestation,
        fake_bin=fake_bin,
        environment=environment,
        commit=commit,
        ssh_marker=ssh_marker,
        build_log=build_log,
        restart_count=restart_count,
        service_state=service_state,
        deadman_state=deadman_state,
        ssh_cutoff=ssh_cutoff,
        fake_deadman=fake_deadman,
        runtime_policy_count=runtime_policy_count,
    )


def _deploy(
    fixture: DeploymentFixture,
    *arguments: str,
    **environment_overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = fixture.environment.copy()
    environment.update(environment_overrides)
    return subprocess.run(
        ["bash", str(fixture.repo / "scripts" / SCRIPT.name), *arguments],
        cwd=fixture.repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_exact_legacy_is_active(fixture: DeploymentFixture) -> None:
    assert not fixture.current.exists()
    assert not fixture.current.is_symlink()
    active_target = Path(fixture.environment["FAKE_ACTIVE_TARGET"])
    assert active_target.read_text().strip() == str(fixture.legacy)
    inspection = subprocess.run(
        [str(fixture.fake_deadman), "inspect-current"],
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspection.returncode == 0, inspection.stderr
    assert inspection.stdout.strip() == f"legacy:{fixture.legacy.name}"


def _assert_fail_closed_stopped(fixture: DeploymentFixture) -> None:
    assert not fixture.current.exists()
    assert not fixture.current.is_symlink()
    assert Path(fixture.environment["FAKE_ACTIVE_TARGET"]).read_text() == ""
    assert fixture.service_state.read_text().strip() == "inactive"


def _remove_immutable_legacy_fixture(fixture: DeploymentFixture) -> None:
    for path in (fixture.legacy, *fixture.legacy.rglob("*")):
        path.chmod(path.stat().st_mode | 0o700)
    shutil.rmtree(fixture.legacy)


def test_dirty_checkout_is_rejected_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    (fixture.repo / "uncommitted.txt").write_text("must never reach the VM\n")

    result = _deploy(fixture)

    assert result.returncode != 0
    assert "modifications non commitees" in result.stdout
    assert "Aucun acces distant n'a ete tente" in result.stdout
    assert not fixture.ssh_marker.exists()


def test_invalid_rust_attestation_is_rejected_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.wheel.write_bytes(b"wheel modified after attestation\n")

    result = _deploy(fixture)

    assert result.returncode != 0
    assert "wheel Rust ne correspond pas" in result.stderr
    assert not fixture.ssh_marker.exists()


def test_unknown_argument_is_rejected_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, "--unknown")

    assert result.returncode != 0
    assert "argument inconnu: --unknown" in result.stderr
    assert not fixture.ssh_marker.exists()


def test_prepare_only_materializes_immutable_release_without_active_mutation(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    current_inode = fixture.current.lstat().st_ino
    stale_releases = _make_collectable_releases(fixture.releases)
    stale_state = {
        release: (
            release.stat().st_mode,
            (release / ".ava-ready").read_bytes(),
            (release / ".ava-release").read_bytes(),
        )
        for release in stale_releases
    }

    first = _deploy(
        fixture,
        "--prepare-only",
        AVA_RELEASE_KEEP="2",
        FAKE_FAIL_GC_CHMOD_TARGET=str(stale_releases[0]),
        FAKE_FAIL_SWITCH_TARGET=str(fixture.releases / fixture.commit),
    )
    build_log = fixture.build_log.read_bytes()
    second = _deploy(
        fixture,
        "--prepare-only",
        AVA_RELEASE_KEEP="2",
        FAKE_FAIL_GC_CHMOD_TARGET=str(stale_releases[0]),
        FAKE_FAIL_SWITCH_TARGET=str(fixture.releases / fixture.commit),
    )

    release = fixture.releases / fixture.commit
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "preparee, immutable et non activee" in first.stdout
    assert "release immutable existante reutilisee" in second.stdout
    assert fixture.current.is_symlink()
    assert fixture.current.lstat().st_ino == current_inode
    assert fixture.current.resolve() == fixture.legacy
    assert fixture.service_state.read_text().strip() == "active"
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not Path(fixture.environment["FAKE_CURL_COUNT"]).exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.build_log.read_bytes() != build_log
    assert fixture.build_log.read_bytes().count(b"docker|") == 4
    assert not Path(fixture.environment["FAKE_HOST_NPM_CALLED"]).exists()
    assert b"uv|" not in fixture.build_log.read_bytes()
    seal_log = Path(fixture.environment["FAKE_SEAL_LOG"]).read_text()
    assert "--uv-version\tuv 0.12.5 (x86_64-unknown-linux-gnu)" in seal_log
    assert "--frontend-static-sha256" in seal_log
    assert "--frontend-build-attestation-sha256" in seal_log
    assert "--runtime-wheelhouse-manifest-sha256" in seal_log
    assert "--runtime-wheel-pin\tdocopt-0.6.2-py2.py3-none-any.whl=" in seal_log

    assert release.is_dir()
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    manifest = (release / ".ava-release").read_text().splitlines()
    assert len(manifest) == 8
    assert manifest[0] == "format=ava-release-v1"
    assert manifest[1] == f"git_sha={fixture.commit}"
    checksum_lines = (manifest[2], manifest[3], manifest[4], manifest[6], manifest[7])
    assert all("sha256=" in line for line in checksum_lines)
    assert not (release / ".ava-building").exists()
    assert not (release / ".git").exists()
    for path in (release, *release.rglob("*")):
        assert not path.lstat().st_mode & 0o222, f"writable release path: {path}"

    staging_release = fixture.staging / fixture.commit
    wheelhouse = staging_release / ".ava-artifacts/python-wheelhouse"
    wheel_names = {path.name for path in wheelhouse.iterdir()}
    assert "docopt-0.6.2-py2.py3-none-any.whl" in wheel_names
    assert "dependency-1.0-py3-none-any.whl" in wheel_names
    assert all(name.endswith(".whl") for name in wheel_names)
    assert not any("triton" in name or "nvidia_cuda" in name for name in wheel_names)
    assert not (staging_release / ".python-wheel-downloader").exists()
    for path in (staging_release, *staging_release.rglob("*")):
        assert not path.lstat().st_mode & 0o222, f"writable staging path: {path}"

    for stale_release, expected in stale_state.items():
        mode, ready, manifest = expected
        assert stale_release.is_dir()
        assert stale_release.stat().st_mode == mode
        assert (stale_release / ".ava-ready").read_bytes() == ready
        assert (stale_release / ".ava-release").read_bytes() == manifest

    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log
    assert "curl " not in ssh_log
    assert "ln -s --" not in ssh_log
    assert "mv -Tf --" not in ssh_log
    assert "chmod -R u+w" not in ssh_log
    assert "-mindepth 1 -maxdepth 1 -type d -printf" not in ssh_log
    assert "--require-hashes --only-binary=:all:" in ssh_log
    assert "--no-deps --index-url https://pypi.org/simple" in ssh_log
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in ssh_log
    assert "--no-cache-dir" in ssh_log
    assert "# ava-lock-release-v2" in ssh_log


def test_prepare_only_seals_baseline_a_but_full_deploy_never_arms_it(
    tmp_path: Path,
) -> None:
    prepared = _make_deployment_fixture(
        tmp_path / "prepare",
        relationship_treatment="shadow-baseline-only-v1",
    )

    prepare_result = _deploy(prepared, "--prepare-only")

    assert prepare_result.returncode == 0, prepare_result.stdout + prepare_result.stderr
    assert (prepared.releases / prepared.commit / ".ava-seal.json").is_file()
    assert not prepared.current.exists()
    assert not prepared.restart_count.exists()
    prepare_ssh = Path(prepared.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-release-seal.py" in prepare_ssh
    assert "ava-deploy-deadman.py" not in prepare_ssh

    refused = _make_deployment_fixture(
        tmp_path / "refused",
        relationship_treatment="shadow-baseline-only-v1",
    )

    full_result = _deploy(refused)

    assert full_result.returncode != 0
    assert "baseline A n'est jamais servable" in full_result.stderr
    assert not refused.ssh_marker.exists()
    assert not refused.current.exists()
    assert not refused.restart_count.exists()
    assert not refused.deadman_state.exists()


def test_frontend_archive_is_canonical_ustar_with_explicit_seal_digest(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, "--prepare-only")

    assert result.returncode == 0, result.stdout + result.stderr
    release = fixture.releases / fixture.commit
    frontend_archive = release / ".ava-artifacts/frontend-static.tar"
    payload = frontend_archive.read_bytes()
    assert payload[257:263] == b"ustar\0"
    with tarfile.open(frontend_archive, mode="r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [
        "assets",
        "assets/app.js",
        "index.html",
    ]
    for member in members:
        assert member.uid == member.gid == member.mtime == 0
        assert member.uname == member.gname == ""
        assert not member.pax_headers
        assert member.mode == (0o755 if member.isdir() else 0o644)

    seal_tokens = (
        Path(fixture.environment["FAKE_SEAL_LOG"])
        .read_text()
        .rstrip("\n\t")
        .split("\t")
    )
    assert seal_tokens[0] == "seal"
    seal_arguments = dict(zip(seal_tokens[1::2], seal_tokens[2::2], strict=True))
    assert (
        seal_arguments["--frontend-static-sha256"]
        == hashlib.sha256(payload).hexdigest()
    )

    artifacts = release / ".ava-artifacts"
    source_archive = artifacts / "source-tree.tar"
    source_rows: list[dict[str, str | int]] = []
    package_payloads: dict[str, bytes] = {}
    with tarfile.open(source_archive, mode="r:") as archive:
        for member in archive.getmembers():
            path = member.name.rstrip("/")
            if not path.startswith("frontend/"):
                continue
            if member.isdir():
                source_rows.append({"mode": "0555", "path": path, "type": "directory"})
                continue
            assert member.isreg()
            stream = archive.extractfile(member)
            assert stream is not None
            content = stream.read()
            source_rows.append(
                {
                    "mode": "0555" if member.mode & 0o111 else "0444",
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "type": "file",
                }
            )
            if path in {"frontend/package.json", "frontend/package-lock.json"}:
                package_payloads[path] = content
    source_rows.sort(key=lambda row: str(row["path"]))
    assert source_rows
    assert all(str(row["path"]).startswith("frontend/") for row in source_rows)
    assert not any(row["path"] == "frontend" for row in source_rows)

    archive_rows: list[dict[str, str | int]] = []
    with tarfile.open(frontend_archive, mode="r:") as archive:
        for member in archive.getmembers():
            path = member.name.rstrip("/")
            if member.isdir():
                archive_rows.append({"mode": "0555", "path": path, "type": "directory"})
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            content = stream.read()
            archive_rows.append(
                {
                    "mode": "0444",
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "type": "file",
                }
            )

    def map_digest(rows: list[dict[str, str | int]]) -> str:
        encoded = json.dumps(
            rows,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    frontend_attestation = artifacts / "frontend-build-attestation.json"
    attestation_payload = frontend_attestation.read_bytes()
    document = json.loads(attestation_payload)
    assert attestation_payload == (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    assert document == {
        "builder": {
            "base_image": (
                "node:22.23.0-slim@sha256:"
                "d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2"
            ),
            "dockerfile_path": "deploy/docker/Dockerfile.frontend-builder",
            "dockerfile_sha256": "sha256:"
            + hashlib.sha256(
                (
                    fixture.repo / "deploy/docker/Dockerfile.frontend-builder"
                ).read_bytes()
            ).hexdigest(),
            "node_version": "22.23.0",
            "npm_version": "10.9.8",
            "platform": "linux/amd64",
        },
        "frontend_source_map_sha256": "sha256:" + map_digest(source_rows),
        "git_sha": fixture.commit,
        "output": {
            "archive_map_sha256": "sha256:" + map_digest(archive_rows),
            "archive_path": "frontend-static.tar",
            "archive_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "build_count": 2,
        },
        "package_json_sha256": "sha256:"
        + hashlib.sha256(package_payloads["frontend/package.json"]).hexdigest(),
        "package_lock_sha256": "sha256:"
        + hashlib.sha256(package_payloads["frontend/package-lock.json"]).hexdigest(),
        "schema_version": "ava.frontend.build-attestation/v1",
        "source_archive_sha256": "sha256:"
        + hashlib.sha256(source_archive.read_bytes()).hexdigest(),
    }
    assert (
        seal_arguments["--frontend-build-attestation-sha256"]
        == hashlib.sha256(attestation_payload).hexdigest()
    )
    docker_lines = fixture.build_log.read_text().splitlines()
    assert len(docker_lines) == 2
    assert all(line.startswith("docker|") for line in docker_lines)
    assert all(
        "build --pull --no-cache --platform linux/amd64" in line
        for line in docker_lines
    )
    assert all("--output type=local,dest=" in line for line in docker_lines)
    assert not Path(fixture.environment["FAKE_HOST_NPM_CALLED"]).exists()


def test_frontend_dependency_fetch_is_locked_but_bundle_build_is_offline() -> None:
    dockerfile = (ROOT / "deploy/docker/Dockerfile.frontend-builder").read_text()
    install_phase = dockerfile.split(
        "COPY frontend/package.json frontend/package-lock.json frontend/", 1
    )[1].split("COPY frontend/ frontend/", 1)[0]
    build_phase = dockerfile.split("COPY frontend/ frontend/", 1)[1].split(
        "RUN static_root=", 1
    )[0]

    assert "npm ci --prefix frontend --ignore-scripts --no-audit --no-fund" in (
        install_phase
    )
    assert "--network=none" not in install_phase
    assert "RUN --network=none env -i" in build_phase
    assert "npm --prefix frontend run build" in build_phase
    assert dockerfile.count("RUN --network=none") == 1


def test_divergent_frontend_builds_are_refused_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, "--prepare-only", FAKE_DOCKER_DIVERGE_OUTPUT="1")

    assert result.returncode != 0
    assert "deux builds frontend ne sont pas octet-identiques" in result.stderr
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.ssh_marker.exists()
    assert not Path(fixture.environment["FAKE_HOST_NPM_CALLED"]).exists()


def test_nonzero_bytes_after_frontend_tar_eof_are_refused_before_any_ssh(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_DOCKER_NONZERO_TAIL="1",
    )

    assert result.returncode != 0
    assert "remplissage final USTAR frontend non canonique" in result.stderr
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.ssh_marker.exists()
    assert not Path(fixture.environment["FAKE_HOST_NPM_CALLED"]).exists()


def test_divergent_frontend_toolchains_are_refused_before_any_ssh(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_DOCKER_DIVERGE_TOOLCHAIN="1",
    )

    assert result.returncode != 0
    assert "chaine Node/npm a diverge" in result.stderr
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.ssh_marker.exists()
    assert not Path(fixture.environment["FAKE_HOST_NPM_CALLED"]).exists()


def test_seal_helper_receives_only_the_complete_frozen_pin_set(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, "--prepare-only")

    assert result.returncode == 0, result.stdout + result.stderr
    release = fixture.releases / fixture.commit
    artifacts = release / ".ava-artifacts"
    tokens = (
        Path(fixture.environment["FAKE_SEAL_LOG"])
        .read_text()
        .rstrip("\n\t")
        .split("\t")
    )
    assert tokens[0] == "seal"
    actual = dict(zip(tokens[1::2], tokens[2::2], strict=True))

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    runtime_root = fixture.repo / "deploy/runtime"
    expected = {
        "--git-sha": fixture.commit,
        "--release-manifest-sha256": digest(release / ".ava-release"),
        "--source-tree-sha256": digest(artifacts / "source-tree.tar"),
        "--frontend-static-sha256": digest(artifacts / "frontend-static.tar"),
        "--frontend-build-attestation-sha256": digest(
            artifacts / "frontend-build-attestation.json"
        ),
        "--rust-tree-sha256": digest(artifacts / "rust-tree.tar"),
        "--rust-wheel-sha256": digest(fixture.wheel),
        "--rust-attestation-sha256": digest(fixture.attestation),
        "--evolutions-sha256": digest(artifacts / "evolutions-v1.json"),
        "--uv-lock-sha256": digest(fixture.repo / "uv.lock"),
        "--runtime-requirements-sha256": digest(
            runtime_root / "ava-runtime-requirements.v1.txt"
        ),
        "--runtime-wheelhouse-manifest-sha256": digest(
            runtime_root / "ava-runtime-wheelhouse.v1.json"
        ),
        "--runtime-wheel-pin": (
            "docopt-0.6.2-py2.py3-none-any.whl="
            + digest(runtime_root / "wheels/docopt-0.6.2-py2.py3-none-any.whl")
        ),
        "--python-runtime-archive": fixture.environment["AVA_PYTHON_RUNTIME_ARCHIVE"],
        "--python-runtime-source-sha256": digest(
            runtime_root / "ava-python-runtime.v1.json"
        ),
        "--python-runtime-sha256": fixture.environment["AVA_PYTHON_RUNTIME_SHA256"],
        "--uv-sha256": (
            "b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46"
        ),
        "--uv-version": "uv 0.12.5 (x86_64-unknown-linux-gnu)",
    }
    assert actual == expected


@pytest.mark.parametrize("mutation", ["missing", "extra", "name", "hash"])
def test_remote_wheelhouse_must_match_the_committed_manifest_before_seal(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_WHEELHOUSE_MUTATION=mutation,
    )

    assert result.returncode != 0
    assert "wheelhouse distant divergent du manifeste source canonique" in result.stderr
    seal_log = Path(fixture.environment["FAKE_SEAL_LOG"])
    assert not seal_log.exists() or not seal_log.read_bytes()
    assert not (fixture.releases / fixture.commit).exists()


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("noncanonical", "manifeste wheelhouse non canonique"),
        ("requirements", "divergent du target, requirements ou uv.lock"),
        ("uv-lock", "divergent du target, requirements ou uv.lock"),
    ],
)
def test_invalid_committed_wheelhouse_manifest_is_refused_before_any_ssh(
    tmp_path: Path, mutation: str, message: str
) -> None:
    fixture = _make_deployment_fixture(
        tmp_path,
        runtime_manifest_mutation=mutation,
    )

    result = _deploy(fixture, "--prepare-only")

    assert result.returncode != 0
    assert message in result.stderr
    assert not fixture.ssh_marker.exists()


def test_prepare_only_never_reclaims_an_existing_lock_through_deadman(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    lock = fixture.staging / ".deploy-lock"
    lock.mkdir(mode=0o700)
    old = time.time() - 1_000
    os.utime(lock, (old, old))

    result = _deploy(
        fixture,
        "--prepare-only",
        AVA_DEPLOY_LOCK_STALE_SECONDS="300",
    )

    assert result.returncode != 0
    assert "detient deja le verrou" in result.stderr
    assert lock.is_dir()
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log


def test_prepare_only_signal_after_lock_release_never_deletes_ready_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_SIGNAL_AFTER_LOCK_RELEASE="1",
    )

    release = fixture.releases / fixture.commit
    assert result.returncode == 143, result.stdout + result.stderr
    assert release.is_dir()
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert (release / ".ava-release").is_file()
    assert not (release / ".ava-building").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert "preparee, immutable et non activee" not in result.stdout
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert f"chmod -R u+w -- {release}" not in ssh_log
    assert f"rm -rf -- {release}\n" not in ssh_log


def test_prepare_only_interrupted_rmdir_is_retried_with_owner_intact(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    fault_dir = tmp_path / "release-rmdir-fault"
    fault_dir.mkdir()
    (fault_dir / "sitecustomize.py").write_text(
        """import errno
import os

real_rmdir = os.rmdir

def interrupted_rmdir(path, *arguments, **keywords):
    target = os.fspath(path)
    if target.endswith('.release'):
        count_path = os.environ['FAKE_LOCK_RMDIR_COUNT']
        count = 0
        if os.path.exists(count_path):
            count = int(open(count_path, encoding='utf-8').read())
        count += 1
        with open(count_path, 'w', encoding='utf-8') as stream:
            stream.write(f'{count}\\n')
        if count == 1:
            owner = os.getxattr(target, 'user.ava_deploy_token').decode('ascii')
            owner_path = os.environ['FAKE_LOCK_OWNER_BEFORE_RMDIR']
            with open(owner_path, 'w', encoding='utf-8') as stream:
                stream.write(owner)
            raise OSError(errno.EINTR, 'injected rmdir interruption')
    return real_rmdir(path, *arguments, **keywords)

os.rmdir = interrupted_rmdir
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
    )

    release = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "verrou de preparation distant non libere" in result.stderr
    assert release.is_dir()
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (release / ".ava-building").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    owner = Path(fixture.environment["FAKE_LOCK_OWNER_BEFORE_RMDIR"]).read_text()
    assert len(owner) == 64
    assert set(owner) <= set("0123456789abcdef")
    assert int(Path(fixture.environment["FAKE_LOCK_RMDIR_COUNT"]).read_text()) == 2
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_signal_after_lock_creation_cleans_owned_lock(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_SIGNAL_AFTER_LOCK_CREATE="1",
    )

    assert result.returncode == 143, result.stdout + result.stderr
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    owner, entries = (
        Path(fixture.environment["FAKE_PUBLISHED_LOCK_SNAPSHOT"])
        .read_text()
        .splitlines()
    )
    assert len(owner) == 64
    assert set(owner) <= set("0123456789abcdef")
    assert entries == "0"
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "# ava-lock-release-v2" in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log


def test_prepare_only_ambiguous_lock_creation_reprobes_token_before_cleanup(
    tmp_path: Path,
) -> None:
    for status in ("130", "255"):
        fixture = _make_deployment_fixture(tmp_path / status)

        result = _deploy(
            fixture,
            "--prepare-only",
            FAKE_AMBIGUOUS_LOCK_CREATE_SIGNAL="1",
            FAKE_AMBIGUOUS_LOCK_CREATE_STATUS=status,
        )

        assert result.returncode == 143, result.stdout + result.stderr
        assert not (fixture.staging / ".deploy-lock").exists()
        assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
        assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
        assert not list(fixture.staging.glob(".deploy-lock.*.control"))
        assert not (fixture.releases / fixture.commit).exists()
        assert not fixture.current.exists()
        ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
        assert "os.getxattr" in ssh_log
        assert "os.removexattr" not in ssh_log
        assert "ava-deploy-deadman.py" not in ssh_log


def test_prepare_only_late_lock_creation_after_ssh_255_is_settled_and_cleaned(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fault_dir = tmp_path / "delayed-lock-publication"
    fault_dir.mkdir()
    (fault_dir / "sitecustomize.py").write_text(
        """import ctypes
import errno
import os
import time

real_cdll = ctypes.CDLL

class RenameAt2:
    argtypes = None
    restype = None

    def __init__(self, function):
        self.function = function

    def __call__(self, old_dirfd, source, new_dirfd, destination, flags):
        source_path = os.fsdecode(source)
        destination_path = os.fsdecode(destination)
        if source_path.endswith('.tmp') and destination_path.endswith('/.deploy-lock'):
            open(os.environ['FAKE_LOCK_CREATE_PAUSED'], 'wb').close()
            for _ in range(3000):
                if os.path.exists(os.environ['FAKE_LOCK_CREATE_RESUME']):
                    break
                time.sleep(0.01)
            else:
                ctypes.set_errno(errno.ETIMEDOUT)
                return -1
        return self.function(old_dirfd, source, new_dirfd, destination, flags)

class Library:
    def __init__(self, library):
        self.library = library
        self.renameat2 = RenameAt2(library.renameat2)

    def __getattr__(self, name):
        return getattr(self.library, name)

def wrapped_cdll(*arguments, **keywords):
    return Library(real_cdll(*arguments, **keywords))

ctypes.CDLL = wrapped_cdll
"""
    )

    started = time.monotonic()
    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_AMBIGUOUS_LOCK_CREATE_STATUS="255",
        FAKE_DELAYED_LOCK_CREATE="1",
        FAKE_LOCK_CREATE_PAUSE_SECONDS="15",
        PYTHONPATH=str(fault_dir),
    )
    deploy_elapsed = time.monotonic() - started

    finished = Path(fixture.environment["FAKE_LOCK_CREATE_FINISHED"])
    deadline = time.monotonic() + 20
    while not finished.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    lock = fixture.staging / ".deploy-lock"
    assert result.returncode != 0
    assert deploy_elapsed < 5, "l'annulation depend encore de la reprise du creator"
    assert finished.is_file(), "la creation distante tardive n'a pas repris"
    create_status = Path(fixture.environment["FAKE_LOCK_CREATE_STATUS"])
    assert create_status.read_text().strip() != "0"
    assert not lock.exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "# ava-lock-cancel-v1" in ssh_log
    assert "renameat2" in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log


def test_prepare_only_ssh_255_before_creator_starts_removes_control_file(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_LOCK_CREATE_NEVER_STARTS="1",
    )

    assert result.returncode != 0
    assert "publication du verrou Ava annulee" in result.stderr
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_ssh_255_after_preflight_is_cancelled_without_orphan(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_AMBIGUOUS_LOCK_PREFLIGHT_STATUS="255",
    )

    assert result.returncode != 0
    assert "preflight du controle de verrou Ava non confirme" in result.stderr
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_sigkill_during_preflight_after_empty_marker_is_recovered(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fault_dir = tmp_path / "empty-marker-kill"
    fault_dir.mkdir()
    (fault_dir / "sitecustomize.py").write_text(
        """import os
import signal

real_open = os.open

def kill_after_marker_create(path, flags, *arguments, **keywords):
    descriptor = real_open(path, flags, *arguments, **keywords)
    if os.fspath(path).endswith('.ava-deploy-owner') and flags & os.O_CREAT:
        os.kill(os.getpid(), signal.SIGKILL)
    return descriptor

os.open = kill_after_marker_create
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
    )

    assert result.returncode != 0
    assert "preflight du controle de verrou Ava non confirme" in result.stderr
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.release"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_sigkill_during_partial_ready_control_is_recovered(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fault_dir = tmp_path / "partial-ready-control-kill"
    fault_dir.mkdir()
    kill_marker = tmp_path / "partial-ready-control-killed"
    (fault_dir / "sitecustomize.py").write_text(
        """import os
import signal

real_write = os.write

def kill_after_partial_control_write(descriptor, payload):
    try:
        target = os.readlink(f'/proc/self/fd/{descriptor}')
    except OSError:
        target = ''
    expected = os.environ['FAKE_PARTIAL_CONTROL_STATE'].encode('ascii') + b':'
    if target.endswith('.control') and payload.startswith(expected):
        real_write(descriptor, payload[:len(expected) - 1])
        open(os.environ['FAKE_PARTIAL_CONTROL_KILL_MARKER'], 'wb').close()
        os.kill(os.getpid(), signal.SIGKILL)
    return real_write(descriptor, payload)

os.write = kill_after_partial_control_write
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
        FAKE_PARTIAL_CONTROL_STATE="ready",
        FAKE_PARTIAL_CONTROL_KILL_MARKER=str(kill_marker),
    )

    assert result.returncode != 0
    assert "preflight du controle de verrou Ava non confirme" in result.stderr
    assert kill_marker.is_file()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.release"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_sigkill_after_lock_publication_recovers_empty_marker(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    fault_dir = tmp_path / "published-empty-marker-kill"
    fault_dir.mkdir()
    kill_marker = tmp_path / "published-empty-marker-killed"
    (fault_dir / "sitecustomize.py").write_text(
        """import os
import signal

real_unlink = os.unlink

def kill_before_published_marker_unlink(path, *arguments, **keywords):
    marker = os.environ['FAKE_PUBLISHED_EMPTY_MARKER_KILL']
    if (
        os.fspath(path) == '.ava-deploy-owner'
        and keywords.get('dir_fd') is not None
        and not os.path.exists(marker)
    ):
        with open(marker, 'wb'):
            pass
        os.kill(os.getpid(), signal.SIGKILL)
    return real_unlink(path, *arguments, **keywords)

os.unlink = kill_before_published_marker_unlink
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
        FAKE_PUBLISHED_EMPTY_MARKER_KILL=str(kill_marker),
    )

    release = fixture.releases / fixture.commit
    assert result.returncode == 0, result.stdout + result.stderr
    assert kill_marker.is_file()
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_sigkill_during_partial_published_control_is_recovered(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    current_inode = fixture.current.lstat().st_ino
    fault_dir = tmp_path / "partial-published-control-kill"
    fault_dir.mkdir()
    kill_marker = tmp_path / "partial-published-control-killed"
    (fault_dir / "sitecustomize.py").write_text(
        """import os
import signal

real_write = os.write

def kill_after_partial_control_write(descriptor, payload):
    try:
        target = os.readlink(f'/proc/self/fd/{descriptor}')
    except OSError:
        target = ''
    expected = os.environ['FAKE_PARTIAL_CONTROL_STATE'].encode('ascii') + b':'
    if target.endswith('.control') and payload.startswith(expected):
        real_write(descriptor, payload[:len(expected) - 1])
        open(os.environ['FAKE_PARTIAL_CONTROL_KILL_MARKER'], 'wb').close()
        os.kill(os.getpid(), signal.SIGKILL)
    return real_write(descriptor, payload)

os.write = kill_after_partial_control_write
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
        FAKE_PARTIAL_CONTROL_STATE="published",
        FAKE_PARTIAL_CONTROL_KILL_MARKER=str(kill_marker),
    )

    release = fixture.releases / fixture.commit
    assert result.returncode == 0, result.stdout + result.stderr
    assert kill_marker.is_file()
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (release / ".ava-building").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.release"))
    assert fixture.current.is_symlink()
    assert fixture.current.lstat().st_ino == current_inode
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_interrupted_cancel_tombstone_is_reaped_by_retry(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fault_dir = tmp_path / "cancel-rmdir-interruption"
    fault_dir.mkdir()
    interruption_marker = tmp_path / "cancel-rmdir-interrupted"
    (fault_dir / "sitecustomize.py").write_text(
        """import errno
import os

real_rmdir = os.rmdir

def interrupt_cancel_rmdir(path, *arguments, **keywords):
    target = os.fspath(path)
    marker = os.environ['FAKE_CANCEL_RMDIR_INTERRUPTED']
    if target.endswith('.cancel') and not os.path.exists(marker):
        with open(marker, 'wb'):
            pass
        raise OSError(errno.EINTR, 'injected cancel rmdir interruption')
    return real_rmdir(path, *arguments, **keywords)

os.rmdir = interrupt_cancel_rmdir
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_LOCK_CREATE_NEVER_STARTS="1",
        FAKE_CANCEL_RMDIR_INTERRUPTED=str(interruption_marker),
        PYTHONPATH=str(fault_dir),
    )

    assert result.returncode != 0
    assert "publication du verrou Ava annulee" in result.stderr
    assert interruption_marker.is_file()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_renameat2_absent_or_eperm_fails_closed_without_lock_or_temp_orphan(
    tmp_path: Path,
) -> None:
    for mode in ("absent", "eperm"):
        fixture = _make_deployment_fixture(tmp_path / mode)
        fault_dir = tmp_path / mode / "renameat2-fault"
        fault_dir.mkdir()
        (fault_dir / "sitecustomize.py").write_text(
            """import ctypes
import errno
import os

mode = os.environ.get("FAKE_RENAMEAT2_MODE")

class RenameAt2:
    argtypes = None
    restype = None

    def __call__(self, *arguments):
        del arguments
        ctypes.set_errno(errno.EPERM)
        return -1

class Library:
    pass

library = Library()
if mode == "eperm":
    library.renameat2 = RenameAt2()
if mode in {"absent", "eperm"}:
    ctypes.CDLL = lambda *arguments, **keywords: library
"""
        )

        result = _deploy(
            fixture,
            "--prepare-only",
            FAKE_RENAMEAT2_MODE=mode,
            PYTHONPATH=str(fault_dir),
        )

        assert result.returncode != 0
        assert "preflight du controle de verrou Ava non confirme" in result.stderr
        assert not (fixture.staging / ".deploy-lock").exists()
        assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
        assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
        assert not list(fixture.staging.glob(".deploy-lock.*.control"))
        assert not (fixture.releases / fixture.commit).exists()
        assert not fixture.current.exists()
        assert fixture.build_log.read_bytes().count(b"docker|") == 2
        assert not fixture.restart_count.exists()
        assert not fixture.deadman_state.exists()
        ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
        assert "renameat2" in ssh_log
        assert "ava-deploy-deadman.py" not in ssh_log
        assert "systemctl" not in ssh_log


def test_prepare_only_never_removes_a_lock_owned_by_another_token(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    lock = fixture.staging / ".deploy-lock"
    lock.mkdir(mode=0o700)
    foreign_token = "f" * 64
    marker = lock / ".ava-deploy-owner"
    marker.write_text(f"{foreign_token}\n")
    marker.chmod(0o600)
    os.setxattr(lock, b"user.ava_deploy_token", foreign_token.encode())

    result = _deploy(fixture, "--prepare-only")

    assert result.returncode != 0
    assert "detient deja le verrou" in result.stderr
    assert lock.is_dir()
    assert marker.read_text().strip() == foreign_token
    assert os.getxattr(lock, b"user.ava_deploy_token") == foreign_token.encode()
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not (fixture.releases / fixture.commit).exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "printf removed" not in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log


def test_lock_publication_inode_swap_preserves_foreign_final_without_restore(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fault_dir = tmp_path / "publish-inode-swap"
    fault_dir.mkdir()
    saved_lock = fixture.staging / ".owned-lock-moved-by-publish-race"
    race_marker = tmp_path / "publish-inode-race-fired"
    foreign_token = "d" * 64
    (fault_dir / "sitecustomize.py").write_text(
        """import ctypes
import os

real_cdll = ctypes.CDLL

class RenameAt2:
    argtypes = None
    restype = None

    def __init__(self, function):
        self.function = function

    def __call__(self, old_dirfd, source, new_dirfd, destination, flags):
        result = self.function(old_dirfd, source, new_dirfd, destination, flags)
        source_path = os.fsdecode(source)
        destination_path = os.fsdecode(destination)
        race_marker = os.environ['FAKE_PUBLISH_SWAP_MARKER']
        if (
            result == 0
            and source_path.endswith('.tmp')
            and destination_path.endswith('/.deploy-lock')
            and not os.path.exists(race_marker)
        ):
            os.rename(destination_path, os.environ['FAKE_PUBLISH_SWAP_SAVED'])
            os.mkdir(destination_path, 0o700)
            os.setxattr(
                destination_path,
                'user.ava_deploy_token',
                os.environ['FAKE_PUBLISH_SWAP_FOREIGN_TOKEN'].encode('ascii'),
            )
            with open(race_marker, 'wb'):
                pass
        return result

class Library:
    def __init__(self, library):
        self.library = library
        self.renameat2 = RenameAt2(library.renameat2)

    def __getattr__(self, name):
        return getattr(self.library, name)

def wrapped_cdll(*arguments, **keywords):
    return Library(real_cdll(*arguments, **keywords))

ctypes.CDLL = wrapped_cdll
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
        FAKE_PUBLISH_SWAP_MARKER=str(race_marker),
        FAKE_PUBLISH_SWAP_SAVED=str(saved_lock),
        FAKE_PUBLISH_SWAP_FOREIGN_TOKEN=foreign_token,
    )

    lock = fixture.staging / ".deploy-lock"
    assert result.returncode != 0
    assert "detient deja le verrou" in result.stderr
    assert race_marker.is_file()
    assert lock.is_dir()
    assert os.getxattr(lock, b"user.ava_deploy_token") == foreign_token.encode()
    assert saved_lock.is_dir()
    saved_owner = os.getxattr(saved_lock, b"user.ava_deploy_token").decode()
    assert len(saved_owner) == 64
    assert saved_owner != foreign_token
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.cancel"))
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_release_inode_swap_preserves_foreign_replacement_and_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    fault_dir = tmp_path / "release-inode-swap"
    fault_dir.mkdir()
    saved_lock = fixture.staging / ".owned-lock-moved-by-race"
    race_marker = tmp_path / "release-inode-race-fired"
    foreign_token = "f" * 64
    (fault_dir / "sitecustomize.py").write_text(
        """import ctypes
import os

real_cdll = ctypes.CDLL

class RenameAt2:
    argtypes = None
    restype = None

    def __init__(self, function):
        self.function = function

    def __call__(self, old_dirfd, source, new_dirfd, destination, flags):
        source_path = os.fsdecode(source)
        destination_path = os.fsdecode(destination)
        race_marker = os.environ['FAKE_RELEASE_SWAP_MARKER']
        if (
            source_path.endswith('/.deploy-lock')
            and destination_path.endswith('.release')
            and not os.path.exists(race_marker)
        ):
            os.rename(source_path, os.environ['FAKE_RELEASE_SWAP_SAVED'])
            os.mkdir(source_path, 0o700)
            os.setxattr(
                source_path,
                'user.ava_deploy_token',
                os.environ['FAKE_RELEASE_SWAP_FOREIGN_TOKEN'].encode('ascii'),
            )
            open(race_marker, 'wb').close()
        return self.function(old_dirfd, source, new_dirfd, destination, flags)

class Library:
    def __init__(self, library):
        self.library = library
        self.renameat2 = RenameAt2(library.renameat2)

    def __getattr__(self, name):
        return getattr(self.library, name)

def wrapped_cdll(*arguments, **keywords):
    return Library(real_cdll(*arguments, **keywords))

ctypes.CDLL = wrapped_cdll
"""
    )

    result = _deploy(
        fixture,
        "--prepare-only",
        PYTHONPATH=str(fault_dir),
        FAKE_RELEASE_SWAP_MARKER=str(race_marker),
        FAKE_RELEASE_SWAP_SAVED=str(saved_lock),
        FAKE_RELEASE_SWAP_FOREIGN_TOKEN=foreign_token,
    )

    lock = fixture.staging / ".deploy-lock"
    release = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "verrou de preparation distant non libere" in result.stderr
    assert race_marker.is_file()
    assert lock.is_dir()
    assert os.getxattr(lock, b"user.ava_deploy_token") == foreign_token.encode()
    assert saved_lock.is_dir()
    saved_owner = os.getxattr(saved_lock, b"user.ava_deploy_token").decode()
    assert len(saved_owner) == 64
    assert saved_owner != foreign_token
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not list(fixture.staging.glob(".deploy-lock.*.control"))
    assert not list(fixture.staging.glob(".deploy-lock.*.tmp"))
    assert not list(fixture.staging.glob(".deploy-lock.*.release"))
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_prepare_only_signal_after_staging_creation_preserves_release_and_releases_lock(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_SIGNAL_AFTER_RELEASE_CREATE="1",
    )

    release = fixture.staging / fixture.commit
    assert result.returncode == 143, result.stdout + result.stderr
    assert release.is_dir()
    assert (release / ".ava-building").read_bytes() == f"{fixture.commit}\n".encode()
    assert (release / ".ava-artifacts").is_dir()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.current.resolve() == fixture.legacy
    assert b"docker|" in fixture.build_log.read_bytes()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert f"chmod -R u+w -- {release}" not in ssh_log
    assert f"rm -rf -- {release}\n" not in ssh_log
    assert "# ava-lock-release-v2" in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log


def test_ambiguous_same_sha_probes_never_delete_ready_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    first = _deploy(fixture, "--prepare-only")
    assert first.returncode == 0, first.stdout + first.stderr
    release = fixture.releases / fixture.commit
    build_log = fixture.build_log.read_bytes()

    def snapshot() -> dict[str, tuple[int, bytes | None]]:
        return {
            str(path.relative_to(release)): (
                path.lstat().st_mode,
                path.read_bytes() if path.is_file() else None,
            )
            for path in (release, *release.rglob("*"))
        }

    before = snapshot()
    second = _deploy(
        fixture,
        "--prepare-only",
        FAKE_AMBIGUOUS_RELEASE_EXISTS_PROBE="1",
        FAKE_AMBIGUOUS_RELEASE_READY_PROBE="1",
    )

    assert second.returncode != 0
    assert "inspection distante de la release same-SHA indeterminee" in second.stderr
    assert snapshot() == before
    assert fixture.build_log.read_bytes().startswith(build_log)
    assert fixture.build_log.read_bytes().count(b"docker|") == 4
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.current.exists()
    assert not fixture.restart_count.exists()


def test_prepare_only_never_observes_current_even_if_its_transport_would_be_ambiguous(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    current_inode = fixture.current.lstat().st_ino

    result = _deploy(
        fixture,
        "--prepare-only",
        FAKE_AMBIGUOUS_CURRENT_EXISTS_PROBE="1",
        FAKE_AMBIGUOUS_CURRENT_LINK_PROBE="1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.current.is_symlink()
    assert fixture.current.lstat().st_ino == current_inode
    assert fixture.current.resolve() == fixture.legacy
    assert (fixture.releases / fixture.commit).is_dir()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert b"docker|" in fixture.build_log.read_bytes()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "inspect-current" not in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log


def test_normal_deploy_ambiguous_current_transport_is_fatal_after_local_build(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    current_inode = fixture.current.lstat().st_ino

    result = _deploy(
        fixture,
        FAKE_AMBIGUOUS_CURRENT_EXISTS_PROBE="1",
        FAKE_AMBIGUOUS_CURRENT_LINK_PROBE="1",
    )

    assert result.returncode != 0
    assert "inspection authoritative du pointeur current indeterminee" in result.stderr
    assert fixture.current.is_symlink()
    assert fixture.current.lstat().st_ino == current_inode
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "inspect-current" in ssh_log
    assert f"test -L {fixture.current}\n" not in ssh_log
    assert f"test -e {fixture.current}\n" not in ssh_log


def test_prepare_only_incomplete_same_sha_is_fatal_and_preserved_byte_for_byte(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    release = fixture.releases / fixture.commit
    release.mkdir()
    (release / ".ava-building").write_text(f"{fixture.commit}\n")
    payload = release / "partial-artifact.bin"
    payload.write_bytes(b"incomplete\x00candidate\n")
    fixture.current.symlink_to(fixture.legacy)
    before = {
        path.relative_to(release): (path.lstat().st_mode, path.read_bytes())
        for path in release.rglob("*")
        if path.is_file()
    }

    result = _deploy(fixture, "--prepare-only")

    assert result.returncode != 0
    assert "release authoritative same-SHA incomplete ou non scellee" in result.stderr
    assert fixture.current.resolve() == fixture.legacy
    assert release.is_dir()
    after = {
        path.relative_to(release): (path.lstat().st_mode, path.read_bytes())
        for path in release.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (release / ".ava-ready").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert f"chmod -R u+w -- {release}" not in ssh_log
    assert f"rm -rf -- {release}\n" not in ssh_log
    assert "ava-deploy-deadman.py" not in ssh_log
    assert "systemctl" not in ssh_log


def test_build_failure_preserves_candidate_without_any_cleanup_probe(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)

    result = _deploy(
        fixture,
        FAKE_AMBIGUOUS_CLEANUP_CURRENT_PROBE="1",
        FAKE_UV_FAIL="1",
    )

    release = fixture.staging / fixture.commit
    assert result.returncode != 0
    assert fixture.current.resolve() == fixture.legacy
    assert release.is_dir()
    assert (release / ".ava-building").read_text().strip() == fixture.commit
    assert (release / ".cleanup-preserve.bin").read_bytes() == (
        b"failed-build-must-be-preserved\x00\n"
    )
    assert not (release / ".ava-ready").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "# ava-cleanup-release-v1" not in ssh_log
    assert f"chmod -R u+w -- {release}" not in ssh_log
    assert f"rm -rf -- {release}\n" not in ssh_log


def test_build_failure_never_runs_cleanup_mutation_that_could_switch_current(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    release = fixture.staging / fixture.commit

    result = _deploy(
        fixture,
        FAKE_SWITCH_CURRENT_DURING_CLEANUP=str(release),
        FAKE_UV_FAIL="1",
    )

    assert result.returncode != 0
    assert fixture.current.resolve() == fixture.legacy
    assert release.is_dir()
    assert (release / ".ava-building").read_text().strip() == fixture.commit
    assert (release / ".cleanup-preserve.bin").read_bytes() == (
        b"failed-build-must-be-preserved\x00\n"
    )
    assert not (release / ".ava-ready").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "# ava-cleanup-release-v1" not in ssh_log
    assert f"chmod -R u+w -- {release}" not in ssh_log
    assert f"rm -rf -- {release}\n" not in ssh_log


def test_release_is_built_outside_legacy_and_switched_atomically(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    legacy_environment = (fixture.legacy / ".venv" / "sentinel").read_bytes()
    legacy_frontend = (fixture.legacy / "frontend" / "dist" / "sentinel").read_bytes()

    result = _deploy(fixture)

    assert result.returncode == 0, result.stdout + result.stderr
    release = fixture.releases / fixture.commit
    assert fixture.current.is_symlink()
    assert fixture.current.resolve() == release
    assert not (release / ".git").exists()
    assert (release / ".ava-ready").is_file()
    assert not (release / ".ava-building").exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert (fixture.legacy / ".venv" / "sentinel").read_bytes() == legacy_environment
    assert (
        fixture.legacy / "frontend" / "dist" / "sentinel"
    ).read_bytes() == legacy_frontend
    assert fixture.restart_count.read_text().strip() == "1"
    assert not fixture.deadman_state.exists()
    build_log = fixture.build_log.read_text()
    assert "docker|" in build_log
    assert str(fixture.legacy) not in build_log
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert f"activate --candidate {release}" in ssh_log
    assert f"--candidate {release}" in ssh_log
    assert f"--expect-current legacy:{fixture.legacy.name}" in ssh_log
    assert f"ln -s -- {release}" not in ssh_log
    assert "git pull" not in ssh_log
    assert f"cd {fixture.legacy}" not in ssh_log
    assert "/proc/4242/cwd" in ssh_log
    assert "/proc/4242/exe" in ssh_log
    evolutions = release / ".ava-artifacts/evolutions-v1.json"
    document = json.loads(evolutions.read_text())
    assert set(document) == {"entries", "git_sha", "schema", "truncated"}
    assert document["schema"] == 1
    assert document["git_sha"] == fixture.commit
    assert type(document["truncated"]) is bool
    assert document["entries"]
    assert all(
        set(entry) == {"body", "date", "subject"} for entry in document["entries"]
    )
    manifest = (release / ".ava-release").read_text().splitlines()
    evolutions_hash = hashlib.sha256(evolutions.read_bytes()).hexdigest()
    assert f"evolutions_sha256={evolutions_hash}" in manifest


def test_first_release_without_previous_is_healthy_and_idempotent(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    _remove_immutable_legacy_fixture(fixture)
    fixture.service_state.write_text("inactive\n")

    first = _deploy(fixture)
    second = _deploy(fixture)

    release = fixture.releases / fixture.commit
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert fixture.current.resolve() == release
    assert fixture.service_state.read_text().strip() == "active"
    assert fixture.restart_count.read_text().strip() == "1"
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()


def test_failed_first_release_stops_service_and_keeps_identifiable_candidate(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    _remove_immutable_legacy_fixture(fixture)
    fixture.service_state.write_text("inactive\n")

    result = _deploy(fixture, FAKE_FAIL_FIRST_HEALTH="1")

    release = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert not fixture.current.exists()
    assert fixture.service_state.read_text().strip() == "inactive"
    assert release.is_dir()
    assert (release / ".ava-ready").is_file()
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()


def test_same_sha_reuses_sealed_release_after_rebuilding_only_reproducible_frontend_pin(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    first = _deploy(fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    release = fixture.releases / fixture.commit
    manifest_before = (release / ".ava-release").read_bytes()
    build_log_before = fixture.build_log.read_bytes()

    second = _deploy(fixture)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "release immutable existante reutilisee" in second.stdout
    assert "aucun redemarrage" in second.stdout
    assert (release / ".ava-release").read_bytes() == manifest_before
    assert fixture.build_log.read_bytes().startswith(build_log_before)
    assert fixture.build_log.read_bytes().count(b"docker|") == 4
    assert fixture.restart_count.read_text().strip() == "1"


def test_same_sha_revalidates_exact_staging_wheelhouse_before_sealer(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    first = _deploy(fixture, "--prepare-only")
    assert first.returncode == 0, first.stdout + first.stderr
    seal_log = Path(fixture.environment["FAKE_SEAL_LOG"])
    seal_log_before = seal_log.read_bytes()

    wheel = (
        fixture.staging
        / fixture.commit
        / ".ava-artifacts/python-wheelhouse/dependency-1.0-py3-none-any.whl"
    )
    original = wheel.read_bytes()
    wheel.chmod(0o644)
    wheel.write_bytes(b"x" * len(original))
    wheel.chmod(0o444)

    second = _deploy(fixture, "--prepare-only")

    assert second.returncode != 0
    assert "release immutable existante reutilisee" in second.stdout
    assert "wheelhouse distant divergent du manifeste source canonique" in second.stderr
    assert seal_log.read_bytes() == seal_log_before


def test_deployer_never_runs_retention_against_root_owned_releases(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    stale_releases = _make_collectable_releases(fixture.releases)
    oldest = stale_releases[0]

    result = _deploy(
        fixture,
        AVA_RELEASE_KEEP="2",
        FAKE_FAIL_GC_CHMOD_TARGET=str(oldest),
    )

    candidate = fixture.releases / fixture.commit
    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.current.resolve() == candidate
    assert fixture.service_state.read_text().strip() == "active"
    assert oldest.is_dir()
    assert "fake GC chmod failure" not in result.stderr
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "chmod -R u+w" not in ssh_log
    assert "restauration de" not in result.stderr
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()


def test_repeated_deploy_never_runs_retention_against_root_owned_releases(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    first = _deploy(fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    candidate = fixture.releases / fixture.commit
    stale_releases = _make_collectable_releases(fixture.releases)
    oldest = stale_releases[0]

    repeated = _deploy(
        fixture,
        AVA_RELEASE_KEEP="2",
        FAKE_FAIL_GC_CHMOD_TARGET=str(oldest),
    )

    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "release deja active et saine; aucun redemarrage" in repeated.stdout
    assert fixture.current.resolve() == candidate
    assert fixture.restart_count.read_text().strip() == "1"
    assert oldest.is_dir()
    assert "fake GC chmod failure" not in repeated.stderr
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "chmod -R u+w" not in ssh_log
    assert "restauration de" not in repeated.stderr
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()


def test_same_sha_rejects_mutated_attestation_and_writable_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    first = _deploy(fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    release = fixture.releases / fixture.commit
    remote_attestation = next((release / ".ava-artifacts").glob("*.attestation"))
    release.chmod(0o750)
    (release / ".ava-artifacts").chmod(0o750)
    remote_attestation.chmod(0o600)
    remote_attestation.write_text("mutated provenance\n")
    remote_attestation.chmod(0o400)
    (release / ".ava-artifacts").chmod(0o550)
    release.chmod(0o550)

    mutated = _deploy(fixture)

    assert mutated.returncode != 0
    assert "scellement root-owned refuse" in mutated.stderr
    assert fixture.current.resolve() == release

    remote_attestation.chmod(0o600)
    remote_attestation.write_bytes(fixture.attestation.read_bytes())
    remote_attestation.chmod(0o400)
    release.chmod(0o750)
    writable = _deploy(fixture)
    assert writable.returncode != 0
    assert "scellement root-owned refuse" in writable.stderr
    assert fixture.current.resolve() == release


def test_ssh_option_injection_is_rejected_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, AVA_VM="-oProxyCommand=touch /tmp/owned")

    assert result.returncode != 0
    assert "destination SSH VM invalide" in result.stderr
    assert not fixture.ssh_marker.exists()


def test_systemd_option_injection_is_rejected_before_any_ssh(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, AVA_SERVICE="-o")

    assert result.returncode != 0
    assert "nom de service invalide" in result.stderr
    assert not fixture.ssh_marker.exists()


def test_a_diverted_current_target_is_rejected_without_deleting_it(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    outside = fixture.remote / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("must survive\n")
    fixture.current.symlink_to(outside)

    result = _deploy(fixture)

    assert result.returncode != 0
    assert "inspection authoritative du pointeur current indeterminee" in result.stderr
    assert marker.read_text() == "must survive\n"
    assert fixture.current.resolve() == outside


def test_deploy_refuses_a_concurrent_backup_lease(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    backup_lease = fixture.staging / ".backup-use.123"
    backup_lease.mkdir()

    result = _deploy(fixture)

    assert result.returncode != 0
    assert backup_lease.is_dir()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_backup_lease_ambiguous_lock_release_is_retried_by_cleanup(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    backup_lease = fixture.staging / ".backup-use.255"
    backup_lease.mkdir()

    result = _deploy(
        fixture,
        FAKE_AMBIGUOUS_FIRST_LOCK_RELEASE="1",
    )

    assert result.returncode != 0
    assert "sauvegarde utilise actuellement" in result.stderr
    assert backup_lease.is_dir()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert int(Path(fixture.environment["FAKE_LOCK_RELEASE_COUNT"]).read_text()) >= 2
    assert not (fixture.releases / fixture.commit).exists()
    assert not fixture.current.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_stale_empty_lock_is_reclaimed_but_fresh_lock_is_not(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    lock = fixture.staging / ".deploy-lock"
    lock.mkdir(mode=0o700)

    fresh = _deploy(fixture, AVA_DEPLOY_LOCK_STALE_SECONDS="300")
    assert fresh.returncode != 0
    assert "recent detient deja le verrou" in fresh.stderr
    assert lock.is_dir()

    old = time.time() - 301
    os.utime(lock, (old, old))
    recovered = _deploy(fixture, AVA_DEPLOY_LOCK_STALE_SECONDS="300")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not lock.exists()


def test_stale_token_lock_marker_is_removed_before_deadman_reclaim(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.staging.mkdir(exist_ok=True)
    lock = fixture.staging / ".deploy-lock"
    lock.mkdir(mode=0o700)
    stale_token = "e" * 64
    marker = lock / ".ava-deploy-owner"
    marker.write_text(f"{stale_token}\n")
    marker.chmod(0o600)
    os.setxattr(lock, b"user.ava_deploy_token", stale_token.encode())
    old = time.time() - 301
    os.utime(lock, (old, old))

    result = _deploy(fixture, AVA_DEPLOY_LOCK_STALE_SECONDS="300")

    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.current.resolve() == fixture.releases / fixture.commit
    assert not lock.exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "os.unlink(marker)" in ssh_log
    assert "reclaim-lock --stale-after 300" in ssh_log


def test_transit_corruption_stops_before_prefill_and_preserves_staging(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_CORRUPT_WHEEL_TRANSFER="1")

    assert result.returncode != 0
    assert "wheel Rust alteree pendant le transfert" in result.stderr
    assert b"docker|" in fixture.build_log.read_bytes()
    assert not fixture.current.exists()
    release = fixture.staging / fixture.commit
    assert release.is_dir()
    assert (release / ".ava-building").read_text().strip() == fixture.commit
    remote_wheel = release / ".ava-artifacts" / fixture.wheel.name
    assert remote_wheel.read_bytes().endswith(b"corrupted in transit\n")
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_build_failure_keeps_legacy_and_preserves_incomplete_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_UV_FAIL="1")

    assert result.returncode != 0
    assert not fixture.current.exists()
    release = fixture.staging / fixture.commit
    assert (release / ".ava-building").read_text().strip() == fixture.commit
    assert (release / ".cleanup-preserve.bin").read_bytes() == (
        b"failed-build-must-be-preserved\x00\n"
    )
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_runtime_policy_failure_after_first_activation_returns_to_stopped_bootstrap(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    _remove_immutable_legacy_fixture(fixture)
    fixture.service_state.write_text("inactive\n")

    result = _deploy(fixture, FAKE_RUNTIME_POLICY_FAIL_AT="1")

    assert result.returncode != 0
    assert "persona ou politique runtime hors de la release attendue" in result.stderr
    assert fixture.runtime_policy_count.read_text().strip() == "1"
    assert not fixture.current.exists()
    release = fixture.releases / fixture.commit
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"


def test_runtime_policy_failure_after_legacy_switch_stops_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_RUNTIME_POLICY_FAIL_AT="1")

    assert result.returncode != 0
    assert "persona ou politique runtime" in result.stderr
    assert "repli fail-closed confirme" in result.stderr
    assert "809 ne sera jamais resservie" in result.stderr
    assert fixture.runtime_policy_count.read_text().strip() == "1"
    _assert_fail_closed_stopped(fixture)
    release = fixture.releases / fixture.commit
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"


def test_untrusted_legacy_bytes_are_never_qualified_as_a_rollback_target(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    legacy_bin = fixture.legacy / ".venv/bin"
    legacy_bin.chmod(0o755)
    (legacy_bin / "jarvis").unlink()
    legacy_bin.chmod(0o555)

    result = _deploy(fixture)

    candidate = fixture.releases / fixture.commit
    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.current.resolve() == candidate
    assert candidate.is_dir()
    assert fixture.build_log.is_file()
    assert fixture.restart_count.read_text().strip() == "1"
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" in ssh_log
    assert f"test -x {fixture.legacy}/.venv/bin/jarvis" not in ssh_log
    assert f"health_check {fixture.legacy}" not in ssh_log


def test_invalid_release_previous_blocks_without_candidate_or_deadman_mutation(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    previous_sha = "a" * 40
    assert previous_sha != fixture.commit
    previous = fixture.releases / previous_sha
    (previous / ".venv/bin").mkdir(parents=True)
    previous_jarvis = previous / ".venv/bin/jarvis"
    _write_executable(previous_jarvis, "#!/bin/sh\nexit 0\n")
    policy_probe = tmp_path / "release-policy-probed"
    previous_python = previous / ".venv/bin/python"
    _write_executable(
        previous_python,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"config.agent.system_prompt_path == ''"* ]]; then
  touch "$FAKE_RELEASE_POLICY_PROBE"
  exit 95
fi
exit 0
""",
    )
    (previous / "src/openjarvis/server/static").mkdir(parents=True)
    (previous / "src/openjarvis/server/static/index.html").write_text(
        "<html>invalid previous policy</html>\n"
    )
    (previous / ".ava-ready").write_text(f"{previous_sha}\n")
    (previous / ".ava-release").write_text(
        "\n".join(
            (
                "format=ava-release-v1",
                f"git_sha={previous_sha}",
                f"source_tree_sha256={'1' * 64}",
                f"rust_tree_sha256={'2' * 64}",
                f"wheel_sha256={'3' * 64}",
                "wheel_filename=openjarvis_rust-1.0.0-"
                "cp312-cp312-manylinux_2_36_x86_64.whl",
                f"attestation_sha256={'4' * 64}",
                f"evolutions_sha256={'5' * 64}",
                "",
            )
        )
    )
    fixture.current.symlink_to(previous)

    result = _deploy(fixture, FAKE_RELEASE_POLICY_PROBE=str(policy_probe))

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "cible precedente scellee hors contrat Ava" in result.stderr
    assert not policy_probe.exists()
    assert fixture.current.resolve() == previous
    assert not candidate.exists()
    assert fixture.build_log.read_bytes().count(b"docker|") == 2
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" not in ssh_log
    assert f"ln -s -- {candidate}" not in ssh_log


def test_current_expectation_drift_before_arm_never_activates_candidate(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_ARM_EXPECTATION_MISMATCH="1")

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "impossible d'armer le dead-man distant" in result.stderr
    _assert_exact_legacy_is_active(fixture)
    assert (candidate / ".ava-ready").read_text().strip() == fixture.commit
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" in ssh_log
    assert "ava-deploy-deadman.py activate" not in ssh_log
    assert f"ln -s -- {candidate}" not in ssh_log


def test_failed_legacy_migration_stops_without_extended_809_health(
    tmp_path: Path,
) -> None:
    """A legacy migration failure is stopped, never health-qualified as 809."""

    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        FAKE_FAIL_FIRST_HEALTH="1",
    )

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    _assert_fail_closed_stopped(fixture)
    assert candidate.is_dir()
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert "repli fail-closed confirme" in result.stderr
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert f"AVA_EXPECTED_RELEASE={fixture.legacy}" not in ssh_log
    assert "dead-man distant reste arme" not in result.stderr


def test_failed_health_rolls_back_and_preserves_candidate(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_FAIL_FIRST_HEALTH="1")

    assert result.returncode != 0
    assert "809 ne sera jamais resservie" in result.stderr
    assert "repli fail-closed confirme" in result.stderr
    _assert_fail_closed_stopped(fixture)
    release = fixture.releases / fixture.commit
    assert (release / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"


def test_wrong_main_process_interpreter_stops_legacy_migration_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(
        fixture,
        FAKE_MAIN_EXE_OVERRIDE="/usr/bin/python3",
        FAKE_MAIN_EXE_OVERRIDE_TARGET=str(candidate),
    )

    assert result.returncode != 0
    assert "n'utilise pas l'interpreteur de la release attendue" in result.stderr
    assert "repli fail-closed confirme" in result.stderr
    _assert_fail_closed_stopped(fixture)
    assert (candidate / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"


def test_wrong_main_process_cwd_stops_legacy_migration_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(
        fixture,
        FAKE_MAIN_CWD_OVERRIDE="/tmp/unsealed-checkout",
        FAKE_MAIN_CWD_OVERRIDE_TARGET=str(candidate),
    )

    assert result.returncode != 0
    assert "ne sert pas la release attendue" in result.stderr
    assert "repli fail-closed confirme" in result.stderr
    _assert_fail_closed_stopped(fixture)
    assert candidate.is_dir()
    assert fixture.restart_count.read_text().strip() == "1"


def test_wrong_main_process_bootstrap_argv_stops_fail_closed(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(fixture, FAKE_BAD_MAIN_ARGV="1")

    assert result.returncode != 0
    assert "bootstrap scelle attendu" in result.stderr
    assert "repli fail-closed confirme" in result.stderr
    _assert_fail_closed_stopped(fixture)
    assert candidate.is_dir()
    assert fixture.restart_count.read_text().strip() == "1"


def test_unhealthy_rollback_keeps_candidate_lock_and_deadman_authority(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(
        fixture,
        FAKE_FAIL_FIRST_HEALTH="1",
        FAKE_DEADMAN_ROLLBACK_FAIL="1",
    )

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    _assert_fail_closed_stopped(fixture)
    assert candidate.is_dir()
    assert fixture.deadman_state.is_file()
    assert (fixture.staging / ".deploy-lock").is_dir()
    assert "dead-man distant reste arme" in result.stderr


def test_ambiguous_ssh_result_after_switch_still_rolls_back(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(fixture, FAKE_FAIL_SWITCH_TARGET=str(candidate))

    assert result.returncode != 0
    assert "809 ne sera jamais resservie" in result.stderr
    _assert_fail_closed_stopped(fixture)
    assert (candidate / ".ava-ready").read_text().strip() == fixture.commit
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()


def test_persistent_ssh_cut_after_switch_is_recovered_by_remote_deadman(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(
        fixture,
        FAKE_PERSISTENT_CUTOFF_TARGET=str(candidate),
    )

    assert result.returncode != 0
    assert fixture.ssh_cutoff.is_file()
    assert fixture.deadman_state.is_file()
    assert fixture.current.resolve() == candidate
    assert (fixture.staging / ".deploy-lock").is_dir()

    recovery_environment = fixture.environment.copy()
    recovery_environment["PATH"] = f"{fixture.fake_bin}:{recovery_environment['PATH']}"
    recovery = subprocess.run(
        [str(fixture.fake_deadman), "check"],
        env=recovery_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert recovery.returncode == 0, recovery.stderr
    assert recovery.stdout.strip() == "legacy-stopped"
    _assert_fail_closed_stopped(fixture)
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_persistent_ssh_cut_during_first_release_returns_to_stopped_state(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    _remove_immutable_legacy_fixture(fixture)
    fixture.service_state.write_text("inactive\n")
    candidate = fixture.releases / fixture.commit

    result = _deploy(
        fixture,
        FAKE_PERSISTENT_CUTOFF_TARGET=str(candidate),
    )

    assert result.returncode != 0
    assert fixture.ssh_cutoff.is_file()
    assert fixture.current.resolve() == candidate
    assert fixture.deadman_state.is_file()
    assert (fixture.staging / ".deploy-lock").is_dir()

    recovery = subprocess.run(
        [str(fixture.fake_deadman), "check"],
        env=fixture.environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert recovery.returncode == 0, recovery.stderr
    assert recovery.stdout.strip() == "bootstrap-stopped"
    assert not fixture.current.exists()
    assert fixture.service_state.read_text().strip() == "inactive"
    assert candidate.is_dir()
    assert not fixture.deadman_state.exists()
    assert not (fixture.staging / ".deploy-lock").exists()


def test_contract_has_no_in_place_checkout_or_frontend_mutation() -> None:
    source = SCRIPT.read_text()
    frontend_builder = (ROOT / "deploy/docker/Dockerfile.frontend-builder").read_text()

    assert 'STAGING_ROOT="${AVA_STAGING_ROOT:-/home/avalon/ava-releases}"' in source
    assert "/var/lib/ava/releases" in source
    assert "/var/lib/ava/current" in source
    assert 'LOCK_PATH="${STAGING_ROOT}/.deploy-lock"' in source
    assert "ava-release-seal.py" in source
    assert "sudo -n $Q_SEAL_HELPER seal" in source
    for argument in (
        "--git-sha",
        "--release-manifest-sha256",
        "--source-tree-sha256",
        "--frontend-static-sha256",
        "--frontend-build-attestation-sha256",
        "--rust-tree-sha256",
        "--rust-wheel-sha256",
        "--rust-attestation-sha256",
        "--evolutions-sha256",
        "--uv-lock-sha256",
        "--runtime-requirements-sha256",
        "--runtime-wheelhouse-manifest-sha256",
        "--runtime-wheel-pin",
        "--python-runtime-archive",
        "--python-runtime-source-sha256",
        "--python-runtime-sha256",
        "--uv-sha256",
        "--uv-version",
    ):
        assert argument in source
    assert "uv 0.12.5 (x86_64-unknown-linux-gnu)" in source
    assert "--format=ustar" in frontend_builder
    assert "--mtime=@0" in frontend_builder
    assert "--owner=0 --group=0 --numeric-owner" in frontend_builder
    assert "--format=ustar --mode='a=rX,u+w'" in frontend_builder
    assert (
        "npm ci --prefix frontend --ignore-scripts --no-audit --no-fund"
        in frontend_builder
    )
    assert "npm install" not in frontend_builder
    assert "env -i" in frontend_builder
    assert "RUN --network=none env -i" in frontend_builder
    assert 'test "$npm_version" = 10.9.8' in frontend_builder
    assert "command -v npm" not in source
    assert "for build_number in 1 2" in source
    assert "--pull" in source and "--no-cache" in source
    assert "FRONTEND_BUILDER_PLATFORM='linux/amd64'" in source
    assert "frontend-build-attestation.json" in source
    assert 'cat -- "$LOCAL_SOURCE_ARCHIVE" | ssh_vm' in source
    assert (
        source.count(
            'git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" '
            '> "$LOCAL_SOURCE_ARCHIVE"'
        )
        == 1
    )
    assert "frontend-static.tar" in source
    assert "python-wheelhouse" in source
    assert ".ava-python .python .ava-ready" in source
    assert "--require-hashes --only-binary=:all:" in source
    assert "--no-cache-dir" in source
    assert "--index-url https://pypi.org/simple" in source
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in source
    assert "nvidia[-_]" in source and "triton" in source
    arm_call = source.rindex("\n  arm_deadman\n")
    activate_call = source.rindex("\n  activate_deadman\n")
    restart_call = source.rindex("sudo -n systemctl restart $Q_SERVICE")
    health_call = source.rindex("if ! health_check")
    confirm_call = source.rindex("\nconfirm_deadman\n")
    assert arm_call < activate_call < restart_call < health_call < confirm_call
    assert "--expect-current" in source
    assert "inspect-current" in source
    assert "sealed:${expected_sha}" in source
    assert "atomic_link" not in source
    assert "ln -s -- $Q_RELEASE" not in source
    assert "mv -Tf -- $Q_CURRENT" not in source
    assert "$Q_SEAL_HELPER activate" not in source
    assert "sudo -n $Q_DEADMAN_HELPER activate --candidate $Q_RELEASE" in source
    assert "/proc/$main_pid/cwd" in source
    assert "/proc/$main_pid/exe" in source
    assert "/proc/{pid}/cmdline" in source
    assert "ava_extensions/runtime_bootstrap.py" in source
    assert (
        "activation refusee: le traitement baseline A n'est jamais servable" in source
    )
    assert "809ade530fedc53424f6fe93a22320d129237e62" in source
    assert "legacy-stopped" in source
    assert "809 ne sera jamais resservie" in source
    assert "validate_legacy_runtime_policy" not in source
    assert "LEGACY_RELEASE" not in source
    assert "/home/avalon/ava-current" not in source
    assert '/home/avalon/ava"' not in source
    assert "rm -rf -- $Q_RELEASE" not in source
    assert "chmod -R u+w -- $Q_RELEASE" not in source
    assert "git pull" not in source
