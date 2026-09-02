"""Causal treatment boundary for the private relationship runtime guard.

The release generator changes exactly the public assignment below between the
baseline and candidate commits.  The baseline can be imported and packaged, but it
cannot start Ava or build an application outside the strictly verified offline shadow
scope derived from a v3 causal binding.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import re
import stat
import threading
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

RELATIONSHIP_GUARD_TREATMENT = "shadow-baseline-only-v1"

__all__ = ("RELATIONSHIP_GUARD_TREATMENT",)

_BASELINE_TREATMENT = "shadow-baseline-" "only-v1"  # fmt: skip
_RUNTIME_TREATMENT = "runtime-enforced-" "v1"  # fmt: skip
_TREATMENT_MODULE_RELATIVE_PATH = Path(
    "ava_extensions/identity/relationship_guard_treatment.py"
)
_TREATMENT_MODULE_PATH = Path(__file__).expanduser().absolute()
_SEALED_RELEASE_ROOT = Path("/var/lib/ava/releases")
_CURRENT_RELEASE_LINK = Path("/var/lib/ava/current")
_TRUSTED_ANCESTRY_ROOT = Path("/")
_MAX_TREATMENT_MODULE_BYTES = 512 * 1024
_MAX_RELEASE_MARKER_BYTES = 8 * 1024
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TRUSTED_RELEASE_OWNER_UID = 0


class _RelationshipGuardTreatmentError(RuntimeError):
    """The release treatment or its causal shadow authority is invalid."""


@dataclass(slots=True)
class _VerifiedShadowFrame:
    binding_identity: tuple[str, ...]
    thread_identity: int
    task: asyncio.Task[Any] | None
    task_is_bound: bool
    active: bool = True
    task_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    link_count: int
    modified_ns: int
    changed_ns: int
    owner_uid: int


@dataclass(frozen=True, slots=True)
class _RegularFileSnapshot:
    payload: bytes
    identity: _FileIdentity


@dataclass(frozen=True, slots=True)
class _ReleaseMarkerSnapshot:
    root: Path
    ancestor_identities: tuple[_FileIdentity, ...]
    root_identity: _FileIdentity
    git_sha: str
    manifest: _RegularFileSnapshot
    ready: _RegularFileSnapshot


@dataclass(frozen=True, slots=True)
class _CurrentReleaseSnapshot:
    ancestor_identities: tuple[_FileIdentity, ...]
    link_identity: _FileIdentity
    release: _ReleaseMarkerSnapshot


@dataclass(frozen=True, slots=True)
class _TreatmentModuleSnapshot:
    file: _RegularFileSnapshot
    directory_identities: tuple[_FileIdentity, ...]


_VERIFIED_SHADOW_FRAME: contextvars.ContextVar[_VerifiedShadowFrame | None] = (
    contextvars.ContextVar("ava_verified_relationship_shadow_frame", default=None)
)
_CLAIMED_BINDING_IDENTITIES: set[tuple[str, ...]] = set()
_CLAIMED_BINDINGS_LOCK = threading.Lock()


def _validate_relationship_guard_treatment() -> str:
    """Return the exact release treatment, rejecting aliases and unknown values."""

    treatment = RELATIONSHIP_GUARD_TREATMENT
    if type(treatment) is not str or treatment not in {
        _BASELINE_TREATMENT,
        _RUNTIME_TREATMENT,
    }:
        raise _RelationshipGuardTreatmentError(
            "relationship guard treatment is missing or invalid"
        )
    return treatment


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        link_count=metadata.st_nlink,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        owner_uid=metadata.st_uid,
    )


def _require_trusted_sealed(identity: _FileIdentity, label: str) -> None:
    if identity.owner_uid != _TRUSTED_RELEASE_OWNER_UID or identity.mode & 0o222:
        raise _RelationshipGuardTreatmentError(
            f"relationship {label} is not owned by the trusted sealed release"
        )


def _strict_trusted_directory_identity(path: Path) -> _FileIdentity:
    candidate = path.expanduser().absolute()
    descriptor = -1
    try:
        before = candidate.lstat()
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        identity = _file_identity(opened)
        after = candidate.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _file_identity(before) != identity
            or _file_identity(after) != identity
        ):
            raise _RelationshipGuardTreatmentError(
                "relationship release directory changed while opening"
            )
        _require_trusted_sealed(identity, "release directory")
        return identity
    except _RelationshipGuardTreatmentError:
        raise
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship release directory is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _trusted_directory_chain(path: Path) -> tuple[_FileIdentity, ...]:
    candidate = path.expanduser().absolute()
    anchor = _TRUSTED_ANCESTRY_ROOT.expanduser().absolute()
    try:
        if (
            candidate.resolve(strict=True) != candidate
            or anchor.resolve(strict=True) != anchor
            or not candidate.is_relative_to(anchor)
        ):
            raise _RelationshipGuardTreatmentError(
                "relationship release ancestry is non-canonical"
            )
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship release ancestry is unavailable"
        ) from exc
    paths: list[Path] = []
    cursor = candidate
    while True:
        paths.append(cursor)
        if cursor == anchor:
            break
        if cursor.parent == cursor:
            raise _RelationshipGuardTreatmentError(
                "relationship release ancestry escapes its trust anchor"
            )
        cursor = cursor.parent
    return tuple(_strict_trusted_directory_identity(item) for item in paths)


def _read_regular_snapshot(
    descriptor: int,
    *,
    before: os.stat_result,
    max_bytes: int,
) -> _RegularFileSnapshot:
    opened = os.fstat(descriptor)
    before_identity = _file_identity(before)
    opened_identity = _file_identity(opened)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_size < 0
        or opened.st_size > max_bytes
        or opened.st_nlink != 1
        or opened_identity != before_identity
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship treatment evidence is not a direct bounded regular file"
        )
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(descriptor)
    if len(payload) != opened.st_size or _file_identity(after) != opened_identity:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment evidence changed while reading"
        )
    return _RegularFileSnapshot(payload=payload, identity=opened_identity)


def _strict_regular_snapshot(path: Path, *, max_bytes: int) -> _RegularFileSnapshot:
    """Read one direct file and bind its bytes to stable descriptor metadata."""

    candidate = path.expanduser().absolute()
    descriptor = -1
    try:
        before = candidate.lstat()
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        snapshot = _read_regular_snapshot(
            descriptor,
            before=before,
            max_bytes=max_bytes,
        )
        after = candidate.lstat()
        if _file_identity(after) != snapshot.identity:
            raise _RelationshipGuardTreatmentError(
                "relationship treatment evidence changed while reading"
            )
        return snapshot
    except _RelationshipGuardTreatmentError:
        raise
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment evidence is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _strict_regular_snapshot_at(
    directory_descriptor: int,
    name: str,
    *,
    max_bytes: int,
) -> _RegularFileSnapshot:
    if not name or "/" in name or name in {".", ".."}:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment evidence name is non-canonical"
        )
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        snapshot = _read_regular_snapshot(
            descriptor,
            before=before,
            max_bytes=max_bytes,
        )
        after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _file_identity(after) != snapshot.identity:
            raise _RelationshipGuardTreatmentError(
                "relationship treatment evidence changed while reading"
            )
        return snapshot
    except _RelationshipGuardTreatmentError:
        raise
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment evidence is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _strict_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Compatibility wrapper for callers that only need verified bytes."""

    return _strict_regular_snapshot(path, max_bytes=max_bytes).payload


