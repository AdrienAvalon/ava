"""Static contracts for the VM-compatible, attested Rust wheel builder."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import textwrap
import uuid
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-rust-attested.sh"
DOCKERFILE = ROOT / "deploy/docker/Dockerfile.rust-builder"
CANONICALIZER = ROOT / "deploy/docker/canonicalize-rust-wheel.py"
SOURCE_DATE_EPOCH = 315_532_800
WHEEL_NAME = "openjarvis_rust-0.1.0-cp312-cp312-manylinux_2_36_x86_64.whl"
DOCKERFILE_FRONTEND = (
    "# syntax=docker/dockerfile:1.7.0@"
    "sha256:4611ea7b7d89ce41ec5c63df83076ccec3fe8daa32a2d9c96e5decb72e9a8d67"
)

PYTHON_IMAGE = (
    "python:3.12.13-slim-bookworm@"
    "sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf"
)
RUST_IMAGE = (
    "rust:1.88.0-bookworm@"
    "sha256:4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0"
)


def _dockerfile_instructions() -> list[str]:
    instructions: list[str] = []
    current: list[str] = []
    for raw_line in DOCKERFILE.read_text().splitlines():
        line = raw_line.strip()
        if not current and (not line or line.startswith("#")):
            continue
        current.append(line.removesuffix("\\").strip())
        if not line.endswith("\\"):
            instructions.append(" ".join(current))
            current = []
    assert not current
    return instructions


def test_builder_shell_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_builder_uses_only_pinned_bookworm_toolchains() -> None:
    dockerfile = DOCKERFILE.read_text()
    script = SCRIPT.read_text()

    assert dockerfile.splitlines()[0] == DOCKERFILE_FRONTEND
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
    assert "SOURCE_DATE_EPOCH=315532800" in dockerfile
    assert "LANG=C.UTF-8" in dockerfile
    assert "LC_ALL=C.UTF-8" in dockerfile
    assert "TZ=UTC" in dockerfile
    assert "CARGO_INCREMENTAL=0" in dockerfile

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
    canonicalizer_call = dockerfile.index(
        "python3 /usr/local/libexec/ava-canonicalize-rust-wheel.py"
    )
    assert dockerfile.index("maturin build") < canonicalizer_call
    assert canonicalizer_call < dockerfile.index("import openjarvis_rust")
    assert dockerfile.index("maturin build") < dockerfile.index(
        "import openjarvis_rust"
    )


def test_rust_source_executes_only_offline_in_an_empty_environment() -> None:
    instructions = _dockerfile_instructions()
    rust_copy = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction == "COPY rust/ rust/"
    )
    final_stage = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction == "FROM scratch AS release"
    )
    source_runs = [
        instruction
        for instruction in instructions[rust_copy + 1 : final_stage]
        if instruction.startswith("RUN ")
    ]
    fetch_runs = [
        instruction for instruction in source_runs if "cargo fetch" in instruction
    ]
    assert len(fetch_runs) == 1
    assert "--manifest-path rust/Cargo.toml --locked" in fetch_runs[0]
    assert "--network=none" not in fetch_runs[0]
    assert "env -i" in fetch_runs[0]

    for instruction in source_runs:
        if instruction == fetch_runs[0]:
            continue
        assert instruction.startswith("RUN --network=none ")
        assert "env -i" in instruction
        assert "proxy" not in instruction.lower()
        assert "secret" not in instruction.lower()

    cargo_test = next(
        instruction for instruction in source_runs if "cargo test" in instruction
    )
    maturin_build = next(
        instruction for instruction in source_runs if "maturin build" in instruction
    )
    for instruction in (cargo_test, maturin_build):
        assert "--locked" in instruction and "--offline" in instruction
        assert "CARGO_NET_OFFLINE=true" in instruction
        for variable in (
            "PATH=",
            "CARGO_HOME=",
            "CARGO_TARGET_DIR=",
            "RUSTUP_HOME=",
            "SOURCE_DATE_EPOCH=315532800",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "TZ=UTC",
        ):
            assert variable in instruction


def test_git_revision_enters_only_the_artifact_only_scratch_stage() -> None:
    dockerfile = DOCKERFILE.read_text()
    builder, final = dockerfile.split("FROM scratch AS release", 1)

    assert "AVA_GIT_SHA" not in builder
    assert final.count("ARG AVA_GIT_SHA") == 1
    assert 'org.opencontainers.image.revision="${AVA_GIT_SHA}"' in final
    assert "COPY --from=builder /artifacts/ /artifacts/" in final
    assert "RUN " not in final


def test_script_builds_an_exact_commit_without_laptop_compilation() -> None:
    source = SCRIPT.read_text()

    assert 'run_git -C "$ROOT" diff --quiet' in source
    assert 'run_git -C "$ROOT" diff --cached --quiet' in source
    assert 'run_git -C "$ROOT" ls-files --others --exclude-standard' in source
    assert '"$DOCKERFILE_REL" "$CANONICALIZER_REL" rust' in source
    assert 'run_git -C "$ROOT" cat-file -e "${GIT_SHA}:${CANONICALIZER_REL}"' in source
    assert source.count('"$DOCKER_BIN" build') == 1
    assert "build_once 1" in source
    assert "build_once 2" in source
    assert "--no-cache" in source
    assert 'cmp --silent -- "$FIRST_WHEEL" "$SECOND_WHEEL"' in source
    assert '"$DOCKER_BIN" image inspect' in source
    assert '"$DOCKER_BIN" cp "$container_id:/artifacts/."' in source
    assert "cargo test" not in source
    assert "maturin build" not in source
    assert "AVA_CARGO_BIN" not in source
    assert "AVA_MATURIN_BIN" not in source
    assert "AVA_RUST_PYTHON" not in source


def test_every_git_read_uses_the_empty_environment_wrapper() -> None:
    source = SCRIPT.read_text()

    assert "ENV_BIN='/usr/bin/env'" in source
    assert "GIT_BIN='/usr/bin/git'" in source
    assert '"$ENV_BIN" -i' in source
    for assignment in (
        'PATH="$SAFE_EXEC_PATH"',
        "HOME=/dev/null/ava-git-home-does-not-exist",
        "XDG_CONFIG_HOME=/dev/null/ava-git-xdg-does-not-exist",
        "LC_ALL=C",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_NO_REPLACE_OBJECTS=1",
    ):
        assert assignment in source
    assert re.search(r"(?m)^\s*git(?:\s|$)", source) is None
    assert source.count('run_git -C "$ROOT"') == 11


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
    assert 'run_git -C "$ROOT" archive --format=tar "$GIT_SHA" rust' in source
    assert re.search(r"WHEEL_SHA256=.*sha256sum", source)
    assert "RENAME_NOREPLACE" in source
    assert 'atomic_publish_directory "$PUBLISH_STAGING" "$OUTPUT_ROOT"' in source
    assert 'mktemp -d "$OUTPUT_PARENT/.${OUTPUT_LEAF}.tmp.XXXXXXXX"' in source
    assert 'mkdir -p -- "$OUTPUT_ROOT"' not in source
    assert re.search(r"chmod[^\n]*OUTPUT_(?:ROOT|PARENT)", source) is None


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

    assert 'image_ref="ava-rust-builder:${GIT_SHA}-${build_number}-$$"' in source
    assert '"$DOCKER_BIN" image rm -f "$image_ref"' in source
    assert '"$DOCKER_BIN" rm -f "$container_id"' in source
    assert '"$image_ref" /ava-artifact-export-only' in source


def _sbom(*, serial: str, timestamp: str) -> bytes:
    return json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "version": 1,
            "serialNumber": f"urn:uuid:{serial}",
            "metadata": {
                "timestamp": timestamp,
                "component": {
                    "type": "library",
                    "name": "openjarvis-python",
                    "version": "0.1.0",
                },
            },
            "components": [
                {
                    "type": "library",
                    "name": "content-owned-dependency",
                    "version": "1.2.3",
                }
            ],
        },
        indent=2,
    ).encode("utf-8")


def _wheel_payload(*, variant: int, native_payload: bytes = b"native-payload") -> bytes:
    dist_info = "openjarvis_rust-0.1.0.dist-info"
    members = [
        ("openjarvis_rust/__init__.py", b"from .openjarvis_rust import *\n"),
        (
            "openjarvis_rust/openjarvis_rust.cpython-312-x86_64-linux-gnu.so",
            native_payload,
        ),
        (f"{dist_info}/METADATA", b"Name: openjarvis-rust\nVersion: 0.1.0\n"),
        (f"{dist_info}/WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"),
        (
            f"{dist_info}/sboms/openjarvis-python.cyclonedx.json",
            _sbom(
                serial=(
                    "d4ae9bb1-96b6-49c1-a177-3c0ac1494699"
                    if variant == 1
                    else "2d12ac1c-3035-42fa-86fd-9aad20f953ec"
                ),
                timestamp=(
                    "2026-08-16T10:35:37.745063020Z"
                    if variant == 1
                    else "2031-01-02T03:04:05.123456789Z"
                ),
            ),
        ),
        (f"{dist_info}/RECORD", b"producer-specific,sha256=discarded,1\r\n"),
    ]
    if variant == 2:
        members.reverse()
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream,
        mode="w",
        compression=zipfile.ZIP_STORED if variant == 1 else zipfile.ZIP_DEFLATED,
        compresslevel=None if variant == 1 else 1,
    ) as archive:
        archive.comment = b"producer-one" if variant == 1 else b"producer-two"
        for name, payload in members:
            info = zipfile.ZipInfo(
                name,
                date_time=(2024, 1, 2, 3, 4, 6)
                if variant == 1
                else (2030, 12, 31, 23, 58, 58),
            )
            info.create_system = 3
            info.compress_type = (
                zipfile.ZIP_STORED if variant == 1 else zipfile.ZIP_DEFLATED
            )
            info.external_attr = (
                stat.S_IFREG | (0o700 if variant == 1 else 0o664)
            ) << 16
            info.comment = b"variable-member-comment"
            archive.writestr(info, payload)
    return stream.getvalue()


def _run_canonicalizer(
    input_path: Path, output_path: Path
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(SOURCE_DATE_EPOCH)
    return subprocess.run(
        [
            sys.executable,
            str(CANONICALIZER),
            "--source-date-epoch",
            str(SOURCE_DATE_EPOCH),
            str(input_path),
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_canonicalizer_removes_sbom_and_zip_nondeterminism(tmp_path: Path) -> None:
    first_input = tmp_path / "first.whl"
    second_input = tmp_path / "second.whl"
    first_output = tmp_path / "first-canonical.whl"
    second_output = tmp_path / "second-canonical.whl"
    idempotent_output = tmp_path / "idempotent.whl"
    first_input.write_bytes(_wheel_payload(variant=1))
    second_input.write_bytes(_wheel_payload(variant=2))

    assert _run_canonicalizer(first_input, first_output).returncode == 0
    assert _run_canonicalizer(second_input, second_output).returncode == 0
    assert first_output.read_bytes() == second_output.read_bytes()
    assert _run_canonicalizer(first_output, idempotent_output).returncode == 0
    assert idempotent_output.read_bytes() == first_output.read_bytes()

    with zipfile.ZipFile(first_output) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        assert names == sorted(names)
        assert archive.comment == b""
        for info in infos:
            expected_mode = 0o100755 if info.filename.endswith(".so") else 0o100644
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.external_attr >> 16 == expected_mode
            assert info.comment == info.extra == b""

        sbom_name = next(name for name in names if name.endswith(".cyclonedx.json"))
        sbom_payload = archive.read(sbom_name)
        sbom = json.loads(sbom_payload)
        assert (
            sbom_payload
            == (
                json.dumps(
                    sbom,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        assert sbom["metadata"]["timestamp"] == "1980-01-01T00:00:00Z"
        serial = sbom["serialNumber"].removeprefix("urn:uuid:")
        assert uuid.UUID(serial).version == 5

        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        assert [row[0] for row in rows] == names
        for name, encoded_digest, rendered_size in rows:
            if name == record_name:
                assert encoded_digest == rendered_size == ""
                continue
            payload = archive.read(name)
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(payload).digest()
            ).rstrip(b"=")
            assert encoded_digest == f"sha256={expected.decode('ascii')}"
            assert rendered_size == str(len(payload))


def test_canonicalizer_accepts_optional_cyclonedx_serial_number(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "without-serial.whl"
    output_path = tmp_path / "canonical.whl"
    input_path.write_bytes(_wheel_payload(variant=1))

    with zipfile.ZipFile(input_path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    sbom_name = next(name for name in members if name.endswith(".cyclonedx.json"))
    sbom = json.loads(members[sbom_name])
    del sbom["serialNumber"]
    members[sbom_name] = json.dumps(sbom).encode("utf-8")
    with zipfile.ZipFile(input_path, mode="w") as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)

    result = _run_canonicalizer(input_path, output_path)

    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output_path) as archive:
        canonical_sbom = json.loads(archive.read(sbom_name))
    serial = canonical_sbom["serialNumber"].removeprefix("urn:uuid:")
    assert uuid.UUID(serial).version == 5


def _unsafe_wheel(path: Path, *, member_name: str, mode: int) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        info = zipfile.ZipInfo(member_name)
        info.create_system = 3
        info.external_attr = mode << 16
        archive.writestr(info, b"unsafe")


def test_canonicalizer_rejects_traversal_and_non_regular_members(
    tmp_path: Path,
) -> None:
    traversal = tmp_path / "traversal.whl"
    symlink = tmp_path / "symlink.whl"
    _unsafe_wheel(traversal, member_name="../escape", mode=stat.S_IFREG | 0o644)
    _unsafe_wheel(symlink, member_name="link", mode=stat.S_IFLNK | 0o777)

    for position, input_path in enumerate((traversal, symlink), start=1):
        output_path = tmp_path / f"unsafe-output-{position}.whl"
        result = _run_canonicalizer(input_path, output_path)
        assert result.returncode != 0
        assert not output_path.exists()


def _patch_zip_field(
    payload: bytes, *, local_offset: int, central_offset: int, value: int
) -> bytes:
    patched = bytearray(payload)
    central = patched.index(b"PK\x01\x02")
    width = 2 if value <= 0xFFFF else 4
    patched[local_offset : local_offset + width] = value.to_bytes(width, "little")
    patched[central + central_offset : central + central_offset + width] = (
        value.to_bytes(width, "little")
    )
    return bytes(patched)


def test_canonicalizer_rejects_duplicates_encryption_and_declared_bombs(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.whl"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, mode="w") as archive:
            for payload in (b"first", b"second"):
                info = zipfile.ZipInfo("duplicate")
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, payload)

    base = tmp_path / "base.whl"
    _unsafe_wheel(base, member_name="regular", mode=stat.S_IFREG | 0o644)
    base_payload = base.read_bytes()
    original_flags = struct.unpack_from("<H", base_payload, 6)[0]
    encrypted = tmp_path / "encrypted.whl"
    encrypted.write_bytes(
        _patch_zip_field(
            base_payload,
            local_offset=6,
            central_offset=8,
            value=original_flags | 0x1,
        )
    )
    declared_bomb = tmp_path / "declared-bomb.whl"
    declared_bomb.write_bytes(
        _patch_zip_field(
            base_payload,
            local_offset=22,
            central_offset=24,
            value=48 * 1024 * 1024 + 1,
        )
    )

    for position, input_path in enumerate(
        (duplicate, encrypted, declared_bomb), start=1
    ):
        output_path = tmp_path / f"rejected-output-{position}.whl"
        result = _run_canonicalizer(input_path, output_path)
        assert result.returncode != 0
        assert not output_path.exists()


def _write_fake_docker(path: Path) -> None:
    source = f"#!{sys.executable}\n" + textwrap.dedent(
        """\
            import os
            import re
            import shutil
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            state = Path(os.environ["FAKE_DOCKER_STATE"])
            state.mkdir(parents=True, exist_ok=True)
            log = state / "calls.log"
            with log.open("a", encoding="utf-8") as stream:
                stream.write(" ".join(args) + "\\n")

            def ordinal(reference: str) -> int:
                match = re.search(r"-(1|2)-[0-9]+$", reference)
                if match is None:
                    raise SystemExit("tag without build ordinal")
                return int(match.group(1))

            if args[0] == "build":
                if args.count("--no-cache") != 1:
                    raise SystemExit("missing --no-cache")
                raise SystemExit(0)
            if args[:2] == ["image", "inspect"]:
                rendered = args[args.index("--format") + 1]
                reference = args[-1]
                build = ordinal(reference)
                if rendered == "{{.Id}}":
                    print("sha256:" + str(build) * 64)
                elif "org.opencontainers.image.revision" in rendered:
                    match = re.search(r":([0-9a-f]{40})-[12]-[0-9]+$", reference)
                    if match is None:
                        raise SystemExit("missing revision")
                    print(match.group(1))
                elif rendered == "{{.Os}}":
                    print("linux")
                elif rendered == "{{.Architecture}}":
                    print("amd64")
                else:
                    raise SystemExit("unknown inspect format")
                raise SystemExit(0)
            if args[0] == "create":
                if args[-1] != "/ava-artifact-export-only":
                    raise SystemExit("scratch export command is missing")
                build = ordinal(args[-2])
                container = ("a" if build == 1 else "b") * 64
                (state / container).write_text(str(build), encoding="ascii")
                print(container)
                raise SystemExit(0)
            if args[0] == "cp":
                container = args[1].split(":", 1)[0]
                build = int((state / container).read_text(encoding="ascii"))
                wheels = os.environ["FAKE_DOCKER_WHEELS"].split(os.pathsep)
                source = Path(wheels[build - 1])
                shutil.copy2(source, Path(args[2]) / source.name)
                raise SystemExit(0)
            if args[0] == "rm" or args[:2] == ["image", "rm"]:
                raise SystemExit(0)
            raise SystemExit("unsupported fake Docker invocation")
            """
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _release_builder_fixture(
    tmp_path: Path, *, second_payload: bytes | None = None
) -> tuple[Path, Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "deploy/docker").mkdir(parents=True)
    (repository / "rust").mkdir()
    shutil.copy2(SCRIPT, repository / "scripts/build-rust-attested.sh")
    shutil.copy2(DOCKERFILE, repository / "deploy/docker/Dockerfile.rust-builder")
    shutil.copy2(CANONICALIZER, repository / "deploy/docker/canonicalize-rust-wheel.py")
    (repository / "rust/placeholder").write_text("exact source\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ava-tests@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Ava tests"], cwd=repository, check=True
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "test fixture"], cwd=repository, check=True)

    first_wheel = tmp_path / "first-build" / WHEEL_NAME
    second_wheel = tmp_path / "second-build" / WHEEL_NAME
    first_wheel.parent.mkdir()
    second_wheel.parent.mkdir()
    first_payload = _wheel_payload(variant=1)
    first_wheel.write_bytes(first_payload)
    second_wheel.write_bytes(second_payload or first_payload)
    fake_docker = tmp_path / "fake-docker"
    _write_fake_docker(fake_docker)
    state = tmp_path / "docker-state"
    artifact_dir = tmp_path / "artifacts"
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "AVA_DOCKER_BIN": str(fake_docker),
            "AVA_RUST_ARTIFACT_DIR": str(artifact_dir),
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_DOCKER_WHEELS": os.pathsep.join(
                (str(first_wheel), str(second_wheel))
            ),
            "TMPDIR": str(temporary_parent),
        }
    )
    return repository, artifact_dir, state, environment


def _run_release_builder(
    repository: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repository / "scripts/build-rust-attested.sh")],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_builder_requires_two_fresh_identical_builds(tmp_path: Path) -> None:
    repository, artifact_dir, state, environment = _release_builder_fixture(tmp_path)
    parent_before = artifact_dir.parent.stat()
    result = _run_release_builder(repository, environment)

    assert result.returncode == 0, result.stderr
    parent_after = artifact_dir.parent.stat()
    assert (parent_after.st_ino, parent_after.st_mode) == (
        parent_before.st_ino,
        parent_before.st_mode,
    )
    calls = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    builds = [line for line in calls if line.startswith("build ")]
    assert len(builds) == 2
    assert all(line.split().count("--no-cache") == 1 for line in builds)
    assert builds[0] != builds[1]
    published = artifact_dir / WHEEL_NAME
    attestation = artifact_dir / f"{WHEEL_NAME}.attestation"
    assert published.is_file() and attestation.is_file()
    expected_sha256 = hashlib.sha256(published.read_bytes()).hexdigest()
    assert f"wheel_sha256={expected_sha256}\n" in attestation.read_text()


def test_release_builder_ignores_alternate_git_dir_and_work_tree(
    tmp_path: Path,
) -> None:
    repository, artifact_dir, _state, environment = _release_builder_fixture(tmp_path)
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    alternate = tmp_path / "alternate-repository"
    alternate.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=alternate, check=True)
    subprocess.run(
        ["git", "config", "user.email", "alternate@example.invalid"],
        cwd=alternate,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Alternate repository"],
        cwd=alternate,
        check=True,
    )
    (alternate / "decoy").write_text("not Ava\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=alternate, check=True)
    subprocess.run(["git", "commit", "-qm", "alternate"], cwd=alternate, check=True)
    alternate_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=alternate, text=True
    ).strip()
    assert alternate_sha != expected_sha
    environment["GIT_DIR"] = str(alternate / ".git")
    environment["GIT_WORK_TREE"] = str(alternate)

    result = _run_release_builder(repository, environment)

    assert result.returncode == 0, result.stderr
    attestation = artifact_dir / f"{WHEEL_NAME}.attestation"
    rendered = attestation.read_text(encoding="utf-8")
    assert f"git_sha={expected_sha}\n" in rendered
    assert alternate_sha not in rendered


def test_release_builder_ignores_repository_replace_refs(tmp_path: Path) -> None:
    repository, artifact_dir, _state, environment = _release_builder_fixture(tmp_path)
    expected_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    empty_tree = subprocess.check_output(
        ["git", "mktree"], cwd=repository, input="", text=True
    ).strip()
    replacement_sha = subprocess.check_output(
        ["git", "commit-tree", empty_tree, "-m", "replacement without source"],
        cwd=repository,
        text=True,
    ).strip()
    subprocess.run(
        ["git", "replace", expected_sha, replacement_sha],
        cwd=repository,
        check=True,
    )
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{expected_sha}:rust"],
            cwd=repository,
            check=False,
        ).returncode
        != 0
    )
    subprocess.run(
        ["git", "--no-replace-objects", "cat-file", "-e", f"{expected_sha}:rust"],
        cwd=repository,
        check=True,
    )

    result = _run_release_builder(repository, environment)

    assert result.returncode == 0, result.stderr
    attestation = artifact_dir / f"{WHEEL_NAME}.attestation"
    assert f"git_sha={expected_sha}\n" in attestation.read_text(encoding="utf-8")


def test_release_builder_rejects_symlink_and_unsticky_shared_tmpdir(
    tmp_path: Path,
) -> None:
    repository, _artifact_dir, state, environment = _release_builder_fixture(tmp_path)
    private_target = tmp_path / "private-target"
    private_target.mkdir(mode=0o700)
    symlink_parent = tmp_path / "temporary-symlink"
    symlink_parent.symlink_to(private_target, target_is_directory=True)
    hostile_parent = tmp_path / "temporary-hostile"
    hostile_parent.mkdir(mode=0o700)
    hostile_parent.chmod(0o777)

    for candidate in (symlink_parent, hostile_parent):
        case_environment = environment.copy()
        case_environment["TMPDIR"] = str(candidate)
        result = _run_release_builder(repository, case_environment)

        assert result.returncode != 0
        assert not (state / "calls.log").exists()
        assert not list(candidate.glob("ava-rust-build.*"))


def test_release_builder_publishes_nothing_when_builds_diverge(tmp_path: Path) -> None:
    repository, artifact_dir, state, environment = _release_builder_fixture(
        tmp_path, second_payload=_wheel_payload(variant=1, native_payload=b"divergent")
    )
    result = _run_release_builder(repository, environment)

    assert result.returncode != 0
    assert "ne produisent pas les memes octets" in result.stderr
    assert not artifact_dir.exists()
    calls = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len([line for line in calls if line.startswith("build ")]) == 2


def test_release_builder_refuses_preexisting_or_symlink_output_without_mutation(
    tmp_path: Path,
) -> None:
    for kind in ("directory", "symlink"):
        case_root = tmp_path / kind
        case_root.mkdir()
        repository, artifact_dir, state, environment = _release_builder_fixture(
            case_root
        )
        protected = case_root / "protected"
        if kind == "directory":
            artifact_dir.mkdir(mode=0o755)
            protected = artifact_dir / "sentinel"
        else:
            protected.mkdir()
            artifact_dir.symlink_to(protected, target_is_directory=True)
            protected = protected / "sentinel"
        protected.write_text("do not mutate\n", encoding="utf-8")
        before = protected.read_bytes()

        result = _run_release_builder(repository, environment)

        assert result.returncode != 0
        assert protected.read_bytes() == before
        assert not (state / "calls.log").exists()
        if kind == "symlink":
            assert artifact_dir.is_symlink()


def test_release_builder_rejects_system_output_before_any_effect(
    tmp_path: Path,
) -> None:
    repository, _artifact_dir, state, environment = _release_builder_fixture(tmp_path)
    system_output = Path("/etc/shared")
    existed_before = system_output.exists() or system_output.is_symlink()
    metadata_before = system_output.lstat() if existed_before else None
    environment["AVA_RUST_ARTIFACT_DIR"] = str(system_output)

    result = _run_release_builder(repository, environment)

    assert result.returncode != 0
    assert not (state / "calls.log").exists()
    if existed_before:
        metadata_after = system_output.lstat()
        assert metadata_before is not None
        assert (
            metadata_after.st_ino,
            metadata_after.st_mode,
            metadata_after.st_uid,
            metadata_after.st_gid,
            metadata_after.st_mtime_ns,
        ) == (
            metadata_before.st_ino,
            metadata_before.st_mode,
            metadata_before.st_uid,
            metadata_before.st_gid,
            metadata_before.st_mtime_ns,
        )
    else:
        assert not system_output.exists() and not system_output.is_symlink()


def test_atomic_publish_never_replaces_a_racing_output(tmp_path: Path) -> None:
    repository, artifact_dir, state, environment = _release_builder_fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    python_wrapper = fake_bin / "python3"
    python_wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import sys
            from pathlib import Path

            target = Path(os.environ["FAKE_ATOMIC_RACE_TARGET"])
            target.mkdir()
            (target / "sentinel").write_text("competitor\\n", encoding="utf-8")
            os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
            """
        ),
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FAKE_ATOMIC_RACE_TARGET"] = str(artifact_dir)

    result = _run_release_builder(repository, environment)

    assert result.returncode != 0
    assert (artifact_dir / "sentinel").read_text(encoding="utf-8") == "competitor\n"
    assert not list(artifact_dir.glob("*.whl"))
    assert not list(artifact_dir.glob("*.attestation"))
    assert not list(artifact_dir.parent.glob(f".{artifact_dir.name}.tmp.*"))
    calls = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len([line for line in calls if line.startswith("build ")]) == 2
