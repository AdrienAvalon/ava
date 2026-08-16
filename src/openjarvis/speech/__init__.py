"""Speech subsystem — lazy registration of speech backends.

Importing the top-level CLI must not import native audio or numerical stacks. Backend
registration therefore happens only when speech is actually resolved or used.
"""

import importlib
import logging

logger = logging.getLogger(__name__)

_BACKEND_MODULES = {
    "faster-whisper": "faster_whisper",
    "openai": "openai_whisper",
    "deepgram": "deepgram",
    "cartesia": "cartesia_tts",
    "kokoro": "kokoro_tts",
    "openai_tts": "openai_tts",
}


def register_builtin_backends(backend: str | None = None) -> None:
    """Importe seulement le backend demande, ou tous pour un inventaire explicite."""

    modules = (
        tuple(_BACKEND_MODULES.values())
        if backend is None
        else (_BACKEND_MODULES[backend],)
        if backend in _BACKEND_MODULES
        else ()
    )
    for module_name in modules:
        try:
            importlib.import_module(f".{module_name}", __name__)
        except Exception as exc:  # noqa: BLE001 - chaque integration est optionnelle
            logger.warning(
                "speech backend %s indisponible (%s)",
                module_name,
                type(exc).__name__,
            )


__all__ = ["register_builtin_backends"]