def _release_marker_snapshot(release_root: Path) -> _ReleaseMarkerSnapshot:
    from ava_extensions.evals.relationship.contracts import ContractError
    from ava_extensions.evals.relationship.release_attestation import (
        _parse_release_manifest,
    )

    root = release_root.expanduser().absolute()
    descriptor = -1
    try:
        if root.parent != _SEALED_RELEASE_ROOT.expanduser().absolute():
            raise _RelationshipGuardTreatmentError(
                "relationship release is outside the sealed release root"
            )
        ancestor_identities = _trusted_directory_chain(root.parent)
        before = root.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise _RelationshipGuardTreatmentError(
                "relationship shadow release root is non-canonical"
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
        root_identity = _file_identity(opened)
        if _file_identity(before) != root_identity or not stat.S_ISDIR(opened.st_mode):
            raise _RelationshipGuardTreatmentError(
                "relationship shadow release root changed before reading"
            )
        _require_trusted_sealed(root_identity, "release root")
        marker = _strict_regular_snapshot_at(
            descriptor,
            ".ava-release",
            max_bytes=_MAX_RELEASE_MARKER_BYTES,
        )
        manifest = _parse_release_manifest(marker.payload)
        ready = _strict_regular_snapshot_at(descriptor, ".ava-ready", max_bytes=128)
        _require_trusted_sealed(marker.identity, "release marker")
        _require_trusted_sealed(ready.identity, "ready marker")
        after_descriptor = os.fstat(descriptor)
        after_path = root.lstat()
        after_ancestor_identities = _trusted_directory_chain(root.parent)
    except (ContractError, KeyError, TypeError) as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship shadow release markers are invalid"
        ) from exc
    except _RelationshipGuardTreatmentError:
        raise
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship shadow release markers are unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    git_sha = manifest.get("git_sha")
    if (
        _file_identity(after_descriptor) != root_identity
        or _file_identity(after_path) != root_identity
        or after_ancestor_identities != ancestor_identities
        or type(git_sha) is not str
        or _GIT_SHA_RE.fullmatch(git_sha) is None
        or ready.payload != f"{git_sha}\n".encode("ascii")
        or root.name != git_sha
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship shadow release markers are inconsistent"
        )
    return _ReleaseMarkerSnapshot(
        root=root,
        ancestor_identities=ancestor_identities,
        root_identity=root_identity,
        git_sha=git_sha,
        manifest=marker,
        ready=ready,
    )


