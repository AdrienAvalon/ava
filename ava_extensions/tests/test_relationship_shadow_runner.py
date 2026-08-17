"""Effect and provenance contracts for the isolated relationship shadow."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.evals.relationship.contracts import (
    canonical_json_bytes,
    load_response_bundle,
    load_suite,
    sha256_bytes,
    sha256_file,
)
from ava_extensions.evals.relationship.evaluator import (
    EXPECTED_RELATIONSHIP_PROFILE_ID,
    evaluate_responses,
)
from ava_extensions.evals.relationship.shadow_runner import (
    ShadowRunError,
    _encode_service_assertion_key,
    run_shadow,
)

DATA_ROOT = Path(__file__).parents[1] / "evals" / "relationship" / "data"
MANIFEST = DATA_ROOT / "manifest.v2.json"
CANDIDATE = DATA_ROOT / "candidate.v2.json"
RELATIONSHIP_MARKER = "[AVA_RELATIONSHIP_PROFILE:"
RUNTIME_IDENTITIES = (
    "matrix:@synthetic-owner:eval.invalid",
    "matrix:@synthetic-guest:eval.invalid",
)


def _candidate_document() -> dict[str, Any]:
    return json.loads(CANDIDATE.read_text(encoding="utf-8"))


def _safe_outputs() -> list[str]:
    responses = _candidate_document()["responses"]
    return [
        "OIDC_POSITIVE_RESPONSE_MUST_STAY_PRIVATE",
        "ROLLBACK_ON_RESPONSE_MUST_STAY_PRIVATE",
        "ROLLBACK_OFF_RESPONSE_MUST_STAY_PRIVATE",
        "ROLLBACK_RESTORED_RESPONSE_MUST_STAY_PRIVATE",
        *(response["text"] for response in responses),
    ]


class FakeEngine:
    engine_id = "relationship-shadow-fake"

    def __init__(self, outputs: list[str | dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.environments: list[dict[str, str | None]] = []
        self.homes: list[tuple[str | None, str | None]] = []
        self.closed = False
        self.models = ["synthetic-model-v1"]
        self.servable_models: set[str] | None = None

    def list_models(self) -> list[str]:
        return list(self.models)

    def can_serve(self, model: str) -> bool:
        allowed = self.models if self.servable_models is None else self.servable_models
        return model in allowed

    def generate(
        self,
        messages,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.homes.append((os.getenv("HOME"), os.getenv("OPENJARVIS_HOME")))
        self.environments.append(
            {
                name: os.getenv(name)
                for name in (
                    "ALL_PROXY",
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_FUTURE_OVERRIDE_CANARY",
                    "AVA_CP_ASSERTION_KEY_ID",
                    "AVA_CP_ASSERTION_PREVIOUS_KEY_FILE",
                    "AVA_CP_ASSERTION_PREVIOUS_KEY_ID",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "NO_PROXY",
                    "OPENAI_COMPAT_API_KEY",
                    "all_proxy",
                    "http_proxy",
                    "https_proxy",
                    "no_proxy",
                )
            }
        )
        self.calls.append(
            {
                "max_tokens": max_tokens,
                "messages": tuple(
                    (
                        str(getattr(message.role, "value", message.role)),
                        str(message.content or ""),
                    )
                    for message in messages
                ),
                "model": model,
                "temperature": temperature,
                "unexpected_kwargs": dict(kwargs),
            }
        )
        if not self.outputs:
            raise AssertionError("fake response queue exhausted")
        output = self.outputs.pop(0)
        if isinstance(output, dict):
            return output
        return {
            "content": output,
            "finish_reason": "stop",
            "model": model,
            "usage": {},
        }

    def close(self) -> None:
        self.closed = True


def _release_attestation(
    tmp_path: Path,
    *,
    adapter: str = "relationship-shadow-fake",
    model: str = "synthetic-model-v1",
    provider: str = "synthetic-fake",
) -> tuple[Path, str]:
    document = {
        "schema_version": "ava.release.attestation/v1",
        "attestation_id": "ava-release-shadow-test-v1",
        "release": {
            "repository": "repo://ava",
            "git_sha": "0123456789abcdef0123456789abcdef01234567",
        },
        "engine": {
            "provider": provider,
            "model": model,
            "revision": "immutable-revision-1",
            "adapter": adapter,
            "config_sha256": "sha256:" + "a" * 64,
        },
        "artifact": {"manifest_sha256": "sha256:" + "b" * 64},
        "canonical_knowledge": False,
    }
    path = tmp_path / "release-attestation.json"
    path.write_bytes(canonical_json_bytes(document) + b"\n")
    return path, sha256_file(path)


def _private_output_directory(tmp_path: Path, name: str = "output") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    return path


def _relationship_allowed(case: dict[str, Any]) -> bool:
    principal = case["principal"]
    return bool(
        principal["verified"]
        and principal["relationship_opt_in"]
        and principal["relationship_subject"] == principal["request_subject"]
    )


@pytest.fixture
def _legacy_baseline_without_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emulate a baseline release that predates the runtime guard hook."""

    from ava_extensions.evals.relationship import shadow_runner
    from openjarvis.server import routes

    monkeypatch.setattr(
        routes,
        "_prepare_relationship_guard_or_503",
        lambda _relationship_overlay: None,
    )
    monkeypatch.setattr(shadow_runner, "_relationship_guard_module", lambda: None)


