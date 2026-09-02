"""The causal baseline must stop ``jarvis serve`` before all startup work."""

from __future__ import annotations

import importlib

from ava_extensions.identity import relationship_guard_treatment as treatment
from click.testing import CliRunner

serve_module = importlib.import_module("openjarvis.cli.serve")


class _ReachedBanner(RuntimeError):
    pass


def test_baseline_serve_refuses_before_banner_credentials_config_or_engine(
    monkeypatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        "shadow-baseline-only-v1",
    )

    monkeypatch.setattr(
        serve_module,
        "print_banner",
        lambda **_kwargs: touched.append("banner"),
    )
    monkeypatch.setattr(
        serve_module,
        "inject_credentials",
        lambda: touched.append("credentials"),
    )
    monkeypatch.setattr(
        serve_module,
        "load_config",
        lambda: touched.append("config"),
    )
    monkeypatch.setattr(
        serve_module,
        "get_engine",
        lambda *_args, **_kwargs: touched.append("engine"),
    )

    result = CliRunner().invoke(serve_module.serve, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert touched == []


def test_runtime_treatment_reaches_banner_first(monkeypatch) -> None:
    touched: list[str] = []
    monkeypatch.setattr(
        treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        "runtime-enforced-v1",
    )

    def reached_banner(**_kwargs) -> None:
        touched.append("banner")
        raise _ReachedBanner

    monkeypatch.setattr(serve_module, "print_banner", reached_banner)
    monkeypatch.setattr(
        serve_module,
        "inject_credentials",
        lambda: touched.append("credentials"),
    )
    monkeypatch.setattr(
        serve_module,
        "load_config",
        lambda: touched.append("config"),
    )
    monkeypatch.setattr(
        serve_module,
        "get_engine",
        lambda *_args, **_kwargs: touched.append("engine"),
    )

    result = CliRunner().invoke(serve_module.serve, [])

    assert isinstance(result.exception, _ReachedBanner)
    assert touched == ["banner"]