def _release_marker_git_sha(release_root: Path) -> str:
    return _release_marker_snapshot(release_root).git_sha


def _current_release_state() -> _CurrentReleaseSnapshot:
    current = _CURRENT_RELEASE_LINK.expanduser().absolute()
    try:
        ancestor_identities = _trusted_directory_chain(current.parent)
        before = current.lstat()
        link_identity = _file_identity(before)
        target = current.resolve(strict=True)
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "current Ava release is unavailable"
        ) from exc
    if (
        not stat.S_ISLNK(before.st_mode)
        or link_identity.owner_uid != _TRUSTED_RELEASE_OWNER_UID
        or not target.is_dir()
    ):
        raise _RelationshipGuardTreatmentError(
            "current Ava release pointer is non-canonical"
        )
    release = _release_marker_snapshot(target)
    try:
        after = current.lstat()
        revalidated_target = current.resolve(strict=True)
        revalidated_release_root = target.lstat()
        revalidated_ancestor_identities = _trusted_directory_chain(current.parent)
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "current Ava release changed during verification"
        ) from exc
    if (
        _file_identity(after) != link_identity
        or revalidated_target != target
        or _file_identity(revalidated_release_root) != release.root_identity
        or revalidated_ancestor_identities != ancestor_identities
    ):
        raise _RelationshipGuardTreatmentError(
            "current Ava release changed during verification"
        )
    return _CurrentReleaseSnapshot(
        ancestor_identities=ancestor_identities,
        link_identity=link_identity,
        release=release,
    )


def _verified_treatment_module_snapshot(
    module_path: Path,
    *,
    release_root: Path,
    expected_sha256: str,
) -> _TreatmentModuleSnapshot:
    try:
        if module_path.resolve(strict=True) != module_path:
            raise _RelationshipGuardTreatmentError(
                "relationship treatment module is linked or indirect"
            )
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module is unavailable"
        ) from exc
    if not module_path.is_relative_to(release_root):
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module is outside its release"
        )
    directory_paths: list[Path] = []
    cursor = module_path.parent
    while True:
        directory_paths.append(cursor)
        if cursor == release_root:
            break
        if cursor.parent == cursor:
            raise _RelationshipGuardTreatmentError(
                "relationship treatment module ancestry is invalid"
            )
        cursor = cursor.parent
    directory_paths.reverse()
    directory_identities = tuple(
        _strict_trusted_directory_identity(path) for path in directory_paths
    )
    snapshot = _strict_regular_snapshot(
        module_path,
        max_bytes=_MAX_TREATMENT_MODULE_BYTES,
    )
    if stat.S_IMODE(snapshot.identity.mode) != 0o444:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module is writable"
        )
    _require_trusted_sealed(snapshot.identity, "treatment module")
    digest = f"sha256:{hashlib.sha256(snapshot.payload).hexdigest()}"
    if digest != expected_sha256:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module digest is not attested"
        )
    revalidated_directories = tuple(
        _strict_trusted_directory_identity(path) for path in directory_paths
    )
    if revalidated_directories != directory_identities:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module ancestry changed while reading"
        )
    return _TreatmentModuleSnapshot(
        file=snapshot,
        directory_identities=directory_identities,
    )