def test_shadow_assertion_key_encodes_trailing_crlf_entropy_as_hex() -> None:
    entropy = b"\xa5" * 46 + b"\r\n"

    encoded = _encode_service_assertion_key(entropy)

    assert encoded == entropy.hex().encode("ascii")
    assert len(encoded) == 96
    assert encoded.endswith(b"0d0a")
    assert encoded.rstrip(b"\r\n") == encoded


def test_shadow_runner_exercises_auth_rollback_and_corpus_without_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _legacy_baseline_without_guard: None,
) -> None:
    from ava_extensions.server import principal as principal_module

    def reject_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("OIDC shadow attempted a network fetch")

    monkeypatch.setattr(principal_module.httpx.Client, "get", reject_network)
    outside_home = tmp_path / "outside-home"
    outside_openjarvis = tmp_path / "outside-openjarvis"
    outside_home.mkdir()
    outside_openjarvis.mkdir()
    monkeypatch.setenv("HOME", str(outside_home))
    monkeypatch.setenv("OPENJARVIS_HOME", str(outside_openjarvis))
    ambient_environment = {
        "ALL_PROXY": "http://ambient-proxy.invalid:8080",
        "ANTHROPIC_API_KEY": "ambient-anthropic-key",
        "ANTHROPIC_AUTH_TOKEN": "ambient-anthropic-token",
        "ANTHROPIC_BASE_URL": "https://ambient-anthropic.invalid",
        "ANTHROPIC_FUTURE_OVERRIDE_CANARY": "ambient-override",
        "AVA_CP_ASSERTION_KEY_ID": "ambient-key",
        "AVA_CP_ASSERTION_PREVIOUS_KEY_FILE": "/ambient/previous.key",
        "AVA_CP_ASSERTION_PREVIOUS_KEY_ID": "ambient-previous",
        "HTTP_PROXY": "http://ambient-proxy.invalid:8080",
        "HTTPS_PROXY": "http://ambient-proxy.invalid:8080",
        "NO_PROXY": "ambient.invalid",
        "OPENAI_COMPAT_API_KEY": "ambient-secret-must-not-reach-engine",
        "all_proxy": "http://ambient-proxy.invalid:8080",
        "http_proxy": "http://ambient-proxy.invalid:8080",
        "https_proxy": "http://ambient-proxy.invalid:8080",
        "no_proxy": "ambient.invalid",
    }
    for name, value in ambient_environment.items():
        monkeypatch.setenv(name, value)
    engine = FakeEngine(_safe_outputs())
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)
    ava_logger = logging.getLogger("ava_extensions")
    logging_state = (
        tuple(ava_logger.handlers),
        ava_logger.level,
        ava_logger.propagate,
        ava_logger.disabled,
        logging.root.manager.disable,
    )

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=output_directory,
        release_attestation_path=attestation_path,
        release_attestation_sha256=attestation_sha256,
        role="baseline",
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert os.environ["HOME"] == str(outside_home)
    assert os.environ["OPENJARVIS_HOME"] == str(outside_openjarvis)
    assert {name: os.environ.get(name) for name in ambient_environment} == (
        ambient_environment
    )
    assert (
        tuple(ava_logger.handlers),
        ava_logger.level,
        ava_logger.propagate,
        ava_logger.disabled,
        logging.root.manager.disable,
    ) == logging_state
    assert list(outside_home.iterdir()) == []
    assert list(outside_openjarvis.iterdir()) == []
    output = result.output_path
    assert output.parent == output_directory
    assert output.name == (
        f"relationship-shadow-baseline-{result.bundle_sha256.removeprefix('sha256:')}.json"
    )
    assert result.bundle_sha256 == sha256_file(output)
    assert result.release_attestation_sha256 == attestation_sha256
    assert result.case_count == 39
    assert result.model_call_count == 43
    assert result.negative_checks == (
        "empty-service-header",
        "malformed-oidc",
        "forged-signature",
        "expired",
        "future-not-before",
        "wrong-audience",
        "unknown-key-id",
        "invalid-matrix-subject",
        "ambiguous-headers",
    )
    assert result.positive_checks == (
        "model-catalog-attested-model",
        "oidc-rs256-local-jwks",
        "rollback-disabled-zero-memory-read-write",
    )
    assert engine.closed is True
    assert engine.outputs == []
    assert len(engine.calls) == 43
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    suite = load_suite(MANIFEST)
    bundle = load_response_bundle(output, suite, expected_role="baseline")
    summary = evaluate_responses(suite, bundle)
    assert summary["gate_pass"] is True
    assert bundle.document["artifact"] == {
        "canonical_knowledge": False,
        "contains_personal_data": False,
        "contains_production_conversations": False,
        "engine": {
            "model": "synthetic-model-v1",
            "provider": "synthetic-fake",
            "revision": "immutable-revision-1",
        },
        "generated_by": "ava-relationship-shadow-runner-v2",
        "guard_observation": {
            "schema_version": "ava.relationship.guard-observation/v2",
            "active": False,
            "policy_id": None,
            "policy_sha256": None,
            "expected_prepare_calls": 0,
            "observed_prepare_calls": 0,
            "expected_apply_calls": 0,
            "observed_apply_calls": 0,
            "actions": [],
        },
        "id": "relationship-shadow-baseline-0123456789ab",
        "policy_sha256": bundle.document["artifact"]["policy_sha256"],
        "prompt_sha256": bundle.document["artifact"]["prompt_sha256"],
        "release_attestation_sha256": attestation_sha256,
        "release": {
            "repository": "repo://ava",
            "git_sha": "0123456789abcdef0123456789abcdef01234567",
            "adapter": "relationship-shadow-fake",
            "config_sha256": "sha256:" + "a" * 64,
            "manifest_sha256": "sha256:" + "b" * 64,
        },
        "role": "baseline",
        "safety_policy_sha256": suite.safety_policy_sha256,
        "source_kind": "offline_shadow",
    }
    serialized = output.read_text(encoding="utf-8")
    assert all(identity not in serialized for identity in RUNTIME_IDENTITIES)
    assert "ROLLBACK_ON_RESPONSE_MUST_STAY_PRIVATE" not in serialized

    prompts = [
        next(content for role, content in call["messages"] if role == "system")
        for call in engine.calls
    ]
    assert [prompt.count(RELATIONSHIP_MARKER) for prompt in prompts[:4]] == [1, 1, 0, 1]
    assert prompts[1] == prompts[3]
    assert prompts[2] != prompts[1]
    assert all(call["temperature"] == 0.0 for call in engine.calls)
    assert all(call["unexpected_kwargs"] == {} for call in engine.calls)
    assert all(call["max_tokens"] <= 32_768 for call in engine.calls)
    assert engine.environments
    for environment in engine.environments:
        assert environment == {
            "ALL_PROXY": None,
            "ANTHROPIC_API_KEY": None,
            "ANTHROPIC_AUTH_TOKEN": None,
            "ANTHROPIC_BASE_URL": None,
            "ANTHROPIC_FUTURE_OVERRIDE_CANARY": None,
            "AVA_CP_ASSERTION_KEY_ID": "current",
            "AVA_CP_ASSERTION_PREVIOUS_KEY_FILE": None,
            "AVA_CP_ASSERTION_PREVIOUS_KEY_ID": None,
            "HTTP_PROXY": None,
            "HTTPS_PROXY": None,
            "NO_PROXY": "127.0.0.1,::1",
            "OPENAI_COMPAT_API_KEY": None,
            "all_proxy": None,
            "http_proxy": None,
            "https_proxy": None,
            "no_proxy": "127.0.0.1,::1",
        }
    assert all(home != str(outside_home) for home, _root in engine.homes)
    assert all(root != str(outside_openjarvis) for _home, root in engine.homes)
    temporary_roots = {Path(root).parent for _home, root in engine.homes if root}
    assert len(temporary_roots) == 1
    assert not next(iter(temporary_roots)).exists()

    cases = {case["id"]: case for case in suite.corpus["cases"]}
    for response in bundle.document["responses"]:
        profile = response["applied_profile"]
        if _relationship_allowed(cases[response["case_id"]]):
            assert profile == {
                "id": EXPECTED_RELATIONSHIP_PROFILE_ID,
                "subject": "synthetic:owner",
            }
        else:
            assert profile is None
        assert response["tool_calls"] == []


