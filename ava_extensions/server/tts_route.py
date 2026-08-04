"""POST /v1/ava/speak — synthesize text to audio via the configured TTS backend.

Returns raw audio bytes with the correct `audio/*` MIME type, usable directly
from the browser as an `<audio>` source or `new Audio(blob)`.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Ensure TTS backends self-register (kokoro, cartesia, openai_tts, ...)
import openjarvis.speech  # noqa: F401
from openjarvis.core.registry import TTSRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/ava", tags=["ava"])


# ⚠ FORMATS ADMIS — `output_format` n'avait AUCUNE contrainte jusqu'au 2026-08-04,
#   contrairement à `text` (min/max_length) et `speed` (ge/le) juste à côté : le motif
#   était sous les yeux. La valeur descend jusqu'à `sf.write(..., format=fmt.upper())`
#   dans `kokoro_tts.py`, donc `{"output_format": "zzz"}` faisait lever `soundfile`
#   dans une route sans `try/except` → HTTP 500 nu. Un 422 dit au client ce qu'il a
#   fait de travers ; un 500 lui fait croire que le serveur est cassé.
#   ⚠ Ce dict est la SOURCE UNIQUE : le `Literal` du champ doit lui rester aligné, et
#   un test le vérifie (`test_tts_route.py`). Deux listes de formats divergeraient —
#   on accepterait un format qu'on ne saurait pas étiqueter, et le navigateur
#   recevrait de l'audio en `application/octet-stream` qu'il refuserait de lire.
MIME_PAR_FORMAT = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
}

# ⚠ INSTANCES MISES EN CACHE — le défaut le plus coûteux de cette route.
#   `backend_cls()` était appelé à CHAQUE requête. Or le pipeline de synthèse est mis
#   en cache sur l'INSTANCE (`KokoroTTSBackend.__init__` pose `self._pipeline = None`,
#   et `_ensure_pipeline` ne teste que `self._pipeline`) : chaque phrase rechargeait
#   donc un `KPipeline` complet. La route est un `def`, donc exécutée dans le
#   threadpool FastAPI — jusqu'à 40 chargements de modèle EN PARALLÈLE, sur une VM qui
#   n'a pas la mémoire pour un seul de trop.
#   ⚠ RECTIFICATION D'UNE AFFIRMATION DE L'AUDIT, mesurée le 2026-08-04 : cette route
#   n'est **pas** anonyme. Le daemon exige `Authorization: Bearer` (`OPENJARVIS_API_KEY`
#   dans `/home/avalon/ava/.env`, injectée au build du frontend) — un appel sans clé
#   reçoit un 401, vérifié en direct sur 8000 comme sur le relais 8080. Le scénario
#   « boucle de 60 curl anonymes » de l'audit était donc faux, et il ne faut pas le
#   propager. Ce qui restait vrai, et suffit à justifier la correction : le navigateur
#   POSTe une requête PAR PHRASE, donc plusieurs synthèses se chevauchent réellement à
#   chaque réponse d'Ava — chacune rechargeant tout un modèle.
#   Gain mesuré par le vrai chemin HTTP, après correction : 1er appel 2,35 s (chargement
#   inclus), puis **0,55 s** — soit 4,3× plus rapide dès la deuxième phrase.
_instances: dict[str, object] = {}
_verrou_instances = threading.Lock()

# ⚠ UNE SYNTHÈSE À LA FOIS PAR BACKEND. Deux raisons, et la seconde est la vraie :
#   (1) borner la charge ; (2) **rien ne garantit qu'un backend soit réentrant** — on
#   partage désormais une instance, donc deux appels concurrents se marcheraient
#   dessus dans le pipeline. Sérialiser par backend est le seul choix sûr, et sans
#   coût réel ici : Ava sert une poignée d'humains, pas un service public.
_verrous_backend: dict[str, threading.Lock] = {}

# ⚠ ET UNE FILE D'ATTENTE BORNÉE, sinon la sérialisation ne fait que déplacer le
#   problème : les requêtes s'empileraient dans le threadpool jusqu'à l'épuiser, et
#   TOUT le daemon deviendrait muet (les routes `/conversation` comprises). Au-delà,
#   on refuse en 503 — un refus franc vaut mieux qu'une attente que le client
#   abandonnera de toute façon.
_ATTENTE_MAX = max(1, int(os.environ.get("AVA_TTS_ATTENTE_MAX", "4")))
_places = threading.BoundedSemaphore(_ATTENTE_MAX)


def _instance(cle: str) -> object:
    """L'instance partagée du backend `cle`, créée une seule fois."""
    with _verrou_instances:
        obj = _instances.get(cle)
        if obj is None:
            obj = TTSRegistry.get(cle)()
            _instances[cle] = obj
            _verrous_backend[cle] = threading.Lock()
        return obj


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str = Field("ff_siwis", description="Voice identifier for the backend")
    backend: str = Field(
        "kokoro-fr",
        description="TTS backend key (kokoro-fr, kokoro, cartesia, openai_tts)",
    )
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Playback speed multiplier")
    output_format: Literal["wav", "mp3", "ogg", "opus", "flac"] = Field(
        "wav", description="Preferred audio format"
    )


