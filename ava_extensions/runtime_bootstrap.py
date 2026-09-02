"""No-site entrypoint shared by the sealed Ava service and causal shadow.

Invoke this source file with the release interpreter and both ``-I`` and ``-S``.
It adds only the three sealed project roots required by Ava; Python's standard
library paths remain those selected by the copied interpreter itself.
"""

from __future__ import annotations

import os
import runpy
import stat
import sys
from pathlib import Path

_RELATIVE_PATH = Path("ava_extensions/runtime_bootstrap.py")
_ALLOWED_DISPATCH = frozenset({"attest", "causal-pair", "compare", "serve", "shadow"})
_SEALED_RELEASES = Path("/var/lib/ava/releases")
_CURRENT_RELEASE = Path("/var/lib/ava/current")
_TRUSTED_UID = 0


class RuntimeBootstrapError(RuntimeError):
    """The process was not launched from a sealed, no-site Ava release."""


def _writable_by_effective_user(metadata: os.stat_result) -> bool:
    if os.geteuid() == metadata.st_uid:
        return bool(metadata.st_mode & stat.S_IWUSR)
    if metadata.st_gid in {os.getegid(), *os.getgroups()}:
        return bool(metadata.st_mode & stat.S_IWGRP)
    return bool(metadata.st_mode & stat.S_IWOTH)


def _direct_directory(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBootstrapError("sealed runtime directory unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or resolved != candidate
        or metadata.st_uid != _TRUSTED_UID
        or _writable_by_effective_user(metadata)
    ):
        raise RuntimeBootstrapError("sealed runtime directory is not immutable")
    return candidate


def _release_root() -> Path:
    source = Path(__file__).expanduser().absolute().resolve(strict=True)
    if source.parts[-len(_RELATIVE_PATH.parts) :] != _RELATIVE_PATH.parts:
        raise RuntimeBootstrapError("runtime bootstrap path is non-canonical")
    root = source.parents[len(_RELATIVE_PATH.parts) - 1]
    if root.parent != _SEALED_RELEASES:
        raise RuntimeBootstrapError("runtime bootstrap is outside sealed releases")
    for ancestor in (
        _SEALED_RELEASES.parents[2],
        _SEALED_RELEASES.parents[1],
        _SEALED_RELEASES.parent,
        _SEALED_RELEASES,
    ):
        _direct_directory(ancestor)
    release_root = _direct_directory(root)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise RuntimeBootstrapError("runtime bootstrap source unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _TRUSTED_UID
        or _writable_by_effective_user(metadata)
    ):
        raise RuntimeBootstrapError("runtime bootstrap source is not immutable")
    return release_root


def _assert_current_release(release_root: Path) -> None:
    try:
        parent = _direct_directory(_CURRENT_RELEASE.parent)
        metadata = _CURRENT_RELEASE.lstat()
        target = _CURRENT_RELEASE.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBootstrapError(
            "authoritative current release unavailable"
        ) from exc
    if (
        parent != _CURRENT_RELEASE.parent
        or not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _TRUSTED_UID
        or target != release_root
    ):
        raise RuntimeBootstrapError(
            "dispatch requires the authoritative current release"
        )


def _assert_no_site_interpreter(release_root: Path) -> None:
    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.ignore_environment != 1
        or not sys.flags.safe_path
    ):
        raise RuntimeBootstrapError("Ava requires python -I -S")
    if os.geteuid() == 0:
        raise RuntimeBootstrapError("Ava runtime must not execute as root")
    expected = release_root / ".venv" / "bin" / "python"
    try:
        executable = Path(sys.executable).expanduser().absolute().resolve(strict=True)
        expected_resolved = expected.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBootstrapError("sealed release interpreter unavailable") from exc
    if executable != expected_resolved:
        raise RuntimeBootstrapError("another interpreter launched Ava")


def _runtime_paths(release_root: Path) -> tuple[Path, Path, Path]:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return (
        _direct_directory(release_root),
        _direct_directory(release_root / "src"),
        _direct_directory(release_root / ".venv" / "lib" / version / "site-packages"),
    )


def _install_runtime_paths(paths: tuple[Path, Path, Path]) -> None:
    rendered = [str(path) for path in paths]
    if any(value in sys.path for value in rendered):
        raise RuntimeBootstrapError("sealed runtime path was injected before bootstrap")
    sys.path[:0] = rendered
    if sys.path[:3] != rendered:
        raise RuntimeBootstrapError("sealed runtime import order was not installed")
    sys.dont_write_bytecode = True


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in _ALLOWED_DISPATCH:
        raise RuntimeBootstrapError("unknown sealed Ava dispatch")
    dispatch, dispatch_arguments = arguments[0], arguments[1:]
    release_root = _release_root()
    _assert_no_site_interpreter(release_root)
    _install_runtime_paths(_runtime_paths(release_root))
    if dispatch in {"causal-pair", "compare", "serve"}:
        _assert_current_release(release_root)
    if dispatch == "shadow":
        sys.argv = ["ava-relationship-shadow", *dispatch_arguments]
        runpy.run_module(
            "ava_extensions.evals.relationship.shadow_runner",
            run_name="__main__",
            alter_sys=True,
        )
        return 0
    if dispatch == "attest":
        if "--release-root" in dispatch_arguments:
            raise RuntimeBootstrapError("attest release root is bootstrap-owned")
        sys.argv = [
            "ava-release-attestation",
            "--release-root",
            str(release_root),
            *dispatch_arguments,
        ]
        runpy.run_module(
            "ava_extensions.evals.relationship.release_attestation",
            run_name="__main__",
            alter_sys=True,
        )
        return 0
    if dispatch == "causal-pair":
        sys.argv = [
            "ava-relationship-causal-pair",
            "causal-pair",
            *dispatch_arguments,
        ]
        runpy.run_module(
            "ava_extensions.evals.relationship.release_attestation",
            run_name="__main__",
            alter_sys=True,
        )
        return 0
    if dispatch == "compare":
        sys.argv = ["ava-relationship-eval", "compare", *dispatch_arguments]
        runpy.run_module(
            "ava_extensions.evals.relationship.cli",
            run_name="__main__",
            alter_sys=True,
        )
        return 0
    sys.argv = ["jarvis", "serve", *dispatch_arguments]
    from openjarvis.cli import main as openjarvis_main

    openjarvis_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RuntimeBootstrapError", "main"]