def test_shadow_runner_rejects_any_engine_tool_call_before_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "TOOL_RESPONSE_MUST_NEVER_BE_PRINTED"
    engine = FakeEngine(
        [
            {
                "content": sentinel,
                "finish_reason": "tool_calls",
                "model": "synthetic-model-v1",
                "tool_calls": [
                    {
                        "arguments": "{}",
                        "id": "synthetic-call",
                        "name": "read-only-status",
                    }
                ],
                "usage": {},
            }
        ]
    )
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError, match="tool call"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert list(output_directory.iterdir()) == []
    assert len(engine.calls) == 1
    assert engine.closed is True


def test_shadow_runner_can_emit_a_separately_versioned_baseline(
    tmp_path: Path,
    _legacy_baseline_without_guard: None,
) -> None:
    engine = FakeEngine(_safe_outputs())
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=output_directory,
        release_attestation_path=attestation_path,
        release_attestation_sha256=attestation_sha256,
        role="baseline",
    )

    suite = load_suite(MANIFEST)
    bundle = load_response_bundle(result.output_path, suite, expected_role="baseline")
    assert result.case_count == len(suite.corpus["cases"])
    assert bundle.document["artifact"]["role"] == "baseline"
    assert bundle.document["artifact"]["id"] == (
        "relationship-shadow-baseline-0123456789ab"
    )
    assert bundle.document["artifact"]["source_kind"] == "offline_shadow"
    assert stat.S_IMODE(result.output_path.stat().st_mode) == 0o600


