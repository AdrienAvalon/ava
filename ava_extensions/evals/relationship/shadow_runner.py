"""Isolated HTTP shadow runner for the synthetic relationship evaluation.

The runner deliberately builds only the real chat router, not the production
daemon.  Authentication, server-owned persona composition and relationship
policy selection therefore cross their actual HTTP boundary while every
unrelated subsystem stays absent.  The generated response bundle is synthetic,
private on disk and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from ava_extensions.identity.relationship_safety import (
    RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
    RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
)

from .contracts import (
    RESPONSES_SCHEMA_VERSION,
    LoadedReleaseAttestation,
    LoadedSuite,
    canonical_json_bytes,
    load_release_attestation,
    load_response_bundle,
    load_suite,
    sha256_bytes,
)
from .evaluator import EXPECTED_RELATIONSHIP_PROFILE_ID

DEFAULT_MANIFEST = Path(__file__).with_name("data") / "manifest.v2.json"
GENERATED_BY = "ava-relationship-shadow-runner-v2"

_RUNTIME_OWNER = "matrix:@synthetic-owner:eval.invalid"
_RUNTIME_GUEST = "matrix:@synthetic-guest:eval.invalid"
_LOGICAL_OWNER = "synthetic:owner"
_LOGICAL_GUEST = "synthetic:guest"
_ASSERTION_ISSUER = "ava-relationship-shadow-runner"
_ASSERTION_AUDIENCE = "ava-relationship-shadow"
_ASSERTION_KEY_ID = "current"
_OIDC_ISSUER = "https://oidc.relationship-shadow.invalid"
_OIDC_AUDIENCE = "ava-relationship-shadow"
_OIDC_JWKS_URL = f"{_OIDC_ISSUER}/jwks"
_OIDC_KEY_ID = "relationship-shadow-rsa"
_OIDC_RUNTIME_OWNER = "synthetic-owner-oidc"
_MAX_GENERATION_TOKENS = 32_768
_REQUESTED_GENERATION_TOKENS = 1_024
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_UNEXPECTED_PERSONAL_OUTPUT = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
)
_RUN_LOCK = threading.Lock()
_EXECUTION_MODES = ("attested", "loopback", "configured-anthropic")


class ShadowRunError(RuntimeError):
    """The shadow run could not prove its isolation or output contract."""


class ShadowArtifactConflictError(ShadowRunError):
    """The immutable output path already exists."""


@dataclass(frozen=True, slots=True)
class ShadowRunResult:
    """Metadata-only result; model response text is intentionally excluded."""

    output_path: Path
    bundle_sha256: str
    case_count: int
    model_call_count: int
    negative_checks: tuple[str, ...]
    positive_checks: tuple[str, ...]
    release_attestation_sha256: str


@dataclass(frozen=True, slots=True)
class _ObservedCall:
    messages: tuple[tuple[str, str], ...]
    model: str
    temperature: float
    max_tokens: int

    @property
    def system_prompt(self) -> str:
        system = [content for role, content in self.messages if role == "system"]
        if len(system) != 1:
            raise ShadowRunError("shadow request did not contain one server prompt")
        return system[0]


@dataclass(frozen=True, slots=True)
class _ObservedGuardAction:
    case_id: str
    action: str
    gate_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    policy_sha256: str
    output_sha256: str


class _RelationshipGuardObserver:
    """Observe le vrai garde sans conserver le texte inspecte."""

    def __init__(self) -> None:
        self.current_case_id: str | None = None
        self.prepare_case_ids: list[str] = []
        self.actions: list[_ObservedGuardAction] = []
        self.prepared_policies: list[tuple[str, str, str]] = []

    def begin_case(self, case_id: str) -> None:
        if self.current_case_id is not None:
            raise ShadowRunError("relationship guard observer case overlap")
        self.current_case_id = case_id

    def observe_prepare(self, guard: Any) -> None:
        if self.current_case_id is None:
            raise ShadowRunError("relationship guard prepare outside corpus case")
        self.prepare_case_ids.append(self.current_case_id)
        if guard is None:
            return
        metadata_method = getattr(guard, "metadata", None)
        metadata = metadata_method() if callable(metadata_method) else {}
        if type(metadata) is not dict:
            raise ShadowRunError("relationship guard metadata is invalid")
        policy_id = metadata.get("policy_id")
        policy_version = metadata.get("policy_version")
        policy_sha256 = getattr(guard, "policy_sha256", None)
        if not all(
            isinstance(value, str) and value
            for value in (policy_id, policy_version, policy_sha256)
        ):
            raise ShadowRunError("relationship guard policy metadata is incomplete")
        self.prepared_policies.append((policy_id, policy_version, policy_sha256))

    def observe_apply(self, decision: Any) -> None:
        if self.current_case_id is None:
            raise ShadowRunError("relationship guard apply outside corpus case")
        action = getattr(decision, "action", None)
        gate_ids = getattr(decision, "gate_ids", None)
        policy_id = getattr(decision, "policy_id", None)
        policy_version = getattr(decision, "policy_version", None)
        policy_sha256 = getattr(decision, "policy_sha256", None)
        output_text = getattr(decision, "output_text", None)
        if action not in {"allow", "replace"}:
            raise ShadowRunError("relationship guard returned an invalid action")
        if type(gate_ids) is not tuple or not all(
            isinstance(gate_id, str) and gate_id for gate_id in gate_ids
        ):
            raise ShadowRunError("relationship guard returned invalid gate ids")
        if (action == "allow") != (not gate_ids):
            raise ShadowRunError("relationship guard action and gates diverge")
        if not all(
            isinstance(value, str) and value
            for value in (policy_id, policy_version, policy_sha256, output_text)
        ):
            raise ShadowRunError("relationship guard decision metadata is incomplete")
        self.actions.append(
            _ObservedGuardAction(
                case_id=self.current_case_id,
                action="pass" if action == "allow" else "replace",
                gate_ids=gate_ids,
                policy_id=policy_id,
                policy_version=policy_version,
                policy_sha256=policy_sha256,
                output_sha256=sha256_bytes(output_text.encode("utf-8")),
            )
        )

    def finish_case(self, response_text: str) -> None:
        if self.current_case_id is None:
            raise ShadowRunError("relationship guard observer has no active case")
        current_actions = [
            action for action in self.actions if action.case_id == self.current_case_id
        ]
        if len(current_actions) > 1:
            raise ShadowRunError("relationship guard applied more than once per case")
        if current_actions and current_actions[0].output_sha256 != sha256_bytes(
            response_text.encode("utf-8")
        ):
            raise ShadowRunError("relationship guard output differs from HTTP response")
        self.current_case_id = None

    def document(
        self,
        *,
        suite: LoadedSuite,
        role: Literal["baseline", "candidate"],
    ) -> dict[str, Any]:
        if self.current_case_id is not None:
            raise ShadowRunError("relationship guard observer ended inside a case")
        if role == "baseline":
            if self.prepare_case_ids or self.actions:
                raise ShadowRunError("baseline release invoked relationship guard")
            return {
                "schema_version": "ava.relationship.guard-observation/v2",
                "active": False,
                "policy_id": None,
                "policy_sha256": None,
                "expected_prepare_calls": 0,
                "observed_prepare_calls": 0,
                "expected_apply_calls": 0,
                "observed_apply_calls": 0,
                "actions": [],
            }

        expected_case_ids = [case["id"] for case in suite.corpus["cases"]]
        expected_apply_ids = [
            case["id"] for case in suite.corpus["cases"] if _relationship_allowed(case)
        ]
        if self.prepare_case_ids != expected_case_ids:
            raise ShadowRunError("candidate guard prepare coverage is incomplete")
        if [action.case_id for action in self.actions] != expected_apply_ids:
            raise ShadowRunError("candidate guard apply coverage is incomplete")
        expected_policy = (
            RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            RELATIONSHIP_TEXT_SAFETY_POLICY_VERSION,
            suite.safety_policy_sha256,
        )
        observed_policies = [
            *self.prepared_policies,
            *[
                (action.policy_id, action.policy_version, action.policy_sha256)
                for action in self.actions
            ],
        ]
        if not observed_policies or any(
            policy != expected_policy for policy in observed_policies
        ):
            raise ShadowRunError("candidate guard policy metadata diverges")
        return {
            "schema_version": "ava.relationship.guard-observation/v2",
            "active": True,
            "policy_id": RELATIONSHIP_TEXT_SAFETY_POLICY_ID,
            "policy_sha256": suite.safety_policy_sha256,
            "expected_prepare_calls": len(expected_case_ids),
            "observed_prepare_calls": len(self.prepare_case_ids),
            "expected_apply_calls": len(expected_apply_ids),
            "observed_apply_calls": len(self.actions),
            "actions": [
                {
                    "case_id": action.case_id,
                    "action": action.action,
                    "gate_ids": list(action.gate_ids),
                }
                for action in self.actions
            ],
        }


def _relationship_guard_module() -> Any | None:
    module_name = "ava_extensions.identity.relationship_guard"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


@contextmanager
def _observe_runtime_relationship_guard(
    routes: Any,
) -> Iterator[_RelationshipGuardObserver]:
    """Wrap the real prepare/apply hooks and delegate without behavior changes."""

    observer = _RelationshipGuardObserver()
    guard_module = _relationship_guard_module()
    if guard_module is None:
        yield observer
        return

    original_prepare = getattr(guard_module, "prepare_relationship_guard", None)
    guard_class = getattr(guard_module, "RelationshipOutputGuard", None)
    original_apply = getattr(guard_class, "apply", None)
    if not callable(original_prepare) or not callable(original_apply):
        yield observer
        return

    def observed_prepare(*args: Any, **kwargs: Any) -> Any:
        guard = original_prepare(*args, **kwargs)
        observer.observe_prepare(guard)
        return guard

    def observed_apply(guard: Any, *args: Any, **kwargs: Any) -> Any:
        decision = original_apply(guard, *args, **kwargs)
        observer.observe_apply(decision)
        return decision

    route_prepare = getattr(routes, "prepare_relationship_guard", None)
    setattr(guard_module, "prepare_relationship_guard", observed_prepare)
    setattr(guard_class, "apply", observed_apply)
    if route_prepare is original_prepare:
        setattr(routes, "prepare_relationship_guard", observed_prepare)
    try:
        yield observer
    finally:
        if route_prepare is original_prepare:
            setattr(routes, "prepare_relationship_guard", route_prepare)
        setattr(guard_class, "apply", original_apply)
        setattr(guard_module, "prepare_relationship_guard", original_prepare)


class _ObservedEngine:
    """Capability-reducing wrapper around the supplied inference engine."""

    def __init__(
        self,
        delegate: Any,
        *,
        expected_adapter: str,
        expected_model: str,
        allow_configured_model: bool = False,
    ) -> None:
        if delegate is None or not callable(getattr(delegate, "generate", None)):
            raise ShadowRunError("shadow engine does not implement generate")
        if not callable(getattr(delegate, "list_models", None)):
            raise ShadowRunError("shadow engine does not implement list_models")
        self._delegate = delegate
        self.engine_id = str(getattr(delegate, "engine_id", "shadow"))
        if self.engine_id != expected_adapter:
            raise ShadowRunError("shadow adapter differs from release attestation")
        self._expected_model = expected_model
        self._allow_configured_model = allow_configured_model
        self.calls: list[_ObservedCall] = []
        self._lock = threading.Lock()

    def list_models(self) -> list[str]:
        models = self._delegate.list_models()
        if not isinstance(models, list) or not all(
            isinstance(model, str) and model for model in models
        ):
            raise ShadowRunError("shadow engine returned an invalid model catalog")
        if len(models) != len(set(models)):
            raise ShadowRunError("shadow engine returned a duplicate model catalog")
        if self._expected_model not in models and self._allow_configured_model:
            can_serve = getattr(self._delegate, "can_serve", None)
            if not callable(can_serve) or can_serve(self._expected_model) is not True:
                raise ShadowRunError(
                    "configured cloud engine cannot serve attested model"
                )
            # CloudEngine's static discovery list can lag a newly configured Claude
            # identifier.  The attested config is authoritative only after can_serve
            # proves that the real provider adapter is present; expose that exact id
            # through the isolated catalog so the HTTP check remains fail-closed.
            models = [*models, self._expected_model]
        return models

    def generate(
        self,
        messages: Sequence[Any],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if "tools" in kwargs:
            raise ShadowRunError("tools reached the relationship shadow engine")
        if temperature != 0.0:
            raise ShadowRunError("relationship shadow generation is not deterministic")
        if not 1 <= max_tokens <= _MAX_GENERATION_TOKENS:
            raise ShadowRunError("relationship shadow token bound was exceeded")

        frozen_messages = tuple(
            (
                str(
                    getattr(
                        getattr(message, "role", ""),
                        "value",
                        getattr(message, "role", ""),
                    )
                ),
                str(getattr(message, "content", "") or ""),
            )
            for message in messages
        )
        with self._lock:
            self.calls.append(
                _ObservedCall(
                    messages=frozen_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            )

        result = self._delegate.generate(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if type(result) is not dict:
            raise ShadowRunError("shadow engine returned an invalid completion")
        if result.get("model") != model:
            raise ShadowRunError("shadow engine returned a different model")
        if result.get("tool_calls"):
            raise ShadowRunError("shadow engine attempted a tool call")
        if result.get("finish_reason") != "stop":
            raise ShadowRunError("shadow engine did not return an exact stop terminal")
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ShadowRunError("shadow engine returned empty response content")
        return result


@contextmanager
def _temporary_environment(
    values: Mapping[str, str], unset: Sequence[str]
) -> Iterator[None]:
    touched = set(values) | set(unset)
    previous = {name: os.environ.get(name) for name in touched}
    try:
        for name in unset:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _quiet_process_output() -> Iterator[None]:
    """Discard backend output while restoring the host logging configuration."""

    ava_logger = logging.getLogger("ava_extensions")
    previous_handlers = list(ava_logger.handlers)
    previous_level = ava_logger.level
    previous_propagate = ava_logger.propagate
    previous_disabled = ava_logger.disabled
    previous_global_disable = logging.root.manager.disable
    with open(os.devnull, "w", encoding="utf-8") as sink:
        logging.disable(logging.CRITICAL)
        try:
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            for handler in list(ava_logger.handlers):
                if handler not in previous_handlers:
                    ava_logger.removeHandler(handler)
                    handler.close()
            ava_logger.handlers[:] = previous_handlers
            ava_logger.setLevel(previous_level)
            ava_logger.propagate = previous_propagate
            ava_logger.disabled = previous_disabled
            logging.disable(previous_global_disable)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short private artifact write")
        view = view[written:]


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _encode_service_assertion_key(entropy: bytes) -> bytes:
    """Encode random key bytes with the text-file contract used by production."""

    if len(entropy) != 48:
        raise ShadowRunError("synthetic assertion key entropy has an invalid size")
    encoded = entropy.hex().encode("ascii")
    if len(encoded) < 32 or encoded.rstrip(b"\r\n") != encoded:
        raise AssertionError("hex-encoded assertion key violated its text contract")
    return encoded


def _replace_private_json(path: Path, document: dict[str, Any]) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ShadowRunError("synthetic policy lost its private mode")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _preflight_output_directory(output_directory: Path) -> Path:
    try:
        resolved = output_directory.resolve(strict=True)
    except OSError as exc:
        raise ShadowRunError("shadow output directory is unavailable") from exc
    if resolved != output_directory.absolute() or not resolved.is_dir():
        raise ShadowRunError("shadow output must be a direct directory")
    metadata = os.lstat(resolved)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ShadowRunError("shadow output directory must be private and owned")
    return resolved


def _publish_bundle(
    output_directory: Path,
    document: dict[str, Any],
    suite: LoadedSuite,
    *,
    role: Literal["baseline", "candidate"],
) -> tuple[Path, str]:
    parent = _preflight_output_directory(output_directory)
    payload = canonical_json_bytes(document) + b"\n"
    if len(payload) > _MAX_OUTPUT_BYTES:
        raise ShadowRunError("shadow response bundle exceeds its size bound")
    bundle_sha256 = sha256_bytes(payload)
    output_path = (
        parent
        / f"relationship-shadow-{role}-{bundle_sha256.removeprefix('sha256:')}.json"
    )
    if output_path.exists() or output_path.is_symlink():
        raise ShadowArtifactConflictError(
            "content-addressed shadow output already exists"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ava-relationship-shadow-", dir=parent
    )
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        load_response_bundle(temporary, suite, expected_role=role)
        try:
            os.link(temporary, output_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ShadowArtifactConflictError("shadow output already exists") from exc
        published = True
        _fsync_directory(parent)
    except BaseException:
        if published:
            try:
                output_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    metadata = os.lstat(output_path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        try:
            output_path.unlink()
        except OSError:
            pass
        raise ShadowRunError("published shadow bundle is not a private regular file")
    return output_path, bundle_sha256


def _runtime_policy(*, enabled: bool) -> dict[str, Any]:
    return {
        "bindings": [
            {
                "issuer": _ASSERTION_ISSUER,
                "profile": EXPECTED_RELATIONSHIP_PROFILE_ID,
                "provider": "service",
                "subject": _RUNTIME_OWNER,
            },
            {
                "issuer": _OIDC_ISSUER,
                "profile": EXPECTED_RELATIONSHIP_PROFILE_ID,
                "provider": "oidc",
                "subject": _OIDC_RUNTIME_OWNER,
            },
        ],
        "enabled": enabled,
        "version": 1,
    }


def _runtime_subject(logical_subject: str) -> str:
    if logical_subject == _LOGICAL_OWNER:
        return _RUNTIME_OWNER
    if logical_subject == _LOGICAL_GUEST:
        return _RUNTIME_GUEST
    raise ShadowRunError("corpus contains an unknown synthetic principal")


def _relationship_allowed(case: dict[str, Any]) -> bool:
    principal = case["principal"]
    return bool(
        principal["verified"]
        and principal["relationship_opt_in"]
        and principal["relationship_subject"] == principal["request_subject"]
    )


def _case_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    messages = [dict(turn) for turn in case["conversation"]]
    visible_facts = case["policy"]["visible_facts"]
    if visible_facts:
        block = (
            "Contexte synthetique explicitement visible pour ce cas :\n"
            + "\n".join(f"- [{fact['id']}] {fact['text']}" for fact in visible_facts)
        )
        messages.insert(len(messages) - 1, {"role": "user", "content": block})
    return messages


def _normalise_for_match(value: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain.lower()).split())


def _visible_memory_claims(
    case: dict[str, Any], response_text: str
) -> list[dict[str, str]]:
    """Attach only allowlisted facts whose versioned accuracy anchors are present."""

    allowed = set(case["policy"]["allowed_memory_claim_ids"])
    if not allowed:
        return []
    anchors = [
        _normalise_for_match(item) for item in case["secondary"]["accuracy_all_of"]
    ]
    normalised_response = _normalise_for_match(response_text)
    if not anchors or not all(anchor in normalised_response for anchor in anchors):
        return []
    return [
        {"fact_id": fact["id"], "text": fact["text"]}
        for fact in case["policy"]["visible_facts"]
        if fact["id"] in allowed
    ]


def _assert_common_prompt(call: _ObservedCall, common_prompt: str) -> None:
    prompt = call.system_prompt
    if prompt.count(common_prompt) != 1 or not prompt.startswith(common_prompt):
        raise ShadowRunError("common Ava persona was not composed exactly once")


def _profile_from_call(
    call: _ObservedCall,
    *,
    relationship_marker: str,
    logical_subject: str,
) -> dict[str, str] | None:
    prompt = call.system_prompt
    expected_marker = f"{relationship_marker}{EXPECTED_RELATIONSHIP_PROFILE_ID}]"
    marker_count = prompt.count(relationship_marker)
    if marker_count == 0:
        return None
    if marker_count != 1 or prompt.count(expected_marker) != 1:
        raise ShadowRunError("unexpected relationship profile reached the model")
    return {"id": EXPECTED_RELATIONSHIP_PROFILE_ID, "subject": logical_subject}


def _extract_completion(response: Any, *, expected_model: str) -> tuple[str, list[Any]]:
    if response.status_code != 200:
        raise ShadowRunError("valid synthetic completion was rejected")
    try:
        document = response.json()
        response_model = document["model"]
        choices = document["choices"]
        choice = choices[0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason")
        text = message["content"]
        tool_calls = message.get("tool_calls") or []
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise ShadowRunError("shadow endpoint returned an invalid completion") from exc
    if response_model != expected_model:
        raise ShadowRunError("shadow endpoint returned a different model")
    if finish_reason != "stop":
        raise ShadowRunError("shadow endpoint did not return an exact stop terminal")
    if not isinstance(text, str) or not text.strip() or len(text) > 24_000:
        raise ShadowRunError("shadow endpoint returned invalid response text")
    if any(pattern.search(text) for pattern in _UNEXPECTED_PERSONAL_OUTPUT):
        raise ShadowRunError("shadow endpoint returned unexpected identity-like data")
    if not isinstance(tool_calls, list) or tool_calls:
        raise ShadowRunError("shadow endpoint returned a tool call")
    return text, tool_calls


def _assert_runtime_isolation(app: Any, config: Any) -> None:
    for name in (
        "agent",
        "agent_manager",
        "agent_scheduler",
        "analytics_bridge",
        "analytics_client",
        "bus",
        "channel_bridge",
        "memory_backend",
        "memory_service",
        "speech_backend",
        "trace_store",
    ):
        if getattr(app.state, name, None) is not None:
            raise ShadowRunError("an unrelated runtime subsystem was instantiated")
    if any(
        (
            config.agent.context_from_memory,
            config.agent_manager.enabled,
            config.analytics.enabled,
            config.channel.enabled,
            config.learning.enabled,
            config.learning.auto_update,
            config.learning.training_enabled,
            config.learning.spec_search.enabled,
            config.memory.enabled,
            config.sessions.enabled,
            config.skills.enabled,
            config.telemetry.enabled,
            config.tools.mcp.enabled,
            config.traces.enabled,
        )
    ):
        raise ShadowRunError("isolated runtime configuration was broadened")


def _build_config() -> Any:
    from openjarvis.core.config import JarvisConfig

    config = JarvisConfig()
    config.agent.context_from_memory = False
    config.agent.tools = ""
    config.agent_manager.enabled = False
    config.analytics.enabled = False
    config.channel.enabled = False
    config.learning.enabled = False
    config.learning.auto_update = False
    config.learning.training_enabled = False
    config.learning.spec_search.enabled = False
    config.learning.skills.auto_optimize = False
    config.memory.enabled = False
    config.scheduler.enabled = False
    config.security.enabled = False
    config.sessions.enabled = False
    config.skills.enabled = False
    config.telemetry.enabled = False
    config.tools.enabled = ""
    config.tools.mcp.enabled = False
    config.traces.enabled = False
    config.workflow.enabled = False
    from ava_extensions.boot import normalize_config

    return normalize_config(config)


def _build_app(
    engine: _ObservedEngine,
    model: str,
    *,
    configured_cloud_catalog: bool,
) -> tuple[Any, Any, Any]:
    import httpx
    from fastapi import FastAPI

    from openjarvis.server import routes
    from openjarvis.server.models import ModelListResponse, ModelObject

    config = _build_config()
    app = FastAPI(title="Ava relationship shadow", docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.state.model = model
    app.state.config = config
    for name in (
        "agent",
        "agent_manager",
        "agent_scheduler",
        "analytics_bridge",
        "analytics_client",
        "bus",
        "channel_bridge",
        "memory_backend",
        "memory_service",
        "speech_backend",
        "trace_store",
    ):
        setattr(app.state, name, None)
    if configured_cloud_catalog:

        @app.get("/v1/models", response_model=ModelListResponse)
        async def list_attested_cloud_model() -> ModelListResponse:
            """Expose the configured provider catalog only inside this shadow app.

            The production route deliberately hides cloud model identifiers from the
            local-model UI.  This earlier, eval-only route still exercises the same
            HTTP contract while sourcing ids from the capability-reduced real engine.
            """

            model_ids = await asyncio.to_thread(engine.list_models)
            return ModelListResponse(data=[ModelObject(id=item) for item in model_ids])

    app.include_router(routes.router)
    _assert_runtime_isolation(app, config)
    return app, httpx, routes


def _sign_assertion(
    sign_service_assertion: Callable[..., str],
    key: bytes,
    subject: str,
    *,
    audience: str = _ASSERTION_AUDIENCE,
    issued_at: int | None = None,
    not_before: int | None = None,
    expires_at: int | None = None,
    signing_key: bytes | None = None,
    key_id: str = _ASSERTION_KEY_ID,
) -> str:
    now = int(time.time())
    issued = now - 1 if issued_at is None else issued_at
    active = issued if not_before is None else not_before
    expires = now + 59 if expires_at is None else expires_at
    return sign_service_assertion(
        key=key if signing_key is None else signing_key,
        issuer=_ASSERTION_ISSUER,
        audience=audience,
        subject=subject,
        issued_at=issued,
        not_before=active,
        expires_at=expires,
        nonce=secrets.token_hex(16),
        key_id=key_id,
    )


async def _post_completion(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    headers: Mapping[str, str] | None = None,
) -> Any:
    return await client.post(
        "/v1/chat/completions",
        headers=dict(headers or {}),
        json={
            "max_tokens": _REQUESTED_GENERATION_TOKENS,
            "messages": messages,
            "model": model,
            "stream": False,
            "temperature": 0.0,
        },
    )


async def _assert_model_catalog(client: Any, expected_model: str) -> None:
    response = await client.get("/v1/models")
    if response.status_code != 200:
        raise ShadowRunError("shadow model catalog was unavailable")
    try:
        document = response.json()
        models = [item["id"] for item in document["data"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ShadowRunError("shadow model catalog was invalid") from exc
    if (
        document.get("object") != "list"
        or not models
        or len(models) != len(set(models))
        or expected_model not in models
    ):
        raise ShadowRunError("attested model absent from /v1/models")


@dataclass(frozen=True, slots=True)
class _OIDCFixture:
    token: str
    jwks: dict[str, Any]


def _build_oidc_fixture() -> _OIDCFixture:
    """Create an ephemeral RS256/JWKS pair without files, sockets or real identity."""

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk.update({"alg": "RS256", "kid": _OIDC_KEY_ID, "use": "sig"})
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": _OIDC_ISSUER,
            "aud": _OIDC_AUDIENCE,
            "sub": _OIDC_RUNTIME_OWNER,
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 120,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": _OIDC_KEY_ID},
    )
    return _OIDCFixture(token=token, jwks={"keys": [public_jwk]})


@contextmanager
def _seed_local_jwks_cache(
    principal_module: Any, fixture: _OIDCFixture
) -> Iterator[None]:
    """Seed only the configured URL; a network fallback would fail the run."""

    cache = principal_module._jwks_cache
    with cache._lock:
        previous = cache._entries.get(_OIDC_JWKS_URL)
        cache._entries[_OIDC_JWKS_URL] = (float("inf"), fixture.jwks)
    try:
        yield
    finally:
        with cache._lock:
            if previous is None:
                cache._entries.pop(_OIDC_JWKS_URL, None)
            else:
                cache._entries[_OIDC_JWKS_URL] = previous


class _MemoryEffectSpy:
    """Backend-shaped tripwire used only around the disabled-policy request."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def invoked(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(name)
            raise ShadowRunError(f"disabled relationship touched {self.label}")

        return invoked


