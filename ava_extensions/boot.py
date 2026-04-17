"""Ava — boot loader.

Importé depuis src/openjarvis/speech/__init__.py via un try/except ImportError
pour rester optionnel (le package upstream fonctionne toujours sans
ava_extensions).
"""
from __future__ import annotations

# Patches SDK Anthropic (thinking adaptatif + prompt caching)
from ava_extensions.patches import anthropic_enhancements  # noqa: F401

# Backends TTS français
from ava_extensions.backends import kokoro_fr_tts  # noqa: F401
