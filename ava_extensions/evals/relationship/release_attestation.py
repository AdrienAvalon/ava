"""Build a relationship-shadow attestation from one deployed Ava release.

The command has no provider/model/adapter override.  Those values are derived
from the immutable release manifest and the exact TOML configuration bytes used
by the deployment.  Configuration contents and provider credentials are never
copied to the attestation or diagnostic output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import tomllib

from .contracts import (
    RELEASE_ATTESTATION_SCHEMA_VERSION,
    ContractError,
    canonical_json_bytes,
    load_release_attestation,
    sha256_bytes,
)

_MODULE_PATH = Path(__file__)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^claude-[A-Za-z0-9._+-]{1,120}$")
_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$")
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


@dataclass(frozen=True, slots=True)
class ReleaseAttestationResult:
    """Non-secret metadata needed to pin the generated document externally."""

    output_path: Path
    sha256: str
    git_sha: str


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
        ):
            raise ContractError("source d'attestation modifiee pendant la lecture")
        return payload
    except OSError as exc:
        raise ContractError("source d'attestation illisible") from exc
    finally:
        os.close(descriptor)


def _release_root(value: str | Path) -> tuple[Path, Path]:
    requested = Path(value).expanduser().absolute()
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ContractError("release Ava introuvable") from exc
    if not resolved.is_dir() or _GIT_SHA_RE.fullmatch(resolved.name) is None:
        raise ContractError("release Ava non immutable ou mal nommee")
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
    output_directory: str | Path,
) -> ReleaseAttestationResult:
    """Derive and publish one immutable, non-secret release attestation."""

    requested_root, resolved_root = _release_root(release_root)
    ready_path = resolved_root / ".ava-ready"
    manifest_path = resolved_root / ".ava-release"
    _strict_regular_bytes(ready_path, max_bytes=1024, allow_empty=True)
    manifest_payload = _strict_regular_bytes(
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest = _parse_release_manifest(manifest_payload)
    if manifest["git_sha"] != resolved_root.name:
        raise ContractError("release et manifeste Git divergents")
    config_payload = _strict_regular_bytes(
        Path(config_path),
        max_bytes=_MAX_CONFIG_BYTES,
    )
    provider, model, adapter = _configured_anthropic_engine(config_payload)
    # Detect an atomic `ava-current` switch while the evidence was being read.
    if requested_root.resolve(strict=True) != resolved_root:
        raise ContractError("release active basculee pendant l'attestation")

    config_sha256 = sha256_bytes(config_payload)
    manifest_sha256 = sha256_bytes(manifest_payload)
    document = {
        "artifact": {"manifest_sha256": manifest_sha256},
        "attestation_id": (
            "ava-release-shadow-"
            f"{manifest['git_sha'][:12]}-{config_sha256.removeprefix('sha256:')[:12]}"
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
            "git_sha": manifest["git_sha"],
            "repository": "repo://ava",
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive an Ava relationship-shadow release attestation"
    )
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = generate_release_attestation(
            release_root=args.release_root,
            config_path=args.config,
            output_directory=args.output_dir,
        )
    except (ContractError, OSError, ValueError):
        print("release attestation generation failed", file=sys.stderr)
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
    "ReleaseAttestationResult",
    "generate_release_attestation",
    "main",
]
