"""Offline regression contracts for Ava's immutable VM delivery path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-vm.sh"


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


class DeploymentFixture(NamedTuple):
    repo: Path
    remote: Path
    legacy: Path
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


def _make_deployment_fixture(tmp_path: Path) -> DeploymentFixture:
    repo = tmp_path / "ava"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    (repo / "rust" / "crates").mkdir(parents=True)
    (repo / "rust" / "crates" / "lib.rs").write_text("pub fn ava() {}\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text('{"scripts":{"build":"true"}}\n')
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
    legacy = remote / "legacy"
    releases = remote / "releases"
    current = remote / "current"
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

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_marker = tmp_path / "ssh-was-called"
    build_log = tmp_path / "build.log"
    restart_count = tmp_path / "restart-count"
    service_state = tmp_path / "service-state"
    service_state.write_text("active\n")
    relay_ca = tmp_path / "relay-ca.crt"
    relay_ca.write_text("test-only public CA\n")
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
if [[ -f "$FAKE_SSH_CUTOFF" ]]; then
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
printf 'npm|%s|%s\n' "$PWD" "$*" >> "$FAKE_BUILD_LOG"
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
  mkdir -p ../src/openjarvis/server/static
  printf '<html>Ava release</html>\n' > ../src/openjarvis/server/static/index.html
fi
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
  arm)
    previous=''
    candidate=''
    bootstrap=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --previous) previous=$2; shift 2 ;;
        --bootstrap) bootstrap=1; shift ;;
        --candidate) candidate=$2; shift 2 ;;
        --ttl) shift 2 ;;
        *) exit 71 ;;
      esac
    done
    [[ -n "$candidate" && ! -e "$FAKE_DEADMAN_STATE" ]]
    if [[ "$bootstrap" -eq 1 ]]; then
      [[ -z "$previous" && ! -e "$AVA_CURRENT_LINK" ]]
      [[ "$(systemctl is-active openjarvis.service || true)" =~ ^(failed|inactive)$ ]]
      previous='<none>'
    else
      [[ -n "$previous" ]]
      [[ "$(readlink -f -- "$AVA_CURRENT_LINK")" == "$previous" ]]
    fi
    printf '%s\n%s\n' "$previous" "$candidate" > "$FAKE_DEADMAN_STATE"
    printf 'armed\n'
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
      result='bootstrap-stopped'
    else
      temporary="${AVA_CURRENT_LINK}.deadman.$$"
      ln -s -- "${state[0]}" "$temporary"
      mv -Tf -- "$temporary" "$AVA_CURRENT_LINK"
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
if [[ "${1:-}" == "/usr/local/libexec/avalon/ava-deploy-deadman.py" ]]; then
  shift
  exec "$FAKE_DEADMAN" "$@"
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
  exec /usr/bin/readlink -f -- "$AVA_CURRENT_LINK"
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

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AVA_JUMP": "fake-jump",
            "AVA_VM": "fake-vm",
            "AVA_RACINE": str(legacy),
            "AVA_RELEASE_ROOT": str(releases),
            "AVA_CURRENT_LINK": str(current),
            "AVA_RUST_WHEEL": str(wheel),
            "AVA_RUST_ATTESTATION": str(attestation),
            "AVA_UV": str(fake_bin / "uv"),
            "AVA_SSH_BIN": str(fake_bin / "ssh"),
            "AVA_HEALTH_DELAY_SECONDS": "0",
            "AVA_RELAY_CA": str(relay_ca),
            "AVA_RELAY_TLS_NAME": "ava-relay.test",
            "FAKE_SSH_MARKER": str(ssh_marker),
            "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
            "FAKE_BUILD_LOG": str(build_log),
            "FAKE_RESTART_COUNT": str(restart_count),
            "FAKE_SERVICE_STATE": str(service_state),
            "FAKE_DEADMAN": str(fake_deadman),
            "FAKE_DEADMAN_STATE": str(deadman_state),
            "FAKE_SSH_CUTOFF": str(ssh_cutoff),
            "FAKE_CURL_COUNT": str(tmp_path / "curl-count"),
            "FAKE_PYTHON_STUB": str(python_stub),
            "FAKE_RUNTIME_POLICY_COUNT": str(runtime_policy_count),
        }
    )
    return DeploymentFixture(
        repo=repo,
        remote=remote,
        legacy=legacy,
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
    fixture: DeploymentFixture, **environment_overrides: str
) -> subprocess.CompletedProcess[str]:
    environment = fixture.environment.copy()
    environment.update(environment_overrides)
    return subprocess.run(
        ["bash", str(fixture.repo / "scripts" / SCRIPT.name)],
        cwd=fixture.repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


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
    assert not (fixture.releases / ".deploy-lock").exists()
    assert (fixture.legacy / ".venv" / "sentinel").read_bytes() == legacy_environment
    assert (
        fixture.legacy / "frontend" / "dist" / "sentinel"
    ).read_bytes() == legacy_frontend
    assert fixture.restart_count.read_text().strip() == "1"
    assert not fixture.deadman_state.exists()
    build_log = fixture.build_log.read_text()
    assert str(release) in build_log
    assert str(fixture.legacy) not in build_log
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "mv -Tf" in ssh_log
    assert "git pull" not in ssh_log
    assert f"cd {fixture.legacy}" not in ssh_log
    assert "/proc/4242/cwd" in ssh_log
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
    shutil.rmtree(fixture.legacy)
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
    assert not (fixture.releases / ".deploy-lock").exists()


def test_failed_first_release_stops_service_and_keeps_identifiable_candidate(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    shutil.rmtree(fixture.legacy)
    fixture.service_state.write_text("inactive\n")

    result = _deploy(fixture, FAKE_FAIL_FIRST_HEALTH="1")

    release = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert not fixture.current.exists()
    assert fixture.service_state.read_text().strip() == "inactive"
    assert release.is_dir()
    assert (release / ".ava-ready").is_file()
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()


def test_same_sha_reuses_release_without_rebuilding_it(tmp_path: Path) -> None:
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
    assert fixture.build_log.read_bytes() == build_log_before
    assert fixture.restart_count.read_text().strip() == "1"


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
    assert "same-SHA divergent" in mutated.stderr
    assert fixture.current.resolve() == release

    release.chmod(0o750)
    writable = _deploy(fixture)
    assert writable.returncode != 0
    assert "redevenue modifiable" in writable.stderr
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
    assert "cible precedente de current hors contrat Ava" in result.stderr
    assert marker.read_text() == "must survive\n"
    assert fixture.current.resolve() == outside


def test_deploy_refuses_a_concurrent_backup_lease(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.releases.mkdir()
    backup_lease = fixture.releases / ".backup-use.123"
    backup_lease.mkdir()

    result = _deploy(fixture)

    assert result.returncode != 0
    assert backup_lease.is_dir()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_stale_empty_lock_is_reclaimed_but_fresh_lock_is_not(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.releases.mkdir()
    lock = fixture.releases / ".deploy-lock"
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


def test_transit_corruption_stops_before_build_and_cleans_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_CORRUPT_WHEEL_TRANSFER="1")

    assert result.returncode != 0
    assert "wheel Rust alteree pendant le transfert" in result.stderr
    assert not fixture.build_log.exists()
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_build_failure_keeps_legacy_and_cleans_incomplete_release(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_UV_FAIL="1")

    assert result.returncode != 0
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_runtime_policy_failure_blocks_before_atomic_switch(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_RUNTIME_POLICY_FAIL_AT="1")

    assert result.returncode != 0
    assert "candidate immuable" in result.stderr
    assert fixture.runtime_policy_count.read_text().strip() == "1"
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert not fixture.restart_count.exists()


def test_runtime_policy_failure_after_switch_rolls_back(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_RUNTIME_POLICY_FAIL_AT="2")

    assert result.returncode != 0
    assert "persona ou politique runtime" in result.stderr
    assert "rollback confirme" in result.stderr
    assert fixture.runtime_policy_count.read_text().strip() == "2"
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "2"


def test_invalid_legacy_previous_blocks_without_pointer_build_or_deadman_mutation(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    policy_probe = tmp_path / "legacy-policy-probed"
    legacy_python = fixture.legacy / ".venv/bin/python"
    _write_executable(
        legacy_python,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"config.agent.system_prompt_path == ''"* ]]; then
  touch "$FAKE_LEGACY_POLICY_PROBE"
  exit 94
fi
exit 0
""",
    )

    result = _deploy(fixture, FAKE_LEGACY_POLICY_PROBE=str(policy_probe))

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "cible precedente de current hors politique runtime Ava" in result.stderr
    assert policy_probe.is_file()
    assert not fixture.current.exists()
    assert not candidate.exists()
    assert not fixture.build_log.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" not in ssh_log
    assert "ln -s --" not in ssh_log
    assert "mv -Tf --" not in ssh_log


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
    assert "cible precedente de current hors politique runtime Ava" in result.stderr
    assert policy_probe.is_file()
    assert fixture.current.resolve() == previous
    assert not candidate.exists()
    assert not fixture.build_log.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" not in ssh_log
    assert f"ln -s -- {candidate}" not in ssh_log


