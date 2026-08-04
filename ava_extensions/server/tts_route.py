"""POST /v1/ava/speak — synthesize text to audio via the configured TTS backend.

Returns raw audio bytes with the correct `audio/*` MIME type, usable directly
from the browser as an `<audio>` source or `new Audio(blob)`.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Ensure TTS backends self-register (kokoro, cartesia, openai_tts, ...)
import openjarvis.speech  # noqa: F401
from openjarvis.core.registry import TTSRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/ava", tags=["ava"])


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str = Field("ff_siwis", description="Voice identifier for the backend")
    backend: str = Field(
        "kokoro-fr",
        description="TTS backend key (kokoro-fr, kokoro, cartesia, openai_tts)",
    )
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
        "default_backend": "kokoro-fr",
        "default_voice": "ff_siwis",
    }


# === Persona ===
# Exposes the current Ava system prompt so the frontend can inject it as a
# system message. The OpenAI-compat /v1/chat/completions streaming path does
# not apply the agent's configured system prompt; sending it explicitly from
# the client is the simplest reliable fix.
_PERSONA_LOCK = threading.Lock()
_PERSONA_CACHE: dict[str, object] = {"mtime": 0.0, "text": ""}


def _load_persona() -> str:
    """Load the Ava persona system prompt, respecting config.agent.system_prompt_path."""
    try:
        from openjarvis.core.config import load_config

        cfg = load_config()
        path = getattr(getattr(cfg, "agent", None), "system_prompt_path", None)
    except Exception:
        path = None
    if not path:
        path = os.path.expanduser("~/ava/ava_extensions/identity/system_prompts/ava.md")
    p = Path(path)
    if not p.exists():
        return ""
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _PERSONA_LOCK:
        if mtime == _PERSONA_CACHE["mtime"]:
            return str(_PERSONA_CACHE["text"])
        text = p.read_text(encoding="utf-8").strip()
        _PERSONA_CACHE["mtime"] = mtime
        _PERSONA_CACHE["text"] = text
        return text


@router.get("/persona")
def persona() -> dict:
    text = _load_persona()
    return {"system_prompt": text, "length": len(text)}


def prewarm_kokoro() -> None:
    """Background prewarm of the Kokoro FR backend to hide cold-start cost.

    Called from FastAPI startup hook (see openjarvis.server.app). Logged on
    failure rather than swallowed silently.
    """

    def _worker() -> None:
        try:
            if not TTSRegistry.contains("kokoro-fr"):
                logger.info("prewarm skipped: kokoro-fr backend not registered")
                return
            backend = TTSRegistry.get("kokoro-fr")()
            backend.synthesize("bonjour", voice_id="ff_siwis", speed=1.0)
            logger.info("kokoro-fr prewarm completed")
        except Exception as exc:
            logger.warning("kokoro-fr prewarm failed: %s", exc)

    threading.Thread(target=_worker, daemon=True, name="kokoro-prewarm").start()


# ══════════════════════════════════════════════════════════════════════════════════════
# Mémoire de conversation — cloisonnée par utilisateur OIDC
# ══════════════════════════════════════════════════════════════════════════════════════
# ⚠ Ces routes vivent ici plutôt que dans un routeur séparé pour une raison pratique :
#   `server/app.py` (fichier AMONT, donc source de conflits à chaque synchronisation)
#   inclut déjà CE routeur. En créer un second obligerait à patcher `app.py` une fois de
#   plus — le dépôt en compte déjà 4, et chacun se paie à chaque merge upstream.


class LigneEntrante(BaseModel):
    """Une ligne de conversation soumise par le client.

    ⚠ MODÈLE PYDANTIC ET NON `await request.json()` — corrigé après revue adversariale.
      Le parsing manuel produisait des **HTTP 500** sur trois entrées banales : un corps
      non-JSON, un corps JSON non-objet, et un élément de liste mal typé. Un modèle rend
      des 422 explicites, et borne les tailles au passage.
      Le fichier contenait déjà `SpeakRequest` avec `max_length=500` juste au-dessus :
      la route conversation ne bornait rien, alors que le motif était sous les yeux.
    """

    role: str = Field(max_length=32)
    texte: str = Field(max_length=8000)
    horodatage: float | None = None


class EnvoiConversation(BaseModel):
    # ⚠ `max_length` sur la liste : sans borne, un client pouvait faire matérialiser un
    #   corps arbitrairement grand en mémoire, sur la boucle d'événements.
    lignes: list[LigneEntrante] = Field(default_factory=list, max_length=50)


# ⚠ CES TROIS ROUTES SONT `def` ET NON `async def` — c'est délibéré, et l'incohérence
#   était réelle : elles font de l'I/O SQLite BLOQUANTE derrière un verrou global, avec
#   un `timeout=10`. En `async def`, FastAPI les exécute directement sur la boucle
#   d'événements : un fichier verrouillé ou un disque lent gelait TOUT le daemon pendant
#   dix secondes — plus de `/speak`, plus de healthcheck. En `def`, FastAPI les délègue
#   au threadpool. C'est d'ailleurs ce que fait `speak` juste au-dessus.


@router.get("/conversation")
def lire_conversation(request: Request) -> dict:
    """Historique de l'utilisateur courant, du plus ancien au plus récent."""
    from ava_extensions.server import conversation as conv

    utilisateur = conv.identite(request.headers)
    if utilisateur is None:
        # ⚠ Pas d'identité → pas d'historique, et surtout PAS de seau commun : c'était
        #   une fuite réelle (deux jetons expirés partageaient la même conversation).
        return {"utilisateur": None, "lignes": []}
    return {"utilisateur": utilisateur, "lignes": conv.lire(utilisateur, limite=400)}


@router.post("/conversation")
def ajouter_conversation(envoi: EnvoiConversation, request: Request) -> dict:
    """Ajoute des lignes à l'historique de l'utilisateur courant.

    ⚠ L'utilisateur n'est JAMAIS pris dans le corps de la requête : il est dérivé du
      jeton. Accepter un champ transmis par le client laisserait n'importe qui écrire
      dans la mémoire d'un autre — le cloisonnement ne vaudrait rien.
    """
    from ava_extensions.server import conversation as conv

    utilisateur = conv.identite(request.headers)
    if utilisateur is None:
        # ⚠ 401 plutôt qu'un écrit mutualisé : mieux vaut perdre la mémoire d'une session
        #   que mélanger celles de deux personnes.
        raise HTTPException(status_code=401, detail="identité absente ou illisible")
    lignes = [x.model_dump() for x in envoi.lignes]
    return {"ecrites": conv.ajouter(utilisateur, lignes)}


@router.delete("/conversation")
def effacer_conversation(request: Request) -> dict:
    """Efface l'historique de l'utilisateur courant — et de lui seul."""
    from ava_extensions.server import conversation as conv

    utilisateur = conv.identite(request.headers)
    if utilisateur is None:
        raise HTTPException(status_code=401, detail="identité absente ou illisible")
    return {"effacees": conv.effacer(utilisateur)}
