"""Persistent agent lifecycle manager.

Composition layer — stores agent state in SQLite, delegates all computation
to the five existing primitives (Intelligence, Agent, Tools, Engine, Learning).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from functools import wraps
from typing import Any, Dict, List, Optional
from uuid import uuid4

from openjarvis.core.paths import get_config_dir

logger = logging.getLogger(__name__)


def _serialized_database(method):
    """Serialize access to the manager's single SQLite connection.

    ``check_same_thread=False`` only permits cross-thread use; it does not make
    one connection safe for concurrent transactions.  The manager is shared by
    FastAPI workers, response background tasks and the scheduler, so every
    compound operation must hold one re-entrant lock from its first statement
    through commit/rollback.
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._database_lock:
            return method(self, *args, **kwargs)

    return wrapped


_CREATE_AGENTS = """\
CREATE TABLE IF NOT EXISTS managed_agents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    agent_type      TEXT NOT NULL DEFAULT 'monitor_operative',
    config_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'idle',
    tick_token      TEXT,
    summary_memory  TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""

_CREATE_TASKS = """\
CREATE TABLE IF NOT EXISTS agent_tasks (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES managed_agents(id),
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    progress_json   TEXT NOT NULL DEFAULT '{}',
    findings_json   TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL
);
"""

_CREATE_BINDINGS = """\
CREATE TABLE IF NOT EXISTS channel_bindings (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES managed_agents(id),
    channel_type    TEXT NOT NULL,
    config_json     TEXT NOT NULL DEFAULT '{}',
    session_id      TEXT,
    routing_mode    TEXT NOT NULL DEFAULT 'dedicated'
);
"""

_CREATE_CHECKPOINTS = """\
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES managed_agents(id),
    tick_id TEXT NOT NULL,
    conversation_state TEXT NOT NULL DEFAULT '{}',
    tool_state TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""

_CREATE_MESSAGES = """\
CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES managed_agents(id),
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'queued',
    status TEXT NOT NULL DEFAULT 'pending',
    reply_to_id TEXT,
    created_at REAL NOT NULL
);
"""

_CREATE_LEARNING_LOG = """\
CREATE TABLE IF NOT EXISTS agent_learning_log (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT,
    data TEXT,
    created_at REAL NOT NULL
);
"""

# Rolling summary fed back into the next tick's prompt, so it stays bounded —
# but 2000 chars clipped real research reports mid-sentence (the findings the
# UI/CLI show come from here). 16k (~4k tokens) holds a full report while
# keeping per-tick prompt growth in check.
_SUMMARY_MAX = 16000
MAX_AGENT_MESSAGE_CHARS = 4 * 32_768
MAX_AGENT_TOOL_CALLS_BYTES = 2 * MAX_AGENT_MESSAGE_CHARS


