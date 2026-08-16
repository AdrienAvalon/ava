"""FastAPI routes for the Agent Manager."""

from __future__ import annotations

import logging
import math
import os
import re as _re
from typing import Any, Dict, List, Literal, Optional, Tuple

from openjarvis.agents.manager import MAX_AGENT_MESSAGE_CHARS, AgentManager
from openjarvis.server.models import MAX_COMPLETION_TOKENS

try:
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel, Field, field_validator
except ImportError:
    raise ImportError("fastapi and pydantic are required for server routes")

logger = logging.getLogger("openjarvis.server.agent_manager")


class CreateAgentRequest(BaseModel):
    name: str
    agent_type: str = "monitor_operative"
    config: Optional[Dict[str, Any]] = None
    template_id: Optional[str] = None


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    agent_type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class CreateTaskRequest(BaseModel):
    description: str


class UpdateTaskRequest(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    findings: Optional[List[Any]] = None


class BindChannelRequest(BaseModel):
    channel_type: str
    config: Optional[Dict[str, Any]] = None
    routing_mode: str = "dedicated"


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_AGENT_MESSAGE_CHARS)
    mode: Literal["queued", "immediate"] = "queued"
    stream: bool = False  # SSE streaming mode

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("managed-agent message cannot be blank")
        return value


class FeedbackRequest(BaseModel):
    score: float
    reason: Optional[str] = None


_BROWSER_SUB_TOOLS = {
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_extract",
    "browser_axtree",
}

# Ava's historical JSONL memory is shared and unauthenticated.  Reject its name
# even when a managed-agent template supplies a raw OpenAI function dictionary:
# unregistering the local class alone would still expose the capability to the
# model and to any remote tool bridge using the same name.
_QUARANTINED_TOOL_NAMES = frozenset({"memoire"})
_MANAGED_AGENT_DENIED_TOOL_NAMES = frozenset(
    {
        "agent_kill",
        "agent_list",
        "agent_send",
        "agent_spawn",
        "apply_patch",
        "code_interpreter",
        "code_interpreter_docker",
        "db_query",
        "docker_shell_exec",
        "execute_pending_actions",
        "file_write",
        "git_commit",
        "kg_add_entity",
        "kg_add_relation",
        "kg_neighbors",
        "kg_query",
        "memory_index",
        "memory_manage",
        "memory_retrieve",
        "memory_search",
        "memory_store",
        "pdf_extract",
        "queue_action",
        "record_decision",
        "retrieval",
        "repl",
        "scan_chunks",
        "shell_exec",
        "skill_manage",
        "user_profile_manage",
    }
)
_MANAGED_AGENT_ALLOWED_TOOL_NAMES = frozenset(
    {
        "calculator",
        "think",
    }
)
_MANAGED_AGENT_ALLOWED_CAPABILITIES: frozenset[str] = frozenset()
_MANAGED_AGENT_MAX_TURNS = 50
_MANAGED_HISTORY_TURNS = 25


def _managed_runtime_values(config: Dict[str, Any]) -> Tuple[float, int, int]:
    """Validate bounded sampler values before any managed-agent effect."""

    temperature = config.get("temperature", 0.7)
    max_tokens = config.get("max_tokens", 1024)
    max_turns = config.get("max_turns", 10)
    max_total_tokens = config.get("max_total_tokens", 0)
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or not 0.0 <= float(temperature) <= 2.0
    ):
        raise ValueError("managed-agent temperature must be between 0 and 2")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= MAX_COMPLETION_TOKENS
    ):
        raise ValueError(
            f"managed-agent max_tokens must be between 1 and {MAX_COMPLETION_TOKENS}"
        )
    if (
        isinstance(max_turns, bool)
        or not isinstance(max_turns, int)
        or not 1 <= max_turns <= _MANAGED_AGENT_MAX_TURNS
    ):
        raise ValueError(
            f"managed-agent max_turns must be between 1 and {_MANAGED_AGENT_MAX_TURNS}"
        )
    if (
        isinstance(max_total_tokens, bool)
        or not isinstance(max_total_tokens, int)
        or max_total_tokens < 0
    ):
        raise ValueError(
            "managed-agent max_total_tokens must be a non-negative integer"
        )
    return float(temperature), max_tokens, max_turns


def _validate_managed_tool_config(config: Dict[str, Any]) -> None:
    """Reject capabilities unavailable at the managed HTTP boundary."""

    if "tools" not in config:
        return
    tools = config["tools"]
    if not isinstance(tools, list):
        raise ValueError("managed-agent tools must be a list")
    rejected: list[str] = []
    for entry in tools:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            function = entry.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        else:
            name = None
        if not isinstance(name, str) or name not in _MANAGED_AGENT_ALLOWED_TOOL_NAMES:
            rejected.append(str(name or "<invalid>"))
    if rejected:
        raise ValueError(
            "managed-agent tools are unavailable at the HTTP boundary: "
            + ", ".join(sorted(set(rejected)))
        )


def _managed_remote_tool_name_allowed(name: Any) -> bool:
    """Apply the local quarantine to explicitly enabled remote adapters too."""

    return (
        isinstance(name, str)
        and bool(name)
        and name in _MANAGED_AGENT_ALLOWED_TOOL_NAMES
        and name not in _QUARANTINED_TOOL_NAMES
        and name not in _MANAGED_AGENT_DENIED_TOOL_NAMES
    )


def _managed_tool_allowed(tool_cls: Any) -> bool:
    """Allow only non-interactive, read-bounded tools in managed HTTP agents."""

    try:
        spec = tool_cls().spec
    except Exception:
        return False
    if spec.name in _QUARANTINED_TOOL_NAMES | _MANAGED_AGENT_DENIED_TOOL_NAMES:
        return False
    if spec.name not in _MANAGED_AGENT_ALLOWED_TOOL_NAMES:
        return False
    if spec.requires_confirmation:
        return False
    return set(spec.required_capabilities) <= _MANAGED_AGENT_ALLOWED_CAPABILITIES


def _trace_has_disallowed_managed_tool(trace: Any) -> bool:
    """Hide legacy trace payloads created before the HTTP tool quarantine."""

    for step in getattr(trace, "steps", ()):
        raw_step_type = getattr(step, "step_type", None)
        step_type = getattr(raw_step_type, "value", raw_step_type)
        if step_type != "tool_call":
            continue
        step_input = getattr(step, "input", None)
        name = step_input.get("tool") if isinstance(step_input, dict) else None
        if name not in _MANAGED_AGENT_ALLOWED_TOOL_NAMES:
            return True
    return False


def _resolve_memory_backend(config: Any) -> Any:
    """Instantiate the configured memory backend, or None if unavailable.

    Mirrors serve.py's setup so an agent tick that runs through the server
    can actually use its memory_* tools. Returns None (so the tools degrade
    gracefully) when memory context is disabled or the backend can't load.
    """
    if config is None or not getattr(config.agent, "context_from_memory", False):
        return None
    try:
        key = config.memory.default_backend
        from openjarvis.tools.storage import register_optional_backends

        register_optional_backends(key)
        from openjarvis.core.registry import MemoryRegistry

        if MemoryRegistry.contains(key):
            return MemoryRegistry.create(key, db_path=config.memory.db_path)
    except Exception:
        logger.debug("Lightweight system: memory backend init failed", exc_info=True)
    return None


class _LightweightSystem:
    """Minimal system facade for the executor — avoids rebuilding the
    full JarvisSystem (which picks a random model from Ollama)."""

    def __init__(
        self,
        engine: Any,
        model: str,
        config: Any = None,
        *,
        http_boundary: bool = False,
    ):
        self.engine = engine
        self.model = model
        self.config = config
        self.http_boundary = http_boundary
        # Wire the configured memory backend so an agent's memory_store /
        # memory_retrieve tools work when the tick runs through the server.
        # The executor injects system.memory_backend into those tools; this
        # facade previously left it None, so they reported "No memory backend
        # configured" even though the backend was configured and active.
        self.memory_backend = None if http_boundary else _resolve_memory_backend(config)


def _make_lightweight_system(
    engine: Any,
    model: str,
    config: Any = None,
    *,
    http_boundary: bool = False,
) -> _LightweightSystem:
    """Build a minimal system with a fresh inference engine.

    The server's ``app.state.engine`` is heavily wrapped
    (MultiEngine -> InstrumentedEngine -> GuardrailsEngine) and can
    return empty content from background threads. Create a fresh
    engine directly (no health checks or model discovery that
    could interfere with in-flight requests).
    """
    try:
        from openjarvis.engine._discovery import get_engine

        cfg = config
        if cfg is None:
            from openjarvis.core.config import load_config

            cfg = load_config()

        pref = cfg.intelligence.preferred_engine
        key = pref or cfg.engine.default
        resolved = get_engine(cfg, key)

        if resolved is not None:
            plain_engine = resolved[1]
        else:
            from openjarvis.engine.ollama import OllamaEngine

            host = cfg.engine.ollama.host if cfg else ""
            plain_engine = OllamaEngine(host=host) if host else OllamaEngine()

        # Wrap with InstrumentedEngine so agent ticks are recorded
        # in telemetry (FLOPs, energy, cost savings).
        try:
            from openjarvis.core.events import get_event_bus
            from openjarvis.telemetry.instrumented_engine import (
                InstrumentedEngine,
            )

            plain_engine = InstrumentedEngine(
                plain_engine,
                get_event_bus(),
            )
        except Exception:
            pass  # telemetry is optional
        return _LightweightSystem(
            plain_engine,
            model,
            cfg,
            http_boundary=http_boundary,
        )
    except Exception:
        pass
    return _LightweightSystem(
        engine,
        model,
        config,
        http_boundary=http_boundary,
    )


def _parse_param_count(model_name: str) -> float:
    """Extract parameter count in billions from model name.

    Examples: 'qwen3.5:9b' -> 9.0, 'qwen3.5:0.8b' -> 0.8
    """
    m = _re.search(r":(\d+(?:\.\d+)?)b", model_name.lower())
    return float(m.group(1)) if m else 0.0


