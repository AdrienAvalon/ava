"""POST /v1/ava/speak — synthesize text to audio via the configured TTS backend.

Returns raw audio bytes with the correct `audio/*` MIME type, usable directly
from the browser as an `<audio>` source or `new Audio(blob)`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Ensure TTS backends self-register (kokoro, cartesia, openai_tts, ...)
import openjarvis.speech  # noqa: F401
from openjarvis.core.registry import TTSRegistry


router = APIRouter(prefix="/v1/ava", tags=["ava"])


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice_id: str = Field("ff_siwis", description="Voice identifier for the backend")
    backend: str = Field("kokoro", description="TTS backend key (kokoro, cartesia, openai_tts)")
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Playback speed multiplier")
    output_format: str = Field("wav", description="Preferred audio format (wav, mp3)")


@router.post("/speak")
def speak(req: SpeakRequest) -> Response:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    if not TTSRegistry.contains(req.backend):
        raise HTTPException(
            status_code=404,
            detail=f"tts backend '{req.backend}' not registered. Available: {list(TTSRegistry.keys())}",
        )

    backend_cls = TTSRegistry.get(req.backend)
    backend = backend_cls()
    result = backend.synthesize(
        text,
        voice_id=req.voice_id,
        speed=req.speed,
        output_format=req.output_format,
    )

    fmt = (result.format or req.output_format or "wav").lower()
    mime_by_fmt = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
    }
    mime = mime_by_fmt.get(fmt, "application/octet-stream")

    return Response(
        content=result.audio,
        media_type=mime,
        headers={
            "X-Ava-TTS-Backend": req.backend,
            "X-Ava-TTS-Voice": result.voice_id or req.voice_id,
            "X-Ava-TTS-Duration": f"{result.duration_seconds:.2f}",
        },
    )


@router.get("/speak/health")
def speak_health() -> dict:
    backends = list(TTSRegistry.keys())
    return {
        "available_backends": backends,
        "default_backend": "kokoro",
        "default_voice": "ff_siwis",
    }