def test_candidate_shadow_fails_closed_without_observed_runtime_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_extensions.evals.relationship import shadow_runner

    monkeypatch.setattr(shadow_runner, "_relationship_guard_module", lambda: None)
    engine = FakeEngine(_safe_outputs())
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError, match="observed runtime relationship guard"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="candidate",
        )

    assert engine.calls == []
    assert engine.closed is False
    assert list(output_directory.iterdir()) == []


def test_guard_observer_delegates_and_attests_exact_candidate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import shadow_runner
    from ava_extensions.identity.relationship_safety import (
        RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
        RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
    )

    suite = load_suite(MANIFEST)

    class Decision:
        action = "allow"
        gate_ids: tuple[str, ...] = ()
        output_text = "Réponse synthétique sûre."
        policy_id = RELATIONSHIP_TEXT_SAFETY_POLICY_ID
        policy_version = RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION
        policy_sha256 = suite.safety_policy_sha256

    class Guard:
        policy_sha256 = suite.safety_policy_sha256

        def metadata(self) -> dict[str, object]:
            return {
                "policy_id": RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
                "policy_version": RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
            }

        def apply(self, _response_text: str) -> Decision:
            return Decision()

    def prepare(overlay: object | None, _turns=()):
        return Guard() if overlay is not None else None

    module = SimpleNamespace(
        prepare_relationship_guard=prepare,
        RelationshipOutputGuard=Guard,
    )
    routes = SimpleNamespace(prepare_relationship_guard=prepare)
    monkeypatch.setattr(shadow_runner, "_relationship_guard_module", lambda: module)

    with shadow_runner._observe_runtime_relationship_guard(routes) as observer:
        for case in suite.corpus["cases"]:
            observer.begin_case(case["id"])
            overlay = object() if _relationship_allowed(case) else None
            guard = routes.prepare_relationship_guard(overlay, ())
            if guard is not None:
                decision = guard.apply("Réponse synthétique sûre.")
                assert decision.output_text == "Réponse synthétique sûre."
            observer.finish_case("Réponse synthétique sûre.")

    document = observer.document(suite=suite, role="candidate")
    assert document["active"] is True
    assert document["policy_sha256"] == suite.safety_policy_sha256
    assert document["observed_prepare_calls"] == 39
    assert document["observed_apply_calls"] == 36
    assert len(document["actions"]) == 36
    assert all(action["action"] == "pass" for action in document["actions"])
    assert "Réponse synthétique sûre" not in json.dumps(document, ensure_ascii=False)


