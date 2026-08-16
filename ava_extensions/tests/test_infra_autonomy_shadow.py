"""Regressions du runner shadow d'autonomie infrastructure v3."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.evals import infra_autonomy_shadow as shadow_runner
from ava_extensions.evals.infra_autonomy.contracts import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from ava_extensions.evals.infra_autonomy_shadow import (
    DEFAULT_MANIFEST,
    SYSTEM_PROMPT_V3,
    InfraShadowError,
    _loopback_url,
    run_shadow,
)
from ava_extensions.evals.infra_autonomy_shadow_contracts import (
    SHADOW_RESPONSES_SCHEMA_VERSION,
    load_shadow_suite,
)

DATA_ROOT = Path(__file__).parents[1] / "evals" / "infra_autonomy" / "data"
POSITIVE = DATA_ROOT / "positive_selftest.v2.json"


class FakeEngine:
    def __init__(
        self,
        outputs: list[str],
        *,
        engine_id: str = "openai-compat",
        model: str = "synthetic-model-v3",
        catalog: list[str] | None = None,
        servable_models: set[str] | None = None,
        finish_reason: str = "stop",
        returned_model: str | None = None,
        tool_calls: Any = None,
    ) -> None:
        self.engine_id = engine_id
        self.outputs = list(outputs)
        self.model = model
        self.catalog = list(catalog) if catalog is not None else [model]
        self.servable_models = servable_models or set()
        self.finish_reason = finish_reason
        self.returned_model = returned_model if returned_model is not None else model
        self.tool_calls = [] if tool_calls is None else tool_calls
        self.calls: list[dict[str, Any]] = []
        self.catalog_calls = 0
        self.closed = False

    def list_models(self) -> list[str]:
        self.catalog_calls += 1
        return list(self.catalog)

    def can_serve(self, model: str) -> bool:
        return model in self.servable_models

    def generate(
        self,
        messages: list[Any],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": [
                    (message.role.value, str(message.content or ""))
                    for message in messages
                ],
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "kwargs": kwargs,
            }
        )
        return {
            "content": self.outputs.pop(0),
            "model": self.returned_model,
            "finish_reason": self.finish_reason,
            "tool_calls": self.tool_calls,
        }

    def close(self) -> None:
        self.closed = True


def _outputs() -> list[str]:
    document = json.loads(POSITIVE.read_text(encoding="utf-8"))
    return [
        canonical_json_bytes(response["model_output"]).decode("utf-8")
        for response in document["responses"]
    ]


def _attestation(
    tmp_path: Path,
    *,
    adapter: str = "openai-compat",
    provider: str = "synthetic-loopback",
    model: str = "synthetic-model-v3",
) -> tuple[Path, str]:
    document = {
        "schema_version": "ava.release.attestation/v1",
        "attestation_id": "ava-infra-shadow-test-v3",
        "release": {
            "repository": "repo:ava/synthetic-shadow",
            "git_sha": "0123456789abcdef0123456789abcdef01234567",
        },
        "engine": {
            "provider": provider,
            "model": model,
            "revision": model,
            "adapter": adapter,
            "config_sha256": "sha256:" + "4" * 64,
        },
        "artifact": {"manifest_sha256": "sha256:" + "5" * 64},
        "canonical_knowledge": False,
    }
    path = tmp_path / "release-attestation.json"
    path.write_bytes(canonical_json_bytes(document) + b"\n")
    return path, sha256_file(path)


def _private_output(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    return output


def _configured_attestation(tmp_path: Path) -> tuple[Path, str]:
    return _attestation(
        tmp_path,
        adapter="cloud",
        provider="anthropic",
        model="claude-sonnet-4-6",
    )


def _configured_engine(**kwargs: Any) -> FakeEngine:
    return FakeEngine(
        _outputs(),
        engine_id="cloud",
        model="claude-sonnet-4-6",
        **kwargs,
    )


def test_v3_recalcule_model_view_et_oracle_depuis_v2_sans_les_confondre() -> None:
    suite = load_shadow_suite(DEFAULT_MANIFEST)

    assert len(suite.model_views_by_case_id) == 32
    assert list(suite.model_views_by_case_id) == list(suite.oracle_by_case_id)
    for case_id, view in suite.model_views_by_case_id.items():
        serialized = canonical_json_bytes(view)
        assert b"runtime_observation" not in serialized
        assert b'"floors"' not in serialized
        assert b"forbidden_canary_ids" not in serialized
        assert serialized != canonical_json_bytes(suite.oracle_by_case_id[case_id])
    assert suite.evaluation_suite.manifest["schema_version"] == (
        "ava.infra-autonomy.manifest/v2"
    )
    assert suite.manifest["freeze"] == {
        "state": "frozen",
        "requires_version_bump": True,
        "v2_untouched": True,
    }
    assert suite.manifest["execution_policy"]["provider_calls"] == (
        "configured-anthropic-explicit-only"
    )


def test_runner_un_generate_exact_par_cas_et_sortie_privee_content_addressee(
    tmp_path: Path,
) -> None:
    suite = load_shadow_suite(DEFAULT_MANIFEST)
    engine = FakeEngine(_outputs())
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=output,
        release_attestation_path=attestation,
        release_attestation_sha256=digest,
    )

    assert result.case_count == result.model_call_count == 32
    assert result.absolute_pass is True
    assert engine.catalog_calls == 1
    assert len(engine.calls) == 32
    assert engine.closed is True
    assert stat.S_IMODE(result.output_path.stat().st_mode) == 0o600
    assert result.bundle_sha256 == sha256_file(result.output_path)
    assert result.bundle_sha256.removeprefix("sha256:") in result.output_path.name
    bundle = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert bundle["schema_version"] == SHADOW_RESPONSES_SCHEMA_VERSION
    assert bundle["artifact"]["release_attestation_sha256"] == digest
    assert bundle["artifact"]["engine"]["model"] == "synthetic-model-v3"
    assert bundle["artifact"]["engine"]["adapter"] == "openai-compat"
    assert bundle["generation"] == {
        "mode": "offline-shadow",
        "network": "disabled",
        "provider_calls": "disabled",
        "tools": "disabled",
        "memory": "disabled",
        "traces": "disabled",
        "perception": "disabled",
        "learning": "disabled",
        "generate_calls_per_case": 1,
        "retry": "disabled",
        "repair": "disabled",
        "runtime_observation": "runner-owned-oracle",
    }
    assert bundle["screening"]["absolute_pass"] is True
    assert bundle["promotion"]["eligible_for_promotion"] is False
    assert bundle["promotion"]["release_activation_authorized"] is False
    assert [item["runtime_observation"] for item in bundle["responses"]] == [
        suite.oracle_by_case_id[case["id"]]["runtime_observation"]
        for case in suite.evaluation_suite.corpus["cases"]
    ]
    for call, case in zip(
        engine.calls, suite.evaluation_suite.corpus["cases"], strict=True
    ):
        assert call["model"] == "synthetic-model-v3"
        assert call["temperature"] == 0.0
        assert call["kwargs"] == {}
        assert call["messages"] == [
            ("system", SYSTEM_PROMPT_V3),
            (
                "user",
                canonical_json_bytes(suite.model_views_by_case_id[case["id"]]).decode(
                    "utf-8"
                ),
            ),
        ]


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "", None])
def test_finish_reason_non_stop_ne_publie_rien(
    tmp_path: Path,
    finish_reason: Any,
) -> None:
    engine = FakeEngine(_outputs(), finish_reason=finish_reason)
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="finish_reason"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
        )

    assert len(engine.calls) == 1
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "invalid",
    [
        "not-json",
        '{"raw_text":"x","raw_text":"y"}',
        '{"raw_text":"x","runtime_observation":{}}',
        "{}",
    ],
)
def test_json_invalide_n_est_jamais_repare_ni_relance(
    tmp_path: Path,
    invalid: str,
) -> None:
    outputs = _outputs()
    outputs[0] = invalid
    engine = FakeEngine(outputs)
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="JSON"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
        )

    assert len(engine.calls) == 1
    assert list(output.iterdir()) == []


def test_outil_modele_est_refuse_avant_publication(tmp_path: Path) -> None:
    engine = FakeEngine(
        _outputs(),
        tool_calls=[{"name": "danger", "arguments": "{}"}],
    )
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="outil"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
        )
    assert list(output.iterdir()) == []


def test_mode_offline_bloque_un_socket_avant_tout_generate(tmp_path: Path) -> None:
    class NetworkEngine(FakeEngine):
        def list_models(self) -> list[str]:
            with socket.socket() as probe:
                probe.connect(("127.0.0.1", 9))
            return super().list_models()

    engine = NetworkEngine(_outputs())
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="echouee"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
        )
    assert engine.calls == []
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://192.0.2.1:8000",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
    ],
)
def test_endpoint_loopback_est_litteral_local_et_sans_identifiant(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _loopback_url(value)


def test_modele_ou_adapter_divergent_est_refuse_avant_generate(tmp_path: Path) -> None:
    engine = FakeEngine(_outputs(), model="other-model")
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="modele atteste"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
        )
    assert engine.calls == []
    assert list(output.iterdir()) == []


def test_attestation_digest_est_verifiee_avant_creation_moteur(tmp_path: Path) -> None:
    created = False

    def factory() -> FakeEngine:
        nonlocal created
        created = True
        return FakeEngine(_outputs())

    attestation, _digest = _attestation(tmp_path)
    output = _private_output(tmp_path)
    with pytest.raises(InfraShadowError, match="attestation"):
        run_shadow(
            engine_factory=factory,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256="sha256:" + "f" * 64,
        )
    assert created is False
    assert list(output.iterdir()) == []


def test_bundle_identique_est_un_noop_content_addresse(tmp_path: Path) -> None:
    attestation, digest = _attestation(tmp_path)
    output = _private_output(tmp_path)
    first = run_shadow(
        engine_factory=lambda: FakeEngine(_outputs()),
        output_directory=output,
        release_attestation_path=attestation,
        release_attestation_sha256=digest,
    )
    second = run_shadow(
        engine_factory=lambda: FakeEngine(_outputs()),
        output_directory=output,
        release_attestation_path=attestation,
        release_attestation_sha256=digest,
    )
    assert first.output_path == second.output_path
    assert first.bundle_sha256 == second.bundle_sha256
    assert len(list(output.iterdir())) == 1
    assert first.bundle_sha256 == sha256_bytes(first.output_path.read_bytes())


def test_publication_initiale_ouvre_une_fois_ferme_et_refuse_un_conflit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _private_output(tmp_path)
    bundle = {"proof": "first-publish"}
    payload = canonical_json_bytes(bundle) + b"\n"
    digest = sha256_bytes(payload)
    expected = output / (
        "infra-autonomy-shadow-candidate-0123456789ab-"
        f"{digest.removeprefix('sha256:')}.json"
    )
    real_open = os.open
    opened_bundle_descriptors: list[int] = []

    def tracked_open(path: Any, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        if Path(path) == expected:
            opened_bundle_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(shadow_runner.os, "open", tracked_open)
    published, published_digest = shadow_runner._publish_bundle(
        output,
        bundle,
        role="candidate",
        git_sha="0123456789abcdef0123456789abcdef01234567",
    )

    assert published == expected
    assert published_digest == digest
    assert expected.read_bytes() == payload
    assert len(opened_bundle_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_bundle_descriptors[0])

    expected.chmod(0o640)
    with pytest.raises(shadow_runner.InfraShadowConflictError, match="conflit"):
        shadow_runner._publish_bundle(
            output,
            bundle,
            role="candidate",
            git_sha="0123456789abcdef0123456789abcdef01234567",
        )
    assert len(opened_bundle_descriptors) == 1


def test_configured_anthropic_conserve_seulement_son_credential_fournisseur(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = {
        name: f"ambient-{index}"
        for index, name in enumerate(
            (
                *shadow_runner._PROXY_ENVIRONMENT,
                *shadow_runner._UNRELATED_PROVIDER_ENVIRONMENT,
            )
        )
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-shadow-sentinel")
    observed: dict[str, str | None] = {}
    engine = _configured_engine()

    def factory() -> FakeEngine:
        for name in (*ambient, "ANTHROPIC_API_KEY"):
            observed[name] = os.environ.get(name)
        return engine

    attestation, digest = _configured_attestation(tmp_path)
    output = _private_output(tmp_path)
    result = run_shadow(
        engine_factory=factory,
        output_directory=output,
        release_attestation_path=attestation,
        release_attestation_sha256=digest,
        execution_mode="configured-anthropic",
    )

    assert observed["ANTHROPIC_API_KEY"] == "anthropic-shadow-sentinel"
    assert all(observed[name] is None for name in ambient)
    assert {name: os.environ.get(name) for name in ambient} == ambient
    assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-shadow-sentinel"
    bundle = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert bundle["generation"]["mode"] == "configured-anthropic-shadow"
    assert bundle["generation"]["network"] == "anthropic-provider-only"
    assert bundle["generation"]["provider_calls"] == "anthropic-only"
    assert bundle["promotion"] == {
        "automatic_promotion": False,
        "eligible_for_adjudication": True,
        "eligible_for_promotion": False,
        "human_review_required": True,
        "independent_review_required": True,
        "promoted": False,
        "release_activation_authorized": False,
    }


@pytest.mark.parametrize(
    ("adapter", "provider", "model"),
    [
        ("openai-compat", "anthropic", "claude-sonnet-4-6"),
        ("cloud", "openai", "claude-sonnet-4-6"),
        ("cloud", "anthropic", "gpt-4o"),
    ],
)
def test_configured_anthropic_refuse_mauvaise_attestation_avant_moteur(
    tmp_path: Path,
    adapter: str,
    provider: str,
    model: str,
) -> None:
    attestation, digest = _attestation(
        tmp_path,
        adapter=adapter,
        provider=provider,
        model=model,
    )
    output = _private_output(tmp_path)
    factory_calls: list[bool] = []

    with pytest.raises(InfraShadowError, match="Anthropic"):
        run_shadow(
            engine_factory=lambda: factory_calls.append(True),
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
            execution_mode="configured-anthropic",
        )

    assert factory_calls == []
    assert list(output.iterdir()) == []


def test_configured_anthropic_exige_modele_atteste_serviable_exact(
    tmp_path: Path,
) -> None:
    engine = _configured_engine(catalog=["claude-haiku-4-5"])
    attestation, digest = _configured_attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="modele atteste"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
            execution_mode="configured-anthropic",
        )

    assert engine.calls == []
    assert list(output.iterdir()) == []


def test_configured_anthropic_accepte_modele_atteste_serviable_hors_catalogue(
    tmp_path: Path,
) -> None:
    engine = _configured_engine(
        catalog=["claude-haiku-4-5"],
        servable_models={"claude-sonnet-4-6"},
    )
    attestation, digest = _configured_attestation(tmp_path)

    result = run_shadow(
        engine_factory=lambda: engine,
        output_directory=_private_output(tmp_path),
        release_attestation_path=attestation,
        release_attestation_sha256=digest,
        execution_mode="configured-anthropic",
    )

    assert result.model_call_count == 32
    assert all(call["model"] == "claude-sonnet-4-6" for call in engine.calls)


def test_configured_anthropic_refuse_modele_retourne_different(tmp_path: Path) -> None:
    engine = _configured_engine(returned_model="claude-haiku-4-5")
    attestation, digest = _configured_attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match="modele retourne divergent"):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
            execution_mode="configured-anthropic",
        )

    assert len(engine.calls) == 1
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("finish_reason", "tool_calls", "message"),
    [
        ("length", [], "finish_reason"),
        ("stop", [{"name": "web_search", "arguments": "{}"}], "outil"),
        ("stop", False, "outil"),
    ],
)
def test_configured_anthropic_refuse_troncature_et_appels_outils(
    tmp_path: Path,
    finish_reason: str,
    tool_calls: Any,
    message: str,
) -> None:
    engine = _configured_engine(
        finish_reason=finish_reason,
        tool_calls=tool_calls,
    )
    attestation, digest = _configured_attestation(tmp_path)
    output = _private_output(tmp_path)

    with pytest.raises(InfraShadowError, match=message):
        run_shadow(
            engine_factory=lambda: engine,
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
            execution_mode="configured-anthropic",
        )

    assert len(engine.calls) == 1
    assert list(output.iterdir()) == []


def test_configured_anthropic_refuse_backend_url_avant_moteur(tmp_path: Path) -> None:
    attestation, digest = _configured_attestation(tmp_path)
    output = _private_output(tmp_path)
    factory_calls: list[bool] = []

    with pytest.raises(InfraShadowError, match="backend URL"):
        run_shadow(
            engine_factory=lambda: factory_calls.append(True),
            output_directory=output,
            release_attestation_path=attestation,
            release_attestation_sha256=digest,
            execution_mode="configured-anthropic",
            backend_url="http://127.0.0.1:8000",
        )

    assert factory_calls == []


def test_cli_configured_anthropic_instancie_cloud_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from openjarvis.engine import cloud

    marker = object()
    observed: dict[str, Any] = {}

    def fake_run_shadow(**kwargs: Any) -> None:
        observed["execution_mode"] = kwargs["execution_mode"]
        observed["backend_url"] = kwargs["backend_url"]
        observed["engine"] = kwargs["engine_factory"]()

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
    assert observed == {
        "backend_url": None,
        "engine": marker,
        "execution_mode": "configured-anthropic",
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_refuse_backend_et_anthropic_ensemble(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        shadow_runner.main(
            [
                "--backend-url",
                "http://127.0.0.1:8000",
                "--configured-anthropic",
                "--release-attestation",
                str(tmp_path / "release-attestation.json"),
                "--release-attestation-sha256",
                "sha256:" + "1" * 64,
                "--output-dir",
                str(tmp_path),
            ]
        )

    assert raised.value.code == 2
