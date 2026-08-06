"""TraceCollector — wraps any BaseAgent to record interaction traces."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, Trace, TraceStep
from openjarvis.traces.store import TraceStore


class TraceCollector:
    """Wraps a ``BaseAgent`` and records a :class:`Trace` for every ``run()``.

    The collector subscribes to the ``EventBus`` to capture inference, tool,
    and memory events emitted during agent execution, converting them into
    ``TraceStep`` objects.  When the agent finishes, the complete ``Trace``
    is persisted to the ``TraceStore`` and published on the bus.

    Enhanced to capture full model response content, tool call arguments and
    results, and the complete conversation message history.

    Usage::

        agent = OrchestratorAgent(engine, model, tools=tools, bus=bus)
        collector = TraceCollector(agent, store=trace_store, bus=bus)
        result = collector.run("What is 2+2?")
        trace = collector.last_trace  # Rich trace with steps + messages
    """

    def __init__(
        self,
        agent: BaseAgent,
        *,
        store: Optional[TraceStore] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self._agent = agent
        self._store = store
        self._bus = bus
        # Departs d'appels d'outil en attente, PAR NOM D'OUTIL (cf. `_on_tool_start`).
        self._tool_starts: dict[str, list[tuple[float, Any]]] = {}
        self._current_steps: list[TraceStep] = []
        self._current_model: str = ""
        self._current_engine: str = ""
        self._last_trace: Optional[Trace] = None

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Execute the wrapped agent and record a trace."""
        self._current_steps = []
        self._current_model = ""
        self._current_engine = ""

        # Subscribe to events for trace collection
        unsubs = self._subscribe()

        started_at = time.time()
        try:
            result = self._agent.run(input, context=context, **kwargs)
        finally:
            self._unsubscribe(unsubs)

        ended_at = time.time()

        # Add final respond step
        self._current_steps.append(
            TraceStep(
                step_type=StepType.RESPOND,
                timestamp=ended_at,
                duration_seconds=0.0,
                output={"content": result.content, "turns": result.turns},
            )
        )

        # Extract messages from agent result metadata
        messages: List[Dict[str, Any]] = result.metadata.get("messages", [])

        # Build and persist the trace
        trace = Trace(
            query=input,
            agent=getattr(self._agent, "agent_id", "unknown"),
            model=self._current_model,
            engine=self._current_engine,
            steps=list(self._current_steps),
            result=result.content,
            messages=messages,
            started_at=started_at,
            ended_at=ended_at,
        )
        # Recompute totals from steps
        for step in trace.steps:
            trace.total_latency_seconds += step.duration_seconds
            trace.total_tokens += step.output.get("tokens", 0)

        # ⚠ ON NOTE LA TRACE, ET SUR UN CRITERE OBJECTIF SEULEMENT.
        #   Mesure du 2026-08-05 : **1 trace notee sur 108**. Le champ `outcome` a de
        #   vrais consommateurs (`traces/analyzer.py`, la route `/v1/feedback/stats`)
        #   mais personne ne l'ecrivait — la boucle d'apprentissage etait donc branchee
        #   sur du vide, et « Ava progresse » restait une impression.
        # ⚠ ON NE JUGE PAS LA QUALITE DE LA REPONSE, ET C'EST DELIBERE. Un juge
        #   automatique noterait des reponses plausibles comme bonnes — precisement le
        #   defaut qu'on passe nos journees a corriger. On note ce qui est VERIFIABLE :
        #   un appel d'outil a-t-il echoue ? C'est la ou vivaient tous les defauts
        #   trouves aujourd'hui, sans exception.
        # ⚠ L'ABSENCE D'ECHEC NE VAUT PAS SUCCES : on laisse alors `outcome` a None
        #   plutot que d'ecrire « success ». Un succes fabrique se lirait comme une
        #   mesure et ferait croire la boucle saine. `None` dit « non evalue », qui est
        #   la verite.
        echecs = [
            s
            for s in trace.steps
            if s.step_type == StepType.TOOL_CALL and s.output.get("success") is False
        ]
        # ⚠ CE QUI SUIT CORRIGE UN DEFAUT DE MA PROPRE MESURE, et il etait pire que le
        # silence qu'il pretendait eviter. En ne notant QUE les echecs, 154 traces sur
        #   171
        #   restaient a `outcome = None` — et `analyzer.py` calcule
        #   `successes / evaluated` en ne comptant QUE les traces notees. L'API publiait
        #   donc `taux_reussite: 0.0588` : **5,9 %**, alors que la quasi-totalite des
        # traces avait abouti sans le moindre echec. Un chiffre catastrophique
        #   fabrique
        #   par l'absence de mesure, pas par la realite.
        # ⚠ ON NE FABRIQUE TOUJOURS PAS DE « success » : ce mot reste reserve a un
        #   jugement
        # de QUALITE, que seul un humain peut porter. `completed` est un fait
        #   verifiable —
        #   la tache est allee au bout sans echec d'outil et sans troncature. C'est la
        # distinction entre « ca a marche » et « c'etait bien », et elle est load-
        #   bearing.
        # ⚠ `incomplete` couvre les deux facons OBJECTIVES de ne pas aboutir : budget de
        #   tours epuise, ou reponse vide. Les laisser a `None` les rendait invisibles.
        # ⚠ UN OUTIL QUI ECHOUE N'EST PAS UN TOUR QUI ECHOUE — et confondre les deux
        #   etait
        #   MON defaut, ecrit le matin meme du 2026-08-06. La regle disait « s'il y a un
        #   echec d'outil, c'est `tool_failure` », SANS regarder si la reponse avait ete
        #   livree. Un agent qui se heurte a un outil, se reprend et rend une reponse
        #   complete etait donc note comme un echec, avec `feedback = 0.0`.
        # ⚠ MESURE, PAS IMPRESSION : sur 210 traces, **19 des 21 `tool_failure` avaient
        #   livre une reponse** (mediane 978 caracteres, jusqu'a 7537). Deux seulement
        #   etaient de vrais echecs. Le taux publie tombait a 89 % quand le reel est
        #   98,1 % — et c'est ELLE qui lit ce chiffre sur elle-meme via `introspection`.
        #   Un systeme qui note ses propres reussites comme des echecs n'apprend pas :
        #   il apprend a se croire mauvais.
        # ⚠ POURQUOI UN VERDICT SEPARE PLUTOT QUE `completed` : la friction est une
        #   information. `recovered` dit « c'est allé au bout, mais un outil a lache en
        #   chemin » — utile pour trouver les outils fragiles, et perdu si on fusionne.
        # C'est la meme distinction que `completed` contre `success` : on nomme un
        #   FAIT,
        #   on ne juge pas la qualite.
        contenu = (result.content or "").strip() if hasattr(result, "content") else ""
        tronquee = (
            bool(getattr(result, "metadata", {}).get("max_turns_exceeded"))
            or not contenu
        )
        # ⚠ LA MACHINE N'ECRIT PLUS DANS `feedback`, ET C'EST LA MEME DISTINCTION QUE
        #   `update_feedback` vient de retablir une couche plus haut : `outcome` porte le
        #   FAIT (ca a marche ou non), `feedback` porte le JUGEMENT DE QUALITE, qui
        #   n'appartient qu'a un humain. Ecrire `feedback = 0.0` sur un echec melangeait
        #   les deux.
        # ⚠ DEFAUT TROUVE EN LUI PARLANT, le 2026-08-06 : interrogee sur ses notes, elle a
        #   repondu « 5 reponses notees par Adrien, dont 1 bonne ». Faux — **4 des 5
        #   venaient de la machine**, une seule etait humaine. Elle lisait ses propres
        #   verdicts automatiques comme des jugements de l'admin, donc se croyait notee
        #   4 fois negativement par quelqu'un qui ne l'avait jamais jugee.
        # ⚠ Le champ reste donc NULL tant qu'un humain n'a rien dit — et `feedback is not
        #   None` signifie desormais exactement « quelqu'un a juge cette reponse ».
        if echecs and not contenu:
            trace.outcome = "tool_failure"
        elif tronquee:
            trace.outcome = "incomplete"
        elif echecs:
            # ⚠ Pas de `feedback = 0.0` ici : le tour a abouti. Une note nulle sur une
            #   reussite est exactement ce que le correctif supprime.
            trace.outcome = "recovered"
        else:
            trace.outcome = "completed"

        self._last_trace = trace

        if self._store is not None:
            self._store.save(trace)

        if self._bus is not None:
            self._bus.publish(EventType.TRACE_COMPLETE, {"trace": trace})

        return result

    @property
    def last_trace(self) -> Optional[Trace]:
        """Return the trace from the most recent ``run()``."""
        return self._last_trace

    # -- event handlers --------------------------------------------------------

    def _subscribe(self) -> list[tuple]:
        if self._bus is None:
            return []
        handlers = [
            (EventType.INFERENCE_START, self._on_inference_start),
            (EventType.INFERENCE_END, self._on_inference_end),
            (EventType.TOOL_CALL_START, self._on_tool_start),
            (EventType.TOOL_CALL_END, self._on_tool_end),
            (EventType.MEMORY_RETRIEVE, self._on_memory_retrieve),
        ]
        for evt_type, handler in handlers:
            self._bus.subscribe(evt_type, handler)
        return handlers

    def _unsubscribe(self, handlers: list[tuple]) -> None:
        if self._bus is None:
            return
        for evt_type, handler in handlers:
            self._bus.unsubscribe(evt_type, handler)

    def _on_inference_start(self, event: Any) -> None:
        self._current_model = event.data.get("model", self._current_model)
        self._current_engine = event.data.get("engine", self._current_engine)
        self._inference_start_time = event.timestamp

    def _on_inference_end(self, event: Any) -> None:
        start = getattr(self, "_inference_start_time", event.timestamp)
        data = event.data
        usage = data.get("usage", {})
        self._current_steps.append(
            TraceStep(
                step_type=StepType.GENERATE,
                timestamp=start,
                duration_seconds=event.timestamp - start,
                input={"model": self._current_model},
                output={
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "tokens": usage.get(
                        "total_tokens",
                        data.get("total_tokens", 0),
                    ),
                    "content": data.get("content", ""),
                    "tool_calls": data.get("tool_calls", []),
                    "tool_results": data.get("tool_results", []),
                    "content_blocks": data.get("content_blocks", []),
                    "finish_reason": data.get("finish_reason", ""),
                },
                metadata={
                    "engine": self._current_engine,
                    "ttft": data.get("ttft", 0.0),
                    "energy_joules": data.get("energy_joules", 0.0),
                    "power_watts": data.get("power_watts", 0.0),
                    "gpu_utilization_pct": data.get(
                        "gpu_utilization_pct",
                        0.0,
                    ),
                    "throughput_tok_per_sec": data.get(
                        "throughput_tok_per_sec",
                        0.0,
                    ),
                },
            )
        )

    def _on_tool_start(self, event: Any) -> None:
        # ⚠ UNE FILE PAR OUTIL, ET NON UN EMPLACEMENT UNIQUE. Le code precedent gardait
        #   `self._tool_start_data = event.data` : un SEUL emplacement, ecrase a chaque
        #   depart. Or le modele appelle plusieurs outils EN PARALLELE — les departs
        #   s'ecrasent, puis toutes les arrivees lisent les arguments du DERNIER.
        #   Constate le 2026-08-05 sur la trace 0edd79a6cbd847c3 : quatre appels
        #   (`journal`, `avalon_status`, `logs`, `logs`) portaient TOUS
        #   `{"question": "redemarrages", "fenetre": "24h"}` — alors que ni `journal` ni
        #   `avalon_status` n'ont le moindre parametre de ce nom.
        # Consequence : le nom de l'outil restait juste (donc les taux de reussite
        #   sont
        # valides), mais on ne pouvait plus savoir CE QUI AVAIT ETE DEMANDE — donc
        #   plus
        #   diagnostiquer un echec. Et `duration_seconds` etait fausse de la meme facon,
        #   mesuree depuis le dernier depart.
        # ⚠ LIMITE ASSUMEE : les evenements ne portent AUCUN identifiant d'appel
        #   (verifie :
        # `{"tool": nom, "arguments": params}` et rien d'autre). Deux appels au MEME
        #   outil
        # dans un meme lot restent donc apparies dans l'ordre d'arrivee — heuristique,
        #   mais
        #   sans commune mesure avec un emplacement global partage par tous les outils.
        file = self._tool_starts.setdefault(str(event.data.get("tool", "")), [])
        file.append((event.timestamp, event.data))

    def _on_tool_end(self, event: Any) -> None:
        file = self._tool_starts.get(str(event.data.get("tool", "")))
        if file:
            start, start_data = file.pop(0)
        else:
            # Fin sans depart connu : on ne fabrique pas d'arguments, on rend le vide.
            start, start_data = event.timestamp, {}
        # Pull through any metadata the tool attached to its ToolResult
        # (e.g. SkillTool's skill/skill_source/skill_kind tags) so the
        # SkillOptimizer can bucket traces by skill name.
        result_metadata = event.data.get("metadata") or {}
        self._current_steps.append(
            TraceStep(
                step_type=StepType.TOOL_CALL,
                timestamp=start,
                duration_seconds=event.data.get(
                    "latency",
                    event.timestamp - start,
                ),
                input={
                    "tool": event.data.get("tool", ""),
                    "arguments": start_data.get("arguments", {}),
                },
                output={
                    "success": event.data.get("success", False),
                    "result": event.data.get("result", ""),
                },
                metadata=dict(result_metadata),
            )
        )

    def _on_memory_retrieve(self, event: Any) -> None:
        self._current_steps.append(
            TraceStep(
                step_type=StepType.RETRIEVE,
                timestamp=event.timestamp,
                duration_seconds=event.data.get("latency", 0.0),
                input={"query": event.data.get("query", "")},
                output={
                    "num_results": event.data.get("num_results", 0),
                },
            )
        )


