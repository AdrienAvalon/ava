"""Ava — Kokoro TTS backend français.

Override de l upstream KokoroTTSBackend qui est câblé en lang_code=\"a\"
(US English) : on fournit un backend distinct enregistré sous l ID
\"kokoro-fr\" qui utilise lang_code=\"f\" et la voix validée \"ff_siwis\".
"""
from __future__ import annotations

from typing import List

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.kokoro_tts import KokoroTTSBackend
from openjarvis.speech.tts import TTSResult


@TTSRegistry.register("kokoro-fr")
class KokoroFRTTSBackend(KokoroTTSBackend):
    """Kokoro TTS français — voix ff_siwis par défaut."""

    backend_id = "kokoro-fr"

    def _ensure_pipeline(self) -> None:
        if self._pipeline is not None:
            return
        try:
            from kokoro import KPipeline
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "kokoro package not installed. Install with: pip install kokoro"
            ) from exc
        self._pipeline = KPipeline(lang_code="f")

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "ff_siwis",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        return super().synthesize(
            text,
            voice_id=voice_id,
            speed=speed,
            output_format=output_format,
        )

    def available_voices(self) -> List[str]:
        return ["ff_siwis"]
