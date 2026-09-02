"""Causal release treatment and verified shadow-scope boundaries."""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from ava_extensions import boot
from ava_extensions.evals.relationship.contracts import (
    LoadedCausalShadowBinding,
    load_causal_shadow_binding,
    reload_causal_shadow_binding,
    sha256_file,
)
from ava_extensions.identity import relationship_guard_treatment as treatment
from ava_extensions.tests.test_relationship_shadow_runner import (
    MANIFEST,
    TREATMENT_PATH,
    _causal_release_evidence,
)
from openjarvis.core.config import JarvisConfig
from openjarvis.server import routes
from openjarvis.server.app import create_app

BASELINE_TREATMENT = "shadow-baseline-only-v1"
RUNTIME_TREATMENT = "runtime-enforced-v1"


@pytest.fixture
def causal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict]:
    evidence = _causal_release_evidence(tmp_path, monkeypatch)
    monkeypatch.setattr(
        treatment,
        "_SEALED_RELEASE_ROOT",
        evidence["baseline_root"].parent,
    )
    monkeypatch.setattr(treatment, "_CURRENT_RELEASE_LINK", evidence["current"])
    monkeypatch.setattr(treatment, "_TRUSTED_ANCESTRY_ROOT", tmp_path)
    monkeypatch.setattr(treatment, "_CLAIMED_BINDING_IDENTITIES", set())
    monkeypatch.setattr(treatment, "_TRUSTED_RELEASE_OWNER_UID", os.geteuid())
    evidence["baseline_root"].parent.chmod(0o555)
    tmp_path.chmod(0o555)
    try:
        yield evidence
    finally:
        tmp_path.chmod(0o700)
        for path in sorted(
            tmp_path.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.fixture
def baseline_binding(
    causal_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> LoadedCausalShadowBinding:
    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        BASELINE_TREATMENT,
    )
    binding = load_causal_shadow_binding(
        release_attestation_path=causal_evidence["baseline"].output_path,
        release_attestation_sha256=causal_evidence["baseline"].sha256,
        peer_release_attestation_path=causal_evidence["candidate"].output_path,
        peer_release_attestation_sha256=causal_evidence["candidate"].sha256,
        causal_pair_path=causal_evidence["pair"].output_path,
        causal_pair_sha256=causal_evidence["pair"].sha256,
        expected_manifest_sha256=sha256_file(MANIFEST),
    )
    monkeypatch.setattr(
        treatment,
        "_TREATMENT_MODULE_PATH",
        causal_evidence["baseline_root"] / TREATMENT_PATH,
    )
    return binding


@pytest.fixture
def candidate_binding(
    causal_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> LoadedCausalShadowBinding:
    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        RUNTIME_TREATMENT,
    )
    binding = load_causal_shadow_binding(
        release_attestation_path=causal_evidence["candidate"].output_path,
        release_attestation_sha256=causal_evidence["candidate"].sha256,
        peer_release_attestation_path=causal_evidence["baseline"].output_path,
        peer_release_attestation_sha256=causal_evidence["baseline"].sha256,
        causal_pair_path=causal_evidence["pair"].output_path,
        causal_pair_sha256=causal_evidence["pair"].sha256,
        expected_manifest_sha256=sha256_file(MANIFEST),
    )
    monkeypatch.setattr(
        treatment,
        "_TREATMENT_MODULE_PATH",
        causal_evidence["candidate_root"] / TREATMENT_PATH,
    )
    return binding


def _config() -> JarvisConfig:
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    return config


def _valid_guard() -> SimpleNamespace:
    return SimpleNamespace(
        policy_sha256=f"sha256:{'0' * 64}",
        with_turns=MagicMock(),
        apply=MagicMock(),
        apply_with_repair=MagicMock(),
        inspect_tool_arguments=MagicMock(),
        metadata=MagicMock(),
        _inspect_tool_arguments_nonmutating=MagicMock(),
        _scrub_trace_fragment=MagicMock(),
        _terminal_decision_applied=MagicMock(),
    )


def test_treatment_marker_is_importable_unique_and_scope_is_not_exported() -> None:
    payload = Path(treatment.__file__).read_bytes()
    selected = treatment._validate_relationship_guard_treatment()
    literal_counts = (
        payload.count(BASELINE_TREATMENT.encode()),
        payload.count(RUNTIME_TREATMENT.encode()),
    )

    assert selected in {BASELINE_TREATMENT, RUNTIME_TREATMENT}
    assert literal_counts in {(1, 0), (0, 1)}
    assert treatment.__all__ == ("RELATIONSHIP_GUARD_TREATMENT",)
    boot._relationship_guard_treatment()


@pytest.mark.parametrize("failure", ("missing", "invalid"))
def test_boot_fails_closed_for_missing_or_invalid_treatment_module(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "missing":

        def load(_name: str):
            raise ModuleNotFoundError("synthetic missing treatment")

    else:

        def load(_name: str):
            return SimpleNamespace(
                RELATIONSHIP_GUARD_TREATMENT="invalid",
                _validate_relationship_guard_treatment=lambda: "invalid",
            )

    monkeypatch.setattr(boot.importlib, "import_module", load)
    with pytest.raises(RuntimeError, match="refuse de demarrer"):
        boot._charger_obligatoire(
            "traitement causal du garde relationnel",
            boot._relationship_guard_treatment,
        )


def test_baseline_refuses_service_app_and_all_route_preflights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        BASELINE_TREATMENT,
    )
    with pytest.raises(RuntimeError, match="cannot start"):
        treatment._assert_active_runtime_treatment()
    with pytest.raises(RuntimeError, match="verified shadow"):
        create_app(object(), "test-model", config=_config())

    for overlay in (None, object()):
        with pytest.raises(HTTPException) as raised:
            routes._prepare_relationship_guard_or_503(overlay)
        assert raised.value.status_code == 503


def test_verified_baseline_scope_builds_app_and_skips_guard_for_all_overlays(
    baseline_binding: LoadedCausalShadowBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_extensions.identity import relationship_guard

    prepare = MagicMock(side_effect=AssertionError("guard must remain untouched"))
    monkeypatch.setattr(relationship_guard, "prepare_relationship_guard", prepare)

    with treatment.verified_relationship_shadow_scope(baseline_binding):
        app = create_app(object(), "test-model", config=_config())
        assert app.state.model == "test-model"

        async def shadow_route_preflights() -> None:
            treatment._bind_verified_relationship_shadow_task()
            assert routes._prepare_relationship_guard_or_503(None) is None
            assert routes._prepare_relationship_guard_or_503(object()) is None

        asyncio.run(shadow_route_preflights())

    prepare.assert_not_called()
    with pytest.raises(HTTPException) as raised:
        routes._prepare_relationship_guard_or_503(None)
    assert raised.value.status_code == 503


def test_runtime_treatment_calls_guard_including_no_overlay_and_keeps_app_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_extensions.identity import relationship_guard

    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        RUNTIME_TREATMENT,
    )
    guard = _valid_guard()
    prepare = MagicMock(side_effect=(None, guard))
    monkeypatch.setattr(relationship_guard, "prepare_relationship_guard", prepare)

    app = create_app(object(), "test-model", config=_config())
    assert app.state.model == "test-model"
    assert routes._prepare_relationship_guard_or_503(None) is None
    assert routes._prepare_relationship_guard_or_503(object()) is guard
    assert prepare.call_args_list[0].args == (None, ())
    assert prepare.call_args_list[1].args[1] == ()


def test_candidate_binding_scope_is_verified_but_never_bypasses_the_guard(
    candidate_binding: LoadedCausalShadowBinding,
) -> None:
    with treatment.verified_relationship_shadow_scope(candidate_binding):
        treatment._assert_application_factory_allowed()
        assert not treatment._relationship_guard_bypassed_for_verified_shadow()


def test_scope_rejects_primitives_nested_reuse_threads_and_foreign_tasks(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    with pytest.raises(RuntimeError, match="loader-issued"):
        with treatment.verified_relationship_shadow_scope(SimpleNamespace()):
            pass

    manager = treatment.verified_relationship_shadow_scope(baseline_binding)
    with manager:
        with pytest.raises(RuntimeError, match="cross or nest"):
            with treatment.verified_relationship_shadow_scope(baseline_binding):
                pass

        copied_context = contextvars.copy_context()
        thread_errors: list[BaseException] = []

        def cross_thread() -> None:
            try:
                copied_context.run(treatment._assert_application_factory_allowed)
            except BaseException as exc:  # noqa: BLE001 - explicit cross-thread probe
                thread_errors.append(exc)

        worker = threading.Thread(target=cross_thread)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(thread_errors) == 1
        assert isinstance(thread_errors[0], RuntimeError)

        async def task_probe() -> None:
            async def premature_foreign_task() -> None:
                with pytest.raises(RuntimeError, match="verified shadow"):
                    treatment._assert_application_factory_allowed()

            await asyncio.create_task(premature_foreign_task())
            treatment._bind_verified_relationship_shadow_task()
            treatment._assert_application_factory_allowed()

            async def foreign_task() -> None:
                with pytest.raises(RuntimeError, match="verified shadow"):
                    treatment._assert_application_factory_allowed()

            await asyncio.create_task(foreign_task())

        asyncio.run(task_probe())

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            pass


def test_scope_resets_after_exception(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    with pytest.raises(ValueError, match="synthetic body failure"):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            raise ValueError("synthetic body failure")

    with pytest.raises(RuntimeError, match="verified shadow"):
        treatment._assert_application_factory_allowed()


def test_copied_context_cannot_outlive_verified_scope(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    copied_context: contextvars.Context | None = None
    with treatment.verified_relationship_shadow_scope(baseline_binding):
        treatment._assert_application_factory_allowed()
        copied_context = contextvars.copy_context()

    assert copied_context is not None
    with pytest.raises(RuntimeError, match="verified shadow"):
        copied_context.run(treatment._assert_application_factory_allowed)


def test_reloaded_copy_of_claimed_evidence_cannot_reuse_the_scope(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    duplicate = reload_causal_shadow_binding(baseline_binding)

    with treatment.verified_relationship_shadow_scope(baseline_binding):
        treatment._assert_application_factory_allowed()

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with treatment.verified_relationship_shadow_scope(duplicate):
            pass


@pytest.mark.parametrize(
    "tamper",
    (
        "module",
        "module_mode",
        "module_symlink",
        "marker",
        "pair_file",
        "release_parent_mode",
        "current_parent_mode",
        "current",
    ),
)
def test_scope_recoups_module_pair_and_current_state(
    baseline_binding: LoadedCausalShadowBinding,
    tamper: str,
) -> None:
    if tamper == "module":
        module_path = treatment._TREATMENT_MODULE_PATH
        module_path.chmod(0o644)
        module_path.write_text("tampered\n", encoding="utf-8")
    elif tamper == "module_mode":
        treatment._TREATMENT_MODULE_PATH.chmod(0o644)
    elif tamper == "module_symlink":
        module_path = treatment._TREATMENT_MODULE_PATH
        module_path.parent.chmod(0o755)
        original = module_path.with_name("relationship_guard_treatment.original.py")
        module_path.rename(original)
        module_path.symlink_to(original.name)
    elif tamper == "marker":
        marker = treatment._TREATMENT_MODULE_PATH.parents[2] / ".ava-ready"
        marker.chmod(0o644)
        marker.write_text("f" * 40 + "\n", encoding="ascii")
    elif tamper == "pair_file":
        pair_path = baseline_binding.causal_pair.path
        pair_path.chmod(0o600)
        pair_path.write_bytes(pair_path.read_bytes() + b"\n")
    elif tamper == "release_parent_mode":
        treatment._SEALED_RELEASE_ROOT.chmod(0o755)
    elif tamper == "current_parent_mode":
        treatment._CURRENT_RELEASE_LINK.parent.chmod(0o755)
    else:
        current = treatment._CURRENT_RELEASE_LINK
        current.parent.chmod(0o755)
        current.unlink()
        current.symlink_to(
            treatment._TREATMENT_MODULE_PATH.parents[2],
            target_is_directory=True,
        )
        current.parent.chmod(0o555)

    with pytest.raises(RuntimeError):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            pass


def test_copied_evidence_bytes_cannot_reset_one_shot_scope(
    baseline_binding: LoadedCausalShadowBinding,
    causal_evidence: dict,
    tmp_path: Path,
) -> None:
    with treatment.verified_relationship_shadow_scope(baseline_binding):
        treatment._assert_application_factory_allowed()

    copied_root = tmp_path / "copied-evidence"
    tmp_path.chmod(0o755)
    copied_root.mkdir()
    baseline_path = copied_root / "baseline.json"
    candidate_path = copied_root / "candidate.json"
    pair_path = copied_root / "pair.json"
    shutil.copyfile(causal_evidence["baseline"].output_path, baseline_path)
    shutil.copyfile(causal_evidence["candidate"].output_path, candidate_path)
    shutil.copyfile(causal_evidence["pair"].output_path, pair_path)
    tmp_path.chmod(0o555)
    duplicate = load_causal_shadow_binding(
        release_attestation_path=baseline_path,
        release_attestation_sha256=causal_evidence["baseline"].sha256,
        peer_release_attestation_path=candidate_path,
        peer_release_attestation_sha256=causal_evidence["candidate"].sha256,
        causal_pair_path=pair_path,
        causal_pair_sha256=causal_evidence["pair"].sha256,
        expected_manifest_sha256=sha256_file(MANIFEST),
    )

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with treatment.verified_relationship_shadow_scope(duplicate):
            pass


def test_module_post_read_same_path_replacement_is_rejected(
    baseline_binding: LoadedCausalShadowBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = treatment._TREATMENT_MODULE_PATH
    original = treatment._strict_regular_snapshot
    replaced = False

    def replace_after_read(path: Path, *, max_bytes: int):
        nonlocal replaced
        snapshot = original(path, max_bytes=max_bytes)
        if Path(path) == module_path and not replaced:
            replaced = True
            module_path.parent.chmod(0o755)
            module_path.chmod(0o644)
            module_path.unlink()
            module_path.write_text(
                'RELATIONSHIP_GUARD_TREATMENT = "tampered-after-read"\n',
                encoding="utf-8",
            )
            module_path.chmod(0o444)
        return snapshot

    monkeypatch.setattr(treatment, "_strict_regular_snapshot", replace_after_read)
    with pytest.raises(RuntimeError):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            pass
    assert replaced


def test_current_symlink_post_read_replacement_is_rejected(
    baseline_binding: LoadedCausalShadowBinding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = treatment._CURRENT_RELEASE_LINK
    target = current.resolve(strict=True)
    original = treatment._release_marker_snapshot
    replaced = False

    def replace_link_after_snapshot(path: Path):
        nonlocal replaced
        snapshot = original(path)
        if Path(path) == target and not replaced:
            replaced = True
            current.parent.chmod(0o755)
            current.unlink()
            current.symlink_to(target, target_is_directory=True)
            current.parent.chmod(0o555)
        return snapshot

    monkeypatch.setattr(
        treatment,
        "_release_marker_snapshot",
        replace_link_after_snapshot,
    )
    with pytest.raises(RuntimeError):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            pass
    assert replaced


def test_current_same_path_directory_replacement_is_rejected(
    candidate_binding: LoadedCausalShadowBinding,
    causal_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_root = causal_evidence["candidate_root"]
    release_parent = candidate_root.parent
    release_parent.chmod(0o755)
    replacement = release_parent / "replacement-candidate"
    replacement.mkdir()
    shutil.copyfile(candidate_root / ".ava-release", replacement / ".ava-release")
    shutil.copyfile(candidate_root / ".ava-ready", replacement / ".ava-ready")
    replacement_module = replacement / TREATMENT_PATH
    replacement_module.parent.mkdir(parents=True)
    replacement_module.write_text(
        'RELATIONSHIP_GUARD_TREATMENT = "tampered-current-root"\n',
        encoding="utf-8",
    )
    replacement_module.chmod(0o444)
    release_parent.chmod(0o555)

    original = treatment._release_marker_snapshot
    call_count = 0

    def replace_after_current_snapshot(path: Path):
        nonlocal call_count
        snapshot = original(path)
        call_count += 1
        if call_count == 2:
            release_parent.chmod(0o755)
            candidate_root.rename(release_parent / "original-candidate")
            replacement.rename(candidate_root)
        return snapshot

    monkeypatch.setattr(
        treatment,
        "_release_marker_snapshot",
        replace_after_current_snapshot,
    )
    with pytest.raises(RuntimeError):
        with treatment.verified_relationship_shadow_scope(candidate_binding):
            pass
    assert call_count == 2


def test_scope_reloads_mutable_documents_from_unchanged_evidence(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    baseline_binding.release_attestation.document["release"]["git_sha"] = "f" * 40
    baseline_binding.peer_release_attestation.document["release"]["treatment"] = (
        BASELINE_TREATMENT
    )
    baseline_binding.causal_pair.document["candidate"]["git_sha"] = "e" * 40

    with treatment.verified_relationship_shadow_scope(baseline_binding):
        treatment._assert_application_factory_allowed()


def test_mutable_documents_cannot_hide_a_forged_binding_scalar(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    candidate_git_sha = baseline_binding.causal_pair.document["candidate"]["git_sha"]
    object.__setattr__(baseline_binding, "release_git_sha", candidate_git_sha)
    baseline_binding.release_attestation.document["release"]["git_sha"] = (
        candidate_git_sha
    )

    with pytest.raises(RuntimeError):
        with treatment.verified_relationship_shadow_scope(baseline_binding):
            pass


def test_scope_manager_cannot_be_entered_from_another_thread(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    manager = treatment.verified_relationship_shadow_scope(baseline_binding)
    copied_context = contextvars.copy_context()
    errors: list[BaseException] = []

    def enter_elsewhere() -> None:
        try:
            copied_context.run(manager.__enter__)
        except BaseException as exc:  # noqa: BLE001 - explicit manager misuse probe
            errors.append(exc)

    worker = threading.Thread(target=enter_elsewhere)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_cross_thread_exit_invalidates_the_originating_scope(
    baseline_binding: LoadedCausalShadowBinding,
) -> None:
    manager = treatment.verified_relationship_shadow_scope(baseline_binding)
    manager.__enter__()
    errors: list[BaseException] = []

    def exit_elsewhere() -> None:
        try:
            manager.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 - explicit manager misuse probe
            errors.append(exc)

    worker = threading.Thread(target=exit_elsewhere)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    with pytest.raises(RuntimeError, match="verified shadow"):
        treatment._assert_application_factory_allowed()
    treatment._VERIFIED_SHADOW_FRAME.set(None)