def test_previous_policy_drift_before_arm_never_arms_or_switches(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    policy_count = tmp_path / "previous-policy-count"
    legacy_python = fixture.legacy / ".venv/bin/python"
    _write_executable(
        legacy_python,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"config.agent.system_prompt_path == ''"* ]]; then
  count=0
  [[ ! -f "$FAKE_PREVIOUS_POLICY_COUNT" ]] \
    || count=$(cat "$FAKE_PREVIOUS_POLICY_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_PREVIOUS_POLICY_COUNT"
  [[ "$count" -ne 2 ]] || exit 96
fi
exit 0
""",
    )

    result = _deploy(fixture, FAKE_PREVIOUS_POLICY_COUNT=str(policy_count))

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert "cible de rollback hors contrat ou politique runtime" in result.stderr
    assert policy_count.read_text().strip() == "2"
    assert fixture.current.resolve() == fixture.legacy
    assert not candidate.exists()
    assert not fixture.restart_count.exists()
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    ssh_log = Path(fixture.environment["FAKE_SSH_LOG"]).read_text()
    assert "ava-deploy-deadman.py probe" in ssh_log
    assert "ava-deploy-deadman.py arm" not in ssh_log
    assert f"ln -s -- {candidate}" not in ssh_log


def test_failed_extended_check_after_completed_rollback_reports_remote_state(
    tmp_path: Path,
) -> None:
    """A previous target may still drift after the final pre-arm validation."""

    fixture = _make_deployment_fixture(tmp_path)
    fixture.current.symlink_to(fixture.legacy)
    policy_count = tmp_path / "rollback-policy-count"
    legacy_python = fixture.legacy / ".venv/bin/python"
    _write_executable(
        legacy_python,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"config.agent.system_prompt_path == ''"* ]]; then
  count=0
  [[ ! -f "$FAKE_ROLLBACK_POLICY_COUNT" ]] \
    || count=$(cat "$FAKE_ROLLBACK_POLICY_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_ROLLBACK_POLICY_COUNT"
  [[ "$count" -ne 3 ]] || exit 94
fi
exit 0
""",
    )

    result = _deploy(
        fixture,
        FAKE_RUNTIME_POLICY_FAIL_AT="2",
        FAKE_ROLLBACK_POLICY_COUNT=str(policy_count),
    )

    candidate = fixture.releases / fixture.commit
    assert result.returncode != 0
    assert policy_count.read_text().strip() == "3"
    assert fixture.current.resolve() == fixture.legacy
    assert candidate.is_dir()
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert "candidate conservee sans dead-man ni verrou" in result.stderr
    assert "dead-man distant reste arme" not in result.stderr


