#!/usr/bin/env python3
"""Generate the closed, offline Ava runtime wheelhouse selection manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path

SCHEMA_VERSION = "ava.runtime-wheelhouse/v1"
TARGET = {
    "implementation": "cpython",
    "platform": "linux_x86_64",
    "python_version": "3.12.13",
}
WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$")
MAX_REQUIREMENTS_BYTES = 16 * 1024 * 1024
MAX_LOCK_BYTES = 64 * 1024 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_WHEELHOUSE_BYTES = 2 * 1024 * 1024 * 1024
MAX_WHEEL_COUNT = 1_000
READ_SIZE = 1024 * 1024


class ManifestError(ValueError):
    """An input or output falls outside the closed manifest contract."""


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_regular(
    path: str | Path,
    *,
    maximum_bytes: int,
    directory_fd: int | None = None,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
        ):
            raise ManifestError(f"unsafe or oversized regular input: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, metadata


def _digest_regular(
    path: str | Path,
    *,
    maximum_bytes: int,
    directory_fd: int | None = None,
) -> tuple[str, int]:
    descriptor, before = _open_regular(
        path, maximum_bytes=maximum_bytes, directory_fd=directory_fd
    )
    digest = hashlib.sha256()
    observed_size = 0
    try:
        while True:
            chunk = os.read(descriptor, READ_SIZE)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > maximum_bytes:
                raise ManifestError(f"input grew beyond its size limit: {path}")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if observed_size != before.st_size or _metadata_identity(
            before
        ) != _metadata_identity(after):
            raise ManifestError(f"input changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}", observed_size


def _wheelhouse_entries(wheelhouse: Path) -> list[dict[str, str]]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    directory_fd = os.open(wheelhouse, flags)
    try:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise ManifestError("wheelhouse is not a directory")
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise ManifestError("wheelhouse cannot be listed") from exc
        if not 1 <= len(names) <= MAX_WHEEL_COUNT:
            raise ManifestError("wheelhouse file count is outside the contract")
        if len(names) != len(set(names)) or len(names) != len(
            {name.casefold() for name in names}
        ):
            raise ManifestError("wheelhouse contains duplicate names")

        entries: list[dict[str, str]] = []
        digests: set[str] = set()
        total_bytes = 0
        for filename in sorted(names, key=os.fsencode):
            try:
                filename.encode("ascii")
            except UnicodeError as exc:
                raise ManifestError("wheel filename is not ASCII") from exc
            if WHEEL_RE.fullmatch(filename) is None:
                raise ManifestError(f"wheelhouse contains a non-wheel: {filename}")
            digest, size = _digest_regular(
                filename,
                maximum_bytes=MAX_WHEEL_BYTES,
                directory_fd=directory_fd,
            )
            total_bytes += size
            if total_bytes > MAX_WHEELHOUSE_BYTES:
                raise ManifestError("wheelhouse exceeds its aggregate size limit")
            if digest in digests:
                raise ManifestError("wheelhouse contains duplicate payloads")
            digests.add(digest)
            entries.append({"filename": filename, "sha256": digest})

        after = os.fstat(directory_fd)
        if _metadata_identity(before) != _metadata_identity(after):
            raise ManifestError("wheelhouse changed while hashing")
        return entries
    finally:
        os.close(directory_fd)


def _canonical_json(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _existing_output_metadata(parent_fd: int, filename: str) -> os.stat_result | None:
    try:
        metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ManifestError("output exists but is not one regular unlinked file")
    return metadata


def _write_output(output: Path, payload: bytes) -> None:
    if not output.name or output.name in {".", ".."}:
        raise ManifestError("explicit output filename is invalid")
    parent = output.parent if output.parent != Path("") else Path(".")
    parent_fd = os.open(
        parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    )
    temporary_name = f".{output.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    temporary_created = False
    try:
        original = _existing_output_metadata(parent_fd, output.name)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_created = True
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ManifestError("short write while creating manifest")
                view = view[written:]
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        current = _existing_output_metadata(parent_fd, output.name)
        if (original is None) != (current is None) or (
            original is not None
            and current is not None
            and _metadata_identity(original) != _metadata_identity(current)
        ):
            raise ManifestError("output changed while generating manifest")
        os.replace(
            temporary_name,
            output.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_created = False
        os.fsync(parent_fd)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def generate_manifest(
    *, wheelhouse: Path, requirements: Path, uv_lock: Path, output: Path
) -> None:
    """Hash one offline wheelhouse and atomically write its source-owned manifest."""
    requirements_sha256, _requirements_size = _digest_regular(
        requirements, maximum_bytes=MAX_REQUIREMENTS_BYTES
    )
    uv_lock_sha256, _lock_size = _digest_regular(uv_lock, maximum_bytes=MAX_LOCK_BYTES)
    entries = _wheelhouse_entries(wheelhouse)
    document: dict[str, object] = {
        "entries": entries,
        "requirements_sha256": requirements_sha256,
        "schema_version": SCHEMA_VERSION,
        "target": TARGET,
        "uv_lock_sha256": uv_lock_sha256,
    }
    _write_output(output, _canonical_json(document))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--uv-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        generate_manifest(
            wheelhouse=arguments.wheelhouse,
            requirements=arguments.requirements,
            uv_lock=arguments.uv_lock,
            output=arguments.output,
        )
    except (ManifestError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