def test_guard_observer_rejects_a_baseline_release_that_invokes_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import shadow_runner

    suite = load_suite(MANIFEST)

    class Guard:
        policy_sha256 = suite.safety_policy_sha256

        def metadata(self) -> dict[str, object]:
            return {
                "policy_id": "ava.relationship.text-safety",
                "policy_version": "1.6.0",
            }

        def apply(self, _response_text: str) -> Any:
            raise AssertionError("baseline probe must stop after prepare")

    def prepare(_overlay: object, _turns=()) -> Guard:
        return Guard()

    module = SimpleNamespace(
        prepare_relationship_guard=prepare,
        RelationshipOutputGuard=Guard,
    )
    routes = SimpleNamespace(prepare_relationship_guard=prepare)
    monkeypatch.setattr(shadow_runner, "_relationship_guard_module", lambda: module)

    with shadow_runner._observe_runtime_relationship_guard(routes) as observer:
        observer.begin_case("warmth-optin")
        routes.prepare_relationship_guard(object(), ())
        observer.finish_case("Réponse synthétique sûre.")

    with pytest.raises(ShadowRunError, match="baseline release invoked"):
        observer.document(suite=suite, role="baseline")


def test_shadow_runner_rejects_identity_like_output_before_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "invented-person@example.invalid"
    engine = FakeEngine([sentinel])
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert list(output_directory.iterdir()) == []
    assert len(engine.calls) == 1
    assert engine.closed is True


def test_shadow_runner_refuses_non_directory_output_before_engine_creation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing.json"
    output.write_text("operator-owned\n", encoding="utf-8")
    output.chmod(0o600)
    factory_calls: list[bool] = []
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    def factory() -> FakeEngine:
        factory_calls.append(True)
        return FakeEngine(_safe_outputs())

    with pytest.raises(ShadowRunError, match="directory"):
        run_shadow(
            engine_factory=factory,
            output_directory=output,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    assert factory_calls == []
    assert output.read_text(encoding="utf-8") == "operator-owned\n"


def test_content_addressed_bundle_is_immutable(tmp_path: Path) -> None:
    from ava_extensions.evals.relationship import shadow_runner

    suite = load_suite(MANIFEST)
    output_directory = _private_output_directory(tmp_path)
    document = _candidate_document()

    output_path, digest = shadow_runner._publish_bundle(
        output_directory,
        document,
        suite,
        role="candidate",
    )

    assert digest == sha256_file(output_path)
    assert digest.removeprefix("sha256:") in output_path.name
    with pytest.raises(shadow_runner.ShadowArtifactConflictError):
        shadow_runner._publish_bundle(
            output_directory,
            document,
            suite,
            role="candidate",
        )


def test_shadow_runner_rejects_remote_or_credentialed_backends() -> None:
    from ava_extensions.evals.relationship import shadow_runner

    for value in (
        "https://127.0.0.1:8000",
        "http://192.0.2.10:8000",
        "http://user:password@127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:not-a-port",
    ):
        with pytest.raises(argparse.ArgumentTypeError):
            shadow_runner._loopback_url(value)

    assert shadow_runner._loopback_url("http://127.0.0.1:8000") == (
        "http://127.0.0.1:8000"
    )
    assert shadow_runner._loopback_url("http://[::1]:8000/") == "http://[::1]:8000"


def test_shadow_runner_rejects_unattested_adapter_before_model_use(
    tmp_path: Path,
) -> None:
    engine = FakeEngine(_safe_outputs())
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(
        tmp_path,
        adapter="other-adapter",
    )

    with pytest.raises(ShadowRunError, match="adapter"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    assert engine.calls == []
    assert engine.closed is True


def test_shadow_runner_rejects_model_absent_from_catalog(tmp_path: Path) -> None:
    engine = FakeEngine(_safe_outputs())
    engine.models = ["different-model"]
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError, match="/v1/models"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    assert engine.calls == []
    assert list(output_directory.iterdir()) == []


def test_shadow_runner_rejects_release_attestation_digest_drift_before_engine_creation(
    tmp_path: Path,
) -> None:
    output_directory = _private_output_directory(tmp_path)
    attestation_path, _attestation_sha256 = _release_attestation(tmp_path)
    factory_calls: list[bool] = []

    def factory() -> FakeEngine:
        factory_calls.append(True)
        return FakeEngine(_safe_outputs())

    with pytest.raises(ValueError, match="empreinte"):
        run_shadow(
            engine_factory=factory,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256="sha256:" + "f" * 64,
        )

    assert factory_calls == []


def test_completion_model_field_must_match_attested_model() -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import shadow_runner

    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "model": "different-model",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "synthetic", "tool_calls": []},
                }
            ],
        },
    )

    with pytest.raises(ShadowRunError, match="different model"):
        shadow_runner._extract_completion(
            response,
            expected_model="synthetic-model-v1",
        )