async def _run_http_suite(
    *,
    app: Any,
    httpx: Any,
    observed_engine: _ObservedEngine,
    suite: LoadedSuite,
    key: bytes,
    policy_path: Path,
    model: str,
    oidc_fixture: _OIDCFixture,
    role: Literal["baseline", "candidate"],
    routes: Any,
) -> tuple[
    list[dict[str, Any]],
    tuple[str, ...],
    tuple[str, ...],
    str,
    dict[str, Any],
]:
    from ava_extensions.identity.relationship import RELATIONSHIP_MARKER
    from ava_extensions.patches.system_prompt_loader import load_common_persona
    from ava_extensions.server import principal as principal_module
    from ava_extensions.server.principal import (
        OIDC_HEADER,
        SERVICE_ASSERTION_HEADER,
        sign_service_assertion,
    )

    common_prompt = load_common_persona().strip()
    if not common_prompt:
        raise ShadowRunError("common Ava persona is empty")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://relationship-shadow.invalid",
        timeout=30.0,
    ) as client:
        await _assert_model_catalog(client, model)
        now = int(time.time())
        valid_owner = _sign_assertion(sign_service_assertion, key, _RUNTIME_OWNER)
        negative_headers = (
            (
                "empty-service-header",
                {SERVICE_ASSERTION_HEADER: ""},
            ),
            (
                "malformed-oidc",
                {OIDC_HEADER: "synthetic.invalid.jwt"},
            ),
            (
                "forged-signature",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _RUNTIME_OWNER,
                        signing_key=secrets.token_bytes(48),
                    )
                },
            ),
            (
                "expired",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _RUNTIME_OWNER,
                        issued_at=now - 100,
                        not_before=now - 100,
                        expires_at=now - 20,
                    )
                },
            ),
            (
                "future-not-before",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _RUNTIME_OWNER,
                        issued_at=now + 30,
                        not_before=now + 30,
                        expires_at=now + 60,
                    )
                },
            ),
            (
                "wrong-audience",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _RUNTIME_OWNER,
                        audience="ava-relationship-shadow-other",
                    )
                },
            ),
            (
                "unknown-key-id",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _RUNTIME_OWNER,
                        key_id="relationship-shadow-unknown",
                    )
                },
            ),
            (
                "invalid-matrix-subject",
                {
                    SERVICE_ASSERTION_HEADER: _sign_assertion(
                        sign_service_assertion,
                        key,
                        _LOGICAL_OWNER,
                    )
                },
            ),
            (
                "ambiguous-headers",
                {
                    OIDC_HEADER: "synthetic.invalid.jwt",
                    SERVICE_ASSERTION_HEADER: valid_owner,
                },
            ),
        )
        for check_name, headers in negative_headers:
            before = len(observed_engine.calls)
            response = await _post_completion(
                client,
                model=model,
                messages=[
                    {"role": "user", "content": "Sonde synthetique d'authentification."}
                ],
                headers=headers,
            )
            if response.status_code != 401 or len(observed_engine.calls) != before:
                raise ShadowRunError("authentication negative reached the model")

        def service_headers(subject: str) -> dict[str, str]:
            return {
                SERVICE_ASSERTION_HEADER: _sign_assertion(
                    sign_service_assertion,
                    key,
                    subject,
                )
            }

        with _seed_local_jwks_cache(principal_module, oidc_fixture):
            before = len(observed_engine.calls)
            oidc_response = await _post_completion(
                client,
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": "Sonde synthetique OIDC locale sans reseau.",
                    }
                ],
                headers={OIDC_HEADER: oidc_fixture.token},
            )
            _extract_completion(oidc_response, expected_model=model)
            if len(observed_engine.calls) != before + 1:
                raise ShadowRunError(
                    "OIDC positive did not make exactly one model call"
                )
            oidc_call = observed_engine.calls[-1]
            _assert_common_prompt(oidc_call, common_prompt)
            if (
                _profile_from_call(
                    oidc_call,
                    relationship_marker=RELATIONSHIP_MARKER,
                    logical_subject=_LOGICAL_OWNER,
                )
                is None
            ):
                raise ShadowRunError("verified local OIDC principal missed its profile")

        rollback_messages = [
            {"role": "user", "content": "Sonde synthetique de rollback relationnel."}
        ]
        rollback_calls: list[_ObservedCall] = []
        for enabled in (True, False, True):
            _replace_private_json(policy_path, _runtime_policy(enabled=enabled))
            memory_read_spy = _MemoryEffectSpy("legacy memory read backend")
            memory_write_spy = _MemoryEffectSpy("legacy memory write backend")
            if not enabled:
                app.state.memory_backend = memory_read_spy
                app.state.memory_service = memory_write_spy
                app.state.config.agent.context_from_memory = True
            before = len(observed_engine.calls)
            try:
                response = await _post_completion(
                    client,
                    model=model,
                    messages=rollback_messages,
                    headers=service_headers(_RUNTIME_OWNER),
                )
                _extract_completion(response, expected_model=model)
            finally:
                if not enabled:
                    app.state.memory_backend = None
                    app.state.memory_service = None
                    app.state.config.agent.context_from_memory = False
            if len(observed_engine.calls) != before + 1:
                raise ShadowRunError(
                    "rollback probe did not make exactly one model call"
                )
            if not enabled and (memory_read_spy.calls or memory_write_spy.calls):
                raise ShadowRunError(
                    "disabled relationship policy touched legacy memory"
                )
            rollback_calls.append(observed_engine.calls[-1])

        for call in rollback_calls:
            _assert_common_prompt(call, common_prompt)
        first_profile = _profile_from_call(
            rollback_calls[0],
            relationship_marker=RELATIONSHIP_MARKER,
            logical_subject=_LOGICAL_OWNER,
        )
        disabled_profile = _profile_from_call(
            rollback_calls[1],
            relationship_marker=RELATIONSHIP_MARKER,
            logical_subject=_LOGICAL_OWNER,
        )
        restored_profile = _profile_from_call(
            rollback_calls[2],
            relationship_marker=RELATIONSHIP_MARKER,
            logical_subject=_LOGICAL_OWNER,
        )
        if (
            first_profile is None
            or disabled_profile is not None
            or restored_profile != first_profile
            or rollback_calls[1].system_prompt != common_prompt
            or rollback_calls[2].system_prompt != rollback_calls[0].system_prompt
        ):
            raise ShadowRunError("relationship policy rollback was not reversible")

        responses: list[dict[str, Any]] = []
        with _observe_runtime_relationship_guard(routes) as guard_observer:
            for case in suite.corpus["cases"]:
                guard_observer.begin_case(case["id"])
                principal = case["principal"]
                headers: dict[str, str] = {}
                if principal["verified"]:
                    headers = service_headers(
                        _runtime_subject(principal["request_subject"])
                    )
                before = len(observed_engine.calls)
                response = await _post_completion(
                    client,
                    model=model,
                    messages=_case_messages(case),
                    headers=headers,
                )
                text, tool_calls = _extract_completion(response, expected_model=model)
                guard_observer.finish_case(text)
                if len(observed_engine.calls) != before + 1:
                    raise ShadowRunError(
                        "corpus case did not make exactly one model call"
                    )
                call = observed_engine.calls[-1]
                _assert_common_prompt(call, common_prompt)
                profile = _profile_from_call(
                    call,
                    relationship_marker=RELATIONSHIP_MARKER,
                    logical_subject=principal["request_subject"],
                )
                if _relationship_allowed(case) != (profile is not None):
                    raise ShadowRunError(
                        "relationship profile crossed a principal boundary"
                    )
                if profile is None and call.system_prompt != common_prompt:
                    raise ShadowRunError(
                        "non-owner prompt diverged from the common persona"
                    )
                if (
                    profile is not None
                    and call.system_prompt != rollback_calls[0].system_prompt
                ):
                    raise ShadowRunError(
                        "owner relationship prompt drifted during the corpus"
                    )
                responses.append(
                    {
                        "applied_profile": profile,
                        "case_id": case["id"],
                        "memory_claims": _visible_memory_claims(case, text),
                        "text": text,
                        "tool_calls": tool_calls,
                    }
                )
        guard_observation = guard_observer.document(suite=suite, role=role)

    prompt_digest = sha256_bytes(
        canonical_json_bytes(
            {
                "common": rollback_calls[1].system_prompt,
                "relationship": rollback_calls[0].system_prompt,
            }
        )
    )
    positive_checks = (
        "model-catalog-attested-model",
        "oidc-rs256-local-jwks",
        "rollback-disabled-zero-memory-read-write",
    )
    return (
        responses,
        tuple(name for name, _headers in negative_headers),
        positive_checks,
        prompt_digest,
        guard_observation,
    )