def _attested_side(binding: Any, role: str) -> tuple[Any, dict[str, Any]]:
    attestation = (
        binding.release_attestation
        if role == binding.role
        else binding.peer_release_attestation
    )
    pair_side = binding.causal_pair.document.get(role)
    if type(pair_side) is not dict:
        raise _RelationshipGuardTreatmentError(
            "relationship causal pair side is invalid"
        )
    document = attestation.document
    release = document.get("release") if type(document) is dict else None
    if type(release) is not dict:
        raise _RelationshipGuardTreatmentError(
            "relationship release attestation is invalid"
        )
    expected = {
        "release_attestation_sha256": attestation.sha256,
        "git_sha": release.get("git_sha"),
        "treatment": release.get("treatment"),
        "deployment_state": release.get("deployment_state"),
        "treatment_module_sha256": release.get("treatment_module_sha256"),
    }
    if any(pair_side.get(key) != value for key, value in expected.items()):
        raise _RelationshipGuardTreatmentError(
            "relationship causal pair diverges from release attestations"
        )
    return attestation, release


def _verify_causal_shadow_binding(candidate_binding: object) -> Any:
    from ava_extensions.evals.relationship.contracts import (
        LoadedCausalPair,
        LoadedCausalShadowBinding,
        LoadedReleaseAttestation,
        reload_causal_shadow_binding,
    )

    if type(candidate_binding) is not LoadedCausalShadowBinding:
        raise _RelationshipGuardTreatmentError(
            "relationship shadow requires a loader-issued causal binding"
        )
    try:
        fresh_binding = reload_causal_shadow_binding(candidate_binding)
    except Exception as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship shadow evidence cannot be reloaded"
        ) from exc
    scalar_fields = (
        "role",
        "treatment",
        "deployment_state",
        "release_git_sha",
        "treatment_module_sha256",
        "evaluation_manifest_sha256",
    )
    if (
        type(fresh_binding) is not LoadedCausalShadowBinding
        or type(fresh_binding.release_attestation) is not LoadedReleaseAttestation
        or type(fresh_binding.peer_release_attestation) is not LoadedReleaseAttestation
        or type(fresh_binding.causal_pair) is not LoadedCausalPair
        or any(
            getattr(candidate_binding, field_name) != getattr(fresh_binding, field_name)
            for field_name in scalar_fields
        )
        or candidate_binding.release_attestation.path
        != fresh_binding.release_attestation.path
        or candidate_binding.release_attestation.sha256
        != fresh_binding.release_attestation.sha256
        or candidate_binding.peer_release_attestation.path
        != fresh_binding.peer_release_attestation.path
        or candidate_binding.peer_release_attestation.sha256
        != fresh_binding.peer_release_attestation.sha256
        or candidate_binding.causal_pair.path != fresh_binding.causal_pair.path
        or candidate_binding.causal_pair.sha256 != fresh_binding.causal_pair.sha256
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship shadow binding differs from reloaded evidence"
        )
    # From this point onward, only strict loader output is trusted.  The frozen outer
    # binding still exposes mutable JSON dictionaries, so none of the caller-held
    # documents may participate in authorization.
    binding = fresh_binding

    treatment = _validate_relationship_guard_treatment()
    expected_role = {
        _BASELINE_TREATMENT: "baseline",
        _RUNTIME_TREATMENT: "candidate",
    }[treatment]
    expected_state = {
        "baseline": "prepared_noncurrent",
        "candidate": "active_current",
    }[expected_role]
    if (
        binding.role != expected_role
        or binding.treatment != treatment
        or binding.deployment_state != expected_state
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship shadow binding differs from executing treatment"
        )

    _local_attestation, local_release = _attested_side(binding, expected_role)
    peer_role = "candidate" if expected_role == "baseline" else "baseline"
    _peer_attestation, peer_release = _attested_side(binding, peer_role)
    expected_peer_treatment = (
        _RUNTIME_TREATMENT if expected_role == "baseline" else _BASELINE_TREATMENT
    )
    expected_peer_state = (
        "active_current" if expected_role == "baseline" else "prepared_noncurrent"
    )
    if (
        local_release.get("git_sha") != binding.release_git_sha
        or local_release.get("treatment") != binding.treatment
        or local_release.get("deployment_state") != binding.deployment_state
        or local_release.get("treatment_module_path")
        != _TREATMENT_MODULE_RELATIVE_PATH.as_posix()
        or local_release.get("treatment_module_sha256")
        != binding.treatment_module_sha256
        or peer_release.get("treatment") != expected_peer_treatment
        or peer_release.get("deployment_state") != expected_peer_state
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship shadow binding fields are inconsistent"
        )

    module_path = _TREATMENT_MODULE_PATH.expanduser().absolute()
    if (
        module_path.parts[-len(_TREATMENT_MODULE_RELATIVE_PATH.parts) :]
        != _TREATMENT_MODULE_RELATIVE_PATH.parts
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module path is non-canonical"
        )
    release_root = module_path.parents[len(_TREATMENT_MODULE_RELATIVE_PATH.parts) - 1]
    try:
        if module_path.resolve(strict=True) != module_path:
            raise _RelationshipGuardTreatmentError(
                "relationship treatment module is linked or indirect"
            )
        if release_root.resolve(strict=True) != release_root:
            raise _RelationshipGuardTreatmentError(
                "relationship shadow release root is linked or indirect"
            )
    except OSError as exc:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module is unavailable"
        ) from exc
    local_markers = _release_marker_snapshot(release_root)
    if local_markers.git_sha != binding.release_git_sha:
        raise _RelationshipGuardTreatmentError(
            "relationship treatment module differs from attested release"
        )
    local_module = _verified_treatment_module_snapshot(
        module_path,
        release_root=release_root,
        expected_sha256=binding.treatment_module_sha256,
    )

    current = _current_release_state()
    candidate_side = binding.causal_pair.document["candidate"]
    candidate_git_sha = candidate_side.get("git_sha")
    if current.release.git_sha != candidate_git_sha:
        raise _RelationshipGuardTreatmentError(
            "current Ava release differs from causal candidate"
        )
    if expected_role == "baseline":
        if (
            current.release.root == release_root
            or binding.release_git_sha == current.release.git_sha
        ):
            raise _RelationshipGuardTreatmentError(
                "baseline relationship release is unexpectedly current"
            )
    elif (
        current.release.root != release_root
        or binding.release_git_sha != current.release.git_sha
    ):
        raise _RelationshipGuardTreatmentError(
            "candidate relationship release is not current"
        )

    current_module_path = current.release.root / _TREATMENT_MODULE_RELATIVE_PATH
    current_module = _verified_treatment_module_snapshot(
        current_module_path,
        release_root=current.release.root,
        expected_sha256=candidate_side.get("treatment_module_sha256"),
    )

    # Recoup every pathname after the independent current-release inspection.
    # Descriptor identities make a same-path replacement observable; the second
    # pass also closes swaps that occur immediately after one coherent read.
    revalidated_local_markers = _release_marker_snapshot(release_root)
    revalidated_local_module = _verified_treatment_module_snapshot(
        module_path,
        release_root=release_root,
        expected_sha256=binding.treatment_module_sha256,
    )
    revalidated_current = _current_release_state()
    revalidated_current_module = _verified_treatment_module_snapshot(
        revalidated_current.release.root / _TREATMENT_MODULE_RELATIVE_PATH,
        release_root=revalidated_current.release.root,
        expected_sha256=candidate_side.get("treatment_module_sha256"),
    )
    if (
        revalidated_local_markers != local_markers
        or revalidated_local_module != local_module
        or revalidated_current != current
        or revalidated_current_module != current_module
    ):
        raise _RelationshipGuardTreatmentError(
            "relationship shadow release changed during final verification"
        )
    return binding