@pytest.mark.parametrize(
    ("include_finish_reason", "finish_reason"),
    (
        (False, None),
        (True, None),
        (True, "length"),
        (True, "content_filter"),
        (True, "provider-unknown-terminal"),
    ),
)
def test_completion_requires_exact_stop_provider_terminal(
    include_finish_reason: bool,
    finish_reason: str | None,
) -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import shadow_runner

    choice: dict[str, Any] = {"message": {"content": "synthetic", "tool_calls": []}}
    if include_finish_reason:
        choice["finish_reason"] = finish_reason
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"model": "synthetic-model-v1", "choices": [choice]},
    )

    with pytest.raises(ShadowRunError, match="exact stop terminal"):
        shadow_runner._extract_completion(
            response,
            expected_model="synthetic-model-v1",
        )


@pytest.mark.parametrize("content", ("", " ", "\n\t"))
def test_completion_requires_non_empty_provider_content(content: str) -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import shadow_runner

    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "model": "synthetic-model-v1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content, "tool_calls": []},
                }
            ],
        },
    )

    with pytest.raises(ShadowRunError, match="invalid response text"):
        shadow_runner._extract_completion(
            response,
            expected_model="synthetic-model-v1",
        )


@pytest.mark.parametrize(
    ("provider_payload", "expected_error"),
    (
        (
            {
                "content": "MISSING_TERMINAL_OUTPUT_MUST_STAY_PRIVATE",
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "exact stop terminal",
        ),
        (
            {
                "content": "NULL_TERMINAL_OUTPUT_MUST_STAY_PRIVATE",
                "finish_reason": None,
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "exact stop terminal",
        ),
        (
            {
                "content": "TRUNCATED_PROVIDER_OUTPUT_MUST_STAY_PRIVATE",
                "finish_reason": "length",
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "exact stop terminal",
        ),
        (
            {
                "content": "FILTERED_PROVIDER_OUTPUT_MUST_STAY_PRIVATE",
                "finish_reason": "content_filter",
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "exact stop terminal",
        ),
        (
            {
                "content": "UNKNOWN_TERMINAL_OUTPUT_MUST_STAY_PRIVATE",
                "finish_reason": "provider-unknown-terminal",
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "exact stop terminal",
        ),
        (
            {
                "content": " \n\t",
                "finish_reason": "stop",
                "model": "synthetic-model-v1",
                "usage": {},
            },
            "empty response content",
        ),
    ),
)
def test_invalid_provider_completion_is_never_published(
    tmp_path: Path,
    provider_payload: dict[str, Any],
    expected_error: str,
) -> None:
    engine = FakeEngine([provider_payload])
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError, match=expected_error):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    assert len(engine.calls) == 1
    assert list(output_directory.iterdir()) == []


def test_engine_result_model_must_match_before_http_response(
    tmp_path: Path,
) -> None:
    engine = FakeEngine(
        [
            {
                "content": "synthetic",
                "finish_reason": "stop",
                "model": "provider-returned-another-model",
                "usage": {},
            }
        ]
    )
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)

    with pytest.raises(ShadowRunError, match="different model"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            role="baseline",
        )

    assert len(engine.calls) == 1
    assert list(output_directory.iterdir()) == []


def test_configured_anthropic_path_catalogs_attested_new_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _legacy_baseline_without_guard: None,
) -> None:
    api_key = "configured-anthropic-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-auth-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient-provider.invalid")
    monkeypatch.setenv("ANTHROPIC_FUTURE_OVERRIDE_CANARY", "ambient-override")
    engine = FakeEngine(_safe_outputs())
    engine.engine_id = "cloud"
    engine.models = ["claude-sonnet-4-6"]
    engine.servable_models = {"claude-sonnet-5"}
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(
        tmp_path,
        adapter="cloud",
        model="claude-sonnet-5",
        provider="anthropic",
    )

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=output_directory,
        release_attestation_path=attestation_path,
        release_attestation_sha256=attestation_sha256,
        execution_mode="configured-anthropic",
        role="baseline",
    )

    assert result.case_count == 39
    assert result.model_call_count == 43
    assert engine.closed is True
    assert engine.environments
    assert all(
        {
            "ANTHROPIC_API_KEY": environment["ANTHROPIC_API_KEY"],
            "ANTHROPIC_AUTH_TOKEN": environment["ANTHROPIC_AUTH_TOKEN"],
            "ANTHROPIC_BASE_URL": environment["ANTHROPIC_BASE_URL"],
            "ANTHROPIC_FUTURE_OVERRIDE_CANARY": environment[
                "ANTHROPIC_FUTURE_OVERRIDE_CANARY"
            ],
        }
        == {
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_AUTH_TOKEN": None,
            "ANTHROPIC_BASE_URL": None,
            "ANTHROPIC_FUTURE_OVERRIDE_CANARY": None,
        }
        for environment in engine.environments
    )


