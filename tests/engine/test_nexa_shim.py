"""Offline terminal-contract tests for the Nexa HTTP shim."""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def shim():
    sdk = types.ModuleType("nexaai")

    class LLM:
        def generate(self, _prompt: str, *, max_tokens: int):
            del max_tokens
            return ["Nexa ", "reply"]

    sdk.LLM = LLM  # type: ignore[attr-defined]
    sys.modules["nexaai"] = sdk
    sys.modules.pop("openjarvis.engine.nexa_shim", None)
    mod = importlib.import_module("openjarvis.engine.nexa_shim")
    mod = importlib.reload(mod)
    yield mod
    sys.modules.pop("openjarvis.engine.nexa_shim", None)
    sys.modules.pop("nexaai", None)


def test_non_streaming_eof_is_not_reported_as_stop(shim):
    response = TestClient(shim.app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Nexa reply"
    assert response.json()["choices"][0]["finish_reason"] == "length"


def test_streaming_eof_is_not_reported_as_stop(shim):
    with TestClient(shim.app).stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as response:
        body = "".join(response.iter_text())

    payloads = [
        json.loads(line[len("data:") :].strip())
        for line in body.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    assert payloads[-1]["choices"][0]["finish_reason"] == "length"