def _causal_binding_identity(binding: Any) -> tuple[str, ...]:
    """Build a stable one-shot identity exclusively from strict loader output."""

    return (
        binding.release_attestation.sha256,
        binding.peer_release_attestation.sha256,
        binding.causal_pair.sha256,
        binding.role,
        binding.treatment,
        binding.deployment_state,
        binding.release_git_sha,
        binding.treatment_module_sha256,
        binding.evaluation_manifest_sha256,
    )


def _verified_shadow_scope_is_active() -> bool:
    frame = _VERIFIED_SHADOW_FRAME.get()
    if frame is None or frame.thread_identity != threading.get_ident():
        return False
    task = _current_task()
    with frame.task_lock:
        if not frame.active:
            return False
        if frame.task_is_bound:
            return task is frame.task
        return task is None


def _bind_verified_relationship_shadow_task() -> None:
    """Bind an unbound verified scope to exactly this running asyncio task."""

    frame = _VERIFIED_SHADOW_FRAME.get()
    task = _current_task()
    if frame is None or frame.thread_identity != threading.get_ident() or task is None:
        raise _RelationshipGuardTreatmentError(
            "relationship shadow task binding requires its verified scope"
        )
    with frame.task_lock:
        if not frame.active or frame.task_is_bound or frame.task is not None:
            raise _RelationshipGuardTreatmentError(
                "relationship shadow task binding cannot be reused"
            )
        frame.task = task
        frame.task_is_bound = True