def test_failed_health_rolls_back_and_removes_candidate(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)

    result = _deploy(fixture, FAKE_FAIL_FIRST_HEALTH="1")

    assert result.returncode != 0
    assert "restauration" in result.stderr
    assert "rollback confirme" in result.stderr
    assert fixture.current.resolve() == fixture.legacy
    assert not (fixture.releases / fixture.commit).exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "2"


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
    assert fixture.current.resolve() == fixture.legacy
    assert candidate.is_dir()
    assert fixture.deadman_state.is_file()
    assert (fixture.releases / ".deploy-lock").is_dir()
    assert "dead-man distant reste arme" in result.stderr


def test_ambiguous_ssh_result_after_switch_still_rolls_back(tmp_path: Path) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    candidate = fixture.releases / fixture.commit

    result = _deploy(fixture, FAKE_FAIL_SWITCH_TARGET=str(candidate))

    assert result.returncode != 0
    assert "restauration" in result.stderr
    assert fixture.current.resolve() == fixture.legacy
    assert not candidate.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"
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
    assert (fixture.releases / ".deploy-lock").is_dir()

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
    assert recovery.stdout.strip() == "rolled-back"
    assert fixture.current.resolve() == fixture.legacy
    assert not fixture.deadman_state.exists()
    assert not (fixture.releases / ".deploy-lock").exists()
    assert fixture.restart_count.read_text().strip() == "1"


