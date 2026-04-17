"""Patches Anthropic SDK : extended thinking adaptatif + prompt caching.

Injecte automatiquement thinking={"type": "adaptive"} et des breakpoints
cache_control ephemeral dans tous les appels messages.create / stream
faits par OpenJarvis via le SDK anthropic. Idempotent : safe à importer
plusieurs fois.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import anthropic.resources.messages as _ant_msgs

log = logging.getLogger(__name__)

# Modèles Claude 4.6+ qui supportent adaptive thinking (cf doc API 2026-04)
_ADAPTIVE_THINKING_MODELS = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
})
# Modèles qui rejettent temperature/top_p/top_k avec thinking
_NO_SAMPLING_WITH_THINKING = frozenset({"claude-opus-4-7"})

_PATCH_MARKER = "_ava_enhanced"


def _enhance_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Injecte thinking + cache_control sur un appel messages.create/stream."""
    model = kwargs.get("model", "") or ""

    # --- Extended thinking (adaptive) ---
    if model in _ADAPTIVE_THINKING_MODELS and "thinking" not in kwargs:
        kwargs["thinking"] = {"type": "adaptive"}
        # API constraint (2026-04): temperature must be 1.0 (default) quand
        # thinking adaptive est actif. top_p et top_k doivent aussi être au
        # defaults. On supprime les overrides pour laisser l API prendre ses
        # valeurs par défaut.
        if model in _NO_SAMPLING_WITH_THINKING:
            for k in ("temperature", "top_p", "top_k"):
                kwargs.pop(k, None)
        else:
            # Sonnet 4.6, Opus 4.6 : temperature DOIT être 1.0 avec thinking
            kwargs["temperature"] = 1.0
            # top_p et top_k: API les accepte mais doivent rester aux defaults
            kwargs.pop("top_p", None)
            kwargs.pop("top_k", None)

    # --- Prompt caching : cache_control ephemeral sur le dernier bloc system ---
    # Anthropic SDK 0.79 supporte cache_control sur blocs text, pas au top level.
    # On convertit system=str → [{"type":"text","text":..., "cache_control":...}].
    system = kwargs.get("system")
    if isinstance(system, str) and system:
        kwargs["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    elif isinstance(system, list) and system:
        last = system[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = {"type": "ephemeral"}

    # --- Prompt caching : cache_control ephemeral sur le dernier tool ---
    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" not in last_tool:
            last_tool["cache_control"] = {"type": "ephemeral"}

    return kwargs


def _wrap(method_name: str) -> None:
    """Remplace Messages.<method_name> par une version qui enhance les kwargs."""
    cls = _ant_msgs.Messages
    original = getattr(cls, method_name)
    if getattr(original, _PATCH_MARKER, False):
        return  # déjà patché

    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        _enhance_kwargs(kwargs)
        return original(self, *args, **kwargs)

    setattr(wrapped, _PATCH_MARKER, True)
    wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    wrapped.__name__ = original.__name__
    setattr(cls, method_name, wrapped)
    log.info("ava: patched anthropic.Messages.%s", method_name)


def apply() -> None:
    for name in ("create", "stream"):
        _wrap(name)


apply()
