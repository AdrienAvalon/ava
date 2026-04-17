"""Ava — boot loader.

Importe tous les modules ava_extensions qui ont besoin d enregistrer des
backends au démarrage du daemon OpenJarvis. Importé depuis
src/openjarvis/speech/__init__.py via un try/except ImportError pour rester
optionnel (le package upstream fonctionne toujours sans ava_extensions).
"""
from __future__ import annotations

# Side-effect imports — chaque module s enregistre via son décorateur.
from ava_extensions.backends import kokoro_fr_tts  # noqa: F401
