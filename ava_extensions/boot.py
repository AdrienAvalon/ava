"""Ava — boot loader.

Importé depuis src/openjarvis/speech/__init__.py via un try/except ImportError
pour rester optionnel (le package upstream fonctionne toujours sans
ava_extensions).
"""
from __future__ import annotations

# Patches SDK Anthropic (thinking adaptatif + prompt caching)
from ava_extensions.patches import anthropic_enhancements, system_prompt_loader  # noqa: F401

# Backends TTS français
from ava_extensions.backends import kokoro_fr_tts, openai_whisper_ava_stt  # noqa: F401

# Skills personnalisées Avalon
from ava_extensions.skills import avalon_status  # noqa: F401
