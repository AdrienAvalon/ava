"""Patch : charger agent.system_prompt_path dans agent.default_system_prompt.

OpenJarvis upstream expose un champ agent.system_prompt_path dans la config
mais seul le loader de recipes YAML le lit. Le BaseAgent, lui, utilise
agent.default_system_prompt. Ce patch comble le gap : si system_prompt_path
est défini et que le fichier existe, on le lit et on écrase
default_system_prompt au moment du premier accès à la config.
"""

from __future__ import annotations

import logging
from pathlib import Path

from openjarvis.core import config as _cfg_mod

log = logging.getLogger(__name__)

_PATCH_MARKER = "_ava_system_prompt_patched"
_original_load = _cfg_mod.load_config


def _patched_load(*args, **kwargs):
    cfg = _original_load(*args, **kwargs)
    try:
        agent = getattr(cfg, "agent", None)
        if agent is None:
            return cfg
        path_str = getattr(agent, "system_prompt_path", "") or ""
        if not path_str:
            return cfg
        p = Path(path_str).expanduser()
        if not p.exists():
            log.warning("ava: system_prompt_path %s does not exist", p)
            return cfg
        content = p.read_text(encoding="utf-8").strip()
        if content:
            agent.default_system_prompt = content
            log.info("ava: loaded persona from %s (%d chars)", p, len(content))
    except Exception:
        log.exception("ava: failed to load system_prompt_path")
    return cfg


if not getattr(_cfg_mod.load_config, _PATCH_MARKER, False):
    setattr(_patched_load, _PATCH_MARKER, True)
    _patched_load.__wrapped__ = _original_load
    _cfg_mod.load_config = _patched_load