_CLOUD_PREFIXES = ("gpt-", "claude-", "gemini-", "o1-", "o3-", "o4-")


def _pick_recommended_model(
    model_ids: list[str],
) -> dict[str, str]:
    """Pick the second-largest local model from a list."""
    local = [m for m in model_ids if not any(m.startswith(p) for p in _CLOUD_PREFIXES)]
    if not local:
        return {
            "model": model_ids[0] if model_ids else "",
            "reason": "Only model available",
        }
    sized = sorted(local, key=_parse_param_count, reverse=True)
    if len(sized) == 1:
        return {"model": sized[0], "reason": "Only local model available"}
    pick = sized[1]  # second-largest
    params = _parse_param_count(pick)
    return {
        "model": pick,
        "reason": f"Second-largest local model ({params}B parameters)",
    }


def _ensure_registries_populated() -> None:
    """Ensure ToolRegistry and ChannelRegistry are populated.

    If the registries are empty (e.g. cleared by test fixtures) but the
    modules are already cached in sys.modules, reload the individual
    submodules to re-execute their @register decorators.
    """
    import importlib
    import sys

    from openjarvis.core.registry import ChannelRegistry, ToolRegistry

    # First, try a normal import (works if modules haven't been imported yet)
    try:
        import openjarvis.channels  # noqa: F401
    except Exception:
        pass

    try:
        import openjarvis.tools  # noqa: F401
    except Exception:
        pass

    # Also try to import browser tools (not included in openjarvis.tools.__init__)
    for _browser_mod in ("openjarvis.tools.browser", "openjarvis.tools.browser_axtree"):
        try:
            importlib.import_module(_browser_mod)
        except Exception:
            pass

    # If registries are still empty, reload individual submodules from sys.modules
    if not ChannelRegistry.keys():
        for mod_name in list(sys.modules):
            if mod_name.startswith("openjarvis.channels.") and not mod_name.endswith(
                "_stubs"
            ):
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass

    if not ToolRegistry.keys():
        for mod_name in list(sys.modules):
            if (
                mod_name.startswith("openjarvis.tools.")
                and not mod_name.endswith("_stubs")
                and not mod_name.endswith("agent_tools")
            ):
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass

    # After reloading tools, also try browser tools if still not registered
    if not any(ToolRegistry.contains(n) for n in _BROWSER_SUB_TOOLS):
        for _browser_mod in (
            "openjarvis.tools.browser",
            "openjarvis.tools.browser_axtree",
        ):
            mod = sys.modules.get(_browser_mod)
            if mod is not None:
                try:
                    importlib.reload(mod)
                except Exception:
                    pass


def build_tools_list() -> List[Dict[str, Any]]:
    """Build unified tools list from ToolRegistry + ChannelRegistry."""
    from openjarvis.core.credentials import TOOL_CREDENTIALS
    from openjarvis.core.registry import ChannelRegistry, ToolRegistry

    _ensure_registries_populated()

    items: List[Dict[str, Any]] = []

    for name, tool_cls in ToolRegistry.items():
        if name in _BROWSER_SUB_TOOLS:
            continue
        if not _managed_tool_allowed(tool_cls):
            continue
        # `spec` is an instance @property on BaseTool subclasses, so
        # we have to instantiate the tool to read it. The earlier
        # implementation used getattr(tool_cls, 'spec') which returns
        # the property descriptor and crashed on spec.description,
        # silently dropping every real tool from the picker.
        try:
            spec = tool_cls().spec
        except Exception as exc:
            logger.debug("Could not instantiate tool %s: %s", name, exc)
            spec = None
        cred_keys = TOOL_CREDENTIALS.get(name, [])
        has_fallback = bool(spec and spec.metadata.get("fallback"))
        items.append(
            {
                "name": name,
                "description": spec.description if spec else "",
                "category": spec.category if spec else "",
                "source": "tool",
                "requires_credentials": len(cred_keys) > 0 and not has_fallback,
                "credential_keys": cred_keys,
                "configured": (
                    has_fallback or all(bool(os.environ.get(k)) for k in cred_keys)
                    if cred_keys
                    else True
                ),
            }
        )

    try:
        for name, _cls in ChannelRegistry.items():
            cred_keys = TOOL_CREDENTIALS.get(name, [])
            items.append(
                {
                    "name": name,
                    "description": (
                        f"{name.replace('_', ' ').title()} messaging channel"
                    ),
                    "category": "communication",
                    "source": "channel",
                    "requires_credentials": len(cred_keys) > 0,
                    "credential_keys": cred_keys,
                    "configured": (
                        all(bool(os.environ.get(k)) for k in cred_keys)
                        if cred_keys
                        else True
                    ),
                }
            )
    except Exception:
        pass

    return items


def _resolve_tool_specs(
    tool_config: Any,
) -> List[Dict[str, Any]]:
    """Convert a template's ``tools`` config into OpenAI-format function specs.

    The template TOML stores tools as a list of string names (e.g.
    ``["file_read", "shell_exec"]``). Engines expect OpenAI-shaped dicts:
    ``{"type": "function", "function": {"name, description, parameters"}}``.

    Special handling:
      * Dict entries pass through as-is, except quarantined capability names.
      * ``browser`` is a synthetic display-only meta-tool that expands
        to the 6 real browser sub-tools (browser_navigate, click, …).
      * Channel names (``slack``, ``gmail``, …) come from the
        ``ChannelRegistry`` and are not directly callable by the LLM —
        they're destinations for ``channel_send``. Silently skip them.
      * Unknown tool names are dropped with a warning.
    """
    if not tool_config:
        return []

    from openjarvis.core.registry import ChannelRegistry, ToolRegistry

    _ensure_registries_populated()

    def _spec_dict_for(name: str) -> Optional[Dict[str, Any]]:
        if not ToolRegistry.contains(name):
            return None
        tool_cls = ToolRegistry.get(name)
        if not _managed_tool_allowed(tool_cls):
            logger.warning("Unsafe managed-agent tool '%s' was dropped", name)
            return None
        try:
            spec = tool_cls().spec
        except Exception as exc:
            logger.warning(
                "Could not build spec for tool '%s' (%s) — dropping",
                name,
                exc,
            )
            return None
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    resolved: List[Dict[str, Any]] = []
    seen: set = set()

    for entry in tool_config:
        if isinstance(entry, dict):
            function = entry.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if (
                not isinstance(name, str)
                or not ToolRegistry.contains(name)
                or not _managed_tool_allowed(ToolRegistry.get(name))
            ):
                logger.warning("Unsafe managed-agent tool '%s' was dropped", name)
                continue
            resolved.append(entry)
            continue
        if not isinstance(entry, str):
            continue
        if entry in _QUARANTINED_TOOL_NAMES:
            logger.warning("Quarantined tool '%s' dropped from agent config", entry)
            continue

        # Expand the synthetic "browser" meta-tool into its sub-tools.
        if entry == "browser":
            for sub in _BROWSER_SUB_TOOLS:
                if sub in seen or not ToolRegistry.contains(sub):
                    continue
                spec_dict = _spec_dict_for(sub)
                if spec_dict:
                    resolved.append(spec_dict)
                    seen.add(sub)
            continue

        # Channels (slack, gmail, …) live in ChannelRegistry and aren't
        # callable by the LLM. Skip silently — the agent talks to them
        # through the `channel_send` tool with a `channel` argument.
        if ChannelRegistry.contains(entry):
            continue

        if not ToolRegistry.contains(entry):
            logger.warning(
                "Tool '%s' referenced in agent config but not in ToolRegistry",
                entry,
            )
            continue

        if entry in seen:
            continue
        spec_dict = _spec_dict_for(entry)
        if spec_dict:
            resolved.append(spec_dict)
            seen.add(entry)

    return resolved


# Per-agent sampler params forwarded to the engine when present in config.
# The OpenAI-compat engine passes **kwargs straight through to the upstream
# payload, so these reach local servers (vLLM / mlx_lm / etc.) that support
# them. Only forwarded when explicitly set, so default agents send nothing
# extra and engines that don't support a key never receive it. (#386)
_SAMPLER_PARAM_KEYS = (
    "top_p",
    "top_k",
    "min_p",
    "repetition_penalty",
    "frequency_penalty",
    "presence_penalty",
)


def _build_managed_system_prompt(system_prompt: str, app_config: Any) -> str:
    """Compose managed HTTP prompts without private installation persona files."""

    del app_config
    from ava_extensions.patches.system_prompt_loader import load_common_persona

    common = load_common_persona().strip()
    if not common:
        raise RuntimeError("Ava common identity is unavailable")
    template = str(system_prompt or "").strip()
    if not template:
        return common
    return f"{common}\n\n## Mission de l'agent géré\n{template}"


def _sampler_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract per-agent sampler params from a managed agent's config (#386)."""
    out: Dict[str, Any] = {}
    for key in _SAMPLER_PARAM_KEYS:
        val = config.get(key)
        if val is not None:
            out[key] = val
    return out


