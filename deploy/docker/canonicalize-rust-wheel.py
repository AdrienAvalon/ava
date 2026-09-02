#!/usr/bin/env python3
"""Canonicalise the release Rust wheel without trusting ZIP metadata.

The generated CycloneDX payload deliberately excludes the Git revision from its
identity.  Marker-only commits that build the same native payload must therefore
produce the same wheel bytes.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_COUNT = 128
MAX_MEMBER_BYTES = 48 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
ZIP_MINIMUM_EPOCH = 315_532_800
ZIP_MAXIMUM_EPOCH = 4_354_819_199
SBOM_NAMESPACE = uuid.UUID("88535b8c-25b7-52a5-b43f-8417d4f55c30")


class CanonicalisationError(ValueError):
    """The input wheel is outside the closed release contract."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalisationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _fixed_timestamp(source_date_epoch: int) -> str:
    if not ZIP_MINIMUM_EPOCH <= source_date_epoch <= ZIP_MAXIMUM_EPOCH:
        raise CanonicalisationError("SOURCE_DATE_EPOCH is outside the ZIP range")
    return (
        datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _canonicalise_sbom(payload: bytes, source_date_epoch: int) -> bytes:
    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_duplicate_safe_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalisationError("CycloneDX SBOM is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise CanonicalisationError("CycloneDX SBOM root must be an object")
    if (
        document.get("bomFormat") != "CycloneDX"
        or document.get("specVersion") != "1.5"
        or document.get("version") != 1
    ):
        raise CanonicalisationError("unexpected CycloneDX identity")
    metadata = document.get("metadata")
    serial = document.get("serialNumber")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("timestamp"), str):
        raise CanonicalisationError("CycloneDX metadata timestamp is missing")
    # CycloneDX 1.5 makes serialNumber optional and maturin 1.14 omits it.  If
    # an upstream producer supplies one, validate it before replacing it with
    # our deterministic UUID; otherwise canonicalisation creates it.
    if serial is not None:
        if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
            raise CanonicalisationError("CycloneDX serialNumber is not a UUID URN")
        try:
            uuid.UUID(serial.removeprefix("urn:uuid:"))
        except ValueError as exc:
            raise CanonicalisationError("CycloneDX serialNumber is invalid") from exc

    metadata["timestamp"] = _fixed_timestamp(source_date_epoch)
    document_without_serial = dict(document)
    document_without_serial.pop("serialNumber", None)
    payload_digest = hashlib.sha256(
        _canonical_json(document_without_serial)
    ).hexdigest()
    document["serialNumber"] = f"urn:uuid:{uuid.uuid5(SBOM_NAMESPACE, payload_digest)}"
    return _canonical_json(document)


def _validate_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or not name.isascii()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != name
    ):
        raise CanonicalisationError(f"unsafe wheel member path: {name!r}")


def _read_members(input_path: Path) -> tuple[dict[str, bytes], str, str]:
    input_stat = input_path.lstat()
    if not stat.S_ISREG(input_stat.st_mode) or input_stat.st_nlink != 1:
        raise CanonicalisationError("input wheel must be one regular, unlinked file")
    if input_stat.st_size <= 0 or input_stat.st_size > MAX_ARCHIVE_BYTES:
        raise CanonicalisationError("input wheel size is outside the release contract")

    members: dict[str, bytes] = {}
    total_size = 0
    try:
        with zipfile.ZipFile(input_path, mode="r") as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_MEMBER_COUNT:
                raise CanonicalisationError(
                    "wheel member count is outside the contract"
                )
            for info in infos:
                _validate_member_name(info.filename)
                if info.filename in members:
                    raise CanonicalisationError("duplicate wheel member")
                mode = info.external_attr >> 16
                if (
                    info.create_system != 3
                    or info.is_dir()
                    or not stat.S_ISREG(mode)
                    or info.flag_bits & 0x1
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise CanonicalisationError(
                        f"unsupported wheel member type: {info.filename}"
                    )
                if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                    raise CanonicalisationError("wheel member is too large")
                total_size += info.file_size
                if total_size > MAX_TOTAL_MEMBER_BYTES:
                    raise CanonicalisationError(
                        "wheel expands beyond the release limit"
                    )
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size
                    > max(1, info.compress_size) * MAX_COMPRESSION_RATIO
                ):
                    raise CanonicalisationError(
                        "wheel member compression ratio is unsafe"
                    )
                payload = archive.read(info)
                if len(payload) != info.file_size:
                    raise CanonicalisationError(
                        "wheel member size changed while reading"
                    )
                members[info.filename] = payload
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise CanonicalisationError("invalid wheel ZIP container") from exc

    records = [name for name in members if name.endswith(".dist-info/RECORD")]
    sboms = [
        name
        for name in members
        if name.endswith(".dist-info/sboms/openjarvis-python.cyclonedx.json")
    ]
    if len(records) != 1 or len(sboms) != 1:
        raise CanonicalisationError(
            "wheel must contain exactly one RECORD and Rust SBOM"
        )
    record_root = records[0].removesuffix("RECORD")
    if not sboms[0].startswith(record_root):
        raise CanonicalisationError(
            "RECORD and CycloneDX SBOM belong to different wheels"
        )
    return members, records[0], sboms[0]


def _record_payload(members: dict[str, bytes], record_name: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name in sorted(members):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        payload = members[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, f"sha256={digest.decode('ascii')}", str(len(payload))))
    return stream.getvalue().encode("utf-8")


def _write_canonical_wheel(
    output_path: Path, members: dict[str, bytes], source_date_epoch: int
) -> None:
    timestamp = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
    zip_timestamp = (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second - (timestamp.second % 2),
    )
    output_path.parent.mkdir(parents=False, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for name in sorted(members):
                info = zipfile.ZipInfo(filename=name, date_time=zip_timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (
                    stat.S_IFREG | (0o755 if name.endswith(".so") else 0o644)
                ) << 16
                info.extra = b""
                info.comment = b""
                archive.writestr(
                    info, members[name], compress_type=zipfile.ZIP_DEFLATED
                )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def canonicalise_wheel(
    input_path: Path, output_path: Path, *, source_date_epoch: int
) -> None:
    """Validate and rewrite one Rust wheel into a deterministic ZIP."""
    if input_path.resolve(strict=False) == output_path.resolve(strict=False):
        raise CanonicalisationError("input and output wheel paths must differ")
    try:
        output_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise CanonicalisationError("output wheel already exists")
    configured_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if configured_epoch is not None and configured_epoch != str(source_date_epoch):
        raise CanonicalisationError(
            "SOURCE_DATE_EPOCH environment and argument diverge"
        )
    _fixed_timestamp(source_date_epoch)
    members, record_name, sbom_name = _read_members(input_path)
    members[sbom_name] = _canonicalise_sbom(members[sbom_name], source_date_epoch)
    members[record_name] = _record_payload(members, record_name)
    _write_canonical_wheel(output_path, members, source_date_epoch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    args = parser.parse_args()
    try:
        canonicalise_wheel(
            args.input, args.output, source_date_epoch=args.source_date_epoch
        )
    except (CanonicalisationError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