def _assert_active_runtime_treatment() -> None:
    if _validate_relationship_guard_treatment() != _RUNTIME_TREATMENT:
        raise _RelationshipGuardTreatmentError(
            "baseline relationship treatment cannot start the Ava service"
        )


def _assert_application_factory_allowed() -> None:
    treatment = _validate_relationship_guard_treatment()
    if treatment == _BASELINE_TREATMENT and not _verified_shadow_scope_is_active():
        raise _RelationshipGuardTreatmentError(
            "baseline relationship treatment is restricted to verified shadow"
        )


def _relationship_guard_bypassed_for_verified_shadow() -> bool:
    treatment = _validate_relationship_guard_treatment()
    if treatment == _RUNTIME_TREATMENT:
        return False
    if not _verified_shadow_scope_is_active():
        raise _RelationshipGuardTreatmentError(
            "baseline relationship treatment is restricted to verified shadow"
        )
    return True


def verified_relationship_shadow_scope(
    binding: object,
) -> AbstractContextManager[None]:
    """Return the one-shot verified scope consumed by the isolated v3 runner."""

    creator_thread = threading.get_ident()
    creator_task = _current_task()

    @contextmanager
    def _scope() -> Iterator[None]:
        if (
            threading.get_ident() != creator_thread
            or _current_task() is not creator_task
            or _VERIFIED_SHADOW_FRAME.get() is not None
        ):
            raise _RelationshipGuardTreatmentError(
                "relationship shadow scope cannot cross or nest execution contexts"
            )
        fresh_binding = _verify_causal_shadow_binding(binding)
        binding_identity = _causal_binding_identity(fresh_binding)
        with _CLAIMED_BINDINGS_LOCK:
            if binding_identity in _CLAIMED_BINDING_IDENTITIES:
                raise _RelationshipGuardTreatmentError(
                    "relationship causal binding scope cannot be reused"
                )
            _CLAIMED_BINDING_IDENTITIES.add(binding_identity)
        frame = _VerifiedShadowFrame(
            binding_identity=binding_identity,
            thread_identity=creator_thread,
            task=creator_task,
            task_is_bound=creator_task is not None,
        )
        token = _VERIFIED_SHADOW_FRAME.set(frame)
        try:
            yield
        finally:
            valid_exit = (
                threading.get_ident() == creator_thread
                and _current_task() is creator_task
                and _VERIFIED_SHADOW_FRAME.get() is frame
            )
            # Invalidate first: if a caller illicitly exits from another Context,
            # Python cannot reset the originating token there, but the copied frame
            # must still cease authorizing the original context immediately.
            with frame.task_lock:
                frame.active = False
            try:
                _VERIFIED_SHADOW_FRAME.reset(token)
            except (RuntimeError, ValueError) as exc:
                raise _RelationshipGuardTreatmentError(
                    "relationship shadow scope exited in a different context"
                ) from exc
            if not valid_exit:
                raise _RelationshipGuardTreatmentError(
                    "relationship shadow scope integrity was lost"
                )

    return _scope()