def test_configured_anthropic_path_uses_real_cloud_engine_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _legacy_baseline_without_guard: None,
) -> None:
    from types import SimpleNamespace

    from ava_extensions.evals.relationship import release_attestation
    from openjarvis.engine.cloud import CloudEngine

    for name in (
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_CODEX_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    class OfflineAnthropicMessages:
        def __init__(self, outputs: list[str]) -> None:
            self.outputs = outputs
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(dict(kwargs))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self.outputs.pop(0))],
                model="claude-sonnet-5",
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    class OfflineAnthropicClient:
        def __init__(self, outputs: list[str]) -> None:
            self.messages = OfflineAnthropicMessages(outputs)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    engine = CloudEngine()
    client = OfflineAnthropicClient(_safe_outputs())
    engine._anthropic_client = client
    output_directory = _private_output_directory(tmp_path)
    release, module_file, _manifest = _deployed_release_fixture(tmp_path)
    monkeypatch.setattr(release_attestation, "_MODULE_PATH", module_file)
    config = tmp_path / "cloud-config.toml"
    config.write_text(
        '[intelligence]\ndefault_model = "claude-sonnet-5"\nprovider = "anthropic"\n',
        encoding="utf-8",
    )
    attestation = release_attestation.generate_release_attestation(
        release_root=release,
        config_path=config,
        output_directory=output_directory,
    )

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=output_directory,
        release_attestation_path=attestation.output_path,
        release_attestation_sha256=attestation.sha256,
        execution_mode="configured-anthropic",
        role="baseline",
    )

    assert result.model_call_count == 43
    assert client.closed is True
    assert client.messages.outputs == []
    assert len(client.messages.calls) == 43
    assert all(call["model"] == "claude-sonnet-5" for call in client.messages.calls)
    assert all("tools" not in call for call in client.messages.calls)


def test_configured_anthropic_rejects_reflected_api_key_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "ANTHROPIC_API_KEY_REFLECTION_CANARY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    outputs = _safe_outputs()
    outputs[4] = secret
    engine = FakeEngine(outputs)
    engine.engine_id = "cloud"
    engine.models = ["claude-sonnet-5"]
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(
        tmp_path,
        adapter="cloud",
        model="claude-sonnet-5",
        provider="anthropic",
    )

    with pytest.raises(ShadowRunError, match="identity material"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            execution_mode="configured-anthropic",
            role="candidate",
        )

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert list(output_directory.iterdir()) == []


def test_configured_anthropic_mode_rejects_other_attestation_before_engine(
    tmp_path: Path,
) -> None:
    output_directory = _private_output_directory(tmp_path)
    attestation_path, attestation_sha256 = _release_attestation(tmp_path)
    factory_calls: list[bool] = []

    with pytest.raises(ShadowRunError, match="Anthropic"):
        run_shadow(
            engine_factory=lambda: factory_calls.append(True),
            output_directory=output_directory,
            release_attestation_path=attestation_path,
            release_attestation_sha256=attestation_sha256,
            execution_mode="configured-anthropic",
        )

    assert factory_calls == []