def test_persistent_ssh_cut_during_first_release_returns_to_stopped_state(
    tmp_path: Path,
) -> None:
    fixture = _make_deployment_fixture(tmp_path)
    shutil.rmtree(fixture.legacy)
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
    assert (fixture.releases / ".deploy-lock").is_dir()

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
    assert not (fixture.releases / ".deploy-lock").exists()


def test_contract_has_no_in_place_checkout_or_frontend_mutation() -> None:
    source = SCRIPT.read_text()

    assert "git pull" not in source
    assert "cd $Q_LEGACY" not in source
    assert "ls -t /tmp/openjarvis_rust" not in source
    assert source.index("remote_wheel_hash=") < source.index("pip install")
    assert source.index("Validations hors ligne avant bascule") < source.index(
        'titre "6/7 Bascule atomique'
    )
    assert source.index("ln -s --") < source.index("mv -Tf --")
    arm_call = source.rindex("\n  arm_deadman\n")
    switch_call = source.rindex('atomic_link "$RELEASE_PATH"')
    assert arm_call < switch_call
    assert source.index("if ! health_check") < source.rindex("confirm_deadman")
    assert "arm_mode='--bootstrap'" in source
    assert "bootstrap-stopped" in source
    assert (
        source.index("systemctl is-active --quiet ava-deploy-deadman.timer") < arm_call
    )
    assert "evolutions-v1.json" in source
    assert "evolutions_sha256=" in source
    assert "source_tree_sha256=" in source
    assert "attestation_sha256=" in source
    assert "source-tree.tar" in source
    assert "reclaim-lock --stale-after" in source
    assert "openjarvis-relay.service openjarvis-relay-tls.service" in source
    assert "https://${RELAY_TLS_NAME}:8443/health" in source
    assert "--cacert $Q_RELAY_CA" in source
    assert "config.agent.system_prompt_path == ''" in source
    assert "AVA_BUNDLED_PERSONA_ONLY=1" in source
    assert "persona=_DEFAULT_PERSONA.resolve()" in source
    assert "persona.is_relative_to(root)" in source
    assert "config.agent.default_system_prompt == persona.read_text" in source
    assert source.count('validate_runtime_policy "$RELEASE_PATH"') == 2
    assert 'validate_runtime_policy "$expected_target"' in source
    previous_validation_calls = [
        index
        for index in range(len(source))
        if source.startswith('validate_runtime_policy "$PREVIOUS_TARGET"', index)
    ]
    assert len(previous_validation_calls) == 2
    assert previous_validation_calls[0] < source.index('titre "2/7 Preparation')
    assert previous_validation_calls[-1] < arm_call
    assert "_lignes_git('', 3) is not None" in source
    assert source.index("EVOLUTIONS_SHA256=") < source.index(
        'titre "1/7 Verrou et bootstrap'
    )
