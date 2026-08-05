"""LLM-backed extraction of durable facts from a conversation turn.

The extractor takes a single (user, assistant) exchange and asks a small
local model to distill any long-term, user-specific facts worth remembering.
It is deliberately defensive: extraction runs on a background thread far from
the request path, so *any* failure — a dropped Ollama connection, a timeout, a
``BrokenPipeError`` when the client went away, or simply unparseable output —
must degrade to "no facts" rather than propagate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

from openjarvis.core.types import Message, Role

logger = logging.getLogger(__name__)

# ⚠ DURCI LE 2026-08-05 APRÈS TROIS DÉFAUTS MESURÉS, chacun observé sur un fait
#   réellement écrit dans `memory_facts.jsonl` :
#   1. UNE PRÉMISSE RÉFUTÉE ÉTAIT MÉMORISÉE COMME VRAIE. Question posée : « comme le
#      chauffage de la grange est éteint depuis hier, la maison doit être froide ? » —
#      affirmation FAUSSE. Ava l'a correctement RÉFUTÉE dans sa réponse (« non, pas du
#      tout, il fait 24-25 °C »), et l'extracteur a tout de même stocké « barn with heating
#      that was turned off yesterday ». Il lisait la QUESTION, pas la CONCLUSION. C'est le
#      défaut le plus dangereux : n'importe quelle erreur de l'utilisateur devient un fait
#      durable, et Ava raisonnera dessus des semaines plus tard.
#   2. UNE QUESTION ÉTABLISSAIT L'EXISTENCE DE SON OBJET. « Quelle est la pression
#      atmosphérique dans le garage ? » a produit DEUX faits : « possède un garage » (il
#      n'y en a pas) et « intéressé par les mesures de pression atmosphérique ». Demander
#      si une chose existe n'est pas affirmer qu'elle existe.
#   3. LANGUE MÉLANGÉE. La moitié des faits étaient en anglais sur une installation
#      entièrement francophone — ce qui fragmente la recherche par similarité et fait
#      manquer des rappels pertinents.
# ⚠ Ce prompt est la SEULE divergence de ce fichier avec l'amont : une resynchronisation
#   produira un conflit visible sur cette constante, ce qui est le comportement voulu.
_DEFAULT_SYSTEM_PROMPT = (
    "Tu extrais des faits DURABLES sur l'utilisateur a partir d'un seul echange. "
    "Un bon fait est stable dans le temps et utile plus tard : preferences, identite, "
    "objectifs, projets en cours, contraintes, relations.\n\n"
    "REGLES ABSOLUES :\n"
    "1. Ne retiens JAMAIS une affirmation que la reponse de l'assistant CONTREDIT ou "
    "corrige. Si l'utilisateur dit « comme X est vrai... » et que la reponse montre que X "
    "est faux, X ne doit PAS etre memorise. Lis la CONCLUSION de l'echange, pas la "
    "premisse de la question.\n"
    "2. Une QUESTION n'etablit pas l'existence de son objet. « Quelle est la temperature "
    "du garage ? » ne prouve ni qu'un garage existe, ni que l'utilisateur s'y interesse. "
    "N'extrais un fait d'une question que si la reponse le CONFIRME.\n"
    "3. Ecris les faits EN FRANCAIS, meme si l'echange melange les langues.\n"
    "4. Ignore les details ponctuels, les banalites, tout ce que l'assistant a dit de "
    "lui-meme, et tout ce qui est deja evident dans l'echange en cours.\n"
    "5. N'ecris pas un fait qui redit un fait deja connu sous une autre forme.\n\n"
    "Reponds UNIQUEMENT par un tableau JSON de chaines courtes (moins de 200 caracteres "
    "chacune). Si rien ne merite d'etre retenu, reponds []."
)


class FactExtractor:
    """Extract memory-worthy facts from a conversation turn via an engine."""

    def __init__(
        self,
        engine: Any,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
        max_facts_per_turn: int = 10,
        max_fact_chars: int = 200,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._engine = engine
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_facts_per_turn = max_facts_per_turn
        self._max_fact_chars = max_fact_chars
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    def extract(self, user_text: str, assistant_text: str = "") -> List[str]:
        """Return durable facts from the exchange. Never raises."""
        user_text = (user_text or "").strip()
        if not user_text:
            return []

        exchange = f"User: {user_text}"
        if assistant_text and assistant_text.strip():
            exchange += f"\nAssistant: {assistant_text.strip()}"

        messages = [
            Message(role=Role.SYSTEM, content=self._system_prompt),
            Message(role=Role.USER, content=exchange),
        ]

        try:
            result = self._engine.generate(
                messages,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except BrokenPipeError:
            # The classic failure mode: the model call's transport died.
            # Extraction is best-effort, so swallow it.
            logger.debug("Memory extraction aborted: broken pipe", exc_info=True)
            return []
        except Exception:  # noqa: BLE001 — extraction must never crash the worker
            logger.debug("Memory extraction failed", exc_info=True)
            return []

        if isinstance(result, dict):
            content = result.get("content", "") or ""
        else:
            content = str(result)

        return self._parse(content)

    # -- parsing ------------------------------------------------------------

    def _parse(self, content: str) -> List[str]:
        """Parse model output into a clean, deduped, capped list of facts."""
        if not content or not content.strip():
            return []

        raw = self._coerce_to_list(content)

        facts: List[str] = []
        seen: set[str] = set()
        for item in raw:
            fact = self._clean_fact(item)
            if not fact:
                continue
            key = fact.lower()
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
            if len(facts) >= self._max_facts_per_turn:
                break
        return facts

    def _coerce_to_list(self, content: str) -> List[str]:
        """Best-effort conversion of model output to a list of strings."""
        # 1. Try to locate and parse a JSON array anywhere in the output
        #    (models often wrap it in prose or code fences).
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except (json.JSONDecodeError, ValueError):
                pass

        # 2. Fall back to line-based parsing (markdown bullets / numbered).
        items: List[str] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line)
            items.append(line)
        return items

    def _clean_fact(self, item: str) -> str:
        fact = str(item).strip().strip("\"'").strip()
        # Drop obvious non-facts the model sometimes emits.
        if not fact or fact.lower() in ("[]", "none", "n/a", "null"):
            return ""
        if len(fact) > self._max_fact_chars:
            fact = fact[: self._max_fact_chars].rstrip()
        return fact


__all__ = ["FactExtractor"]
