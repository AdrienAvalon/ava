"""Ava — boot loader.

Importé depuis src/openjarvis/speech/__init__.py via un try/except ImportError
pour rester optionnel (le package upstream fonctionne toujours sans
ava_extensions).
"""

from __future__ import annotations

# Backends TTS français
from ava_extensions.backends import kokoro_fr_tts, openai_whisper_ava_stt  # noqa: F401

# Patches SDK Anthropic (thinking adaptatif + prompt caching)
from ava_extensions.patches import (  # noqa: F401
    anthropic_enhancements,
    system_prompt_loader,
)

# Skills personnalisées Avalon
# ⚠ TOUT NOUVEL OUTIL DOIT ÊTRE IMPORTÉ ICI, sans quoi il n'existe pas. Le décorateur
#   `@ToolRegistry.register` ne s'exécute qu'au chargement du module : un fichier déposé
#   dans `skills/` mais jamais importé est du code mort que rien ne signale — le registre
#   ne s'en plaint pas, l'outil est simplement absent de la liste proposée au modèle.
from ava_extensions.skills import (
    avalon_status,  # noqa: F401
    home_assistant,  # noqa: F401
)
