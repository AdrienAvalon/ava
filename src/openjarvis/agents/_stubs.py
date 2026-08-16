"""ABC for agent implementations.

Adapted from IPW's ``BaseAgent`` at ``src/agents/base.py``.
Provides ``BaseAgent`` with concrete helper methods for event emission,
message building, and generation, plus ``ToolUsingAgent`` intermediate
base for agents that accept tools.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openjarvis.core.config import load_config
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import Conversation, Message, Role, ToolResult
from openjarvis.engine._stubs import InferenceEngine

_FINISH_REASON_ALIASES = {
    # Anthropic non-streaming stop reasons.
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "function_call": "tool_calls",
    "max_tokens": "length",
    "pause_turn": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}
_STANDARD_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def normalize_finish_reason(value: Any) -> Optional[str]:
    """Return one provider-independent terminal reason without inventing one."""

    if not isinstance(value, str) or not value.strip():
        return None
    reason = value.strip().lower()
    return _FINISH_REASON_ALIASES.get(reason, reason)


def is_complete_tool_call_finish_reason(value: Any) -> bool:
    """Return whether a provider proved its structured tool payload complete."""

    # Google and Ollama non-streaming adapters may return a structured tool
    # call with their normal STOP terminal rather than OpenAI's TOOL_CALLS.
    return normalize_finish_reason(value) in {"tool_calls", "stop"}


def tool_call_arguments_are_complete(value: Any) -> bool:
    """Accept only a complete JSON object as function-call arguments."""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return isinstance(json.loads(value), dict)
    except (json.JSONDecodeError, TypeError):
        return False


def _standard_usage(usage: Any) -> Dict[str, int]:
    """Extract non-negative standard counters and derive a missing total."""

    raw = usage if isinstance(usage, dict) else {}

    def _counter(key: str) -> int:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return 0

    prompt_tokens = _counter("prompt_tokens")
    completion_tokens = _counter("completion_tokens")
    if "total_tokens" in raw:
        total_tokens = _counter("total_tokens")
    else:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def configure_tool_execution_security(
    agent: Any,
    *,
    capability_policy: Optional[Any],
    boundary_guard: Optional[Any],
    principal_provenance: Optional[str] = None,
) -> None:
    """Post-wire a tool agent without trusting its class/runtime identifier."""

    executor = getattr(agent, "_executor", None)
    configure = getattr(executor, "configure_execution_security", None)
    if callable(configure):
        configure(
            capability_policy=capability_policy,
            boundary_guard=boundary_guard,
            principal_provenance=principal_provenance,
        )


@dataclass(slots=True)
class AgentContext:
    """Runtime context handed to an agent on each invocation."""

    conversation: Conversation = field(default_factory=Conversation)
    tools: List[str] = field(default_factory=list)
    memory_results: List[Any] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    """Result returned after an agent completes a run."""

    content: str
    tool_results: List[ToolResult] = field(default_factory=list)
    turns: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    """Base class for all agent implementations.

    Subclasses must be registered via
    ``@AgentRegistry.register("name")`` to become discoverable.

    Provides concrete helper methods that eliminate boilerplate in
    subclasses:

    - :meth:`_emit_turn_start` / :meth:`_emit_turn_end` -- event bus
    - :meth:`_build_messages` -- conversation + system prompt assembly
    - :meth:`_generate` -- delegates to engine with stored defaults
    - :meth:`_max_turns_result` -- standard max-turns-exceeded result
    - :meth:`_strip_think_tags` -- remove ``<think>`` blocks
    """

    agent_id: str
    accepts_tools: bool = False

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        bus: Optional[EventBus] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        prompt_builder: Optional[Any] = None,
    ) -> None:
        self._engine = engine
        self._model = model
        self._bus = bus
        self._prompt_builder = prompt_builder

        # Three-tier resolution: explicit arg > config > class default > hardcoded
        if temperature is not None and max_tokens is not None:
            self._temperature = temperature
            self._max_tokens = max_tokens
        else:
            try:
                cfg = load_config()
                self._temperature = (
                    temperature
                    if temperature is not None
                    else cfg.intelligence.temperature
                )
                self._max_tokens = (
                    max_tokens
                    if max_tokens is not None
                    else cfg.intelligence.max_tokens
                )
            except Exception:
                self._temperature = (
                    temperature
                    if temperature is not None
                    else getattr(self, "_default_temperature", 0.7)
                )
                self._max_tokens = (
                    max_tokens
                    if max_tokens is not None
                    else getattr(self, "_default_max_tokens", 1024)
                )

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def _emit_turn_start(self, input: str) -> None:
        """Publish ``AGENT_TURN_START`` if an event bus is available."""
        if self._bus:
            self._bus.publish(
                EventType.AGENT_TURN_START,
                {"agent": self.agent_id, "input": input},
            )

    def _emit_turn_end(self, **data: Any) -> None:
        """Publish ``AGENT_TURN_END`` if an event bus is available."""
        if self._bus:
            payload: Dict[str, Any] = {"agent": self.agent_id}
            payload.update(data)
            self._bus.publish(EventType.AGENT_TURN_END, payload)

    def _apply_persona(self, system_prompt: Optional[str]) -> Optional[str]:
        """Append SOUL/MEMORY/USER persona to a self-assembled system prompt.

        Agents like ``monitor_operative`` / ``operative`` build their own
        system prompt and bypass ``_build_messages`` (and thus the prompt
        builder). This lets them honor the same persona files as one-shot
        ``jarvis ask`` (#376) by *appending* persona to — never replacing —
        their specialized instructions. No-op when no ``prompt_builder`` is
        wired or no persona files exist.
        """
        if self._prompt_builder is None:
            return system_prompt
        persona = self._prompt_builder.persona_sections()
        if not persona:
            return system_prompt
        return f"{system_prompt}\n\n{persona}" if system_prompt else persona

    def _apply_server_identity(
        self,
        system_prompt: Optional[str],
        context: Optional[AgentContext],
    ) -> Optional[str]:
        """Prefer the authenticated server identity over all legacy persona files.

        Operative agents assemble messages without ``_build_messages``.  This
        helper gives every agent class the same fail-closed composition rule:
        server identity first, optional class-specific protocol second, and no
        SOUL/MEMORY/USER material on an HTTP request.
        """

        candidate = None
        if context is not None:
            value = context.metadata.get("server_identity_prompt")
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
        if candidate is None:
            return self._apply_persona(system_prompt)
        if system_prompt:
            return (
                f"{candidate}\n\n## Protocole spécialisé de cet agent\n{system_prompt}"
            )
        return candidate

    def _build_messages(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        *,
        system_prompt: Optional[str] = None,
    ) -> list[Message]:
        """Assemble the message list for a generate call.

        Optionally prepends a system prompt, then appends any context
        conversation messages, and finally the user input.
        """
        messages: list[Message] = []
        # Check if the context already supplies a system message
        _context_has_system = (
            context
            and context.conversation.messages
            and any(m.role == Role.SYSTEM for m in context.conversation.messages)
        )

        server_identity_prompt = None
        if context is not None:
            candidate = context.metadata.get("server_identity_prompt")
            if isinstance(candidate, str) and candidate.strip():
                server_identity_prompt = candidate.strip()

        if server_identity_prompt is not None:
            # Only in-process server code sets this value after authenticating
            # the request principal.  It is already the composition of the
            # common persona and any authorized relationship overlay.
            effective_system_prompt = self._apply_server_identity(
                system_prompt,
                context,
            )
        elif self._prompt_builder is not None:
            effective_system_prompt = self._prompt_builder.build()
        elif system_prompt:
            effective_system_prompt = system_prompt
        elif _context_has_system:
            effective_system_prompt = None
        else:
            # Fall back to the config-level default (grounds local models)
            try:
                cfg = load_config()
                effective_system_prompt = cfg.agent.default_system_prompt or None
            except Exception:
                effective_system_prompt = None
        if effective_system_prompt:
            messages.append(Message(role=Role.SYSTEM, content=effective_system_prompt))
        if context and context.conversation.messages:
            messages.extend(context.conversation.messages)
        messages.append(Message(role=Role.USER, content=input))
        return messages

    def _generate(self, messages: list[Message], **extra_kwargs: Any) -> dict:
        """Call ``engine.generate()`` with stored defaults.

        Extra kwargs (e.g. ``tools``) are forwarded to the engine.
        Publishes INFERENCE_START/END events on the bus when the engine
        does not publish its own (i.e. non-instrumented engines).
        """
        if self._bus and not getattr(self._engine, "_publishes_events", False):
            engine_id = getattr(self._engine, "engine_id", "")
            self._bus.publish(
                EventType.INFERENCE_START,
                {"model": self._model, "engine": engine_id},
            )

        result = self._engine.generate(
            messages,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **extra_kwargs,
        )

        if self._bus and not getattr(self._engine, "_publishes_events", False):
            usage = result.get("usage", {})
            self._bus.publish(
                EventType.INFERENCE_END,
                {
                    "model": self._model,
                    "usage": usage,
                    "content": result.get("content", ""),
                    "tool_calls": result.get("tool_calls", []),
                    "finish_reason": result.get("finish_reason", ""),
                },
            )

        return result

    def _max_turns_result(
        self,
        tool_results: list[ToolResult],
        turns: int,
        content: str = "",
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """Build the standard result for when ``max_turns`` is exceeded."""
        self._emit_turn_end(turns=turns, max_turns_exceeded=True)
        md: Dict[str, Any] = {"max_turns_exceeded": True}
        if metadata:
            md.update(metadata)
        return AgentResult(
            content=content or "Maximum turns reached without a final answer.",
            tool_results=tool_results,
            turns=turns,
            metadata=md,
        )

    def _check_continuation(
        self,
        result: dict,
        messages: list,
        *,
        max_continuations: int = 2,
    ) -> str:
        """Re-prompt truncated generations and retain their true terminal state.

        Anthropic's non-streaming API calls token exhaustion ``max_tokens`` and
        normal completion ``end_turn``.  Normalize those provider values,
        concatenate at most *max_continuations* follow-ups, and update ``result``
        in place with the final reason and cumulative standard token usage.  The
        mutation lets existing callers keep the historical string return value
        while still propagating completeness evidence to persistence boundaries.
        """

        segment = result.get("content", "") or ""
        content = segment
        finish_reason = normalize_finish_reason(result.get("finish_reason"))
        cumulative_usage = _standard_usage(result.get("usage"))

        for _ in range(max_continuations):
            if finish_reason != "length":
                break
            # Append only the latest segment. Appending the whole accumulated
            # response on every pass duplicates earlier text in model context.
            from openjarvis.core.types import Message, Role

            messages.append(Message(role=Role.ASSISTANT, content=segment))
            messages.append(
                Message(
                    role=Role.USER,
                    content="Continue from where you left off.",
                ),
            )
            cont = self._generate(messages)
            segment = cont.get("content", "") or ""
            content += segment
            continuation_usage = _standard_usage(cont.get("usage"))
            for key in _STANDARD_USAGE_KEYS:
                cumulative_usage[key] += continuation_usage[key]
            finish_reason = normalize_finish_reason(cont.get("finish_reason"))

        result["content"] = content
        result["finish_reason"] = finish_reason
        original_usage = result.get("usage")
        merged_usage = dict(original_usage) if isinstance(original_usage, dict) else {}
        merged_usage.update(cumulative_usage)
        result["usage"] = merged_usage

        return content

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """Remove ``<think>...</think>`` blocks from model output.

        Handles both ``<think>...</think>`` and the common distilled-model
        pattern where the opening ``<think>`` is absent and the response
        begins directly with reasoning text followed by ``</think>``.
        """
        # Full <think>...</think> blocks
        text = re.sub(
            r"<think>.*?</think>\s*",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Leading content before a bare </think> (no opening tag)
        text = re.sub(r"^.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
        return text.strip()

    @abstractmethod
    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Execute the agent on *input* and return an ``AgentResult``."""


class ToolUsingAgent(BaseAgent):
    """Intermediate base for agents that accept and use tools.

    Sets ``accepts_tools = True`` for CLI/SDK introspection, and
    initialises a :class:`ToolExecutor` from the provided tools.
    """

    accepts_tools: bool = True

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        tools: Optional[List["BaseTool"]] = None,  # noqa: F821
        bus: Optional[EventBus] = None,
        max_turns: Optional[int] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        loop_guard_config: Optional[Any] = None,
        capability_policy: Optional[Any] = None,
        boundary_guard: Optional[Any] = None,
        agent_id: Optional[str] = None,
        principal_provenance: Optional[str] = None,
        interactive: bool = False,
        confirm_callback: Optional[Any] = None,
        skill_few_shot_examples: Optional[List[str]] = None,
        prompt_builder: Optional[Any] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            bus=bus,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_builder=prompt_builder,
        )
        from openjarvis.tools._stubs import ToolExecutor

        self._tools = tools or []
        # Plan 2B I3: store optimized few-shot examples for agents to inject
        # into their own system prompt templates as appropriate.
        self._skill_few_shot_examples = list(skill_few_shot_examples or [])
        _aid = agent_id or getattr(self, "agent_id", "")
        self._executor = ToolExecutor(
            self._tools,
            bus=bus,
            capability_policy=capability_policy,
            boundary_guard=boundary_guard,
            agent_id=_aid,
            principal_provenance=principal_provenance,
            interactive=interactive,
            confirm_callback=confirm_callback,
        )
        # Resolve max_turns: explicit arg > config > class default > 10
        if max_turns is not None:
            self._max_turns = max_turns
        else:
            try:
                cfg = load_config()
                self._max_turns = cfg.agent.max_turns
            except Exception:
                self._max_turns = getattr(self, "_default_max_turns", 10)

        # Loop guard
        self._loop_guard = None
        try:
            from openjarvis.agents.loop_guard import LoopGuard, LoopGuardConfig

            if loop_guard_config is None:
                loop_guard_config = LoopGuardConfig()
            elif isinstance(loop_guard_config, dict):
                loop_guard_config = LoopGuardConfig(**loop_guard_config)
            if loop_guard_config.enabled:
                self._loop_guard = LoopGuard(loop_guard_config, bus=bus)
        except ImportError:
            pass


__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "ToolUsingAgent",
    "configure_tool_execution_security",
    "is_complete_tool_call_finish_reason",
    "normalize_finish_reason",
    "tool_call_arguments_are_complete",
]
