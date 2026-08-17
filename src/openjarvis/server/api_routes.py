"""Extended API routes for agents, workflows, memory, traces, etc."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---- Request/Response models ----


class MemoryStoreRequest(BaseModel):
    content: str
    metadata: Optional[Dict[str, Any]] = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


class MemoryIndexRequest(BaseModel):
    path: str


class BudgetLimitsRequest(BaseModel):
    max_tokens_per_day: Optional[int] = None
    max_requests_per_hour: Optional[int] = None


class FeedbackScoreRequest(BaseModel):
    trace_id: str
    score: float
    source: str = "api"


class OptimizeRunRequest(BaseModel):
    benchmark: str
    max_trials: int = 20
    optimizer_model: str = "claude-sonnet-4-6"
    max_samples: int = 50


# ---- Agent routes ----

agents_router = APIRouter(prefix="/v1/agents", tags=["agents"])

_LEGACY_AGENT_HTTP_QUARANTINE_DETAIL = (
    "Legacy shared agent HTTP API is quarantined; use /v1/managed-agents"
)


def _reject_legacy_agent_http() -> None:
    """Reject the global upstream agent store before any read or mutation."""

    raise HTTPException(
        status_code=410,
        detail=_LEGACY_AGENT_HTTP_QUARANTINE_DETAIL,
    )


@agents_router.get("")
async def list_agents(request: Request):
    """Quarantine the unscoped upstream agent registry."""

    del request
    _reject_legacy_agent_http()


@agents_router.post("")
async def create_agent(request: Request):
    """Reject creation in the unscoped upstream agent registry."""

    del request
    _reject_legacy_agent_http()


@agents_router.delete("/{agent_id}")
async def kill_agent(agent_id: str, request: Request):
    """Reject mutation of the unscoped upstream agent registry."""

    del agent_id, request
    _reject_legacy_agent_http()


@agents_router.post("/{agent_id}/message")
async def message_agent(agent_id: str, request: Request):
    """Reject messages to the unscoped upstream agent registry."""

    del agent_id, request
    _reject_legacy_agent_http()


# ---- Memory routes ----

memory_router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _require_legacy_memory_http_opt_in() -> None:
    """Keep the shared, principal-less memory API closed in Ava by default."""

    import os

    enabled = os.environ.get("OPENJARVIS_ENABLE_LEGACY_MEMORY_HTTP", "").strip().lower()
    if enabled not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(
            status_code=410,
            detail="Legacy shared memory HTTP API is disabled",
        )


def _get_memory_backend(request: Request):
    """Return the app-level memory backend, falling back to a fresh SQLiteMemory.

    Raises ``HTTPException(503)`` with an actionable message when the backend
    cannot be built because the mandatory ``openjarvis_rust`` extension is not
    installed in the serving venv. This is deliberately distinct from a benign
    "memory not configured" case (which returns ``None``): a missing native
    extension must fail loudly, never silently degrade (#502).
    """
    backend = getattr(request.app.state, "memory_backend", None)
    if backend is None:
        from openjarvis.tools.storage._stubs import MemoryBackendUnavailable

        try:
            from openjarvis.tools.storage.sqlite import SQLiteMemory

            backend = SQLiteMemory()
        except MemoryBackendUnavailable as exc:
            # The native extension is missing — surface a loud, actionable error
            # rather than a misleading "no backend" / silent no-op.
            logger.error("%s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:
            # Memory is genuinely unconfigured for a benign reason — preserve
            # the existing graceful "no backend" behaviour.
            return None
    return backend


@memory_router.post("/store")
async def memory_store(req: MemoryStoreRequest, request: Request):
    """Store content in memory."""
    _require_legacy_memory_http_opt_in()
    backend = _get_memory_backend(request)
    if backend is None:
        # Memory is intentionally disabled; report it honestly instead of a
        # 200 that silently discards the write (#502).
        raise HTTPException(status_code=503, detail="Memory is not configured")
    try:
        backend.store(req.content, metadata=req.metadata or {})
        return {"status": "stored"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/search")
async def memory_search(req: MemorySearchRequest, request: Request):
    """Search memory for relevant content."""
    _require_legacy_memory_http_opt_in()
    backend = _get_memory_backend(request)
    if backend is None:
        return {"results": []}
    try:
        results = backend.retrieve(req.query, top_k=req.top_k)
        items = [
            {
                "content": r.content,
                "score": getattr(r, "score", 0.0),
                "metadata": getattr(r, "metadata", {}),
            }
            for r in results
        ]
        return {"results": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/stats")
async def memory_stats(request: Request):
    """Get memory backend statistics."""
    _require_legacy_memory_http_opt_in()
    backend = _get_memory_backend(request)
    if backend is None:
        return {"entries": 0, "backend": "none", "status": "not_configured"}
    try:
        return {
            "entries": backend.count(),
            "backend": getattr(backend, "backend_id", "unknown"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/config")
async def memory_config(request: Request):
    """Return current memory configuration.

    Reports memory as *unavailable* (rather than falsely claiming
    ``backend_type: sqlite``) when the native ``openjarvis_rust`` extension is
    missing, so the UI can show the real cause instead of a healthy-looking
    config that backs a silent no-op (#502).
    """
    _require_legacy_memory_http_opt_in()
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from openjarvis.core.config import load_config

            config = load_config()
        backend = getattr(request.app.state, "memory_backend", None)
        available = True
        detail: Optional[str] = None
        if backend is None:
            from openjarvis.tools.storage._stubs import MemoryBackendUnavailable

            try:
                from openjarvis.tools.storage.sqlite import SQLiteMemory

                backend = SQLiteMemory()
            except MemoryBackendUnavailable as exc:
                available = False
                detail = str(exc)
            except Exception:
                # Benign: cannot construct a probe backend here, but the
                # configured default is still what would be used.
                pass
        return {
            "backend_type": (
                backend.backend_id
                if backend is not None
                else config.memory.default_backend
            ),
            "available": available,
            "detail": detail,
            "context_top_k": config.memory.context_top_k,
            "context_min_score": config.memory.context_min_score,
            "context_max_tokens": config.memory.context_max_tokens,
            "context_from_memory": config.agent.context_from_memory,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/index")
async def memory_index(req: MemoryIndexRequest, request: Request):
    """Index files from a path into memory."""
    _require_legacy_memory_http_opt_in()
    try:
        import os
        from pathlib import Path

        from openjarvis.security.file_policy import is_sensitive_file
        from openjarvis.tools.storage.ingest import ingest_path

        requested = Path(req.path).expanduser()
        target = requested.resolve()
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

        # The indexing API is fail-closed without explicit workspace roots.
        # It must never become an arbitrary-filesystem read primitive.
        workspace = os.environ.get("OPENJARVIS_WORKSPACE", "").strip()
        if not workspace:
            raise HTTPException(
                status_code=403,
                detail="No memory indexing workspace is configured",
            )
        roots = [
            Path(d).expanduser().resolve()
            for d in workspace.split(os.pathsep)
            if d.strip()
        ]
        if not any(target == root or root in target.parents for root in roots):
            raise HTTPException(
                status_code=403,
                detail="Path is outside the allowed workspace directories.",
            )

        fact_paths = {
            (Path.home() / ".openjarvis" / "memory_facts.jsonl").resolve(),
        }
        app_config = getattr(request.app.state, "config", None)
        configured_facts = getattr(
            getattr(app_config, "memory", None),
            "facts_path",
            None,
        )
        if isinstance(configured_facts, str) and configured_facts.strip():
            fact_paths.add(Path(configured_facts).expanduser().resolve())
        env_facts = os.environ.get("AVA_FACTS_PATH", "").strip()
        if env_facts:
            fact_paths.add(Path(env_facts).expanduser().resolve())

        def _is_quarantined_fact(candidate: Path) -> bool:
            if candidate.name == "memory_facts.jsonl":
                return True
            resolved = candidate.resolve(strict=False)
            if resolved in fact_paths:
                return True
            if not candidate.exists():
                return False
            for facts_path in fact_paths:
                try:
                    if facts_path.exists() and os.path.samefile(candidate, facts_path):
                        return True
                except OSError:
                    continue
            return False

        if (
            requested.is_symlink()
            or _is_quarantined_fact(requested)
            or _is_quarantined_fact(target)
        ):
            raise HTTPException(
                status_code=403,
                detail="Legacy shared Ava facts are quarantined",
            )

        # Validate every descendant before handing the directory to the generic
        # ingester.  Name-only checks are insufficient: a harmless-looking
        # symlink can otherwise escape the workspace and expose the quarantined
        # JSONL (or any sensitive file) when ``ingest_path`` follows it.
        candidates = [target]
        if target.is_dir():
            for directory, directories, filenames in os.walk(target, followlinks=False):
                base = Path(directory)
                candidates.extend(base / name for name in directories)
                candidates.extend(base / name for name in filenames)
        for candidate in candidates:
            if candidate.is_symlink():
                raise HTTPException(
                    status_code=403,
                    detail="Symbolic links are not allowed in memory indexing",
                )
            resolved = candidate.resolve(strict=True)
            if not any(resolved == root or root in resolved.parents for root in roots):
                raise HTTPException(
                    status_code=403,
                    detail="Indexed content escapes the allowed workspace",
                )
            if _is_quarantined_fact(candidate) or _is_quarantined_fact(resolved):
                raise HTTPException(
                    status_code=403,
                    detail="Legacy shared Ava facts are quarantined",
                )
            if resolved.is_file() and is_sensitive_file(resolved):
                raise HTTPException(
                    status_code=403,
                    detail="Refusing to index a sensitive file.",
                )

        backend = _get_memory_backend(request)
        if backend is None:
            raise HTTPException(status_code=503, detail="Memory is not configured")

        chunks = ingest_path(target)
        stored = 0
        for chunk in chunks:
            metadata = {"source": getattr(chunk, "source", str(target))}
            if hasattr(chunk, "metadata") and chunk.metadata:
                metadata.update(chunk.metadata)
            backend.store(chunk.content, metadata=metadata)
            stored += 1

        result = {"status": "indexed", "chunks_indexed": stored}
        if stored == 0:
            # "indexed" must never silently mean "stored nothing". Surface why
            # so a folder of short notes doesn't look like a successful no-op
            # (#502 follow-up).
            result["note"] = (
                "no content was indexed — the path contained no readable "
                "documents with indexable text"
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Traces routes ----

traces_router = APIRouter(prefix="/v1/traces", tags=["traces"])

_TOOL_PROOF_SCHEMA = "ava.tool-execution-proof/v1"
_TOOL_PROOF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TOOL_PROOF_MAX_STEPS = 256
_TOOL_PROOF_MAX_CALLS = 64
_TOOL_PROOF_MAX_DISTINCT = 16


def _require_trace_principal(request: Request):
    """Return the verified principal that owns a trace-facing request.

    The daemon API key authenticates an application, not the person whose
    conversation a trace contains.  Trace content and feedback therefore need
    the same cryptographic principal boundary as durable conversations.
    """

    from ava_extensions.server.principal import resolve_request_principal

    principal = resolve_request_principal(request.headers)
    if principal is None:
        raise HTTPException(status_code=401, detail="Ava identity required")
    return principal


def _trace_owned_by(trace: Any, provenance: str) -> bool:
    metadata = getattr(trace, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("provenance") == provenance


def _serialise_trace(trace) -> dict:
    """Convert a Trace dataclass to a frontend-friendly dict."""
    import datetime
    from dataclasses import asdict

    d = asdict(trace)
    d["id"] = d.pop("trace_id", "")
    started = d.pop("started_at", 0.0)
    d["created_at"] = (
        datetime.datetime.fromtimestamp(started, tz=datetime.timezone.utc).isoformat()
        if started
        else None
    )
    dur = d.pop("total_latency_seconds", 0.0)
    d["duration_ms"] = round(dur * 1000)
    for step in d.get("steps", []):
        st = step.get("step_type")
        if hasattr(st, "value"):
            step["step_type"] = st.value
    return d


def _tool_execution_proof(trace: Any) -> dict[str, Any]:
    """Project one trace to a bounded, content-free execution proof.

    Tool arguments, outputs, metadata, timings and errors deliberately never cross
    this boundary.  The names and success bits come from server-owned execution
    events recorded after capability and confirmation checks, not from model prose.
    """

    from openjarvis.core.types import StepType

    trace_id = getattr(trace, "trace_id", None)
    result = getattr(trace, "result", None)
    steps = getattr(trace, "steps", None)
    if (
        not isinstance(trace_id, str)
        or not trace_id
        or not isinstance(steps, list)
        or len(steps) > _TOOL_PROOF_MAX_STEPS
    ):
        raise ValueError("invalid trace")

    aggregate: dict[str, list[int]] = {}
    call_count = 0
    for step in steps:
        step_type = getattr(step, "step_type", None)
        if step_type not in {StepType.TOOL_CALL, StepType.TOOL_CALL.value}:
            continue
        call_count += 1
        if call_count > _TOOL_PROOF_MAX_CALLS:
            raise ValueError("too many tool calls")
        input_data = getattr(step, "input", None)
        output_data = getattr(step, "output", None)
        if not isinstance(input_data, dict) or not isinstance(output_data, dict):
            raise ValueError("invalid tool step")
        name = input_data.get("tool")
        success = output_data.get("success")
        if (
            not isinstance(name, str)
            or _TOOL_PROOF_NAME.fullmatch(name) is None
            or type(success) is not bool
        ):
            raise ValueError("invalid tool step")
        counts = aggregate.setdefault(name, [0, 0])
        if len(aggregate) > _TOOL_PROOF_MAX_DISTINCT:
            raise ValueError("too many distinct tools")
        counts[0] += 1
        counts[1] += int(success)

    calls = [
        {
            "tool": name,
            "count": count,
            "successes": successes,
            "failures": count - successes,
        }
        for name, (count, successes) in sorted(aggregate.items())
    ]
    return {
        "schema": _TOOL_PROOF_SCHEMA,
        "trace_id": trace_id,
        "complete": (
            getattr(trace, "outcome", None) == "completed"
            and isinstance(result, str)
            and bool(result.strip())
            and all(call["failures"] == 0 for call in calls)
        ),
        "call_count": call_count,
        "calls": calls,
    }


@traces_router.get("")
async def list_traces(request: Request, limit: int = 20):
    """List recent traces belonging to the verified request principal."""
    principal = _require_trace_principal(request)
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            return {"traces": []}
        traces = store.list_traces(
            provenance=principal.provenance,
            limit=max(1, min(limit, 100)),
        )
        items = [_serialise_trace(t) for t in traces]
        return {"traces": items}
    except Exception as exc:
        return {"traces": [], "error": str(exc)}


@traces_router.get("/{trace_id}/tool-execution-proof")
async def get_tool_execution_proof(trace_id: str, request: Request):
    """Return only a bounded proof of executed tools for the trace owner."""

    principal = _require_trace_principal(request)
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        trace = store.get(trace_id)
        if trace is None or not _trace_owned_by(trace, principal.provenance):
            raise HTTPException(status_code=404, detail="Trace not found")
        return _tool_execution_proof(trace)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Invalid trace tool proof", exc_info=True)
        raise HTTPException(status_code=409, detail="Trace proof unavailable") from None


@traces_router.get("/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """Get a specific trace only when it belongs to the request principal."""
    principal = _require_trace_principal(request)
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        trace = store.get(trace_id)
        if trace is None or not _trace_owned_by(trace, principal.provenance):
            raise HTTPException(status_code=404, detail="Trace not found")
        return _serialise_trace(trace)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Telemetry routes ----

telemetry_router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])


@telemetry_router.get("/stats")
async def telemetry_stats(request: Request):
    """Get aggregated telemetry statistics."""
    try:
        from dataclasses import asdict

        from openjarvis.core.config import DEFAULT_CONFIG_DIR
        from openjarvis.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            return {"total_requests": 0, "total_tokens": 0}

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            d = asdict(stats)
            d.pop("per_model", None)
            d.pop("per_engine", None)
            d["total_requests"] = d.pop("total_calls", 0)
            return d
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


@telemetry_router.get("/energy")
async def telemetry_energy(request: Request):
    """Get energy monitoring data."""
    try:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR
        from openjarvis.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            return {
                "total_energy_j": 0,
                "energy_per_token_j": 0,
                "avg_power_w": 0,
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            total_energy = stats.total_energy_joules
            total_tokens = stats.total_tokens
            total_latency = stats.total_latency
            return {
                "total_energy_j": total_energy,
                "energy_per_token_j": (
                    total_energy / total_tokens if total_tokens > 0 else 0
                ),
                "avg_power_w": (
                    total_energy / total_latency if total_latency > 0 else 0
                ),
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


# ---- Skills routes ----

skills_router = APIRouter(prefix="/v1/skills", tags=["skills"])


@skills_router.get("")
async def list_skills(request: Request):
    """List installed skills."""
    try:
        from openjarvis.core.registry import SkillRegistry

        skills = []
        for key in sorted(SkillRegistry.keys()):
            skills.append({"name": key})
        return {"skills": skills}
    except Exception as exc:
        logger.warning("Failed to list skills: %s", exc)
        return {"skills": []}


@skills_router.post("")
async def install_skill(request: Request):
    """Install a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Use TOML files in ~/.openjarvis/skills/",
    }


@skills_router.delete("/{skill_name}")
async def remove_skill(skill_name: str, request: Request):
    """Remove a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Skill removal not yet supported via API",
    }


# ---- Sessions routes ----

sessions_router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@sessions_router.get("")
async def list_sessions(request: Request, limit: int = 20):
    """The unscoped legacy session API is quarantined.

    It referenced a non-existent store module and returned a misleading empty
    success.  Wiring it to either real store would expose cross-channel history
    without a verified-principal ownership model.  Ava's durable conversation
    route is the supported, principal-scoped replacement.
    """

    del request, limit
    raise HTTPException(
        status_code=410,
        detail="Legacy session API is quarantined; use Ava conversation history",
    )


@sessions_router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    """Reject direct reads from the unscoped legacy session store."""

    del session_id, request
    raise HTTPException(
        status_code=410,
        detail="Legacy session API is quarantined; use Ava conversation history",
    )


# ---- Budget routes ----

budget_router = APIRouter(prefix="/v1/budget", tags=["budget"])

_budget_limits: Dict[str, Any] = {
    "max_tokens_per_day": None,
    "max_requests_per_hour": None,
}
_budget_usage: Dict[str, int] = {
    "tokens_today": 0,
    "requests_this_hour": 0,
}


@budget_router.get("")
async def get_budget(request: Request):
    """Get current budget usage and limits."""
    return {"limits": _budget_limits, "usage": _budget_usage}


@budget_router.put("/limits")
async def set_budget_limits(req: BudgetLimitsRequest, request: Request):
    """Update budget limits."""
    if req.max_tokens_per_day is not None:
        _budget_limits["max_tokens_per_day"] = req.max_tokens_per_day
    if req.max_requests_per_hour is not None:
        _budget_limits["max_requests_per_hour"] = req.max_requests_per_hour
    return {"status": "updated", "limits": _budget_limits}


# ---- Prometheus metrics ----

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus-compatible metrics endpoint."""
    try:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR
        from openjarvis.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("# no telemetry data\n", media_type="text/plain")

        agg = TelemetryAggregator(db_path)
        stats = agg.summary()

        lines = [
            "# HELP openjarvis_requests_total Total requests processed",
            "# TYPE openjarvis_requests_total counter",
            f"openjarvis_requests_total {stats.get('total_requests', 0)}",
            "# HELP openjarvis_tokens_total Total tokens generated",
            "# TYPE openjarvis_tokens_total counter",
            f"openjarvis_tokens_total {stats.get('total_tokens', 0)}",
            "# HELP openjarvis_latency_avg_ms Average latency in milliseconds",
            "# TYPE openjarvis_latency_avg_ms gauge",
            f"openjarvis_latency_avg_ms {stats.get('avg_latency_ms', 0)}",
        ]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("# No metrics available\n", media_type="text/plain")


# ---- WebSocket streaming routes ----

websocket_router = APIRouter(tags=["websocket"])


def _record_ws_trace(
    trace_store,
    *,
    query: str,
    result: str,
    model: str,
    started_at: float,
    ended_at: float,
    provenance: str | None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Record a trace for a completed WebSocket chat (best-effort)."""
    if trace_store is None or not result:
        return
    from openjarvis.traces.collector import record_response_trace

    record_response_trace(
        trace_store,
        query=query,
        result=result,
        model=model,
        started_at=started_at,
        ended_at=ended_at,
        provenance=provenance,
        metadata=metadata,
    )


class _WebSocketIdentityRejectedError(RuntimeError):
    """A supplied Ava credential did not establish exactly one principal."""


async def _websocket_trust_context(websocket: WebSocket):
    """Resolve the same identity, relationship and persona boundary as HTTP chat."""

    from openjarvis.server.routes import (
        _base_identity_prompt,
        _identity_header_present,
        _prepare_relationship_guard_or_503,
        _relationship_context,
    )

    relationship_context = await asyncio.to_thread(
        _relationship_context,
        websocket.headers,
    )
    principal_context = None
    if len(relationship_context) == 2:
        principal, relationship_overlay = relationship_context
    elif len(relationship_context) == 3:
        principal, relationship_overlay, _protect_legacy_memory = relationship_context
    elif len(relationship_context) == 4:
        (
            principal,
            relationship_overlay,
            _protect_legacy_memory,
            principal_context,
        ) = relationship_context
    else:
        raise RuntimeError("invalid Ava identity context result")
    if principal is None and _identity_header_present(websocket.headers):
        raise _WebSocketIdentityRejectedError
    base_identity_prompt = await asyncio.to_thread(
        _base_identity_prompt,
        getattr(websocket.app.state, "config", None),
    )
    from ava_extensions.identity.principal_context import (
        compose_principal_context_prompt,
    )

    base_identity_prompt = compose_principal_context_prompt(
        base_identity_prompt,
        principal_context,
        display_name_already_present=bool(
            relationship_overlay is not None and relationship_overlay.display_name
        ),
    )
    relationship_guard = _prepare_relationship_guard_or_503(relationship_overlay)
    return (
        principal,
        relationship_overlay,
        base_identity_prompt,
        relationship_guard,
    )


async def _close_untrusted_websocket(websocket: WebSocket, exc: Exception) -> None:
    """Reject a handshake without exposing verifier or policy diagnostics."""

    from ava_extensions.identity.relationship import RelationshipPolicyError

    if isinstance(exc, _WebSocketIdentityRejectedError):
        code = 1008
        reason = "Ava identity rejected"
    elif isinstance(exc, HTTPException) and exc.detail == (
        "Ava relationship output policy unavailable"
    ):
        code = 1011
        reason = "Ava relationship output policy unavailable"
    elif isinstance(exc, RelationshipPolicyError):
        code = 1011
        reason = "Ava relationship policy unavailable"
    else:
        code = 1011
        reason = "Ava identity unavailable"
    logger.warning("WebSocket chat rejected before model (%s)", type(exc).__name__)
    await websocket.close(code=code, reason=reason)


@websocket_router.websocket("/v1/chat/stream")
async def websocket_chat_stream(websocket: WebSocket):
    """Stream chat responses over a WebSocket connection.

    Accepts JSON messages of the form::

        {"message": "...", "model": "...", "agent": "..."}

    Sends back JSON chunks::

        {"type": "chunk", "content": "..."}   -- per-token streaming
        {"type": "done",  "content": "..."}   -- final assembled response
        {"type": "error", "detail": "..."}    -- on failure
    """
    from openjarvis.server.auth_middleware import websocket_authorized

    expected_key = getattr(websocket.app.state, "api_key", "")
    if not websocket_authorized(websocket, expected_key):
        # 1008 = policy violation; reject before accepting the connection.
        await websocket.close(code=1008)
        return
    try:
        # Reject static credential/policy failures before accepting the channel.
        await _websocket_trust_context(websocket)
    except Exception as exc:  # noqa: BLE001 - all trust failures are fail-closed
        await _close_untrusted_websocket(websocket, exc)
        return
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid JSON"},
                )
                continue

            message = data.get("message")
            if not isinstance(message, str) or not message.strip():
                await websocket.send_json(
                    {"type": "error", "detail": "Missing 'message' field"},
                )
                continue

            try:
                # Re-read the policy for every turn so revocation also closes an
                # already established WebSocket before another model call.
                (
                    principal,
                    relationship_overlay,
                    base_identity_prompt,
                    relationship_guard,
                ) = await _websocket_trust_context(websocket)
            except Exception as exc:  # noqa: BLE001 - generic client response
                logger.warning(
                    "WebSocket chat trust context became unavailable (%s)",
                    type(exc).__name__,
                )
                await websocket.send_json(
                    {"type": "error", "detail": "Ava identity unavailable"},
                )
                await websocket.close(code=1011)
                return

            model = data.get("model") or getattr(
                websocket.app.state,
                "model",
                "default",
            )
            engine = getattr(websocket.app.state, "engine", None)
            if engine is None:
                await websocket.send_json(
                    {"type": "error", "detail": "Chat unavailable"},
                )
                continue

            from openjarvis.core.types import Message, Role
            from openjarvis.server.routes import (
                _RELATIONSHIP_STREAM_BUFFER_BYTES,
                _apply_relationship_guard,
                _assembled_stream_tool_calls,
                _bind_relationship_guard,
                _copy_engine_for_relationship_events,
                _ensure_identity_prompt,
                _motif_arret,
                _reject_client_temporal_context_marker,
                _relationship_request_event_bus,
                _runtime_completion_limit,
            )

            if "temporal_context" in data:
                await websocket.send_json(
                    {
                        "type": "error",
                        "detail": "Ava temporal context rejected",
                    }
                )
                continue
            try:
                _reject_client_temporal_context_marker([message])
            except HTTPException as exc:
                if exc.status_code == 503:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "detail": "Ava temporal context unavailable",
                        }
                    )
                    await websocket.close(code=1011)
                    return
                await websocket.send_json(
                    {
                        "type": "error",
                        "detail": "Ava temporal context rejected",
                    }
                )
                continue

            messages = _ensure_identity_prompt(
                [Message(role=Role.USER, content=message)],
                base_identity_prompt,
                relationship_overlay,
            )
            relationship_guard = _bind_relationship_guard(
                relationship_guard,
                messages,
            )
            if relationship_guard is not None:
                event_bus = getattr(websocket.app.state, "bus", None)
                if event_bus is None:
                    event_bus = getattr(engine, "_bus", None)
                if event_bus is not None:
                    request_bus = _relationship_request_event_bus(
                        event_bus,
                        relationship_guard,
                    )
                    engine = _copy_engine_for_relationship_events(
                        engine,
                        request_bus,
                    )
            max_tokens = _runtime_completion_limit(websocket)

            # This WS path streams straight from the engine (no agent /
            # TraceCollector), so record the interaction directly once it
            # finishes — otherwise WebSocket chats never reach traces.db.
            import time as _time

            trace_store = getattr(websocket.app.state, "trace_store", None)
            _ws_started_at = _time.time()

            try:
                # Only the rich stream can prove whether the provider completed
                # normally.  The legacy text-only stream erases max-token and
                # transport terminals, so it must not be treated as success.
                stream_fn = getattr(engine, "stream_full", None)
                if stream_fn is not None and inspect.isasyncgenfunction(stream_fn):
                    full_content = ""
                    terminal_reason: Optional[str] = None
                    buffered_content: list[str] = []
                    buffered_bytes = 0
                    tool_call_batches: list[list[dict[str, Any]]] = []
                    gen = stream_fn(
                        messages,
                        model=model,
                        max_tokens=max_tokens,
                    )
                    async for chunk in gen:
                        content = chunk.content or ""
                        if content:
                            if relationship_guard is not None:
                                content_bytes = len(content.encode("utf-8"))
                                if content_bytes > (
                                    _RELATIONSHIP_STREAM_BUFFER_BYTES - buffered_bytes
                                ):
                                    raise RuntimeError(
                                        "relationship websocket exceeds output buffer"
                                    )
                                buffered_bytes += content_bytes
                                buffered_content.append(content)
                            full_content += content
                            if relationship_guard is None:
                                await websocket.send_json(
                                    {"type": "chunk", "content": content},
                                )
                        if relationship_guard is not None and chunk.tool_calls:
                            encoded_tool_calls = json.dumps(
                                chunk.tool_calls,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                            if len(encoded_tool_calls) > (
                                _RELATIONSHIP_STREAM_BUFFER_BYTES - buffered_bytes
                            ):
                                raise RuntimeError(
                                    "relationship websocket exceeds output buffer"
                                )
                            buffered_bytes += len(encoded_tool_calls)
                            tool_call_batches.append(chunk.tool_calls)
                        if chunk.finish_reason is not None:
                            terminal_reason = _motif_arret(
                                {"finish_reason": chunk.finish_reason}
                            )
                    if terminal_reason != "stop" or not full_content.strip():
                        await websocket.send_json(
                            {"type": "error", "detail": "Chat response incomplete"},
                        )
                        continue
                    effective_content = full_content
                    relationship_trace_metadata = None
                    if relationship_guard is not None:
                        try:
                            decision = _apply_relationship_guard(
                                relationship_guard,
                                full_content,
                                _assembled_stream_tool_calls(tool_call_batches),
                            )
                            relationship_trace_metadata = relationship_guard.metadata()
                            if not isinstance(relationship_trace_metadata, dict):
                                raise RuntimeError(
                                    "relationship guard metadata is invalid"
                                )
                        except Exception:
                            logger.warning(
                                "WebSocket relationship policy failed before emission"
                            )
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "detail": "Chat output policy unavailable",
                                }
                            )
                            continue
                        effective_content = decision.output_text
                        chunks_to_emit = (
                            [effective_content]
                            if decision.action == "replace"
                            else buffered_content
                        )
                        for buffered_content_chunk in chunks_to_emit:
                            await websocket.send_json(
                                {
                                    "type": "chunk",
                                    "content": buffered_content_chunk,
                                }
                            )
                    await websocket.send_json(
                        {"type": "done", "content": effective_content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=effective_content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                        provenance=(
                            principal.provenance if principal is not None else None
                        ),
                        metadata=relationship_trace_metadata,
                    )
                else:
                    # No rich stream — single-shot generate. Blocking upstream
                    # call, so run in a worker thread to keep the event loop free.
                    result = await asyncio.to_thread(
                        engine.generate,
                        messages,
                        model=model,
                        max_tokens=max_tokens,
                    )
                    content = (
                        result.get("content", "")
                        if isinstance(
                            result,
                            dict,
                        )
                        else ""
                    )
                    terminal_reason = _motif_arret(
                        {
                            "finish_reason": (
                                result.get("finish_reason")
                                if isinstance(result, dict)
                                else None
                            )
                        }
                    )
                    if content and relationship_guard is None:
                        await websocket.send_json(
                            {"type": "chunk", "content": content},
                        )
                    if terminal_reason != "stop" or not content.strip():
                        await websocket.send_json(
                            {"type": "error", "detail": "Chat response incomplete"},
                        )
                        continue
                    effective_content = content
                    relationship_trace_metadata = None
                    if relationship_guard is not None:
                        try:
                            if len(content.encode("utf-8")) > (
                                _RELATIONSHIP_STREAM_BUFFER_BYTES
                            ):
                                raise RuntimeError(
                                    "relationship websocket exceeds output buffer"
                                )
                            decision = _apply_relationship_guard(
                                relationship_guard,
                                content,
                                (
                                    result.get("tool_calls")
                                    if isinstance(result, dict)
                                    else None
                                ),
                            )
                            relationship_trace_metadata = relationship_guard.metadata()
                            if not isinstance(relationship_trace_metadata, dict):
                                raise RuntimeError(
                                    "relationship guard metadata is invalid"
                                )
                        except Exception:
                            logger.warning(
                                "WebSocket relationship policy failed before emission"
                            )
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "detail": "Chat output policy unavailable",
                                }
                            )
                            continue
                        effective_content = decision.output_text
                        await websocket.send_json(
                            {"type": "chunk", "content": effective_content},
                        )
                    await websocket.send_json(
                        {"type": "done", "content": effective_content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=effective_content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                        provenance=(
                            principal.provenance if principal is not None else None
                        ),
                        metadata=relationship_trace_metadata,
                    )
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                logger.warning(
                    "WebSocket chat generation failed (%s)",
                    type(exc).__name__,
                )
                await websocket.send_json(
                    {"type": "error", "detail": "Chat generation failed"},
                )
    except WebSocketDisconnect:
        pass  # Client disconnected — nothing to clean up


# ---- Learning routes ----

learning_router = APIRouter(prefix="/v1/learning", tags=["learning"])


@learning_router.get("/stats")
async def learning_stats(request: Request):
    """Return learning system statistics across all sub-policies."""
    result: Dict[str, Any] = {}

    # Skill discovery
    try:
        from openjarvis.learning.agents.skill_discovery import SkillDiscovery

        discovery = SkillDiscovery()
        result["skill_discovery"] = {
            "available": True,
            "discovered_count": len(discovery.discovered_skills),
        }
    except Exception as exc:
        logger.warning("Failed to load skill discovery stats: %s", exc)
        result["skill_discovery"] = {"available": False}

    return result


@learning_router.get("/policy")
async def learning_policy(request: Request):
    """Return current routing policy configuration."""
    result: Dict[str, Any] = {}

    # Load config and extract learning section
    try:
        from openjarvis.core.config import load_config

        config = load_config()
        lc = config.learning
        result["enabled"] = lc.enabled
        result["update_interval"] = lc.update_interval
        result["auto_update"] = lc.auto_update
        result["routing"] = {
            "policy": lc.routing.policy,
            "min_samples": lc.routing.min_samples,
        }
        result["intelligence"] = {
            "policy": lc.intelligence.policy,
        }
        result["agent"] = {
            "policy": lc.agent.policy,
        }
        result["metrics"] = {
            "accuracy_weight": lc.metrics.accuracy_weight,
            "latency_weight": lc.metrics.latency_weight,
            "cost_weight": lc.metrics.cost_weight,
            "efficiency_weight": lc.metrics.efficiency_weight,
        }
    except Exception as exc:
        logger.warning("Failed to load learning config: %s", exc)
        result["enabled"] = False
        result["routing"] = {"policy": "heuristic", "min_samples": 5}
        result["intelligence"] = {"policy": "none"}
        result["agent"] = {"policy": "none"}
        result["metrics"] = {}

    return result


# ---- Speech routes ----

speech_router = APIRouter(prefix="/v1/speech", tags=["speech"])


@speech_router.post("/transcribe")
async def transcribe_speech(request: Request):
    """Transcribe uploaded audio to text."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        raise HTTPException(status_code=501, detail="Speech backend not configured")

    form = await request.form()
    audio_file = form.get("file")
    if audio_file is None:
        raise HTTPException(status_code=400, detail="Missing 'file' field")

    audio_bytes = await audio_file.read()
    if len(audio_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 5 MB)")
    language = form.get("language")

    # Detect format from filename
    filename = getattr(audio_file, "filename", "audio.wav")
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "wav"

    try:
        result = await asyncio.to_thread(
            backend.transcribe,
            audio_bytes,
            format=ext,
            language=language or None,
        )
    except Exception as exc:
        logger.exception("Speech transcription failed")
        raise HTTPException(
            status_code=500,
            detail=f"Speech transcription failed: {exc}",
        ) from exc

    return {
        "text": result.text,
        "language": result.language,
        "confidence": result.confidence,
        "duration_seconds": result.duration_seconds,
    }


@speech_router.get("/health")
async def speech_health(request: Request):
    """Check if a speech backend is available."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        return {"available": False, "reason": "No speech backend configured"}
    try:
        available = backend.health()
        reason = None
    except Exception as exc:
        logger.exception("Speech health check failed")
        available = False
        reason = str(exc)

    if not available and reason is None:
        last_error = getattr(backend, "last_error", None)
        if callable(last_error):
            reason = last_error()

    return {
        "available": available,
        "backend": backend.backend_id,
        **({"reason": reason} if reason else {}),
    }


# ---- Feedback routes ----

feedback_router = APIRouter(prefix="/v1/feedback", tags=["feedback"])


@feedback_router.post("")
async def submit_feedback(req: FeedbackScoreRequest, request: Request):
    """Submit feedback only for a trace owned by the verified principal."""
    principal = _require_trace_principal(request)
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="No trace database")
        trace = store.get(req.trace_id)
        if trace is None or not _trace_owned_by(trace, principal.provenance):
            # Do not reveal whether another principal owns the identifier.
            raise HTTPException(status_code=404, detail="Trace not found")
        updated = store.update_feedback(req.trace_id, req.score)

        if not updated:
            raise HTTPException(
                status_code=404, detail=f"Trace '{req.trace_id}' not found"
            )
        return {"status": "recorded", "trace_id": req.trace_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@feedback_router.get("/stats")
async def feedback_stats(request: Request):
    """Statistiques REELLES des retours, et taux de reussite par outil.

    ⚠ CETTE ROUTE RENVOYAIT `{"total": 0, "mean_score": 0.0}` EN DUR, quoi
      qu'il y ait en base. Une API qui repond « aucun retour » alors qu'il y
      en a est pire qu'une API absente : on en conclut que le mecanisme ne
      sert a rien, et on cesse de l'alimenter.
      Defaut trouve le 2026-08-05 en cherchant pourquoi la boucle d'apprentissage
      n'apprenait rien — reponse : elle n'avait jamais tourne (0 note, 0 outcome sur 65
      traces), et ce point de mesure ne pouvait pas le montrer.
    ⚠ Elle expose aussi `per_tool`, que `TraceAnalyzer` calculait DEJA sans
      qu'aucune route ne le rende lisible. C'est ce chiffre-la qui dit ou
      porter l'effort d'entrainement.
    """
    principal = _require_trace_principal(request)
    try:
        from openjarvis.traces.analyzer import TraceAnalyzer

        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            # ⚠ On DIT qu'on ne sait pas plutot que de rendre des zeros :
            #   « pas de base »
            #   et « aucun retour » ne sont pas la meme chose.
            raise HTTPException(status_code=404, detail="No trace database")

        notes = [
            t.feedback
            for t in store.list_traces(
                provenance=principal.provenance,
                limit=10000,
            )
            if getattr(t, "feedback", None) is not None
        ]
        analyseur = TraceAnalyzer(store)
        resume = analyseur.summary(provenance=principal.provenance)
        par_outil = analyseur.per_tool_stats(provenance=principal.provenance)

        return {
            "total": len(notes),
            "mean_score": (sum(notes) / len(notes)) if notes else None,
            # ⚠ `total_traces`, PAS un `evaluated` que j'avais DEVINE : `TraceSummary`
            #   ne porte pas ce champ, donc le `getattr` rendait `null` — un chiffre
            #   creux dans la route ecrite justement pour denoncer les chiffres creux.
            #   Champs reels : total_traces, total_steps, avg_latency, avg_tokens,
            #   success_rate, total_energy_joules.
            "traces_totales": getattr(resume, "total_traces", None),
            "taux_reussite": getattr(resume, "success_rate", None),
            "per_tool": par_outil,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Optimize routes ----

optimize_router = APIRouter(prefix="/v1/optimize", tags=["optimize"])


@optimize_router.get("/runs")
async def list_optimize_runs(request: Request):
    """List optimization runs."""
    try:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR
        from openjarvis.learning.optimize.store import OptimizationStore

        db_path = DEFAULT_CONFIG_DIR / "optimize.db"
        if not db_path.exists():
            return {"runs": []}

        store = OptimizationStore(db_path)
        runs = store.list_runs()
        store.close()
        return {"runs": runs}
    except Exception as exc:
        logger.warning("Failed to list optimization runs: %s", exc)
        return {"runs": []}


@optimize_router.get("/runs/{run_id}")
async def get_optimize_run(run_id: str, request: Request):
    """Get optimization run details."""
    try:
        from openjarvis.core.config import DEFAULT_CONFIG_DIR
        from openjarvis.learning.optimize.store import OptimizationStore

        db_path = DEFAULT_CONFIG_DIR / "optimize.db"
        if not db_path.exists():
            return {"run_id": run_id, "status": "not_found"}

        store = OptimizationStore(db_path)
        run = store.get_run(run_id)
        store.close()

        if run is None:
            return {"run_id": run_id, "status": "not_found"}

        return {
            "run_id": run.run_id,
            "status": run.status,
            "benchmark": run.benchmark,
            "trials": len(run.trials),
            "best_trial_id": (run.best_trial.trial_id if run.best_trial else None),
        }
    except Exception as exc:
        logger.warning("Failed to get optimization run %s: %s", run_id, exc)
        return {"run_id": run_id, "status": "not_found"}


@optimize_router.post("/runs")
async def start_optimize_run(req: OptimizeRunRequest, request: Request):
    """Start a new optimization run."""
    return {"status": "started", "run_id": "placeholder"}


def include_all_routes(app) -> None:
    """Include all extended API routers in a FastAPI app."""
    from openjarvis.server.approval_routes import (
        router as approval_router,  # noqa: PLC0415
    )

    app.include_router(approval_router)
    app.include_router(agents_router)
    app.include_router(memory_router)
    app.include_router(traces_router)
    app.include_router(telemetry_router)
    app.include_router(skills_router)
    app.include_router(sessions_router)
    app.include_router(budget_router)
    app.include_router(metrics_router)
    app.include_router(websocket_router)
    app.include_router(learning_router)
    app.include_router(speech_router)
    app.include_router(feedback_router)
    app.include_router(optimize_router)

    # Agent Manager routes (if available)
    try:
        if hasattr(app.state, "agent_manager") and app.state.agent_manager:
            from openjarvis.server.agent_manager_routes import (  # noqa: PLC0415
                create_agent_manager_router,
            )

            (
                agents_r,
                templates_r,
                global_r,
                tools_r,
                sendblue_r,
            ) = create_agent_manager_router(app.state.agent_manager)
            app.include_router(agents_r)
            app.include_router(templates_r)
            app.include_router(global_r)
            app.include_router(tools_r)
            app.include_router(sendblue_r)
    except ImportError:
        pass

    # WebSocket bridge for real-time agent events
    try:
        from openjarvis.core.events import get_event_bus
        from openjarvis.server.ws_bridge import create_ws_router

        ws_router = create_ws_router(get_event_bus())
        app.include_router(ws_router)
    except Exception:
        logger.debug("WebSocket bridge not available", exc_info=True)


__all__ = [
    "include_all_routes",
    "agents_router",
    "memory_router",
    "traces_router",
    "telemetry_router",
    "skills_router",
    "sessions_router",
    "budget_router",
    "metrics_router",
    "websocket_router",
    "learning_router",
    "speech_router",
    "feedback_router",
    "optimize_router",
]