class AgentManager:
    """Persistent agent lifecycle manager with SQLite backing."""

    def __init__(self, db_path: str, *, clear_stale_running: bool = False) -> None:
        self._db_path = str(db_path)
        self._database_lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(_CREATE_AGENTS)
        self._conn.execute(_CREATE_TASKS)
        self._conn.execute(_CREATE_BINDINGS)
        self._conn.executescript(_CREATE_CHECKPOINTS)
        self._conn.executescript(_CREATE_MESSAGES)
        self._conn.executescript(_CREATE_LEARNING_LOG)
        self._conn.commit()
        # Schema migrations for runtime columns
        _MIGRATIONS = [
            "ALTER TABLE managed_agents ADD COLUMN total_tokens INTEGER DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN total_cost REAL DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN total_runs INTEGER DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN last_run_at REAL",
            "ALTER TABLE managed_agents ADD COLUMN last_activity_at REAL",
            "ALTER TABLE managed_agents ADD COLUMN stall_retries INTEGER DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN current_activity TEXT DEFAULT ''",
            "ALTER TABLE managed_agents ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN output_tokens INTEGER DEFAULT 0",
            "ALTER TABLE managed_agents ADD COLUMN tick_token TEXT",
            # JSON-encoded array of {tool, arguments, result, success, latency}
            "ALTER TABLE agent_messages ADD COLUMN tool_calls TEXT",
            # Exact user-message linkage for concurrent streaming turns.
            "ALTER TABLE agent_messages ADD COLUMN reply_to_id TEXT",
        ]
        for migration in _MIGRATIONS:
            try:
                self._conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_messages_reply"
            " ON agent_messages(agent_id, reply_to_id)"
            " WHERE direction = 'agent_to_user' AND reply_to_id IS NOT NULL"
        )
        self._conn.commit()
        # Only the authoritative long-running process (the API server, which
        # owns the scheduler) may sweep running→idle on boot. Short-lived CLI
        # commands (`jarvis agents list/info/...`) and the SystemBuilder path
        # used by `run`/`ask` MUST NOT: they share this DB with a server that
        # may be mid-tick, and an unconditional sweep here flips an actively
        # running agent back to "idle" — which is exactly why `list` reported
        # "idle" while a tick was running elsewhere. Only the authoritative
        # server startup may recover crash leftovers; start_tick never guesses
        # that a slow worker is dead from elapsed time alone.
        if clear_stale_running:
            self._clear_stale_running_state()

    @_serialized_database
    def _clear_stale_running_state(self) -> None:
        """Reset any agent stuck in ``status='running'`` on startup.

        Tick worker threads are ``daemon=True`` — when the server process
        exits (SIGTERM, crash, restart), they die without running the
        ``finally`` clause that calls :meth:`end_tick`, leaving the DB row
        in ``running`` forever. The :meth:`start_tick` guard then rejects
        every subsequent run with "Agent is already running".

        The server boot holds zero tick locks by definition, so any persisted
        ``running`` is a zombie. Sweep it back to ``idle`` and clear the
        activity string so the UI doesn't show a stale "Preparing tick..."
        indicator. Call this only from a process that owns tick execution.
        """
        cur = self._conn.execute(
            "UPDATE managed_agents SET status = 'idle', tick_token = NULL,"
            " current_activity = '',"
            " updated_at = ? WHERE status = 'running'",
            (time.time(),),
        )
        self._conn.commit()
        if cur.rowcount:
            logger.info(
                "AgentManager: cleared stale 'running' status on %d agent(s)",
                cur.rowcount,
            )
        orphaned = self._conn.execute(
            "UPDATE agent_messages SET status = 'failed'"
            " WHERE direction = 'user_to_agent' AND status = 'processing'"
        )
        self._conn.commit()
        if orphaned.rowcount:
            logger.warning(
                "AgentManager: terminally failed %d orphaned message claim(s)",
                orphaned.rowcount,
            )

    @_serialized_database
    def close(self) -> None:
        self._conn.close()

    # ── Agent CRUD ────────────────────────────────────────────────

    @_serialized_database
    def create_agent(
        self,
        name: str,
        agent_type: str = "monitor_operative",
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        agent_id = uuid.uuid4().hex[:12]
        now = time.time()
        # Pin the model the executor will actually use into the agent's config
        # so the UI reflects what runs. Without this, config has no "model" and
        # the executor silently falls back to _AGENT_TICK_DEFAULT_MODEL while
        # the Overview shows a stale/default value. Lazy import avoids any
        # import-order coupling with the executor module.
        from openjarvis.agents.executor import _AGENT_TICK_DEFAULT_MODEL

        config = dict(config or {})
        if not config.get("model"):
            config["model"] = _AGENT_TICK_DEFAULT_MODEL
        config_json = json.dumps(config)
        self._conn.execute(
            "INSERT INTO managed_agents"
            " (id, name, agent_type, config_json,"
            " status, summary_memory, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'idle', '', ?, ?)",
            (agent_id, name, agent_type, config_json, now, now),
        )
        self._conn.commit()
        return self.get_agent(agent_id)  # type: ignore[return-value]

    @_serialized_database
    def list_agents(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM managed_agents"
        if not include_archived:
            query += " WHERE status != 'archived'"
        query += " ORDER BY updated_at DESC"
        rows = self._conn.execute(query).fetchall()
        return [self._row_to_agent(r) for r in rows]

    @_serialized_database
    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM managed_agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return self._row_to_agent(row) if row else None

    @_serialized_database
    def update_agent(self, agent_id: str, **kwargs: Any) -> Dict[str, Any]:
        serialized_config = json.dumps(kwargs["config"]) if "config" in kwargs else None
        if "status" in kwargs:
            target = kwargs["status"]
            allowed_targets = {
                "idle",
                "paused",
                "archived",
                "error",
                "needs_attention",
                "budget_exceeded",
            }
            if target not in allowed_targets:
                raise ValueError(
                    "generic status updates cannot acquire or invent a tick lock"
                )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._conn.execute(
                "SELECT status FROM managed_agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Agent {agent_id} does not exist")
            if "status" in kwargs and current["status"] == "running":
                raise ValueError("a live tick can only be released by its owner token")

            sets: List[str] = []
            vals: List[Any] = []
            for key in ("name", "agent_type", "status", "current_activity"):
                if key in kwargs:
                    sets.append(f"{key} = ?")
                    vals.append(kwargs[key])
                    if key == "status":
                        sets.append("tick_token = NULL")
            if serialized_config is not None:
                sets.append("config_json = ?")
                vals.append(serialized_config)
            total_runs_increment = kwargs.get("total_runs_increment", 0)
            if total_runs_increment:
                sets.append("total_runs = total_runs + ?")
                vals.append(total_runs_increment)
                sets.append("last_run_at = ?")
                vals.append(time.time())
            total_cost_increment = kwargs.get("total_cost_increment", 0)
            if total_cost_increment:
                sets.append("total_cost = total_cost + ?")
                vals.append(total_cost_increment)
            total_tokens_increment = kwargs.get("total_tokens_increment", 0)
            if total_tokens_increment:
                sets.append("total_tokens = total_tokens + ?")
                vals.append(total_tokens_increment)
            input_tokens_increment = kwargs.get("input_tokens_increment", 0)
            if input_tokens_increment:
                sets.append("input_tokens = input_tokens + ?")
                vals.append(input_tokens_increment)
            output_tokens_increment = kwargs.get("output_tokens_increment", 0)
            if output_tokens_increment:
                sets.append("output_tokens = output_tokens + ?")
                vals.append(output_tokens_increment)
            if "last_activity_at" in kwargs:
                sets.append("last_activity_at = ?")
                vals.append(kwargs["last_activity_at"])
            if "stall_retries" in kwargs:
                sets.append("stall_retries = ?")
                vals.append(kwargs["stall_retries"])
            sets.append("updated_at = ?")
            vals.append(time.time())
            vals.append(agent_id)
            self._conn.execute(
                f"UPDATE managed_agents SET {', '.join(sets)} WHERE id = ?", vals
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self.get_agent(agent_id)  # type: ignore[return-value]

    @_serialized_database
    def delete_agent(self, agent_id: str) -> None:
        self._transition_agent_status(
            agent_id,
            target="archived",
            allowed={
                "idle",
                "paused",
                "error",
                "needs_attention",
                "budget_exceeded",
                "archived",
            },
            action="archived",
        )

    @_serialized_database
    def pause_agent(self, agent_id: str) -> None:
        self._transition_agent_status(
            agent_id,
            target="paused",
            allowed={"idle", "paused"},
            action="paused",
        )

    @_serialized_database
    def resume_agent(self, agent_id: str) -> None:
        self._transition_agent_status(
            agent_id,
            target="idle",
            allowed={"paused", "idle"},
            action="resumed",
        )

    def _transition_agent_status(
        self,
        agent_id: str,
        *,
        target: str,
        allowed: set[str],
        action: str,
    ) -> None:
        """Apply a control transition without releasing a live worker tick."""

        placeholders = ", ".join("?" for _ in allowed)
        values = (target, time.time(), agent_id, *sorted(allowed))
        cursor = self._conn.execute(
            "UPDATE managed_agents SET status = ?, updated_at = ?"
            f" WHERE id = ? AND status IN ({placeholders})",
            values,
        )
        if cursor.rowcount == 1:
            self._conn.commit()
            return
        self._conn.rollback()
        row = self._conn.execute(
            "SELECT status FROM managed_agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Agent {agent_id} does not exist")
        raise ValueError(f"agent in status {row['status']!r} cannot be {action}")

    @_serialized_database
    def _set_status(self, agent_id: str, status: str) -> None:
        if status == "running":
            raise ValueError("use start_tick() to acquire a running state")
        self._conn.execute(
            "UPDATE managed_agents SET status = ?, tick_token = NULL,"
            " updated_at = ? WHERE id = ? AND status != 'running'",
            (status, time.time(), agent_id),
        )
        self._conn.commit()

    # ── Tick concurrency guard ────────────────────────────────────

    @_serialized_database
    def start_tick(self, agent_id: str) -> str:
        """Mark agent as running. Raises ValueError if already running.

        The transaction is cross-process, not merely protected by this
        instance's RLock. A ``running`` row is never overtaken from elapsed time:
        a slow model may legitimately be silent for minutes. Crash leftovers
        are terminally reconciled only by authoritative server startup.
        """
        now = time.time()
        tick_token = uuid4().hex
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT status, updated_at FROM managed_agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Agent {agent_id} does not exist")
            if row["status"] != "idle":
                raise ValueError(
                    f"agent in status {row['status']!r} cannot execute a tick"
                )
            cursor = self._conn.execute(
                "UPDATE managed_agents SET status = 'running', tick_token = ?,"
                " updated_at = ?"
                " WHERE id = ? AND status = 'idle'",
                (tick_token, now, agent_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("agent tick lock disappeared")
            self._conn.commit()
            return tick_token
        except Exception:
            self._conn.rollback()
            raise

    @_serialized_database
    def end_tick(
        self,
        agent_id: str,
        tick_token: str,
        *,
        status: str = "idle",
    ) -> bool:
        """Release one running tick directly into its terminal status.

        Final state and lock release are one SQLite update. Callers must never
        expose an intermediate ``idle`` row and then set ``error`` or
        ``budget_exceeded``: another worker could acquire that gap.
        """

        if status not in {"idle", "error", "needs_attention", "budget_exceeded"}:
            raise ValueError(f"invalid terminal tick status: {status}")
        if not isinstance(tick_token, str) or not tick_token:
            raise ValueError("tick owner token is required")
        cursor = self._conn.execute(
            "UPDATE managed_agents SET status = ?, tick_token = NULL, "
            "current_activity = '', updated_at = ?"
            " WHERE id = ? AND status = 'running' AND tick_token = ?",
            (status, time.time(), agent_id, tick_token),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    # ── Checkpoints ───────────────────────────────────────────────

    _CHECKPOINT_RETENTION = 5

    @_serialized_database
    def save_checkpoint(
        self,
        agent_id: str,
        tick_id: str,
        conversation_state: dict,
        tool_state: dict,
    ) -> dict:
        cp_id = uuid4().hex[:16]
        now = time.time()
        self._conn.execute(
            "INSERT INTO agent_checkpoints"
            " (id, agent_id, tick_id, conversation_state, tool_state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                cp_id,
                agent_id,
                tick_id,
                json.dumps(conversation_state),
                json.dumps(tool_state),
                now,
            ),
        )
        # Prune old checkpoints beyond retention limit
        self._conn.execute(
            "DELETE FROM agent_checkpoints WHERE agent_id = ? AND id NOT IN "
            "(SELECT id FROM agent_checkpoints WHERE agent_id = ?"
            " ORDER BY created_at DESC LIMIT ?)",
            (agent_id, agent_id, self._CHECKPOINT_RETENTION),
        )
        self._conn.commit()
        return {
            "id": cp_id,
            "agent_id": agent_id,
            "tick_id": tick_id,
            "created_at": now,
        }

    @_serialized_database
    def list_checkpoints(self, agent_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM agent_checkpoints"
            " WHERE agent_id = ? ORDER BY created_at DESC",
            (agent_id,),
        ).fetchall()
        return [self._row_to_checkpoint(r) for r in rows]

    @_serialized_database
    def get_latest_checkpoint(self, agent_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM agent_checkpoints"
            " WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        return self._row_to_checkpoint(row) if row else None

    @_serialized_database
    def recover_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        current = self._conn.execute(
            "SELECT status FROM managed_agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"Agent {agent_id} does not exist")
        if current["status"] not in {
            "error",
            "needs_attention",
            "budget_exceeded",
        }:
            raise ValueError(
                f"agent in status {current['status']!r} cannot be recovered"
            )
        checkpoint = self.get_latest_checkpoint(agent_id)
        # Reset only a terminal state. A live/stalled worker must retain its
        # lock until an authoritative process boundary reconciles it.
        self._transition_agent_status(
            agent_id,
            target="idle",
            allowed={"error", "needs_attention", "budget_exceeded"},
            action="recovered",
        )
        return checkpoint

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "tick_id": row["tick_id"],
            "conversation_state": json.loads(row["conversation_state"]),
            "tool_state": json.loads(row["tool_state"]),
            "created_at": row["created_at"],
        }

    # ── Summary memory ────────────────────────────────────────────

    @_serialized_database
    def update_summary_memory(self, agent_id: str, summary: str) -> None:
        truncated = summary[:_SUMMARY_MAX]
        self._conn.execute(
            "UPDATE managed_agents SET summary_memory = ?, updated_at = ? WHERE id = ?",
            (truncated, time.time(), agent_id),
        )
        self._conn.commit()

    # ── Task CRUD ─────────────────────────────────────────────────

    @_serialized_database
    def create_task(
        self, agent_id: str, description: str, status: str = "pending"
    ) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO agent_tasks (id, agent_id, description, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, agent_id, description, status, now),
        )
        self._conn.commit()
        return self._get_task(task_id)  # type: ignore[return-value]

    @_serialized_database
    def list_tasks(
        self, agent_id: str, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM agent_tasks WHERE agent_id = ?"
        params: List[Any] = [agent_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    @_serialized_database
    def update_task(self, task_id: str, **kwargs: Any) -> Dict[str, Any]:
        sets: List[str] = []
        vals: List[Any] = []
        for key in ("description", "status"):
            if key in kwargs:
                sets.append(f"{key} = ?")
                vals.append(kwargs[key])
        if "progress" in kwargs:
            sets.append("progress_json = ?")
            vals.append(json.dumps(kwargs["progress"]))
        if "findings" in kwargs:
            sets.append("findings_json = ?")
            vals.append(json.dumps(kwargs["findings"]))
        if not sets:
            return self._get_task(task_id)  # type: ignore[return-value]
        vals.append(task_id)
        self._conn.execute(
            f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        self._conn.commit()
        return self._get_task(task_id)  # type: ignore[return-value]

    @_serialized_database
    def delete_task(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM agent_tasks WHERE id = ?", (task_id,))
        self._conn.commit()

    @_serialized_database
    def _get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    # ── Channel bindings ──────────────────────────────────────────

    @_serialized_database
    def bind_channel(
        self,
        agent_id: str,
        channel_type: str,
        config: Optional[Dict[str, Any]] = None,
        routing_mode: str = "dedicated",
    ) -> Dict[str, Any]:
        binding_id = uuid.uuid4().hex[:12]
        session_id = uuid.uuid4().hex[:16]
        config_json = json.dumps(config or {})
        self._conn.execute(
            "INSERT INTO channel_bindings "
            "(id, agent_id, channel_type, config_json, session_id, routing_mode) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (binding_id, agent_id, channel_type, config_json, session_id, routing_mode),
        )
        self._conn.commit()
        return self._get_binding(binding_id)  # type: ignore[return-value]

    @_serialized_database
    def list_channel_bindings(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM channel_bindings WHERE agent_id = ?", (agent_id,)
        ).fetchall()
        return [self._row_to_binding(r) for r in rows]

    @_serialized_database
    def unbind_channel(self, binding_id: str) -> None:
        self._conn.execute("DELETE FROM channel_bindings WHERE id = ?", (binding_id,))
        self._conn.commit()

    @_serialized_database
    def _get_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM channel_bindings WHERE id = ?", (binding_id,)
        ).fetchone()
        return self._row_to_binding(row) if row else None

    @_serialized_database
    def find_binding_for_channel(
        self, channel_type: str, channel_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find a dedicated binding for a specific channel."""
        rows = self._conn.execute(
            "SELECT * FROM channel_bindings WHERE channel_type = ?",
            (channel_type,),
        ).fetchall()
        for row in rows:
            binding = self._row_to_binding(row)
            config = binding.get("config", {})
            if config.get("channel") == channel_id:
                return binding
        return None

    # ── Templates ─────────────────────────────────────────────────

    @staticmethod
    def list_templates() -> List[Dict[str, Any]]:
        """Discover built-in and user templates."""
        import importlib.resources

        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        templates: List[Dict[str, Any]] = []

        # Built-in templates
        try:
            tpl_dir = importlib.resources.files("openjarvis.agents") / "templates"
            for item in tpl_dir.iterdir():
                if str(item).endswith(".toml"):
                    data = tomllib.loads(item.read_text(encoding="utf-8"))
                    tpl = data.get("template", {})
                    tpl["source"] = "built-in"
                    templates.append(tpl)
        except Exception:
            pass

        # User templates
        user_dir = get_config_dir() / "templates"
        if user_dir.is_dir():
            for f in user_dir.glob("*.toml"):
                try:
                    data = tomllib.loads(f.read_text(encoding="utf-8"))
                    tpl = data.get("template", {})
                    tpl["source"] = "user"
                    templates.append(tpl)
                except Exception:
                    pass

        return templates

    @_serialized_database
    def create_from_template(
        self, template_id: str, name: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Create an agent from a template with optional overrides."""
        templates = self.list_templates()
        tpl = next((t for t in templates if t.get("id") == template_id), None)
        if not tpl:
            raise ValueError(f"Template not found: {template_id}")
        skip = {"id", "name", "description", "source"}
        config = {k: v for k, v in tpl.items() if k not in skip}
        if overrides:
            config.update(overrides)
        agent_type = config.pop("agent_type", "monitor_operative")

        # Expand system_prompt_template with instruction
        prompt_tpl = config.pop("system_prompt_template", "")
        if prompt_tpl:
            instruction = config.get("instruction", "")
            config["system_prompt"] = prompt_tpl.format(
                instruction=instruction or "(No specific instruction provided)",
            )

        return self.create_agent(name=name, agent_type=agent_type, config=config)

    # ── Message queue ─────────────────────────────────────────────

    @_serialized_database
    def send_message(self, agent_id: str, content: str, mode: str = "queued") -> dict:
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_AGENT_MESSAGE_CHARS
        ):
            raise ValueError(
                "managed-agent message must contain"
                f" 1..{MAX_AGENT_MESSAGE_CHARS} characters"
            )
        msg_id = uuid4().hex[:16]
        now = time.time()
        _sql = (
            "INSERT INTO agent_messages"
            " (id, agent_id, direction, content, mode, status, created_at)"
            " VALUES (?, ?, 'user_to_agent', ?, ?, 'pending', ?)"
        )
        self._conn.execute(_sql, (msg_id, agent_id, content, mode, now))
        self._conn.commit()
        return {
            "id": msg_id,
            "agent_id": agent_id,
            "direction": "user_to_agent",
            "content": content,
            "mode": mode,
            "status": "pending",
            "created_at": now,
        }

    @_serialized_database
    def send_claimed_message(
        self, agent_id: str, content: str, mode: str = "immediate"
    ) -> dict:
        """Insert a message directly as processing in one indivisible operation."""

        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_AGENT_MESSAGE_CHARS
        ):
            raise ValueError(
                "managed-agent message must contain"
                f" 1..{MAX_AGENT_MESSAGE_CHARS} characters"
            )
        msg_id = uuid4().hex[:16]
        now = time.time()
        self._conn.execute(
            "INSERT INTO agent_messages"
            " (id, agent_id, direction, content, mode, status, created_at)"
            " VALUES (?, ?, 'user_to_agent', ?, ?, 'processing', ?)",
            (msg_id, agent_id, content, mode, now),
        )
        self._conn.commit()
        return {
            "id": msg_id,
            "agent_id": agent_id,
            "direction": "user_to_agent",
            "content": content,
            "mode": mode,
            "status": "processing",
            "created_at": now,
        }

    @_serialized_database
    def store_agent_response(
        self,
        agent_id: str,
        content: str,
        tool_calls: Optional[list] = None,
    ) -> dict:
        """Store an agent-to-user response message.

        ``tool_calls`` is an optional list of ``{tool, arguments, result,
        success, latency}`` dicts captured during the turn. They are stored
        as JSON alongside the message so the UI can replay them after a
        page reload.
        """
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_AGENT_MESSAGE_CHARS
        ):
            raise ValueError(
                "managed-agent response must contain"
                f" 1..{MAX_AGENT_MESSAGE_CHARS} characters"
            )
        msg_id = uuid4().hex[:16]
        now = time.time()
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        )
        if (
            tool_calls_json is not None
            and len(tool_calls_json.encode("utf-8")) > MAX_AGENT_TOOL_CALLS_BYTES
        ):
            raise ValueError("managed-agent tool call envelope is too large")
        self._conn.execute(
            "INSERT INTO agent_messages"
            " (id, agent_id, direction, content, mode, status, created_at,"
            " tool_calls, reply_to_id)"
            " VALUES (?, ?, 'agent_to_user', ?, 'immediate', 'delivered', ?, ?, NULL)",
            (msg_id, agent_id, content, now, tool_calls_json),
        )
        self._conn.commit()
        return {
            "id": msg_id,
            "agent_id": agent_id,
            "direction": "agent_to_user",
            "content": content,
            "mode": "immediate",
            "status": "delivered",
            "created_at": now,
            "tool_calls": tool_calls or None,
            "reply_to_id": None,
        }

    @_serialized_database
    def list_messages(self, agent_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_messages"
            " WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    @_serialized_database
    def get_pending_messages(self, agent_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_messages"
            " WHERE agent_id = ? AND direction = 'user_to_agent'"
            " AND status = 'pending' ORDER BY created_at ASC",
            (agent_id,),
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    @_serialized_database
    def claim_message(self, agent_id: str, message_id: str) -> dict:
        """Atomically claim one pending user message before any model/tool effect."""

        cursor = self._conn.execute(
            "UPDATE agent_messages SET status = 'processing'"
            " WHERE id = ? AND agent_id = ? AND direction = 'user_to_agent'"
            " AND status = 'pending'",
            (message_id, agent_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise ValueError("message is not pending or does not belong to this agent")
        row = self._conn.execute(
            "SELECT * FROM agent_messages WHERE id = ? AND agent_id = ?",
            (message_id, agent_id),
        ).fetchone()
        self._conn.commit()
        if row is None:  # Defensive: the row was updated in the same transaction.
            raise RuntimeError("claimed message disappeared")
        return self._row_to_message(row)

    @_serialized_database
    def claim_next_message(self, agent_id: str) -> Optional[dict]:
        """Claim the oldest pending message, processing at most one per tick."""

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id FROM agent_messages"
                " WHERE agent_id = ? AND direction = 'user_to_agent'"
                " AND status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1",
                (agent_id,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            cursor = self._conn.execute(
                "UPDATE agent_messages SET status = 'processing'"
                " WHERE id = ? AND status = 'pending'",
                (row["id"],),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("pending message claim lost")
            claimed = self._conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (row["id"],)
            ).fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if claimed is None:
            raise RuntimeError("claimed message disappeared")
        return self._row_to_message(claimed)

    @_serialized_database
    def complete_message_turn(
        self,
        agent_id: str,
        message_id: str,
        content: str,
        tool_calls: Optional[list] = None,
    ) -> dict:
        """Atomically persist one response and deliver its claimed user message."""

        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_AGENT_MESSAGE_CHARS
        ):
            raise ValueError(
                "completed managed-agent response must contain"
                f" 1..{MAX_AGENT_MESSAGE_CHARS} characters"
            )
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        )
        if (
            tool_calls_json is not None
            and len(tool_calls_json.encode("utf-8")) > MAX_AGENT_TOOL_CALLS_BYTES
        ):
            raise ValueError("managed-agent tool call envelope is too large")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            source = self._conn.execute(
                "SELECT status FROM agent_messages"
                " WHERE id = ? AND agent_id = ? AND direction = 'user_to_agent'",
                (message_id, agent_id),
            ).fetchone()
            if source is None:
                raise ValueError("source message does not exist")
            existing = self._conn.execute(
                "SELECT * FROM agent_messages"
                " WHERE agent_id = ? AND direction = 'agent_to_user'"
                " AND reply_to_id = ?",
                (agent_id, message_id),
            ).fetchone()
            if existing is not None:
                existing_tools = existing["tool_calls"]
                if existing["content"] != content or existing_tools != tool_calls_json:
                    raise ValueError(
                        "message turn already completed with another response"
                    )
                if source["status"] != "delivered":
                    raise RuntimeError("completed response has an undelivered source")
                self._conn.commit()
                return self._row_to_message(existing)
            if source["status"] != "processing":
                raise ValueError("source message is not claimed")

            response_id = uuid4().hex[:16]
            now = time.time()
            self._conn.execute(
                "INSERT INTO agent_messages"
                " (id, agent_id, direction, content, mode, status, created_at,"
                " tool_calls, reply_to_id)"
                " VALUES (?, ?, 'agent_to_user', ?, 'immediate', 'delivered', ?, ?, ?)",
                (
                    response_id,
                    agent_id,
                    content,
                    now,
                    tool_calls_json,
                    message_id,
                ),
            )
            delivered = self._conn.execute(
                "UPDATE agent_messages SET status = 'delivered'"
                " WHERE id = ? AND agent_id = ? AND status = 'processing'",
                (message_id, agent_id),
            )
            if delivered.rowcount != 1:
                raise RuntimeError("claimed source message changed before completion")
            response = self._conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (response_id,)
            ).fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if response is None:
            raise RuntimeError("completed response disappeared")
        return self._row_to_message(response)

    @_serialized_database
    def mark_message_delivered(self, message_id: str) -> None:
        self._conn.execute(
            "UPDATE agent_messages SET status = 'delivered' WHERE id = ?",
            (message_id,),
        )
        self._conn.commit()

    @_serialized_database
    def mark_message_failed(self, message_id: str) -> None:
        self._conn.execute(
            "UPDATE agent_messages SET status = 'failed'"
            " WHERE id = ? AND direction = 'user_to_agent'"
            " AND status IN ('pending', 'processing')",
            (message_id,),
        )
        self._conn.commit()

    @_serialized_database
    def add_agent_response(self, agent_id: str, content: str) -> dict:
        msg_id = uuid4().hex[:16]
        now = time.time()
        _sql = (
            "INSERT INTO agent_messages"
            " (id, agent_id, direction, content, mode, status, created_at)"
            " VALUES (?, ?, 'agent_to_user', ?, 'immediate', 'responded', ?)"
        )
        self._conn.execute(_sql, (msg_id, agent_id, content, now))
        self._conn.commit()
        return {
            "id": msg_id,
            "agent_id": agent_id,
            "direction": "agent_to_user",
            "content": content,
            "mode": "immediate",
            "status": "responded",
            "created_at": now,
        }

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict:
        tool_calls = None
        reply_to_id = None
        try:
            raw = row["tool_calls"]
        except (IndexError, KeyError):
            raw = None
        if raw:
            try:
                tool_calls = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                tool_calls = None
        try:
            reply_to_id = row["reply_to_id"]
        except (IndexError, KeyError):
            pass
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "direction": row["direction"],
            "content": row["content"],
            "mode": row["mode"],
            "status": row["status"],
            "created_at": row["created_at"],
            "tool_calls": tool_calls,
            "reply_to_id": reply_to_id,
        }

    # ── Learning log ──────────────────────────────────────────

    @_serialized_database
    def add_learning_log(
        self,
        agent_id: str,
        event_type: str,
        description: str = "",
        data: dict | None = None,
    ) -> dict:
        log_id = uuid4().hex[:16]
        now = time.time()
        self._conn.execute(
            "INSERT INTO agent_learning_log"
            " (id, agent_id, event_type, description, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (log_id, agent_id, event_type, description, json.dumps(data or {}), now),
        )
        self._conn.commit()
        return {
            "id": log_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "description": description,
            "data": data or {},
            "created_at": now,
        }

    @_serialized_database
    def list_learning_log(self, agent_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_learning_log"
            " WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "event_type": r["event_type"],
                "description": r["description"],
                "data": json.loads(r["data"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── Row converters ────────────────────────────────────────────

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> Dict[str, Any]:
        config_raw = row["config_json"]
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_type": row["agent_type"],
            "config": json.loads(config_raw) if config_raw else {},
            "status": row["status"],
            "summary_memory": row["summary_memory"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "total_tokens": row["total_tokens"] or 0,
            "total_cost": row["total_cost"] or 0.0,
            "total_runs": row["total_runs"] or 0,
            "last_run_at": row["last_run_at"],
            "last_activity_at": row["last_activity_at"],
            "stall_retries": row["stall_retries"] or 0,
            "current_activity": row["current_activity"] or "",
            "input_tokens": row["input_tokens"] or 0,
            "output_tokens": row["output_tokens"] or 0,
        }

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
        progress_raw = row["progress_json"]
        findings_raw = row["findings_json"]
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "description": row["description"],
            "status": row["status"],
            "progress": json.loads(progress_raw) if progress_raw else {},
            "findings": json.loads(findings_raw) if findings_raw else [],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_binding(row: sqlite3.Row) -> Dict[str, Any]:
        config_raw = row["config_json"]
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "channel_type": row["channel_type"],
            "config": json.loads(config_raw) if config_raw else {},
            "session_id": row["session_id"] or "",
            "routing_mode": row["routing_mode"] or "auto",
        }