def _sanitize_managed_history(
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove legacy tool results no longer allowed at the HTTP boundary."""

    unsafe_reply_ids: set[str] = set()
    sanitized: List[Dict[str, Any]] = []
    for message in history:
        stored = message.get("tool_calls")
        if stored:
            if not isinstance(stored, list) or any(
                not isinstance(call, dict)
                or not _managed_remote_tool_name_allowed(call.get("tool"))
                for call in stored
            ):
                reply_to_id = message.get("reply_to_id")
                if reply_to_id:
                    unsafe_reply_ids.add(str(reply_to_id))
                continue
        sanitized.append(dict(message))
    return [
        message
        for message in sanitized
        if str(message.get("id") or "") not in unsafe_reply_ids
    ]


def _replay_history_messages(
    history: List[Dict[str, Any]],
    exclude_id: str,
) -> List[Any]:
    """Rebuild prior-turn LLM messages from stored managed-agent history.

    When a stored assistant turn recorded ``tool_calls``, replay them as an
    assistant tool-use message followed by the corresponding tool-result
    messages — so the model sees its own prior tool pattern instead of
    regressing to fabricated tool output on later turns. Without this, only
    the assistant's text is replayed and the tool-use signal is lost (#382).
    """
    from openjarvis.core.types import Message, Role, ToolCall

    chronological = [
        message
        for message in reversed(_sanitize_managed_history(history))
        if message.get("id") != exclude_id
    ]
    users = {
        str(message.get("id")): message
        for message in chronological
        if message.get("direction") == "user_to_agent"
        and message.get("status", "delivered") == "delivered"
        and message.get("id")
    }
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    legacy_user: Dict[str, Any] | None = None
    used_users: set[str] = set()
    for message in chronological:
        direction = message.get("direction")
        if direction == "user_to_agent":
            if message.get("status", "delivered") == "delivered":
                legacy_user = message
            continue
        if (
            direction != "agent_to_user"
            or message.get("status", "delivered") != "delivered"
        ):
            continue
        reply_to_id = message.get("reply_to_id")
        user = users.get(str(reply_to_id)) if reply_to_id else legacy_user
        user_id = str(user.get("id")) if user is not None else ""
        if user is None or not user_id or user_id in used_users:
            continue
        pairs.append((user, message))
        used_users.add(user_id)
        if not reply_to_id:
            legacy_user = None

    messages: List[Any] = []
    for user, assistant_message in pairs[-_MANAGED_HISTORY_TURNS:]:
        messages.append(Message(role=Role.USER, content=user.get("content") or ""))
        m = assistant_message
        stored = m.get("tool_calls")
        if stored:
            calls = []
            results = []
            for i, tc in enumerate(stored):
                call_id = f"hist-{m.get('id', '')}-{i}"
                calls.append(
                    ToolCall(
                        id=call_id,
                        name=tc.get("tool", ""),
                        arguments=tc.get("arguments") or "",
                    )
                )
                results.append(
                    Message(
                        role=Role.TOOL,
                        content=str(tc.get("result", "")),
                        tool_call_id=call_id,
                        name=tc.get("tool", ""),
                    )
                )
            messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=m.get("content") or None,
                    tool_calls=calls,
                )
            )
            messages.extend(results)
        else:
            messages.append(
                Message(role=Role.ASSISTANT, content=m.get("content") or "")
            )
    return messages


def _instantiate_managed_tool(
    tool_cls: Any,
    name: str,
    *,
    engine: Any,
    model: str,
    app_state: Any,
) -> Any:
    """Instantiate a tool with the same dependency injection as the canonical
    ``cli/ask.py`` path, so ``memory_*`` / ``channel_*`` / ``llm`` tools work
    instead of silently failing with "No backend configured" (#395).
    """
    try:
        from openjarvis.cli.ask import (
            _CHANNEL_TOOLS,
            _MEMORY_TOOLS,
            _get_memory_backend,
        )
    except Exception:  # pragma: no cover - cli import should always succeed
        _MEMORY_TOOLS = frozenset()
        _CHANNEL_TOOLS = frozenset()
        _get_memory_backend = None

    if name in _MEMORY_TOOLS:
        backend = getattr(app_state, "memory_backend", None) if app_state else None
        if backend is None and app_state is not None and _get_memory_backend:
            cfg = getattr(app_state, "config", None)
            if cfg is not None:
                backend = _get_memory_backend(cfg)
        if backend is None:
            logger.warning(
                "Memory tool %r instantiated without a backend — calls will "
                "return no results.",
                name,
            )
        return tool_cls(backend=backend)
    if name in _CHANNEL_TOOLS:
        channel = getattr(app_state, "channel_bridge", None) if app_state else None
        if channel is None:
            logger.warning(
                "Channel tool %r instantiated without a channel — calls will "
                "fail with 'No channel backend configured'.",
                name,
            )
        return tool_cls(channel=channel)
    if name == "llm":
        return tool_cls(engine=engine, model=model)
    return tool_cls()


def _build_deep_research_tools(
    engine: Any,
    model: str,
    knowledge_db_path: str = "",
    *,
    allow_personal_knowledge: bool = True,
) -> list:
    """Build the 4 DeepResearch tools from a KnowledgeStore.

    Returns an empty list if the knowledge DB does not exist.
    """
    from openjarvis.tools.think import ThinkTool

    if not allow_personal_knowledge:
        return [ThinkTool()]

    from pathlib import Path

    if not knowledge_db_path:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR

        knowledge_db_path = str(DEFAULT_CONFIG_DIR / "knowledge.db")

    if not Path(knowledge_db_path).exists():
        return []

    from openjarvis.connectors.retriever import TwoStageRetriever
    from openjarvis.connectors.store import KnowledgeStore
    from openjarvis.tools.knowledge_search import KnowledgeSearchTool
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool
    from openjarvis.tools.scan_chunks import ScanChunksTool

    store = KnowledgeStore(knowledge_db_path)
    retriever = TwoStageRetriever(store)
    return [
        KnowledgeSearchTool(retriever=retriever),
        KnowledgeSQLTool(store=store),
        ScanChunksTool(store=store, engine=engine, model=model),
        ThinkTool(),
    ]


def _merge_tool_call_fragments(
    accumulated: Dict[int, Dict[str, Any]],
    fragments: List[Dict[str, Any]],
) -> None:
    """Merge incremental tool_call delta fragments into accumulated state.

    OpenAI-compatible APIs send tool_calls as incremental fragments keyed
    by ``index``. Each fragment may contain partial ``function.name`` and/or
    ``function.arguments`` strings that must be concatenated.
    """
    for frag in fragments:
        idx = frag.get("index", 0)
        if idx not in accumulated:
            accumulated[idx] = {
                "id": frag.get("id", ""),
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        entry = accumulated[idx]
        if frag.get("id"):
            entry["id"] = frag["id"]
        fn = frag.get("function", {})
        if fn.get("name"):
            entry["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            entry["function"]["arguments"] += fn["arguments"]


def _get_mcp_tools(app_state: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (openai_tools_list, mcp_adapters_by_name).

    Lazily discovers MCP tools from config and caches them on ``app_state``
    so that subsequent requests reuse the same connections.
    """
    cached = getattr(app_state, "_mcp_tools_cache", None)
    if cached is not None:
        return cached

    import json as _json

    from openjarvis.core.config import load_config

    openai_tools: List[Dict[str, Any]] = []
    adapters_by_name: Dict[str, Any] = {}

    try:
        app_config = load_config()
    except Exception as exc:
        logger.warning("Failed to load config for MCP discovery: %s", exc)
        return openai_tools, adapters_by_name

    if not app_config.tools.mcp.enabled or not app_config.tools.mcp.servers:
        return openai_tools, adapters_by_name

    from openjarvis.mcp.client import MCPClient
    from openjarvis.mcp.transport import StdioTransport, StreamableHTTPTransport
    from openjarvis.tools.mcp_adapter import MCPToolProvider

    # Keep clients alive so transports persist for tool calls at runtime
    mcp_clients: list = getattr(app_state, "_mcp_clients", [])

    try:
        server_list = _json.loads(app_config.tools.mcp.servers)
    except (_json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to parse MCP server config: %s", exc)
        return openai_tools, adapters_by_name

    if not isinstance(server_list, list):
        return openai_tools, adapters_by_name

    for server_cfg in server_list:
        cfg = _json.loads(server_cfg) if isinstance(server_cfg, str) else server_cfg
        name = cfg.get("name", "<unnamed>")
        url = cfg.get("url")
        # Bearer token from config — mirrors the builder.py fix for #461.
        token = cfg.get("token")
        command = cfg.get("command", "")
        args = cfg.get("args", [])

        try:
            if url:
                transport = StreamableHTTPTransport(url=url, token=token)
            elif command:
                transport = StdioTransport(command=[command] + args)
            else:
                logger.warning(
                    "MCP server '%s' has neither 'url' nor 'command' — skipping",
                    name,
                )
                continue

            client = MCPClient(transport)
            client.initialize()
            mcp_clients.append(client)

            provider = MCPToolProvider(client)
            discovered = provider.discover()

            # Per-server tool filtering
            include_tools = set(cfg.get("include_tools", []))
            exclude_tools = set(cfg.get("exclude_tools", []))
            if include_tools:
                discovered = [t for t in discovered if t.spec.name in include_tools]
            if exclude_tools:
                discovered = [t for t in discovered if t.spec.name not in exclude_tools]

            for adapter in discovered:
                spec = adapter.spec
                if not _managed_remote_tool_name_allowed(spec.name):
                    logger.warning(
                        "Unsafe managed-agent MCP tool '%s' was dropped",
                        spec.name,
                    )
                    continue
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": spec.name,
                            "description": spec.description,
                            "parameters": spec.parameters,
                        },
                    }
                )
                adapters_by_name[spec.name] = adapter

            logger.info(
                "Discovered %d MCP tools from server '%s'",
                len(discovered),
                name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to discover MCP tools from '%s': %s",
                name,
                exc,
            )

    app_state._mcp_clients = mcp_clients
    if openai_tools:
        app_state._mcp_tools_cache = (openai_tools, adapters_by_name)
    return openai_tools, adapters_by_name


def _sse_chunk(chunk_id: str, model: str, content: str) -> str:
    """Build a single SSE content chunk."""
    import json as _json

    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {_json.dumps(data)}\n\n"


def _tool_progress_label(tool_name: str, args: str) -> str:
    """Human-readable label for a tool call in progress."""
    labels = {
        "knowledge_search": "Searching your knowledge base",
        "knowledge_sql": "Querying data with SQL",
        "scan_chunks": "Scanning documents for semantic matches",
        "think": "Planning next step",
    }
    label = labels.get(tool_name, f"Using {tool_name}")
    if args and tool_name != "think":
        # Try to extract the query/question from args JSON
        try:
            import json as _json

            parsed = _json.loads(args)
            q = parsed.get("query") or parsed.get("question") or ""
            if q:
                label += f' — "{q[:50]}"'
        except Exception:
            pass
    return label