def _assert_temp_runtime_clean(runtime_root: Path, allowed_files: set[Path]) -> None:
    discovered: set[Path] = set()
    for path in runtime_root.rglob("*"):
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ShadowRunError("isolated runtime created a special artifact")
        discovered.add(path)
    if discovered != allowed_files:
        raise ShadowRunError(
            "isolated runtime produced an unrelated persistent artifact"
        )
    for path in allowed_files:
        metadata = os.lstat(path)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ShadowRunError("synthetic runtime material lost its private mode")


def _close_engine(engine: Any) -> None:
    close = getattr(engine, "close", None)
    if callable(close):
        close()


def _execute_isolated(
    *,
    suite: LoadedSuite,
    engine_factory: Callable[[], Any],
    release_attestation: LoadedReleaseAttestation,
    role: Literal["baseline", "candidate"],
    execution_mode: Literal["attested", "loopback", "configured-anthropic"],
) -> tuple[dict[str, Any], int, tuple[str, ...], tuple[str, ...]]:
    if role == "candidate" and _relationship_guard_module() is None:
        raise ShadowRunError(
            "candidate shadow requires an observed runtime relationship guard"
        )
    release_document = release_attestation.document
    engine_attestation = release_document["engine"]
    release = release_document["release"]
    artifact = release_document["artifact"]
    provider = engine_attestation["provider"]
    model = engine_attestation["model"]
    revision = engine_attestation["revision"]
    adapter = engine_attestation["adapter"]
    configured_cloud = execution_mode == "configured-anthropic"
    ambient_anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    with tempfile.TemporaryDirectory(
        prefix="ava-relationship-shadow-"
    ) as temporary_name:
        runtime_root = Path(temporary_name)
        runtime_root.chmod(0o700)
        synthetic_home = runtime_root / "home"
        openjarvis_home = runtime_root / "openjarvis"
        synthetic_home.mkdir(mode=0o700)
        openjarvis_home.mkdir(mode=0o700)
        key_path = runtime_root / "service-assertion.key"
        policy_path = runtime_root / "relationship-policy.json"
        key = _encode_service_assertion_key(secrets.token_bytes(48))
        oidc_fixture = _build_oidc_fixture()
        _write_private_new(key_path, key)
        _replace_private_json(policy_path, _runtime_policy(enabled=True))

        environment = {
            "AVA_CP_ASSERTION_AUDIENCE": _ASSERTION_AUDIENCE,
            "AVA_CP_ASSERTION_ISSUER": _ASSERTION_ISSUER,
            "AVA_CP_ASSERTION_KEY_ID": _ASSERTION_KEY_ID,
            "AVA_CP_ASSERTION_KEY_FILE": str(key_path),
            "AVA_PERCEPTION": "0",
            "AVA_PERCEPTION_DB": str(openjarvis_home / "perception.db"),
            "AVA_RELATIONSHIP_POLICY_FILE": str(policy_path),
            "AVA_ROUTING_LOG": str(openjarvis_home / "routing-probe.jsonl"),
            "AVA_OIDC_AUDIENCE": _OIDC_AUDIENCE,
            "AVA_OIDC_ISSUER": _OIDC_ISSUER,
            "AVA_OIDC_JWKS_URL": _OIDC_JWKS_URL,
            "DO_NOT_TRACK": "1",
            "HOME": str(synthetic_home),
            "NO_PROXY": "127.0.0.1,::1",
            "OPENJARVIS_CONFIG": str(openjarvis_home / "config.toml"),
            "OPENJARVIS_HOME": str(openjarvis_home),
            "OPENJARVIS_NO_ANALYTICS": "1",
            "XDG_DATA_HOME": str(runtime_root / "xdg-data"),
            "no_proxy": "127.0.0.1,::1",
        }
        unset = [
            "ALL_PROXY",
            "AVA_CP_ASSERTION_PREVIOUS_KEY_FILE",
            "AVA_CP_ASSERTION_PREVIOUS_KEY_ID",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "OPENAI_COMPAT_API_KEY",
            "all_proxy",
            "https_proxy",
            "http_proxy",
        ]
        # A loopback or test adapter receives no ambient provider credential.  The
        # configured Anthropic path keeps only its own SDK credential and removes
        # every unrelated cloud capability before the engine is constructed.
        unrelated_provider_keys = (
            "DEEPSEEK_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "MINIMAX_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_CODEX_API_KEY",
            "OPENAI_CODEX_BASE_URL",
            "OPENROUTER_API_KEY",
        )
        unset.extend(unrelated_provider_keys)
        ambient_anthropic_names = sorted(
            name for name in os.environ if name.startswith("ANTHROPIC_")
        )
        unset.extend(
            name
            for name in ambient_anthropic_names
            if not (configured_cloud and name == "ANTHROPIC_API_KEY")
        )
        with _temporary_environment(environment, unset):
            engine: Any | None = None
            active_error: BaseException | None = None
            try:
                engine = engine_factory()
                observed = _ObservedEngine(
                    engine,
                    expected_adapter=adapter,
                    expected_model=model,
                    allow_configured_model=configured_cloud,
                )
                app, httpx, routes = _build_app(
                    observed,
                    model,
                    configured_cloud_catalog=configured_cloud,
                )
                from ava_extensions.perception import collecteur

                perception_thread = getattr(collecteur, "_fil", None)
                if perception_thread is not None and perception_thread.is_alive():
                    raise ShadowRunError(
                        "relationship shadow requires perception to be stopped"
                    )
                from ava_extensions.server import principal as principal_module

                with _seed_local_jwks_cache(principal_module, oidc_fixture):
                    (
                        responses,
                        negative_checks,
                        positive_checks,
                        prompt_digest,
                        guard_observation,
                    ) = asyncio.run(
                        _run_http_suite(
                            app=app,
                            httpx=httpx,
                            observed_engine=observed,
                            suite=suite,
                            key=key,
                            policy_path=policy_path,
                            model=model,
                            oidc_fixture=oidc_fixture,
                            role=role,
                            routes=routes,
                        )
                    )
            except BaseException as exc:
                active_error = exc
                raise
            finally:
                if engine is not None:
                    try:
                        _close_engine(engine)
                    except Exception as exc:
                        if active_error is None:
                            raise ShadowRunError(
                                "shadow engine did not close cleanly"
                            ) from exc

        _assert_temp_runtime_clean(runtime_root, {key_path, policy_path})
        policy_digest = sha256_bytes(
            canonical_json_bytes(_runtime_policy(enabled=True))
        )
        bundle = {
            "artifact": {
                "canonical_knowledge": False,
                "contains_personal_data": False,
                "contains_production_conversations": False,
                "engine": {"model": model, "provider": provider, "revision": revision},
                "generated_by": GENERATED_BY,
                "guard_observation": guard_observation,
                "id": f"relationship-shadow-{role}-{release['git_sha'][:12]}",
                "policy_sha256": policy_digest,
                "prompt_sha256": prompt_digest,
                "release_attestation_sha256": release_attestation.sha256,
                "release": {
                    "repository": release["repository"],
                    "git_sha": release["git_sha"],
                    "adapter": adapter,
                    "config_sha256": engine_attestation["config_sha256"],
                    "manifest_sha256": artifact["manifest_sha256"],
                },
                "role": role,
                "safety_policy_sha256": suite.safety_policy_sha256,
                "source_kind": "offline_shadow",
            },
            "corpus": {
                "id": suite.corpus["corpus_id"],
                "version": suite.corpus["version"],
            },
            "responses": responses,
            "schema_version": RESPONSES_SCHEMA_VERSION,
        }
        serialized = canonical_json_bytes(bundle)
        forbidden = (
            key,
            key.hex().encode("ascii"),
            base64.urlsafe_b64encode(key).rstrip(b"="),
            _RUNTIME_OWNER.encode("utf-8"),
            _RUNTIME_GUEST.encode("utf-8"),
            ambient_anthropic_api_key.encode("utf-8"),
        )
        if any(value and value in serialized for value in forbidden):
            raise ShadowRunError(
                "runtime identity material entered the response bundle"
            )
        return bundle, len(observed.calls), negative_checks, positive_checks


