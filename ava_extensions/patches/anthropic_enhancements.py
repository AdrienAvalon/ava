"""Patches Anthropic SDK : extended thinking adaptatif + prompt caching.

Injecte automatiquement thinking={"type": "adaptive"} et des breakpoints
cache_control ephemeral dans tous les appels messages.create / stream
faits par OpenJarvis via le SDK anthropic. Idempotent : safe à importer
plusieurs fois.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic.resources.messages as _ant_msgs

log = logging.getLogger(__name__)

# Modèles Claude 4.6+ qui supportent adaptive thinking (cf doc API 2026-04)
_ADAPTIVE_THINKING_MODELS = frozenset(
    {
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
)
# Modèles qui rejettent temperature/top_p/top_k avec thinking
_NO_SAMPLING_WITH_THINKING = frozenset({"claude-opus-4-7"})

# ⚠ GÉNÉRATION 5 : `temperature` EST REFUSÉ, TOUJOURS — pas seulement avec thinking.
#   L'API répond `400 invalid_request_error: "temperature is deprecated for this model"`,
#   et OpenJarvis l'envoie systématiquement (`engine/cloud.py`) : **tout appel échoue**.
#   Vécu le 2026-08-04 en passant `default_model` de `claude-sonnet-4-6` à
#   `claude-sonnet-5` — Ava a cessé de répondre d'un coup.
#   ⚠ ET LE SYMPTÔME NE DÉSIGNE PAS LA CAUSE : le navigateur affiche « Error during
#   generation: Client error '400 Bad Request' », l'API rend un `500 Internal Server
#   Error` sans trace, et le journal systemd ne montre rien. Le motif utile est enterré
#   trois couches plus bas, dans le SDK. On part chercher une clé invalide, un quota
#   épuisé, un nom de modèle inexistant — trois pistes fausses.
#   ⚠ Préfixes et non liste exhaustive : `claude-sonnet-5`, `claude-opus-5` et leurs
#   futures variantes datées partagent la contrainte. Énumérer obligerait à éditer ce
#   fichier à chaque sortie, et l'oubli se paierait par une panne TOTALE.
_TEMPERATURE_INTERDITE = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")

_PATCH_MARKER = "_ava_enhanced"


def _enhance_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Injecte thinking + cache_control sur un appel messages.create/stream."""
    model = kwargs.get("model", "") or ""

    # ⚠ AVANT toute autre logique : un `temperature` refusé fait échouer l'appel entier,
    #   quels que soient les autres réglages. On le retire donc en premier.
    if model.startswith(_TEMPERATURE_INTERDITE):
        kwargs.pop("temperature", None)

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
