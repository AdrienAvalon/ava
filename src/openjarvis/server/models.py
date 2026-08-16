"""Pydantic request/response models for the OpenAI-compatible API."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

# One API request may not ask a backend for an unbounded completion.  This is also
# the envelope used by Ava's durable conversation store: 32k tokens at a conservative
# four characters per token fit exactly below its 128 KiB per-message limit.
MAX_COMPLETION_TOKENS = 32_768

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_role_shape(self) -> "ChatMessage":
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported chat message role")
        if self.tool_calls is not None and self.role != "assistant":
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid on tool messages")
        return self


class ChatCompletionRequest(BaseModel):
    # Empty means "use the model selected by the daemon".  Ava's browser and
    # Control Plane are first-party clients and must not duplicate that mutable
    # runtime choice; an explicit OpenAI-compatible client may still override it.
    model: str = ""
    messages: List[ChatMessage]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, allow_inf_nan=False)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_COMPLETION_TOKENS)
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    #: ⚠ QUI POSE LA QUESTION — champ standard de l'API OpenAI, et le PRÉREQUIS de la
    #: mémoire épisodique. Mesure du 2026-08-07 : les 352 traces sont indiscernables
    #: entre elles (même `agent`, même `engine`, `messages` et `metadata` vides), alors
    #: qu'elles mélangent DEUX sources — les vraies conversations de l'admin par Matrix,
    #: et les sondes adverses que je lui envoie avec des prémisses DÉLIBÉRÉMENT FAUSSES.
    #: Exposer ce registre comme « ta mémoire des échanges » lui ferait donc se souvenir
    #: que l'admin a dit des choses que j'ai inventées pour la tester — « le chauffage
    #: des parents est coupé depuis ce matin » figure dans le registre à côté de « c'est
    #: bien le port 1 de l'Aruba », l'un fabriqué et l'autre réel.
    #: ⚠ D'où l'ordre : la provenance D'ABORD, la mémoire épisodique ENSUITE.
    #: Un registre
    #: qu'on ne peut pas attribuer est pire qu'un registre absent — il a l'autorité du
    #: verbatim.
    user: Optional[str] = None

    @model_validator(mode="after")
    def validate_tool_declarations(self) -> "ChatCompletionRequest":
        """Accept only the OpenAI function-tool shape at the HTTP boundary."""

        for tool in self.tools or []:
            function = tool.get("function")
            if (
                tool.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"].strip()
                or "name" in tool
            ):
                raise ValueError("invalid HTTP tool declaration")
            if function["name"] == "memoire":
                raise ValueError("legacy Ava memory tool is unavailable over HTTP")
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AudioMeta(BaseModel):
    url: str


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    audio: Optional[AudioMeta] = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class ComplexityInfo(BaseModel):
    score: float
    tier: str
    suggested_max_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: List[Choice] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    complexity: Optional[ComplexityInfo] = None
    #: ⚠ L'IDENTIFIANT DE TRACE, ET C'EST LE CHAÎNON QUI MANQUAIT À TOUTE LA BOUCLE
    #: D'APPRENTISSAGE. Mesure du 2026-08-07 : **1 trace notée sur 327**. La route
    #: `POST /v1/feedback` existe depuis toujours et fonctionne ; le verdict machine
    #: est riche (289 `completed`, 33 `recovered`, 2 `tool_failure`). Ce qui manquait
    #: n'était ni le stockage, ni la route, ni l'analyse : c'est que l'identifiant ne
    #: SORTAIT JAMAIS du processus. Personne ne pouvait noter parce que personne ne
    #: savait quoi nommer.
    #: ⚠ `None` quand les traces sont désactivées — jamais une chaîne vide, qui se
    #: passerait pour un identifiant valide et produirait des 404 inexplicables.
    trace_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Streaming chunk models
# ---------------------------------------------------------------------------


class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    # Streaming tool_calls (OpenAI delta shape, with `index`). Present only
    # on streamed raw function-calling responses (stream:true + tools).
    tool_calls: Optional[List[Dict[str, Any]]] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: List[StreamChoice] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "openjarvis"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelObject] = Field(default_factory=list)


__all__ = [
    "ChatCompletionChunk",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatMessage",
    "Choice",
    "ChoiceMessage",
    "ComplexityInfo",
    "DeltaMessage",
    "ModelListResponse",
    "ModelObject",
    "MAX_COMPLETION_TOKENS",
    "StreamChoice",
    "UsageInfo",
]