def run_shadow(
    *,
    engine_factory: Callable[[], Any],
    output_directory: str | Path,
    release_attestation_path: str | Path,
    release_attestation_sha256: str,
    role: Literal["baseline", "candidate"] = "candidate",
    execution_mode: Literal[
        "attested", "loopback", "configured-anthropic"
    ] = "attested",
) -> ShadowRunResult:
    """Run the synthetic HTTP shadow in a dedicated, effect-free process.

    The function temporarily owns process-wide environment and stdout/stderr.
    Callers must therefore invoke it from a fresh standalone process, not from
    Ava's daemon or another multi-threaded host.
    """

    if role not in {"baseline", "candidate"}:
        raise ShadowRunError("invalid response bundle role")
    if execution_mode not in _EXECUTION_MODES:
        raise ShadowRunError("invalid relationship shadow execution mode")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ShadowRunError("relationship shadow cannot run inside an event loop")
    if not _RUN_LOCK.acquire(blocking=False):
        raise ShadowRunError("another relationship shadow run is active")

    destination = Path(output_directory).expanduser().absolute()
    try:
        _preflight_output_directory(destination)
        release_attestation = load_release_attestation(
            release_attestation_path,
            expected_sha256=release_attestation_sha256,
        )
        engine_attestation = release_attestation.document["engine"]
        if execution_mode == "loopback" and engine_attestation["adapter"] != (
            "openai-compat"
        ):
            raise ShadowRunError("loopback execution requires openai-compat adapter")
        if execution_mode == "configured-anthropic" and (
            engine_attestation["adapter"] != "cloud"
            or engine_attestation["provider"] != "anthropic"
            or not engine_attestation["model"].startswith("claude-")
        ):
            raise ShadowRunError(
                "configured Anthropic execution differs from release attestation"
            )
        suite = load_suite(DEFAULT_MANIFEST)
        with _quiet_process_output():
            try:
                bundle, model_calls, negative_checks, positive_checks = (
                    _execute_isolated(
                        suite=suite,
                        engine_factory=engine_factory,
                        release_attestation=release_attestation,
                        role=role,
                        execution_mode=execution_mode,
                    )
                )
            except ShadowRunError:
                raise
            except Exception as exc:
                raise ShadowRunError("relationship shadow execution failed") from exc
        output_path, bundle_sha256 = _publish_bundle(
            destination,
            bundle,
            suite,
            role=role,
        )
        return ShadowRunResult(
            output_path=output_path,
            bundle_sha256=bundle_sha256,
            case_count=len(bundle["responses"]),
            model_call_count=model_calls,
            negative_checks=negative_checks,
            positive_checks=positive_checks,
            release_attestation_sha256=release_attestation.sha256,
        )
    finally:
        _RUN_LOCK.release()


