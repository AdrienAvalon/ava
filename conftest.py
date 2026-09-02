"""Global safety boundary for every pytest collection in the Ava fork."""

import os

import pytest

# This root conftest is loaded before child conftests and test modules. Tests may
# exercise perception components explicitly, but collection must never start the
# background observer against a real Control Plane.
os.environ["AVA_PERCEPTION"] = "0"


@pytest.fixture(autouse=True)
def _runtime_relationship_treatment_for_ordinary_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run ordinary app tests with the only treatment that may serve traffic.

    The dedicated causal-treatment tests override this fixture explicitly when
    they exercise the non-servable baseline and its verified shadow scope.
    """

    from ava_extensions.identity import relationship_guard_treatment

    monkeypatch.setattr(
        relationship_guard_treatment,
        "RELATIONSHIP_GUARD_TREATMENT",
        "runtime-enforced-v1",
    )
