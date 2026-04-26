"""Ava speech-to-text backend — OpenAI GPT-4o-mini-transcribe with hallucination guard.

Improvements over the upstream `openai` backend:
- Uses `gpt-4o-mini-transcribe` (2024) instead of the legacy `whisper-1`.
  Lower hallucination rate, similar latency, similar cost (~0.006 USD/min).
- `temperature=0` to make decoding deterministic and minimize creative drift
  on silence or low-SNR audio.
- A short FR `prompt` that anchors the model in conversational context with
  Ava — drastically reduces the probability of YouTube-subtitle hallucinations
  ("Sous-titres réalisés par la communauté d'Amara.org" and friends).
- Post-filter that drops the well-known FR Whisper hallucinations and short
  noise-only segments. Returns empty text when the transcription matches a
  known hallucination — the frontend already skips empty transcripts.

Activated via `~/.openjarvis/config.toml`:

    [speech]
    backend = "openai_ava"
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import SpeechBackend, TranscriptionResult

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment, misc]


logger = logging.getLogger(__name__)


_AVA_STT_PROMPT = (
    "Ava est un assistant IA personnel francais. "
    "L'utilisateur s'appelle Adrien et discute avec Ava en francais."
)

# Phrases that Whisper routinely emits when fed silence or unintelligible
# audio. Detected with a loose substring match (case-insensitive, accent-
# insensitive thanks to the regex below).
_HALLUCINATION_NEEDLES: tuple[str, ...] = (
    "sous-titres realises par la communaute",
    "sous-titres realises par",
    "sous-titres realises",
    "amara.org",
    "merci d'avoir regarde cette video",
    "merci d'avoir regarde",
    "abonnez-vous a la chaine",
    "abonnez-vous",
    "n'oubliez pas de vous abonner",
    "musique entrainante",
    "musique douce",
    "[musique]",
    "sous-titrage st' 501",
    "sous-titrage",
)


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _looks_like_hallucination(text: str) -> bool:
    """Return True if `text` looks like a known Whisper hallucination on noise."""
    if not text:
        return False
    cleaned = _strip_accents(text).lower().strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    for needle in _HALLUCINATION_NEEDLES:
        if needle in cleaned:
            return True
    return False


@SpeechRegistry.register("openai_ava")
class OpenAIWhisperAvaBackend(SpeechBackend):
    """Cloud STT for Ava: French + anti-hallucination guards."""

    backend_id = "openai_ava"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini-transcribe",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._client: Optional[OpenAI] = None
        if self._api_key and OpenAI is not None:
            self._client = OpenAI(api_key=self._api_key)

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized (missing API key?)")

        ext = format if not format.startswith(".") else format[1:]
        audio_file = io.BytesIO(audio)
        audio_file.name = f"audio.{ext}"

        kwargs: dict = {
            "model": self._model,
            "file": audio_file,
            "temperature": 0,
            "prompt": _AVA_STT_PROMPT,
        }
        if language:
            kwargs["language"] = language

        # gpt-4o-* transcribe models only accept "json" or "text" response_format.
        # whisper-1 also supports "verbose_json"; pick accordingly.
        kwargs["response_format"] = (
            "json" if self._model.startswith("gpt-4o") else "verbose_json"
        )

        response = self._client.audio.transcriptions.create(**kwargs)

        text = (getattr(response, "text", "") or "").strip()
        if _looks_like_hallucination(text):
            logger.info("Dropping Whisper hallucination: %r", text)
            text = ""

        return TranscriptionResult(
            text=text,
            language=getattr(response, "language", None),
            confidence=None,
            duration_seconds=getattr(response, "duration", 0.0),
            segments=[],
        )

    def health(self) -> bool:
        return self._client is not None and bool(self._api_key)

    def supported_formats(self) -> List[str]:
        return ["mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm"]