def _loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("an IP loopback backend is required") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError(
            "backend URL must be an explicit HTTP loopback endpoint"
        )
    return value.rstrip("/")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one isolated relationship shadow bundle"
    )
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--backend-url", type=_loopback_url)
    backend.add_argument(
        "--configured-anthropic",
        action="store_true",
        help=(
            "use the configured Ava CloudEngine and its ambient Anthropic credential"
        ),
    )
    parser.add_argument("--release-attestation", required=True)
    parser.add_argument("--release-attestation-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--role", choices=("baseline", "candidate"), default="candidate"
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1.0 <= args.timeout_seconds <= 600.0:
        print("relationship shadow run failed", file=sys.stderr)
        return 2

    def engine_factory() -> Any:
        if args.configured_anthropic:
            # Import boot before the engine so Ava's mandatory SDK guards are active
            # for the real Anthropic request.  AVA_PERCEPTION=0 and the isolated HOME
            # are already in force at this point.
            import ava_extensions.boot  # noqa: F401
            from openjarvis.engine.cloud import CloudEngine

            return CloudEngine()
        from openjarvis.engine.openai_compat_engines import OpenAICompatEngine

        return OpenAICompatEngine(host=args.backend_url, timeout=args.timeout_seconds)

    try:
        run_shadow(
            engine_factory=engine_factory,
            output_directory=args.output_dir,
            release_attestation_path=args.release_attestation,
            release_attestation_sha256=args.release_attestation_sha256,
            role=args.role,
            execution_mode=(
                "configured-anthropic" if args.configured_anthropic else "loopback"
            ),
        )
    except (OSError, ShadowRunError, ValueError):
        print("relationship shadow run failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MANIFEST",
    "ShadowArtifactConflictError",
    "ShadowRunError",
    "ShadowRunResult",
    "main",
    "run_shadow",
]
