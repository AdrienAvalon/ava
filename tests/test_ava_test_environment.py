"""Regression tests for process-wide Ava test isolation."""

import os


def test_perception_is_disabled_for_the_whole_pytest_process() -> None:
    """The root conftest must neutralise perception before test imports."""
    assert os.environ.get("AVA_PERCEPTION") == "0"