@router.post("/speak")
def speak(req: SpeakRequest) -> Response:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    if not TTSRegistry.contains(req.backend):
        # ⚠ NE PLUS ÉNUMÉRER LE REGISTRE. Le message d'origine renvoyait
        #   `list(TTSRegistry.keys())`, c'est-à-dire l'inventaire des backends
        #   configurés — donc des intégrations en place et des clés d'API détenues.
        #   Portée exacte (mesurée) : l'appelant est authentifié par `OPENJARVIS_API_KEY`,
        #   donc ce n'était pas une fuite publique — mais cette clé est **injectée dans le
        #   bundle du frontend au build**, elle n'a donc pas la valeur d'un secret fort.
        #   Ne pas divulguer l'inventaire reste le bon réflexe ; ce n'était simplement pas
        #   une urgence. La liste des backends valides est déjà dans la description du
        #   champ, à destination des clients légitimes.
        logger.warning("tts: backend inconnu demandé (%r)", req.backend[:40])
        raise HTTPException(status_code=404, detail="unknown tts backend")

    backend = _instance(req.backend)

    if not _places.acquire(blocking=False):
        # ⚠ 503 + `Retry-After` : le client SAIT qu'il doit réessayer, au lieu
        #   d'interpréter un échec comme une panne du serveur.
        raise HTTPException(
            status_code=503,
            detail="tts busy",
            headers={"Retry-After": "2"},
        )
    try:
        with _verrous_backend[req.backend]:
            result = backend.synthesize(  # type: ignore[attr-defined]
                text,
                voice_id=req.voice_id,
                speed=req.speed,
                output_format=req.output_format,
            )
    except Exception as e:  # noqa: BLE001
        # ⚠ La route n'avait AUCUN gestionnaire : toute erreur du backend (clé d'API
        #   expirée, modèle absent, voix inconnue) sortait en 500 nu, sans trace
        #   utilisable. On journalise le détail côté serveur et on rend un message
        #   stable au client — sans recopier l'exception, qui peut porter une URL
        #   interne ou un fragment de clé.
        logger.exception("tts: échec de synthèse (backend=%s)", req.backend)
        raise HTTPException(status_code=502, detail="tts backend failed") from e
    finally:
        _places.release()

    fmt = (result.format or req.output_format or "wav").lower()
    mime = MIME_PAR_FORMAT.get(fmt, "application/octet-stream")

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
    """Sonde de vivacité de la synthèse vocale.

    ⚠ NE PLUS ÉNUMÉRER LES BACKENDS. Cette route rendait `list(TTSRegistry.keys())`,
      soit exactement la divulgation qu'on vient de retirer du 404 quelques lignes
      plus haut, et **sans aucun consommateur** (vérifié : rien dans `frontend/`,
      `ava_extensions/` ni `scripts/`). L'inventaire des backends dit quelles
      intégrations sont configurées, donc quelles clés d'API la machine détient.
      ⚠ Comme la route `/speak`, celle-ci est derrière `OPENJARVIS_API_KEY` — ce n'était
      pas une divulgation publique. Mais cette clé vit dans le bundle du frontend : elle
      protège d'un passant, pas de quelqu'un qui a ouvert la page.
      Le nombre suffit à répondre à la seule question utile : « la synthèse est-elle
      opérationnelle ? »
    """
    return {
        "ok": TTSRegistry.contains("kokoro-fr") or bool(list(TTSRegistry.keys())),
        "backends_enregistres": len(list(TTSRegistry.keys())),
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

    # ⚠ REFUS EXPLICITE PLUTÔT QUE FILTRAGE SILENCIEUX (2026-08-04). Le rôle était un
    #   `str` libre. `ajouter()` écartait bien les rôles inconnus — la protection
    #   contre l'injection d'un `system` dans l'historique rejoué a TOUJOURS fonctionné,
    #   il ne faut pas prétendre le contraire — mais la route rendait **200**, et le
    #   client n'apprenait le rejet qu'en comparant `ecrites` au nombre de lignes qu'il
    #   avait envoyées. Personne ne fait cette comparaison. Un client qui se met à
    #   émettre un rôle invalide (bug de sérialisation, montée de version du frontend)
    #   croirait donc écrire un historique qui n'existe pas, et le défaut ne se
    #   manifesterait que bien plus tard, sous la forme « Ava ne se souvient pas » —
    #   c'est-à-dire loin de sa cause.
    #   `ajouter()` GARDE son filtre : c'est une fonction publique, appelable hors
    #   de cette route. Deux couches, chacune à sa place.
    role: Literal["user", "assistant"]
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
