"""TraceCollector — wraps any BaseAgent to record interaction traces."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, Trace, TraceStep
from openjarvis.engine._finish import conservative_finish_reason
from openjarvis.traces.store import TraceStore


@dataclass(frozen=True, slots=True)
class TraceContentFilterResult:
    """Sanitized content returned by a request-local trace filter."""

    content: str = field(repr=False)
    suppress_structured_output: bool = False
    force_stop: bool = False


class TraceContentFilter(Protocol):
    """Filter model-owned text before any trace or response is persisted."""

    def __call__(
        self,
        content: str,
        structured_output_json: Sequence[str],
        *,
        allow_conversation_echo: bool,
        final: bool,
    ) -> TraceContentFilterResult: ...


TraceMetadataProvider = Callable[[], Mapping[str, object]]


def _structured_json(value: Any) -> str:
    """Return bounded inspection input without falling back to unsafe repr()."""

    def dataclass_default(item: Any) -> Any:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.asdict(item)
        raise TypeError

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=dataclass_default,
        )
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("structured trace output cannot be inspected") from None


def _structured_filter_inputs(*values: Any) -> tuple[str, ...]:
    return tuple(
        _structured_json(value) for value in values if value not in (None, [], {}, "")
    )


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
        self._active_content_filter: TraceContentFilter | None = None
        self._replacement_filter_result: TraceContentFilterResult | None = None
        self._last_content_filter_result: TraceContentFilterResult | None = None
        self._final_filter_applied = False

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        *,
        provenance: Optional[str] = None,
        content_filter: TraceContentFilter | None = None,
        trace_metadata_provider: TraceMetadataProvider | None = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Execute the wrapped agent and record a trace.

        ⚠ `provenance` dit QUI a posé la question. Sans lui, les traces sont
          indiscernables — mesuré le 2026-08-07 — et le registre mélange les vraies
          conversations de l'admin avec les sondes adverses à prémisse fausse. C'est le
          prérequis de la mémoire épisodique, pas un agrément.
        """
        self._current_steps = []
        self._current_model = ""
        self._current_engine = ""
        self._tool_starts = {}
        self._active_content_filter = content_filter
        self._replacement_filter_result = None
        self._last_content_filter_result = None
        self._final_filter_applied = False

        # Subscribe to events for trace collection
        unsubs = self._subscribe()

        started_at = time.time()
        try:
            try:
                result = self._agent.run(input, context=context, **kwargs)
            except Exception:
                if content_filter is not None:
                    # The Ava observability extension records `_current_steps`
                    # when an agent raises.  Overlay events are intentionally
                    # request-local and still raw at this point, so never let
                    # that fallback persist or log them.
                    self._current_steps = []
                    raise RuntimeError("filtered agent execution failed") from None
                raise
        finally:
            self._unsubscribe(unsubs)

        ended_at = time.time()

        if content_filter is not None:
            try:
                result = self._filter_agent_result(result)
            except Exception:
                # Fail closed without handing raw request-local events to the
                # failure-trace patch installed by Ava.
                self._current_steps = []
                raise RuntimeError("trace content filtering failed") from None

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
        trace_metadata: dict[str, object] = (
            {"provenance": provenance} if provenance else {}
        )
        if trace_metadata_provider is not None:
            provided_metadata = trace_metadata_provider()
            if not isinstance(provided_metadata, Mapping):
                raise RuntimeError("trace metadata provider returned invalid data")
            trace_metadata.update(provided_metadata)

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
            # ⚠ Absent plutôt que deviné : une trace sans provenance déclarée reste
            #   `inconnue`, elle ne devient pas « l'admin » par défaut. Le défaut le
            #   plus dangereux serait d'attribuer à quelqu'un des propos qu'il n'a pas
            #   tenus.
            metadata=trace_metadata,
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
        # ToolExecutor deliberately emits TOOL_CALL_START only after registry,
        # capability, boundary and confirmation checks.  A denied or malformed
        # attempt therefore exists in AgentResult.tool_results but has no trace step.
        # Ignoring it here would let a later successful plan-only sequence erase the
        # refused attempt and make the trace look fully completed.  Inspect only the
        # server-owned success bit; never persist the rejected arguments or result.
        echecs_avant_demarrage = [
            tool_result
            for tool_result in getattr(result, "tool_results", [])
            if type(getattr(tool_result, "success", None)) is not bool
            or tool_result.success is False
        ]
        echec_outil = bool(echecs or echecs_avant_demarrage)
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
        metadata = getattr(result, "metadata", {})
        motif_arret = conservative_finish_reason(metadata.get("finish_reason"))
        tronquee = (
            bool(metadata.get("max_turns_exceeded"))
            or not contenu
            or motif_arret != "stop"
        )
        # ⚠ LA MACHINE N'ECRIT PLUS DANS `feedback`, ET C'EST LA MEME DISTINCTION QUE
        #   `update_feedback` vient de retablir une couche plus haut : `outcome` porte
        #   le FAIT (ca a marche ou non), `feedback` porte le JUGEMENT DE QUALITE, qui
        #   n'appartient qu'a un humain. Ecrire `feedback = 0.0` sur un echec melangeait
        #   les deux.
        # ⚠ DEFAUT TROUVE EN LUI PARLANT, le 2026-08-06 : interrogee sur ses notes, elle
        #   a repondu « 5 reponses notees par Adrien, dont 1 bonne ». Faux — **4 des 5
        #   venaient de la machine**, une seule etait humaine. Elle lisait ses propres
        #   verdicts automatiques comme des jugements de l'admin, donc se croyait notee
        #   4 fois negativement par quelqu'un qui ne l'avait jamais jugee.
        # ⚠ Le champ reste donc NULL tant qu'un humain n'a rien dit — et `feedback is
        #   not None` signifie desormais exactement « quelqu'un a juge cette reponse ».
        if echec_outil and not contenu:
            trace.outcome = "tool_failure"
        elif tronquee:
            trace.outcome = "incomplete"
        elif echec_outil:
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

    @property
    def last_content_filter_result(self) -> TraceContentFilterResult | None:
        """Return the final request-local filter decision, when configured."""

        return self._last_content_filter_result

    def _apply_content_filter(
        self,
        content: str,
        structured_output_json: Sequence[str] = (),
        *,
        allow_conversation_echo: bool,
        final: bool,
    ) -> TraceContentFilterResult:
        content_filter = self._active_content_filter
        if content_filter is None:
            return TraceContentFilterResult(content=content)
        if final and self._final_filter_applied:
            raise RuntimeError("terminal trace content filter already applied")
        if not isinstance(content, str) or any(
            not isinstance(item, str) for item in structured_output_json
        ):
            raise RuntimeError("trace content filter received invalid data")
        result = content_filter(
            content,
            tuple(structured_output_json),
            allow_conversation_echo=allow_conversation_echo,
            final=final,
        )
        if (
            not isinstance(result, TraceContentFilterResult)
            or not isinstance(result.content, str)
            or type(result.suppress_structured_output) is not bool
            or type(result.force_stop) is not bool
            or (result.force_stop and not result.suppress_structured_output)
            or (final and result.suppress_structured_output and not result.content)
        ):
            raise RuntimeError("trace content filter returned invalid data")
        if final:
            self._final_filter_applied = True
            self._last_content_filter_result = result
            if result.suppress_structured_output:
                self._replacement_filter_result = result
        return result

    def _filter_messages(
        self,
        messages: Any,
        final_decision: TraceContentFilterResult,
    ) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            raise RuntimeError("trace messages cannot be inspected")
        normalised: list[dict[str, Any]] = []
        for message in messages:
            if dataclasses.is_dataclass(message) and not isinstance(message, type):
                message = dataclasses.asdict(message)
            if not isinstance(message, Mapping):
                raise RuntimeError("trace messages cannot be inspected")
            normalised.append(dict(message))

        assistant_indexes = [
            index
            for index, message in enumerate(normalised)
            if message.get("role") == "assistant"
        ]
        last_assistant = assistant_indexes[-1] if assistant_indexes else None
        filtered_messages: list[dict[str, Any]] = []
        for index, message in enumerate(normalised):
            filtered = dict(message)
            role = filtered.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise RuntimeError("trace message role cannot be inspected")
            if role == "system":
                # A system turn may contain the private server-owned overlay.
                # It is model context, not conversation history for traces.
                continue
            content = filtered.get("content")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise RuntimeError("trace message content cannot be inspected")
            if role == "user":
                filtered_messages.append({"role": role, "content": content})
                continue

            structured = _structured_filter_inputs(
                {
                    key: value
                    for key, value in filtered.items()
                    if key not in {"role", "content"}
                }
            )
            is_current_assistant = role == "assistant" and index == last_assistant
            message_decision = self._apply_content_filter(
                content,
                structured,
                allow_conversation_echo=is_current_assistant,
                final=False,
            )
            decision = (
                final_decision
                if is_current_assistant and final_decision.suppress_structured_output
                else message_decision
            )
            # Steps retain inspected tool detail. Conversation messages under
            # an active filter deliberately keep only their canonical text.
            filtered_messages.append({"role": role, "content": decision.content})
        return filtered_messages

    def _scrub_steps_after_replacement(self) -> None:
        scrubbed: list[TraceStep] = []
        for step in self._current_steps:
            if step.step_type == StepType.TOOL_CALL:
                continue
            if step.step_type == StepType.GENERATE:
                step.output.pop("tool_calls", None)
                step.output.pop("tool_results", None)
                step.output.pop("content_blocks", None)
                step.output["finish_reason"] = "stop"
            scrubbed.append(step)
        self._current_steps = scrubbed

    def _scrub_trace_steps(self) -> None:
        """Sanitize request-local event snapshots before store or publication."""

        for step in self._current_steps:
            if step.step_type == StepType.GENERATE:
                content = step.output.get("content", "")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    raise RuntimeError("trace inference content cannot be inspected")
                decision = self._apply_content_filter(
                    content,
                    _structured_filter_inputs(
                        step.output.get("tool_calls"),
                        step.output.get("content_blocks"),
                    ),
                    allow_conversation_echo=True,
                    final=False,
                )
                step.output["content"] = decision.content
                step.output.pop("content_blocks", None)

                tool_result_decision = self._apply_content_filter(
                    "",
                    _structured_filter_inputs(step.output.get("tool_results")),
                    allow_conversation_echo=False,
                    final=False,
                )
                if tool_result_decision.suppress_structured_output:
                    step.output["tool_results"] = []
                if decision.suppress_structured_output:
                    step.output.pop("tool_calls", None)
                    step.output.pop("tool_results", None)
                    step.output["finish_reason"] = "stop"
                continue

            if step.step_type != StepType.TOOL_CALL:
                continue
            argument_decision = self._apply_content_filter(
                "",
                _structured_filter_inputs(step.input.get("arguments")),
                allow_conversation_echo=False,
                final=False,
            )
            if argument_decision.suppress_structured_output:
                step.input["arguments"] = {}

            result_content = step.output.get("result", "")
            if result_content is None:
                result_content = ""
            if not isinstance(result_content, str):
                result_content = _structured_json(result_content)
            result_decision = self._apply_content_filter(
                result_content,
                (),
                allow_conversation_echo=False,
                final=False,
            )
            step.output["result"] = result_decision.content

            metadata_decision = self._apply_content_filter(
                "",
                _structured_filter_inputs(step.metadata),
                allow_conversation_echo=False,
                final=False,
            )
            if metadata_decision.suppress_structured_output:
                step.metadata = {}

    def _filter_external_tool_results(self, tool_results: Any) -> list[Any]:
        if not isinstance(tool_results, list):
            raise RuntimeError("trace tool results cannot be inspected")
        filtered_results: list[Any] = []
        for tool_result in tool_results:
            content = getattr(tool_result, "content", None)
            if not isinstance(content, str):
                raise RuntimeError("trace tool result content cannot be inspected")
            metadata = getattr(tool_result, "metadata", {})
            usage = getattr(tool_result, "usage", {})
            decision = self._apply_content_filter(
                content,
                _structured_filter_inputs(metadata, usage),
                allow_conversation_echo=False,
                final=False,
            )
            if decision.content != content or decision.suppress_structured_output:
                if not dataclasses.is_dataclass(tool_result):
                    raise RuntimeError("trace tool result cannot be scrubbed")
                tool_result = dataclasses.replace(
                    tool_result,
                    content=decision.content,
                    metadata=({} if decision.suppress_structured_output else metadata),
                    usage=({} if decision.suppress_structured_output else usage),
                )
            filtered_results.append(tool_result)
        return filtered_results

    def _filter_agent_result(self, result: AgentResult) -> AgentResult:
        metadata = dict(getattr(result, "metadata", {}) or {})
        structured = _structured_filter_inputs(
            {
                key: metadata[key]
                for key in (
                    "tool_calls",
                    "content_blocks",
                    "audio",
                    "audio_path",
                )
                if key in metadata and metadata[key] not in (None, [], {}, "")
            }
        )
        decision = self._apply_content_filter(
            result.content,
            structured,
            allow_conversation_echo=True,
            final=True,
        )
        self._scrub_trace_steps()
        if "messages" in metadata:
            metadata["messages"] = self._filter_messages(
                metadata["messages"],
                decision,
            )
        result.tool_results = self._filter_external_tool_results(result.tool_results)
        if "tool_results" in metadata:
            tool_result_decision = self._apply_content_filter(
                "",
                _structured_filter_inputs(metadata["tool_results"]),
                allow_conversation_echo=False,
                final=False,
            )
            if tool_result_decision.suppress_structured_output:
                metadata.pop("tool_results", None)

        if self._replacement_filter_result is not None:
            decision = self._replacement_filter_result
            self._scrub_steps_after_replacement()
        result.content = decision.content
        if decision.suppress_structured_output:
            result.tool_results = []
            for key in (
                "tool_calls",
                "tool_results",
                "content_blocks",
                "audio",
                "audio_path",
            ):
                metadata.pop(key, None)
            metadata["finish_reason"] = "stop"
        else:
            metadata.pop("content_blocks", None)
        result.metadata = metadata
        return result

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
        content = data.get("content", "")
        if self._active_content_filter is not None:
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise RuntimeError("trace inference content cannot be inspected")
        tool_calls = data.get("tool_calls", [])
        tool_results = data.get("tool_results", [])
        content_blocks = data.get("content_blocks", [])
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
                    "content": content,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "content_blocks": content_blocks,
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
        result_content = event.data.get("result", "")
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
                    "result": result_content,
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
    provenance: str | None = None,
    metadata: Mapping[str, object] | None = None,
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
        trace_metadata: dict[str, object] = (
            {"provenance": provenance} if provenance else {}
        )
        if metadata is not None:
            trace_metadata.update(metadata)
        trace = Trace(
            query=query,
            agent=agent,
            model=model,
            engine=engine,
            result=result,
            started_at=started_at,
            ended_at=ended_at,
            metadata=trace_metadata,
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


__all__ = [
    "TraceCollector",
    "TraceContentFilter",
    "TraceContentFilterResult",
    "TraceMetadataProvider",
    "record_response_trace",
]