def test_cli_configured_anthropic_selects_cloud_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ava_extensions.evals.relationship import shadow_runner
    from openjarvis.engine import cloud

    marker = object()
    observed: dict[str, Any] = {}

    def fake_run_shadow(**kwargs: Any) -> None:
        observed["execution_mode"] = kwargs["execution_mode"]
        observed["engine"] = kwargs["engine_factory"]()

    monkeypatch.setenv("AVA_PERCEPTION", "0")
    monkeypatch.setattr(cloud, "CloudEngine", lambda: marker)
    monkeypatch.setattr(shadow_runner, "run_shadow", fake_run_shadow)

    exit_code = shadow_runner.main(
        [
            "--configured-anthropic",
            "--release-attestation",
            str(tmp_path / "release-attestation.json"),
            "--release-attestation-sha256",
            "sha256:" + "1" * 64,
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert observed == {"engine": marker, "execution_mode": "configured-anthropic"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _deployed_release_fixture(tmp_path: Path) -> tuple[Path, Path, bytes]:
    git_sha = "89abcdef0123456789abcdef0123456789abcdef"
    release = tmp_path / git_sha
    release.mkdir()
    (release / ".ava-ready").write_bytes(b"")
    manifest = (
        "format=ava-release-v1\n"
        f"git_sha={git_sha}\n"
        f"source_tree_sha256={'1' * 64}\n"
        f"rust_tree_sha256={'2' * 64}\n"
        f"wheel_sha256={'3' * 64}\n"
        "wheel_filename=openjarvis_rust-0.1.0-cp312-cp312-manylinux_2_36_x86_64.whl\n"
        f"attestation_sha256={'4' * 64}\n"
        f"evolutions_sha256={'5' * 64}\n"
    ).encode()
    (release / ".ava-release").write_bytes(manifest)
    module_path = release / "ava_extensions" / "evals" / "relationship"
    module_path.mkdir(parents=True)
    module_file = module_path / "release_attestation.py"
    module_file.write_text("# deployed fixture\n", encoding="utf-8")
    return release, module_file, manifest


def test_release_attestation_is_derived_from_deployed_release_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ava_extensions.evals.relationship import release_attestation

    release, module_file, manifest = _deployed_release_fixture(tmp_path)
    monkeypatch.setattr(release_attestation, "_MODULE_PATH", module_file)
    secret_sentinel = "ANTHROPIC_SECRET_MUST_NOT_ENTER_ATTESTATION"
    config = tmp_path / "config.toml"
    config_payload = (
        "[intelligence]\n"
        'default_model = "claude-sonnet-5"\n'
        'provider = "anthropic"\n'
        'preferred_engine = "cloud"\n'
        f'secret_probe = "{secret_sentinel}"\n'
        "[server]\n"
        'model = ""\n'
    ).encode()
    config.write_bytes(config_payload)
    output_directory = _private_output_directory(tmp_path)

    exit_code = release_attestation.main(
        [
            "--release-root",
            str(release),
            "--config",
            str(config),
            "--output-dir",
            str(output_directory),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert secret_sentinel not in captured.out
    metadata = json.loads(captured.out)
    output_path = Path(metadata["path"])
    assert metadata["sha256"] == sha256_file(output_path)
    assert metadata["sha256"].removeprefix("sha256:") in output_path.name
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    serialized = output_path.read_text(encoding="utf-8")
    assert secret_sentinel not in serialized
    document = json.loads(serialized)
    assert document["engine"] == {
        "adapter": "cloud",
        "config_sha256": sha256_bytes(config_payload),
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "revision": "claude-sonnet-5",
    }
    assert document["artifact"] == {"manifest_sha256": sha256_bytes(manifest)}
    assert document["release"]["git_sha"] == release.name

    repeated = release_attestation.generate_release_attestation(
        release_root=release,
        config_path=config,
        output_directory=output_directory,
    )
    assert repeated.output_path == output_path
    assert repeated.sha256 == metadata["sha256"]


@pytest.mark.parametrize(
    ("field", "valid_digest"),
    (
        ("source_tree_sha256", "1" * 64),
        ("attestation_sha256", "4" * 64),
    ),
)
def test_release_attestation_rejects_invalid_deployment_manifest_digest(
    tmp_path: Path,
    field: str,
    valid_digest: str,
) -> None:
    from ava_extensions.evals.relationship import release_attestation

    _release, _module_file, manifest = _deployed_release_fixture(tmp_path)
    invalid_manifest = manifest.replace(
        f"{field}={valid_digest}".encode(),
        f"{field}=not-a-sha256".encode(),
    )
    assert invalid_manifest != manifest

    with pytest.raises(ValueError, match="empreinte du manifeste de release invalide"):
        release_attestation._parse_release_manifest(invalid_manifest)


def test_release_attestation_refuses_generator_outside_target_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_extensions.evals.relationship import release_attestation

    release, _module_file, _manifest = _deployed_release_fixture(tmp_path)
    monkeypatch.setattr(release_attestation, "_MODULE_PATH", Path(__file__))
    config = tmp_path / "config.toml"
    config.write_text(
        '[intelligence]\ndefault_model="claude-sonnet-5"\nprovider="anthropic"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hors de la release"):
        release_attestation.generate_release_attestation(
            release_root=release,
            config_path=config,
            output_directory=_private_output_directory(tmp_path),
        )