def record_response_trace(
    store: Optional[TraceStore],
    *,
    query: str,
    result: str,
    model: str = "",
    engine: str = "",
    agent: str = "server",
    started_at: float,
    ended_at: float,
) -> Optional[Trace]:
    """Persist a minimal single-step ``Trace`` for a non-agent response.

    The streaming SSE and WebSocket chat paths stream straight from the
    engine, bypassing the agent (and therefore ``TraceCollector``). They call
    this so those interactions still land in ``traces.db`` — otherwise streamed
    chats, which are the desktop GUI's main path, would never produce traces.

    Best-effort: returns the saved ``Trace`` or ``None`` (when *store* is
    ``None`` or persistence raised), and never propagates an exception into the
    caller's response path.
    """
    if store is None:
        return None
    try:
        duration = max(0.0, ended_at - started_at)
        trace = Trace(
            query=query,
            agent=agent,
            model=model,
            engine=engine,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
            steps=[
                TraceStep(
                    step_type=StepType.RESPOND,
                    timestamp=ended_at,
                    duration_seconds=duration,
                    output={"content": result},
                )
            ],
        )
        trace.total_latency_seconds = duration
        store.save(trace)
        return trace
    except Exception:
        import logging

        logging.getLogger("openjarvis.traces").debug(
            "record_response_trace failed", exc_info=True
        )
        return None


__all__ = ["TraceCollector", "record_response_trace"]