async def _stream_managed_agent(
    *,
    manager: AgentManager,
    agent_record: Dict[str, Any],
    user_content: str,
    message_id: str,
    tick_token: str,
    engine: Any,
    bus: Any,
    app_state: Any = None,
) -> StreamingResponse:
    """Run a managed agent with real LLM token streaming via SSE.

    Uses ``engine.stream_full()`` to yield tokens as they arrive from the
    LLM. Supports multi-turn tool-calling: when the model emits tool_calls,
    they are executed and the results fed back for the next turn.
    """
    import json
    import uuid

    from openjarvis.core.types import Message, Role

    agent_id = agent_record["id"]
    config = agent_record.get("config", {})
    # Resolve the model: prefer the agent's own config, then the server's
    # resolved model (app.state.model — what the engine was booted with),
    # and only then the legacy engine._model attr. OllamaEngine takes the
    # model per-call and exposes no _model attr, so without the app_state
    # fallback this resolved to "" and Ollama 400'd on an empty model.
    model = (
        config.get("model")
        or getattr(app_state, "model", None)
        or getattr(engine, "_model", "")
    )
    system_prompt = config.get("system_prompt")
    temperature, max_tokens, max_turns = _managed_runtime_values(config)

    # Build conversation messages from history + current input
    llm_messages: List[Message] = []

    # Wire the SystemPromptBuilder to inject SOUL.md / MEMORY.md / USER.md
    # persona files (parity with the CLI/ask path) — see #431.
    app_config = getattr(app_state, "config", None)
    if app_config is None:
        from openjarvis.core.config import load_config

        app_config = load_config()

    final_system_prompt = _build_managed_system_prompt(system_prompt or "", app_config)

    if final_system_prompt and final_system_prompt.strip():
        llm_messages.append(
            Message(role=Role.SYSTEM, content=final_system_prompt.strip())
        )

    # Resolve agent type and class for DeepResearch tool wiring
    agent_type = agent_record.get("agent_type", "")
    deep_research_tools: list[Any] = []
    if agent_type == "deep_research":
        deep_research_tools = _build_deep_research_tools(
            engine=engine,
            model=model,
            allow_personal_knowledge=False,
        )

    # Load prior conversation context (DESC order, reverse for chronological).
    # Replaying recorded tool_calls (assistant tool-use + tool results) keeps
    # multi-turn tool behaviour from regressing to fabricated output (#382).
    history = manager.list_messages(agent_id, limit=(2 * _MANAGED_HISTORY_TURNS) + 1)
    llm_messages.extend(_replay_history_messages(history, message_id))

    # Append the current user message
    llm_messages.append(Message(role=Role.USER, content=user_content))

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # For deep_research agents: run the full agent loop, not raw streaming
    if agent_type == "deep_research" and app_state is not None:
        if deep_research_tools:

            async def generate_deep_research():
                """Run DeepResearchAgent in thread, stream progress + result."""
                import asyncio
                import queue
                import threading
                import time as _dr_time

                from openjarvis.agents._stubs import AgentContext
                from openjarvis.agents.deep_research import DeepResearchAgent

                progress_q: queue.Queue = queue.Queue()
                dr_tool_calls: List[Dict[str, Any]] = []

                # Log query start
                _dr_start = _dr_time.time()
                try:
                    manager.add_learning_log(
                        agent_id,
                        "query_start",
                        f"Query: {user_content[:100]}",
                        {"full_query": user_content},
                    )
                except Exception as _log_exc:
                    logger.warning(
                        "Failed to log query_start: %s",
                        _log_exc,
                    )

                # Patch the agent's tool executor to emit progress
                dr_agent = DeepResearchAgent(
                    engine=engine,
                    model=model,
                    tools=deep_research_tools,
                    max_turns=max_turns,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    interactive=True,
                    confirm_callback=lambda _prompt: True,
                )
                dr_context = AgentContext()
                dr_context.metadata["server_identity_prompt"] = final_system_prompt
                dr_context.conversation.messages.extend(
                    message
                    for message in llm_messages[:-1]
                    if message.role != Role.SYSTEM
                )

                # Wrap the executor to capture tool calls
                original_execute = dr_agent._executor.execute

                def _tracked_execute(tc):
                    tool_name = tc.name
                    full_args = tc.arguments or ""
                    args_str = full_args[:80]
                    # Log tool call start
                    try:
                        manager.add_learning_log(
                            agent_id,
                            "tool_call",
                            f"Calling {tool_name}: {args_str}",
                            {"tool": tool_name, "arguments": full_args[:4096]},
                        )
                    except Exception as _tc_exc:
                        logger.warning("Log tool_call failed: %s", _tc_exc)

                    progress_q.put(
                        {
                            "type": "tool_start",
                            "tool": tool_name,
                            "args": args_str,
                            "full_args": full_args,
                        }
                    )
                    _tool_start = _dr_time.monotonic()
                    result = original_execute(tc)
                    _tool_latency_ms = (_dr_time.monotonic() - _tool_start) * 1000
                    if not result.success:
                        # Tool internals may contain exception text, paths or
                        # credentials.  Managed HTTP turns receive only a
                        # stable failure marker; details stay in server logs.
                        result.content = f"Tool '{tool_name}' failed."
                    dr_tool_calls.append(
                        {
                            "tool": tool_name,
                            "arguments": full_args[:4096],
                            "result": (result.content or "")[:1024],
                            "success": bool(result.success),
                            "latency": float(_tool_latency_ms),
                        }
                    )

                    # Log tool result
                    try:
                        _ok = "succeeded" if result.success else "failed"
                        _clen = len(result.content) if result.content else 0
                        manager.add_learning_log(
                            agent_id,
                            "tool_result",
                            f"{tool_name} {_ok} ({_clen} chars)",
                            {
                                "tool": tool_name,
                                "success": result.success,
                                "output_length": _clen,
                            },
                        )
                    except Exception as _tr_exc:
                        logger.warning("Log tool_result failed: %s", _tr_exc)

                    progress_q.put(
                        {
                            "type": "tool_end",
                            "tool": tool_name,
                            "arguments": full_args[:4096],
                            "success": result.success,
                            "latency": _tool_latency_ms,
                            "result": (result.content or "")[:1024],
                        }
                    )
                    return result

                dr_agent._executor.execute = _tracked_execute

                def _run_agent():
                    agent_metadata = {}
                    terminal_status = "error"
                    try:
                        result = dr_agent.run(user_content, context=dr_context)
                        content = result.content or "No results found."
                        agent_metadata = result.metadata or {}
                        manager.complete_message_turn(
                            agent_id,
                            message_id,
                            content,
                            tool_calls=dr_tool_calls or None,
                        )
                        terminal_status = "idle"
                    except Exception:
                        manager.mark_message_failed(message_id)
                        logger.error(
                            "Deep-research managed turn failed",
                            exc_info=True,
                        )
                        content = "Error: managed deep-research generation failed"

                    elapsed = _dr_time.time() - _dr_start

                    # Log BEFORE queue put (put triggers SSE end)
                    try:
                        is_err = content.startswith("Error:")
                        manager.add_learning_log(
                            agent_id,
                            "query_error" if is_err else "query_complete",
                            f"{'Error' if is_err else 'Response'}: "
                            f"{len(content)} chars in {elapsed:.1f}s",
                            {
                                "response_length": len(content),
                                "elapsed_seconds": round(elapsed, 2),
                            },
                        )
                    except Exception as _qc_exc:
                        logger.warning(
                            "Log failed: %s",
                            _qc_exc,
                        )

                    manager.end_tick(
                        agent_id,
                        tick_token,
                        status=terminal_status,
                    )
                    progress_q.put(
                        {
                            "type": "error" if content.startswith("Error:") else "done",
                            "content": content,
                            "metadata": agent_metadata,
                            "elapsed": elapsed,
                        }
                    )

                thread = threading.Thread(target=_run_agent, daemon=True)
                try:
                    thread.start()
                except Exception:
                    manager.mark_message_failed(message_id)
                    manager.end_tick(agent_id, tick_token, status="error")
                    logger.error(
                        "Deep-research managed worker could not start",
                        exc_info=True,
                    )
                    yield (
                        'data: {"error":{"type":"worker_unavailable",'
                        '"message":"Managed deep-research worker unavailable"}}\n\n'
                    )
                    yield "data: [DONE]\n\n"
                    return

                _pending_dr_starts: Dict[str, str] = {}

                # Stream progress events and final content
                while True:
                    try:
                        event = await asyncio.to_thread(progress_q.get, timeout=600)
                    except Exception:
                        # Timeout
                        manager.mark_message_failed(message_id)
                        yield (
                            'data: {"error":{"type":"generation_timeout",'
                            '"message":"Managed deep-research agent timed out"}}\n\n'
                        )
                        yield "data: [DONE]\n\n"
                        return

                    if event["type"] == "tool_start":
                        tool = event["tool"]
                        args = event.get("args", "")
                        full_args = event.get("full_args", "")
                        _pending_dr_starts[tool] = full_args
                        # Structured event so the UI can render a tool_call
                        # message card (same shape as the non-DR path).
                        _start_payload = json.dumps(
                            {"tool": tool, "arguments": full_args}
                        )
                        yield f"event: tool_call_start\ndata: {_start_payload}\n\n"
                        # Keep the human-readable progress label for the
                        # thinking-bubble fallback.
                        label = _tool_progress_label(tool, args)
                        progress_data = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": None,
                                    "tool_progress": label,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(progress_data)}\n\n"

                    elif event["type"] == "tool_end":
                        tool = event["tool"]
                        _pending_dr_starts.pop(tool, None)
                        _end_payload = json.dumps(
                            {
                                "tool": tool,
                                "success": bool(event.get("success", False)),
                                "latency": float(event.get("latency", 0.0)),
                                "result": event.get("result", ""),
                            }
                        )
                        yield f"event: tool_call_end\ndata: {_end_payload}\n\n"

                    elif event["type"] in ("done", "error"):
                        content = event["content"]
                        meta = event.get("metadata", {})
                        elapsed_s = event.get("elapsed", 0)

                        if event["type"] == "error":
                            manager.mark_message_failed(message_id)
                            yield (
                                'data: {"error":{"type":"generation_error",'
                                '"message":"Managed agent generation failed"}}\n\n'
                            )
                            yield "data: [DONE]\n\n"
                            break

                        # Stream content word-by-word
                        words = content.split(" ")
                        for i, word in enumerate(words):
                            token = word if i == 0 else " " + word
                            yield _sse_chunk(chunk_id, model, token)

                        # Build usage + telemetry
                        prompt_tok = meta.get("prompt_tokens", 0)
                        comp_tok = meta.get("completion_tokens", 0)
                        total_tok = meta.get("total_tokens", 0)
                        word_count = len(words)
                        speed = round(word_count / elapsed_s) if elapsed_s > 0 else 0

                        # Final chunk with usage + telemetry
                        finish_data = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": prompt_tok,
                                "completion_tokens": comp_tok,
                                "total_tokens": total_tok or (prompt_tok + comp_tok),
                            },
                            "telemetry": {
                                "engine": "ollama",
                                "model_id": model,
                                "total_ms": round(elapsed_s * 1000),
                                "tokens_per_sec": speed,
                                "tool_calls": len(meta.get("sources", [])),
                            },
                        }
                        yield f"data: {json.dumps(finish_data)}\n\n"
                        yield "data: [DONE]\n\n"
                        break

            return StreamingResponse(
                generate_deep_research(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

    # Build extra kwargs for stream_full (e.g. tools from config).
    # Template stores tool names as strings; convert to OpenAI function specs
    # so the engine can actually bind them to the model.
    stream_kwargs: Dict[str, Any] = {}
    resolved_tools = _resolve_tool_specs(config.get("tools"))
    if resolved_tools:
        stream_kwargs["tools"] = resolved_tools

    # Forward any per-agent sampler params (repetition_penalty, top_p, …) so
    # locally-hosted models can be tuned per agent (#386).
    stream_kwargs.update(_sampler_kwargs(config))

    # Remote MCP adapters have no enforceable local path/capability contract.
    # Even an adapter named ``file_read`` can behave differently from the
    # locally audited tool and reach the quarantined legacy memory. Managed
    # HTTP conversations therefore never expose MCP tools; MCP remains
    # available to explicitly constructed non-HTTP systems with their own
    # authority boundary.
    mcp_adapters: Dict[str, Any] = {}

    # Shared state between the generator and the BackgroundTask that
    # runs after the SSE response completes (or the client disconnects
    # mid-stream). Starlette guarantees the BackgroundTask runs in both
    # cases, so we use it as the single, reliable persistence point.
    persist_state: Dict[str, Any] = {
        "content": "",
        "tool_calls": [],
        "persisted": False,
        "terminal_success": False,
        "persistence_error": False,
        "tick_released": False,
    }

    import threading as _persist_threading

    finalize_lock = _persist_threading.Lock()

    def _persist_final() -> None:
        if persist_state["persisted"]:
            return
        persist_state["persisted"] = True
        if persist_state["terminal_success"] and persist_state["content"]:
            try:
                manager.complete_message_turn(
                    agent_id,
                    message_id,
                    persist_state["content"],
                    tool_calls=persist_state["tool_calls"] or None,
                )
            except Exception as store_exc:
                persist_state["terminal_success"] = False
                persist_state["persistence_error"] = True
                logger.error(
                    "Failed to store agent response: %s",
                    store_exc,
                    exc_info=True,
                )
                try:
                    manager.mark_message_failed(message_id)
                except Exception:
                    logger.error(
                        "Failed to mark uncommitted agent message as failed",
                        exc_info=True,
                    )
        else:
            try:
                manager.mark_message_failed(message_id)
            except Exception as store_exc:
                logger.error(
                    "Failed to mark agent message as failed: %s",
                    store_exc,
                    exc_info=True,
                )
        try:
            content = persist_state["content"] or ""
            succeeded = bool(
                persist_state["terminal_success"]
                and not persist_state["persistence_error"]
            )
            failure_reason = (
                "persistence_error"
                if persist_state["persistence_error"]
                else "incomplete_response"
            )
            manager.add_learning_log(
                agent_id,
                "query_complete" if succeeded else "query_error",
                (
                    f"Response: {len(content)} chars, "
                    f"{len(persist_state['tool_calls'])} tool calls"
                    if succeeded
                    else f"Managed turn failed: {failure_reason}"
                ),
                {
                    "response_length": len(content),
                    "tool_calls": len(persist_state["tool_calls"]),
                    "failure_reason": None if succeeded else failure_reason,
                },
            )
        except Exception as _qc_exc:
            logger.warning("Log terminal managed-query state failed: %s", _qc_exc)

    async def _generate_body():
        """Async generator yielding SSE-formatted chunks with real token streaming."""

        collected_content = ""
        collected_tool_calls: List[Dict[str, Any]] = []
        messages_for_llm = list(llm_messages)
        turns = 0

        import time as _lgtime

        _query_start_ts = _lgtime.time()
        try:
            manager.add_learning_log(
                agent_id,
                "query_start",
                f"Query: {user_content[:100]}",
                {"full_query": user_content},
            )
        except Exception as _qs_exc:
            logger.warning("Log query_start failed: %s", _qs_exc)

        while turns < max_turns:
            turns += 1
            turn_content = ""
            tool_call_fragments: Dict[int, Dict[str, Any]] = {}
            current_finish_reason = None

            try:
                async for chunk in engine.stream_full(
                    messages_for_llm,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **stream_kwargs,
                ):
                    # Stream content tokens immediately to the client
                    if chunk.content:
                        turn_content += chunk.content
                        # Mirror partial content so a disconnect during
                        # generation still saves what we've produced.
                        persist_state["content"] = collected_content + turn_content
                        chunk_data = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": chunk.content},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk_data)}\n\n"

                    # Accumulate tool_call fragments
                    if chunk.tool_calls:
                        _merge_tool_call_fragments(
                            tool_call_fragments,
                            chunk.tool_calls,
                        )

                    if chunk.finish_reason:
                        current_finish_reason = chunk.finish_reason

            except Exception as exc:
                logger.error("Managed agent stream error: %s", exc, exc_info=True)
                error_data = {
                    "error": {
                        "type": "generation_error",
                        "message": "Managed agent generation failed",
                    }
                }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Handle tool calls: execute tools and loop for next turn
            if tool_call_fragments and current_finish_reason == "tool_calls":
                # Build the assistant message with tool_calls
                sorted_tcs = [
                    tool_call_fragments[i] for i in sorted(tool_call_fragments.keys())
                ]

                # Add assistant message with tool_calls to conversation
                from openjarvis.core.types import ToolCall as MsgToolCall

                assistant_msg = Message(
                    role=Role.ASSISTANT,
                    content=turn_content or None,
                    tool_calls=[
                        MsgToolCall(
                            id=tc["id"],
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        )
                        for tc in sorted_tcs
                    ],
                )
                messages_for_llm.append(assistant_msg)

                # Execute each tool call and append results. Emit
                # tool_call_start/tool_call_end around each call so the UI
                # can render them live (same event names as the main chat
                # in stream_bridge.py).
                import time as _time

                for tc in sorted_tcs:
                    tool_name = tc["function"]["name"]
                    tool_args = tc["function"]["arguments"]
                    tool_result_content = f"Tool '{tool_name}' not available"
                    tool_succeeded = False

                    _start_payload = json.dumps(
                        {"tool": tool_name, "arguments": tool_args}
                    )
                    yield f"event: tool_call_start\ndata: {_start_payload}\n\n"
                    try:
                        manager.add_learning_log(
                            agent_id,
                            "tool_call",
                            f"Calling {tool_name}: {tool_args[:80]}",
                            {"tool": tool_name, "arguments": tool_args or ""},
                        )
                    except Exception as _tc_exc:
                        logger.warning("Log tool_call failed: %s", _tc_exc)
                    tool_start_ms = _time.monotonic() * 1000

                    try:
                        # Try MCP adapter first (external tools)
                        mcp_adapter = mcp_adapters.get(tool_name)
                        if (
                            mcp_adapter is not None
                            and _managed_remote_tool_name_allowed(tool_name)
                        ):
                            try:
                                parsed_args = json.loads(tool_args) if tool_args else {}
                            except (json.JSONDecodeError, TypeError):
                                parsed_args = {}
                            result = mcp_adapter.execute(**parsed_args)
                            tool_succeeded = bool(result.success)
                            tool_result_content = (
                                result.content
                                if tool_succeeded
                                else f"Tool '{tool_name}' failed."
                            )
                        else:
                            # Try to use ToolExecutor if tools are configured
                            from openjarvis.core.registry import ToolRegistry
                            from openjarvis.tools._stubs import (
                                ToolCall as StubToolCall,
                            )
                            from openjarvis.tools._stubs import (
                                ToolExecutor,
                            )

                            tool_cls = ToolRegistry.get(tool_name)
                            if tool_cls is not None and _managed_tool_allowed(tool_cls):
                                # Inject backend / channel / engine the same
                                # way cli/ask.py does, else memory_* / channel_*
                                # / llm tools fail with "No backend configured"
                                # on every call (#395).
                                tool_instance = _instantiate_managed_tool(
                                    tool_cls,
                                    tool_name,
                                    engine=engine,
                                    model=model,
                                    app_state=app_state,
                                )
                                executor = ToolExecutor(
                                    tools=[tool_instance],
                                    bus=bus,
                                    interactive=False,
                                    confirm_callback=None,
                                )
                                result = executor.execute(
                                    StubToolCall(
                                        id=tc["id"],
                                        name=tool_name,
                                        arguments=tool_args,
                                    ),
                                )
                                tool_succeeded = bool(result.success)
                                tool_result_content = (
                                    result.content
                                    if tool_succeeded
                                    else f"Tool '{tool_name}' failed."
                                )
                            else:
                                logger.warning(
                                    "Tool '%s' not found in registry or MCP adapters",
                                    tool_name,
                                )
                    except Exception as tool_exc:
                        logger.error(
                            "Tool execution error for %s: %s",
                            tool_name,
                            tool_exc,
                            exc_info=True,
                        )
                        tool_result_content = f"Tool '{tool_name}' failed."

                    tool_latency_ms = (_time.monotonic() * 1000) - tool_start_ms
                    collected_tool_calls.append(
                        {
                            "tool": tool_name,
                            "arguments": tool_args[:4096],
                            "result": tool_result_content[:1024],
                            "success": tool_succeeded,
                            "latency": tool_latency_ms,
                        }
                    )
                    # Update the shared persist state so mid-stream
                    # disconnects still capture already-executed tools.
                    persist_state["tool_calls"] = list(collected_tool_calls)
                    try:
                        _ok = "succeeded" if tool_succeeded else "failed"
                        _clen = len(tool_result_content) if tool_result_content else 0
                        manager.add_learning_log(
                            agent_id,
                            "tool_result",
                            f"{tool_name} {_ok} ({_clen} chars)",
                            {
                                "tool": tool_name,
                                "success": tool_succeeded,
                                "output_length": _clen,
                            },
                        )
                    except Exception as _tr_exc:
                        logger.warning("Log tool_result failed: %s", _tr_exc)
                    _end_payload = json.dumps(
                        {
                            "tool": tool_name,
                            "success": tool_succeeded,
                            "latency": tool_latency_ms,
                            "result": tool_result_content[:1024],
                        }
                    )
                    yield f"event: tool_call_end\ndata: {_end_payload}\n\n"

                    # Add tool result message to conversation
                    messages_for_llm.append(
                        Message(
                            role=Role.TOOL,
                            content=tool_result_content,
                            tool_call_id=tc["id"],
                            name=tool_name,
                        )
                    )

                # Continue to next turn (loop back to stream_full)
                collected_content += turn_content
                # Mirror to shared state so BackgroundTask can persist
                # even if the client disconnects mid-stream.
                persist_state["content"] = collected_content
                persist_state["tool_calls"] = list(collected_tool_calls)
                continue

            # No tool calls — this is the final response
            collected_content += turn_content
            persist_state["content"] = collected_content
            persist_state["tool_calls"] = list(collected_tool_calls)
            persist_state["terminal_success"] = bool(
                collected_content.strip() and current_finish_reason == "stop"
            )
            break

        # Commit before the completion marker. The BackgroundTask remains the
        # disconnect fallback for partial streams, while the normal path never
        # acknowledges completion before its exact user/assistant link exists.
        _persist_final()

        if not persist_state["terminal_success"]:
            yield (
                'data: {"error":{"type":"empty_or_incomplete_response",'
                '"message":"Managed agent returned no complete response"}}\n\n'
            )
            yield "data: [DONE]\n\n"
            return

        # Final chunk with finish_reason
        final_data = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
        yield f"data: {json.dumps(final_data)}\n\n"
        yield "data: [DONE]\n\n"

    async def generate():
        """Guarantee terminal persistence even when ASGI raises ClientDisconnect."""

        try:
            async for event in _generate_body():
                yield event
        finally:
            _finalize_request()

    def _finalize_request() -> None:
        """Persist and release the tick once across EOF, error and disconnect."""

        with finalize_lock:
            _persist_final()
            if not persist_state["tick_released"]:
                manager.end_tick(
                    agent_id,
                    tick_token,
                    status=("idle" if persist_state["terminal_success"] else "error"),
                )
                persist_state["tick_released"] = True

    from starlette.background import BackgroundTask

    class _DisconnectSafeStreamingResponse(StreamingResponse):
        async def __call__(self, scope, receive, send) -> None:
            try:
                await super().__call__(scope, receive, send)
            finally:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        logger.warning(
                            "Failed to close managed-agent stream iterator",
                            exc_info=True,
                        )
                _finalize_request()

    return _DisconnectSafeStreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        background=BackgroundTask(_persist_final),
    )


def create_agent_manager_router(
    manager: AgentManager,
) -> Tuple[APIRouter, APIRouter, APIRouter, APIRouter, APIRouter]:
    """Create FastAPI routers with agent management endpoints.

    Returns a 5-tuple:
    ``(agents_router, templates_router, global_router, tools_router, sendblue_router)``.
    """
    agents_router = APIRouter(prefix="/v1/managed-agents", tags=["managed-agents"])
    templates_router = APIRouter(prefix="/v1/templates", tags=["templates"])

    # ── Agent lifecycle ──────────────────────────────────────

    @agents_router.get("")
    async def list_agents():
        return {"agents": manager.list_agents()}

    @agents_router.post("")
    async def create_agent(req: CreateAgentRequest, request: Request):
        effective_config = dict(req.config or {})
        if req.template_id:
            template = next(
                (
                    item
                    for item in manager.list_templates()
                    if item.get("id") == req.template_id
                ),
                None,
            )
            if template is None:
                raise HTTPException(status_code=404, detail="Template not found")
            effective_config = {
                key: value
                for key, value in template.items()
                if key not in {"id", "name", "description", "source"}
            }
            effective_config.update(req.config or {})
        try:
            _managed_runtime_values(effective_config)
            _validate_managed_tool_config(effective_config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if req.template_id:
            agent = manager.create_from_template(
                req.template_id, req.name, overrides=req.config
            )
        else:
            agent = manager.create_agent(
                name=req.name, agent_type=req.agent_type, config=req.config
            )

        # Register with scheduler if cron/interval
        scheduler = getattr(request.app.state, "agent_scheduler", None)
        sched_type = (req.config or {}).get("schedule_type", "manual")
        if scheduler and sched_type in ("cron", "interval"):
            scheduler.register_agent(agent["id"])

        return agent

    @agents_router.get("/{agent_id}")
    async def get_agent(agent_id: str):
        agent = manager.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        return agent

    @agents_router.patch("/{agent_id}")
    async def update_agent(agent_id: str, req: UpdateAgentRequest):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        kwargs: Dict[str, Any] = {}
        if req.name is not None:
            kwargs["name"] = req.name
        if req.agent_type is not None:
            kwargs["agent_type"] = req.agent_type
        if req.config is not None:
            try:
                _managed_runtime_values(req.config)
                _validate_managed_tool_config(req.config)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            kwargs["config"] = req.config
        return manager.update_agent(agent_id, **kwargs)

    @agents_router.delete("/{agent_id}")
    async def delete_agent(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            manager.delete_agent(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "archived"}

    @agents_router.post("/{agent_id}/pause")
    async def pause_agent(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            manager.pause_agent(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "paused"}

    @agents_router.post("/{agent_id}/resume")
    async def resume_agent(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            manager.resume_agent(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "idle"}

    @agents_router.post("/{agent_id}/run")
    async def run_agent(agent_id: str, request: Request):
        import threading

        agent = manager.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        # Acquire tick BEFORE spawning thread — prevents race
        try:
            tick_token = manager.start_tick(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Re-use the server's engine + model so we don't pick a
        # random model from Ollama's list.
        server_engine = getattr(request.app.state, "engine", None)
        server_model = getattr(request.app.state, "model", "")
        server_config = getattr(request.app.state, "config", None)

        def _run_tick():
            try:
                from openjarvis.agents.executor import AgentExecutor
                from openjarvis.core.events import get_event_bus

                _ts = getattr(request.app.state, "trace_store", None)
                executor = AgentExecutor(
                    manager=manager,
                    event_bus=get_event_bus(),
                    trace_store=_ts,
                )
                system = _make_lightweight_system(
                    server_engine,
                    server_model,
                    server_config,
                    http_boundary=True,
                )
                executor.set_system(system)
                # The route handler above already called start_tick() to
                # serialize concurrent POSTs; tell the executor not to
                # re-acquire, otherwise it bails on its own guard and the
                # tick never runs.
                executor.execute_tick(
                    agent_id,
                    lock_already_held=True,
                    tick_token=tick_token,
                )
            except Exception as exc:
                logger.error(
                    "Run-tick failed for agent %s: %s",
                    agent_id,
                    exc,
                    exc_info=True,
                )
                manager.end_tick(agent_id, tick_token, status="error")

        worker = threading.Thread(target=_run_tick, daemon=True)
        try:
            worker.start()
        except Exception as exc:
            manager.end_tick(agent_id, tick_token, status="error")
            raise HTTPException(
                status_code=503,
                detail="Managed-agent worker unavailable",
            ) from exc
        return {"status": "running", "agent_id": agent_id}

    # ── Recover ──────────────────────────────────────────────

    @agents_router.post("/{agent_id}/recover")
    def recover_agent(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            checkpoint = manager.recover_agent(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"recovered": True, "checkpoint": checkpoint}

    # ── Tasks ────────────────────────────────────────────────

    @agents_router.get("/{agent_id}/tasks")
    async def list_tasks(agent_id: str, status: Optional[str] = None):
        return {"tasks": manager.list_tasks(agent_id, status=status)}

    @agents_router.post("/{agent_id}/tasks")
    async def create_task(agent_id: str, req: CreateTaskRequest):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        return manager.create_task(agent_id, description=req.description)

    @agents_router.get("/{agent_id}/tasks/{task_id}")
    async def get_task(agent_id: str, task_id: str):
        task = manager._get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @agents_router.patch("/{agent_id}/tasks/{task_id}")
    async def update_task(agent_id: str, task_id: str, req: UpdateTaskRequest):
        kwargs: Dict[str, Any] = {}
        if req.description is not None:
            kwargs["description"] = req.description
        if req.status is not None:
            kwargs["status"] = req.status
        if req.progress is not None:
            kwargs["progress"] = req.progress
        if req.findings is not None:
            kwargs["findings"] = req.findings
        return manager.update_task(task_id, **kwargs)

    @agents_router.delete("/{agent_id}/tasks/{task_id}")
    async def delete_task(agent_id: str, task_id: str):
        manager.delete_task(task_id)
        return {"status": "deleted"}

    # ── Channel bindings ─────────────────────────────────────

    @agents_router.get("/{agent_id}/channels")
    async def list_channels(agent_id: str):
        return {"bindings": manager.list_channel_bindings(agent_id)}

    @agents_router.post("/{agent_id}/channels")
    async def bind_channel(
        agent_id: str,
        req: BindChannelRequest,
        request: Request,
    ):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        binding = manager.bind_channel(
            agent_id,
            channel_type=req.channel_type,
            config=req.config,
            routing_mode=req.routing_mode,
        )

        # Start iMessage daemon if binding iMessage
        if req.channel_type == "imessage":
            identifier = (req.config or {}).get("identifier", "")
            if identifier:
                try:
                    from openjarvis.channels.imessage_daemon import (
                        is_running,
                        run_daemon,
                    )

                    if not is_running():
                        import threading

                        engine = getattr(request.app.state, "engine", None)
                        if engine:
                            from openjarvis.server.agent_manager_routes import (
                                _build_deep_research_tools,
                            )

                            tools = _build_deep_research_tools(engine=engine, model="")
                            if tools:
                                from openjarvis.agents.deep_research import (
                                    DeepResearchAgent,
                                )

                                agent_inst = DeepResearchAgent(
                                    engine=engine,
                                    model=getattr(engine, "_model", ""),
                                    tools=tools,
                                    interactive=True,
                                    confirm_callback=lambda _prompt: True,
                                )

                                def handler(text: str) -> str:
                                    result = agent_inst.run(text)
                                    return result.content or "No results."

                                t = threading.Thread(
                                    target=run_daemon,
                                    kwargs={
                                        "chat_identifier": identifier,
                                        "handler": handler,
                                    },
                                    daemon=True,
                                )
                                t.start()
                except Exception as exc:
                    logger.warning("Failed to start iMessage daemon: %s", exc)

        # Initialize SendBlue channel if binding sendblue
        if req.channel_type == "sendblue":
            config = req.config or {}
            api_key_id = config.get("api_key_id", "")
            api_secret_key = config.get("api_secret_key", "")
            from_number = config.get("from_number", "")
            if api_key_id and api_secret_key:
                try:
                    from openjarvis.channels.sendblue import (
                        SendBlueChannel,
                    )

                    sb_channel = SendBlueChannel(
                        api_key_id=api_key_id,
                        api_secret_key=api_secret_key,
                        from_number=from_number,
                    )
                    sb_channel.connect()
                    # Store on app state so webhook route can use it
                    request.app.state.sendblue_channel = sb_channel

                    # Create or update the channel bridge
                    bridge = getattr(request.app.state, "channel_bridge", None)
                    if bridge and hasattr(bridge, "_channels"):
                        bridge._channels["sendblue"] = sb_channel
                    else:
                        # Create a new ChannelBridge with DeepResearch
                        from openjarvis.server.channel_bridge import (
                            ChannelBridge,
                        )
                        from openjarvis.server.session_store import (
                            SessionStore,
                        )

                        session_store = SessionStore()
                        engine = getattr(request.app.state, "engine", None)
                        dr_agent = None
                        if engine:
                            from openjarvis.server.agent_manager_routes import (
                                _build_deep_research_tools as _bdr,
                            )

                            tools = _bdr(engine=engine, model="")
                            if tools:
                                from openjarvis.agents.deep_research import (
                                    DeepResearchAgent,
                                )

                                model_name = getattr(engine, "_model", "") or getattr(
                                    request.app.state,
                                    "model",
                                    "",
                                )
                                dr_agent = DeepResearchAgent(
                                    engine=engine,
                                    model=model_name,
                                    tools=tools,
                                    interactive=True,
                                    confirm_callback=lambda _prompt: True,
                                )
                        bus = getattr(request.app.state, "bus", None)
                        if bus is None:
                            from openjarvis.core.events import EventBus

                            bus = EventBus()
                        bridge = ChannelBridge(
                            channels={"sendblue": sb_channel},
                            session_store=session_store,
                            bus=bus,
                            agent_manager=manager,
                            deep_research_agent=dr_agent,
                        )
                        request.app.state.channel_bridge = bridge

                    logger.info(
                        "SendBlue channel connected: %s",
                        from_number,
                    )
                except Exception as exc:
                    logger.warning("Failed to init SendBlue channel: %s", exc)

        # Start Slack via slack-bolt Socket Mode
        if req.channel_type == "slack":
            config = req.config or {}
            bot_token = config.get("bot_token", "")
            app_token = config.get("app_token", "")
            if bot_token and app_token:
                try:
                    from openjarvis.channels.slack_daemon import (
                        start_slack_daemon,
                    )
                    from openjarvis.channels.slack_daemon import (
                        stop_daemon as stop_slack,
                    )

                    # Stop any existing daemon first
                    stop_slack()

                    # Spawn as subprocess (reliable)
                    srv_model = (
                        getattr(
                            getattr(
                                request.app.state,
                                "engine",
                                None,
                            ),
                            "_model",
                            "qwen3.5:9b",
                        )
                        or "qwen3.5:9b"
                    )
                    pid = start_slack_daemon(
                        bot_token=bot_token,
                        app_token=app_token,
                        model=srv_model,
                    )
                    logger.info(
                        "Slack daemon started (PID %d)",
                        pid,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to start Slack: %s",
                        exc,
                    )

        return binding

    @agents_router.delete("/{agent_id}/channels/{binding_id}")
    async def unbind_channel(
        agent_id: str,
        binding_id: str,
        request: Request,
    ):
        try:
            binding = manager._get_binding(binding_id)
            if binding:
                ch_type = binding.get("channel_type")
                if ch_type == "imessage":
                    from openjarvis.channels.imessage_daemon import (
                        stop_daemon,
                    )

                    stop_daemon()
                elif ch_type == "slack":
                    from openjarvis.channels.slack_daemon import (
                        stop_daemon as stop_slack_daemon,
                    )

                    stop_slack_daemon()
        except Exception:
            pass
        manager.unbind_channel(binding_id)
        return {"status": "unbound"}

    # ── Messaging ────────────────────────────────────────────

    @agents_router.get("/{agent_id}/messages")
    def list_messages(agent_id: str):
        return {"messages": _sanitize_managed_history(manager.list_messages(agent_id))}

    @agents_router.post("/{agent_id}/messages")
    async def send_message(agent_id: str, req: SendMessageRequest, request: Request):
        agent_record = manager.get_agent(agent_id)
        if not agent_record:
            raise HTTPException(status_code=404, detail="Agent not found")
        if agent_record["status"] == "archived":
            raise HTTPException(status_code=409, detail="Agent is archived")

        if req.stream or req.mode == "immediate":
            try:
                _managed_runtime_values(agent_record.get("config", {}))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        executes_now = req.stream or req.mode == "immediate"
        engine = getattr(request.app.state, "engine", None) if executes_now else None
        bus = getattr(request.app.state, "bus", None) if executes_now else None
        if executes_now and engine is None:
            raise HTTPException(
                status_code=503,
                detail="Engine not available for streaming",
            )

        tick_token: str | None = None
        if executes_now:
            try:
                tick_token = manager.start_tick(agent_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            # Immediate execution enters the DB directly as ``processing``;
            # there is no observable pending window for the scheduler to steal.
            if executes_now:
                msg = manager.send_claimed_message(
                    agent_id,
                    req.content,
                    mode=req.mode,
                )
                claimed = msg
            else:
                msg = manager.send_message(agent_id, req.content, mode=req.mode)
                claimed = None
        except Exception:
            if tick_token is not None:
                manager.end_tick(agent_id, tick_token)
            raise

        if not executes_now:
            return msg
        assert tick_token is not None

        if not req.stream:
            import threading

            from openjarvis.agents.executor import AgentExecutor
            from openjarvis.core.events import get_event_bus

            def _immediate_tick() -> None:
                try:
                    executor = AgentExecutor(
                        manager=manager,
                        event_bus=bus or get_event_bus(),
                        trace_store=getattr(request.app.state, "trace_store", None),
                    )
                    executor.set_system(
                        _make_lightweight_system(
                            engine,
                            getattr(request.app.state, "model", ""),
                            getattr(request.app.state, "config", None),
                            http_boundary=True,
                        )
                    )
                    executor.execute_tick(
                        agent_id,
                        lock_already_held=True,
                        tick_token=tick_token,
                        claimed_message=claimed,
                    )
                except Exception:
                    logger.error(
                        "Immediate managed-agent tick failed",
                        exc_info=True,
                    )
                    manager.mark_message_failed(msg["id"])
                    manager.end_tick(agent_id, tick_token)

            worker = threading.Thread(target=_immediate_tick, daemon=True)
            try:
                worker.start()
            except Exception as exc:
                manager.mark_message_failed(msg["id"])
                manager.end_tick(agent_id, tick_token)
                raise HTTPException(
                    status_code=503,
                    detail="Immediate managed-agent worker unavailable",
                ) from exc
            return msg

        try:
            return await _stream_managed_agent(
                manager=manager,
                agent_record=agent_record,
                user_content=req.content,
                message_id=msg["id"],
                tick_token=tick_token,
                engine=engine,
                bus=bus,
                app_state=request.app.state,
            )
        except ValueError as exc:
            manager.mark_message_failed(msg["id"])
            manager.end_tick(agent_id, tick_token, status="error")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            manager.mark_message_failed(msg["id"])
            manager.end_tick(agent_id, tick_token, status="error")
            raise

    # ── State inspection ─────────────────────────────────────

    @agents_router.get("/{agent_id}/state")
    def get_agent_state(agent_id: str):
        agent = manager.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {
            "agent": agent,
            "tasks": manager.list_tasks(agent_id),
            "channels": manager.list_channel_bindings(agent_id),
            "messages": _sanitize_managed_history(manager.list_messages(agent_id)),
            "checkpoint": manager.get_latest_checkpoint(agent_id),
        }

    # ── Learning ─────────────────────────────────────────────

    @agents_router.get("/{agent_id}/learning")
    def get_learning_log(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        return {"learning_log": manager.list_learning_log(agent_id)}

    @agents_router.post("/{agent_id}/learning/run")
    def trigger_learning(agent_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        from openjarvis.core.events import EventType, get_event_bus

        bus = get_event_bus()
        bus.publish(EventType.AGENT_LEARNING_STARTED, {"agent_id": agent_id})
        return {"status": "triggered"}

    # ── Traces ───────────────────────────────────────────────

    @agents_router.get("/{agent_id}/traces")
    def list_traces(agent_id: str, limit: int = 20):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            from openjarvis.core.config import load_config
            from openjarvis.core.paths import get_config_dir
            from openjarvis.traces.store import TraceStore

            config = load_config()
            store = TraceStore(
                config.traces.db_path or str(get_config_dir() / "traces.db")
            )
            traces = store.list_traces(agent=agent_id, limit=limit)
            return {
                "traces": [
                    {
                        "id": t.trace_id,
                        "outcome": t.outcome,
                        "duration": t.total_latency_seconds,
                        "started_at": t.started_at,
                        "steps": len(t.steps),
                        "metadata": t.metadata,
                    }
                    for t in traces
                ]
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @agents_router.get("/{agent_id}/traces/{trace_id}")
    def get_trace(agent_id: str, trace_id: str):
        if not manager.get_agent(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        try:
            from openjarvis.core.config import load_config
            from openjarvis.core.paths import get_config_dir
            from openjarvis.traces.store import TraceStore

            config = load_config()
            store = TraceStore(
                config.traces.db_path or str(get_config_dir() / "traces.db")
            )
            trace = store.get(trace_id)
            if (
                trace is None
                or trace.agent != agent_id
                or _trace_has_disallowed_managed_tool(trace)
            ):
                raise HTTPException(status_code=404, detail="Trace not found")
            return {
                "id": trace.trace_id,
                "agent": trace.agent,
                "outcome": trace.outcome,
                "duration": trace.total_latency_seconds,
                "started_at": trace.started_at,
                "steps": [
                    {
                        "step_type": s.step_type.value,
                        "input": s.input,
                        "output": s.output,
                        "duration": s.duration_seconds,
                        "metadata": s.metadata,
                    }
                    for s in trace.steps
                ],
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Templates ────────────────────────────────────────────

    @templates_router.get("")
    async def list_templates():
        return {"templates": AgentManager.list_templates()}

    @templates_router.post("/{template_id}/instantiate")
    async def instantiate_template(template_id: str, req: CreateAgentRequest):
        templates = manager.list_templates()
        template = next(
            (item for item in templates if item.get("id") == template_id),
            None,
        )
        if template is None:
            raise HTTPException(status_code=404, detail="Template not found")
        merged = {
            key: value
            for key, value in template.items()
            if key not in {"id", "name", "description", "source"}
        }
        merged.update(req.config or {})
        try:
            _managed_runtime_values(merged)
            _validate_managed_tool_config(merged)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return manager.create_from_template(template_id, req.name, overrides=req.config)

    # ── Global agent endpoints ───────────────────────────────

    global_router = APIRouter(tags=["agents-global"])

    @global_router.get("/v1/agents/errors")
    def list_error_agents():
        all_agents = manager.list_agents()
        error_agents = [
            a
            for a in all_agents
            if a["status"] in ("error", "needs_attention", "stalled", "budget_exceeded")
        ]
        return {"agents": error_agents}

    @global_router.get("/v1/agents/health")
    def agents_health():
        all_agents = manager.list_agents()
        from collections import Counter

        counts = Counter(a["status"] for a in all_agents)
        return {
            "total": len(all_agents),
            "by_status": dict(counts),
        }

    @global_router.get("/v1/recommended-model")
    def recommended_model(request: Request):
        engine = getattr(request.app.state, "engine", None)
        if engine is None:
            return {"model": "", "reason": "No engine available"}
        try:
            models = engine.list_models()
        except Exception:
            models = []
        return _pick_recommended_model(models)

    # ── Tools & credentials ──────────────────────────────────

    tools_router = APIRouter(prefix="/v1/tools", tags=["tools"])

    @tools_router.get("")
    def list_tools(request: Request):
        del request
        # Remote MCP and browser meta-tools are disabled at this unauthenticated
        # HTTP boundary. Advertising them would let the UI persist a capability
        # that execution then silently drops.
        return {"tools": build_tools_list()}

    @tools_router.post("/{tool_name}/credentials")
    async def save_tool_credentials(tool_name: str, request: Request):
        from openjarvis.core.credentials import save_credential

        body = await request.json()
        saved = []
        for key, value in body.items():
            save_credential(tool_name, key, value)
            saved.append(key)
        return {"saved": saved}

    @tools_router.delete("/{tool_name}/credentials/{key}")
    def remove_tool_credential(tool_name: str, key: str):
        from openjarvis.core.credentials import delete_credential

        delete_credential(tool_name, key)
        return {"deleted": key}

    @tools_router.get("/{tool_name}/credentials/status")
    def credential_status(tool_name: str):
        from openjarvis.core.credentials import get_credential_status

        return get_credential_status(tool_name)

    # ── SendBlue auto-setup helpers ─────────────────────────

    sendblue_router = APIRouter(prefix="/v1/channels/sendblue", tags=["sendblue"])

    @sendblue_router.post("/verify")
    async def sendblue_verify(request: Request):
        """Verify SendBlue credentials and return assigned phone lines."""
        body = await request.json()
        api_key_id = body.get("api_key_id", "")
        api_secret_key = body.get("api_secret_key", "")
        if not api_key_id or not api_secret_key:
            raise HTTPException(
                status_code=400,
                detail="api_key_id and api_secret_key are required",
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.sendblue.co/api/lines",
                    headers={
                        "sb-api-key-id": api_key_id,
                        "sb-api-secret-key": api_secret_key,
                    },
                )
            if resp.status_code == 401:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid SendBlue credentials",
                )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"SendBlue API error: {resp.text[:200]}",
                )
            data = resp.json()
            # data might be a list of lines or {"lines": [...]}
            lines = (
                data
                if isinstance(data, list)
                else data.get("lines", data.get("data", []))
            )
            numbers = []
            for line in lines:
                if isinstance(line, str):
                    num = line
                elif isinstance(line, dict):
                    num = (
                        line.get("number")
                        or line.get("phone_number")
                        or line.get("from_number")
                    )
                else:
                    num = None
                if num:
                    numbers.append(num)
            return {
                "valid": True,
                "numbers": numbers,
                "raw": data,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach SendBlue: {exc}",
            )

    @sendblue_router.post("/register-webhook")
    async def sendblue_register_webhook(request: Request):
        """Auto-register the /webhooks/sendblue endpoint with SendBlue."""
        body = await request.json()
        api_key_id = body.get("api_key_id", "")
        api_secret_key = body.get("api_secret_key", "")
        webhook_url = body.get("webhook_url", "")
        if not webhook_url:
            raise HTTPException(
                status_code=400,
                detail="webhook_url is required",
            )

        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.sendblue.co/api/account/webhooks",
                    headers={
                        "sb-api-key-id": api_key_id,
                        "sb-api-secret-key": api_secret_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "receive": webhook_url,
                    },
                )
            return {
                "registered": resp.status_code < 300,
                "status": resp.status_code,
                "response": resp.json() if resp.status_code < 300 else resp.text[:200],
            }
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to register webhook: {exc}",
            )

    @sendblue_router.post("/test")
    async def sendblue_test(request: Request):
        """Send a test message via SendBlue to verify the setup works."""
        body = await request.json()
        api_key_id = body.get("api_key_id", "")
        api_secret_key = body.get("api_secret_key", "")
        from_number = body.get("from_number", "")
        to_number = body.get("to_number", "")
        if not to_number:
            raise HTTPException(
                status_code=400,
                detail="to_number is required",
            )

        import httpx

        try:
            payload: Dict[str, str] = {
                "number": to_number,
                "content": (
                    "Hello from your OpenJarvis agent! "
                    "Text this number anytime to search your "
                    "personal data. Reply with any question to try it."
                ),
            }
            if from_number:
                payload["from_number"] = from_number

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.sendblue.co/api/send-message",
                    headers={
                        "sb-api-key-id": api_key_id,
                        "sb-api-secret-key": api_secret_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            return {
                "sent": resp.status_code < 300,
                "status": resp.status_code,
                "response": resp.json() if resp.status_code < 300 else resp.text[:200],
            }
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to send test message: {exc}",
            )

    @sendblue_router.get("/health")
    async def sendblue_health(request: Request):
        """Check if the SendBlue channel bridge is wired and ready."""
        sb = getattr(request.app.state, "sendblue_channel", None)
        bridge = getattr(request.app.state, "channel_bridge", None)
        has_bridge = bridge is not None and (
            hasattr(bridge, "_channels") and "sendblue" in bridge._channels
        )
        return {
            "channel_connected": sb is not None,
            "bridge_wired": has_bridge,
            "ready": sb is not None and has_bridge,
        }

    return (
        agents_router,
        templates_router,
        global_router,
        tools_router,
        sendblue_router,
    )
