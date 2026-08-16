"""Analytics opt-out and anonymous-identity contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from openjarvis.analytics.client import AnalyticsClient
from openjarvis.analytics.identity import (
    _env_opt_out,
    get_or_create_anon_id,
    is_analytics_enabled,
    reset_anon_id,
)
from openjarvis.core.config import AnalyticsConfig, JarvisConfig
from openjarvis.server.app import create_app


@pytest.fixture(autouse=True)
def _clean_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("OPENJARVIS_NO_ANALYTICS", raising=False)


@pytest.mark.parametrize("name", ["DO_NOT_TRACK", "OPENJARVIS_NO_ANALYTICS"])
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "anything"])
def test_truthy_environment_values_disable_analytics(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    assert _env_opt_out() is True


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_falsy_environment_values_do_not_disable_analytics(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", value)
    assert _env_opt_out() is False


def test_explicit_opt_in_is_visible_to_the_public_gate() -> None:
    assert is_analytics_enabled(AnalyticsConfig(enabled=True)) is True


def test_environment_opt_out_never_enables_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENJARVIS_NO_ANALYTICS", "1")
    assert is_analytics_enabled(AnalyticsConfig(enabled=False)) is False


@pytest.mark.parametrize(
    ("configured", "environment_name", "environment_value", "expected"),
    [
        (True, None, None, True),
        (False, None, None, False),
        (True, "DO_NOT_TRACK", "1", False),
        (True, "OPENJARVIS_NO_ANALYTICS", "yes", False),
        (True, "OPENJARVIS_NO_ANALYTICS", "false", True),
    ],
)
def test_public_gate_outside_pytest(
    configured: bool,
    environment_name: str | None,
    environment_value: str | None,
    expected: bool,
) -> None:
    environment = os.environ.copy()
    for name in ("PYTEST_CURRENT_TEST", "DO_NOT_TRACK", "OPENJARVIS_NO_ANALYTICS"):
        environment.pop(name, None)
    environment["AVA_PERCEPTION"] = "0"
    if environment_name is not None and environment_value is not None:
        environment[environment_name] = environment_value
    code = (
        "from openjarvis.analytics.identity import is_analytics_enabled; "
        "from openjarvis.core.config import AnalyticsConfig; "
        f"print(is_analytics_enabled(AnalyticsConfig(enabled={configured!r})))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines()[-1] == str(expected)


def test_ava_configuration_defaults_to_external_analytics_disabled() -> None:
    assert AnalyticsConfig().enabled is False


def test_disabled_client_creates_neither_sdk_nor_anonymous_id(tmp_path) -> None:  # noqa: ANN001
    anon_id_path = tmp_path / "anon_id"
    client = AnalyticsClient(
        AnalyticsConfig(enabled=False, anon_id_path=str(anon_id_path))
    )

    assert client.enabled is False
    assert client.anon_id == ""
    assert not anon_id_path.exists()


def test_disabled_server_wires_no_external_analytics(tmp_path) -> None:  # noqa: ANN001
    config = JarvisConfig()
    config.analytics.enabled = False
    config.analytics.anon_id_path = str(tmp_path / "anon_id")
    config.traces.enabled = False

    app = create_app(None, "test", config=config)

    assert app.state.analytics_client is None
    assert app.state.analytics_bridge is None
    assert not Path(config.analytics.anon_id_path).exists()


@pytest.mark.parametrize(
    ("environment_update", "expected_returncode"),
    [
        ({}, 1),
        ({"OPENJARVIS_ENABLE_ANALYTICS": "yes"}, 0),
        ({"OPENJARVIS_ENABLE_ANALYTICS": "yes", "DO_NOT_TRACK": "1"}, 1),
        (
            {
                "OPENJARVIS_ENABLE_ANALYTICS": "yes",
                "OPENJARVIS_NO_ANALYTICS": "yes",
            },
            1,
        ),
        ({"OPENJARVIS_ENABLE_ANALYTICS": "yes", "DO_NOT_TRACK": "false"}, 0),
    ],
)
def test_installer_requires_opt_in_and_honors_kill_switches(
    environment_update: dict[str, str], expected_returncode: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/install/install.sh").read_text(encoding="utf-8")
    start = source.index("analytics_enabled() {")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    environment = os.environ.copy()
    environment.pop("DO_NOT_TRACK", None)
    environment.pop("OPENJARVIS_NO_ANALYTICS", None)
    environment.pop("OPENJARVIS_ENABLE_ANALYTICS", None)
    environment.update(environment_update)

    result = subprocess.run(
        ["bash", "-c", f"{function}\nanalytics_enabled"],
        env=environment,
        check=False,
    )
    assert result.returncode == expected_returncode


def test_anonymous_id_is_persistent_and_resettable(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "anon_id"
    first = get_or_create_anon_id(path)
    assert get_or_create_anon_id(path) == first

    replacement = reset_anon_id(path)
    assert replacement != first
    assert path.read_text(encoding="utf-8").strip() == replacement
    assert list(tmp_path.glob("anon_id*.tmp")) == []
