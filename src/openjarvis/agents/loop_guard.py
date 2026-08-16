"""Agent loop guard — detect and prevent degenerate tool-calling loops."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Optional

from openjarvis.core.events import EventBus, EventType


@dataclass(slots=True)
class LoopGuardConfig:
    """Configuration for the loop guard."""

    enabled: bool = True
    max_identical_calls: int = 3  # SHA-256 of (tool_name, arguments)
    ping_pong_window: int = 6  # detect A-B-A-B cycling
    poll_tool_budget: int = 5  # max calls to same polling tool
    max_context_messages: int = 100  # context overflow threshold
    warn_before_block: bool = True  # warn on first cycle, block on second


@dataclass(slots=True)
class LoopVerdict:
    """Result of a loop guard check."""

    blocked: bool = False
    reason: str = ""
    warned: bool = False
    cycle_key: str = ""


class LoopGuard:
    """Detect and prevent degenerate agent loops.

    Features:
    1. Hash tracking: SHA-256 of (tool_name, args) blocks after max_identical_calls
    2. Ping-pong detection: Sliding window detects A-B-A-B or A-B-C-A-B-C patterns
    3. Poll-tool awareness: Tools with spec.metadata["polling"] = True
       get relaxed budget
    4. Context overflow recovery: 4-stage compression of message history
    """

    def __init__(self, config: LoopGuardConfig, *, bus: Optional[EventBus] = None):
        self._config = config
        self._bus = bus
        # Track call hashes and their counts
        self._call_counts: dict[str, int] = {}
        # Track tool name sequence for pattern detection
        self._tool_sequence: deque[str] = deque(maxlen=config.ping_pong_window * 2)
        # Track per-tool call counts (for polling budget)
        self._per_tool_counts: dict[str, int] = {}
        # Track cycle keys that have already been warned (for warn-before-block)
        self._warned_cycles: set[str] = set()
        # A blocked Rust verdict repeats on every retry. Telemetry describes the
        # cycle transition, not every rejected attempt, so publish it once per cycle.
        self._triggered_cycles: set[str] = set()

        try:
            from openjarvis._rust_bridge import get_rust_module

            _rust = get_rust_module()
            self._rust_impl = _rust.LoopGuard(
                max_identical=config.max_identical_calls,
                max_ping_pong=(
                    config.ping_pong_window // 2 if config.ping_pong_window > 1 else 2
                ),
                poll_budget=config.poll_tool_budget,
            )
        except Exception:
            self._rust_impl = None

    def reinitialiser(self) -> None:
        """Repart de zero — a appeler au DEBUT de chaque requete.

        ⚠ POURQUOI. Le garde-fou vit sur l'instance d'agent, elle-meme partagee par tout
          le service. Ses compteurs s'accumulaient donc sur la VIE DU PROCESSUS : passe
          trois appels identiques, un outil devenait DEFINITIVEMENT bloque jusqu'au
          redemarrage. Mesure du 2026-08-06 : Ava ne pouvait plus relire
          `cartographie-si.md` — apres quatre lectures reussies, la cinquieme et toutes
          les suivantes ont ete refusees, y compris la premiere d'une conversation
          NEUVE.
        ⚠ Une boucle est un phenomene INTERNE A UNE TACHE : relire le meme document
          demain est legitime, le relire dix fois dans la meme reponse ne l'est pas. Le
          compteur doit donc avoir la duree de la tache, pas celle du service.
        """
        self._call_counts.clear()
        self._per_tool_counts.clear()
        self._warned_cycles.clear()
        self._triggered_cycles.clear()
        if self._rust_impl is not None:
            # Le module Rust garde ses propres compteurs : on le reconstruit, faute
            # d'une methode de remise a zero exposee.
            try:
                from openjarvis._rust_bridge import get_rust_module

                _rust = get_rust_module()
                self._rust_impl = _rust.LoopGuard(
                    max_identical=self._config.max_identical_calls,
                    max_ping_pong=(
                        self._config.ping_pong_window // 2
                        if self._config.ping_pong_window > 1
                        else 2
                    ),
                    poll_budget=self._config.poll_tool_budget,
                )
            except Exception:  # noqa: BLE001 — un garde-fou qui ne se remet pas a zero
                self._rust_impl = None  # vaut mieux qu'un garde-fou qui bloque tout

    def check_call(self, tool_name: str, arguments: str) -> LoopVerdict:
        """Check whether a tool call should proceed or be blocked."""
        if self._rust_impl is not None:
            rust_result = self._rust_impl.check(tool_name, arguments)
            # Support both raw Rust return (str | None) and LoopVerdict
            if isinstance(rust_result, LoopVerdict):
                verdict = rust_result
            elif rust_result is not None:
                cycle_key = self._stable_cycle_key(
                    tool_name,
                    arguments,
                    rust_result,
                )
                if cycle_key not in self._triggered_cycles:
                    self._triggered_cycles.add(cycle_key)
                    self._emit_triggered("rust_guard", tool_name)
                verdict = LoopVerdict(
                    blocked=True,
                    reason=rust_result,
                    cycle_key=cycle_key,
                )
            else:
                verdict = LoopVerdict()
        else:
            verdict = self._python_check(tool_name, arguments)

        # Wrap with warn-before-block logic
        if verdict.blocked and self._config.warn_before_block:
            cycle_key = verdict.cycle_key or self._stable_cycle_key(
                tool_name,
                arguments,
                verdict.reason,
            )
            if cycle_key not in self._warned_cycles:
                self._warned_cycles.add(cycle_key)
                return LoopVerdict(
                    blocked=False,
                    warned=True,
                    reason=verdict.reason,
                    cycle_key=cycle_key,
                )
        return verdict

    @staticmethod
    def _stable_cycle_key(tool_name: str, arguments: str, reason: str) -> str:
        """Name a loop independently of counters embedded in its message."""

        normalized = reason.casefold()
        if "identical" in normalized:
            digest = hashlib.sha256(f"{tool_name}:{arguments}".encode()).hexdigest()[
                :16
            ]
            return f"identical:{digest}"
        if "ping-pong" in normalized or "repetitive tool" in normalized:
            return "ping-pong"
        if "poll budget" in normalized:
            return "poll-budget"
        return "other:" + hashlib.sha256(reason.encode()).hexdigest()[:16]

    def _python_check(self, tool_name: str, arguments: str) -> LoopVerdict:
        """Pure-Python fallback when Rust backend is not available."""
        # 1. Hash tracking — identical calls
        call_hash = hashlib.sha256(f"{tool_name}:{arguments}".encode()).hexdigest()[:16]
        self._call_counts[call_hash] = self._call_counts.get(call_hash, 0) + 1
        if self._call_counts[call_hash] >= self._config.max_identical_calls:
            # Emit once at the threshold. A warned call may be retried, but repeated
            # blocked attempts must not manufacture duplicate alert events.
            if self._call_counts[call_hash] == self._config.max_identical_calls:
                self._emit_triggered("identical_call", tool_name)
            return LoopVerdict(
                blocked=True,
                cycle_key=f"identical:{call_hash}",
                reason=(
                    f"Identical call to '{tool_name}' repeated "
                    f"{self._call_counts[call_hash]} times "
                    f"(max {self._config.max_identical_calls})."
                ),
            )

        # 2. Per-tool budget (polling tools)
        self._per_tool_counts[tool_name] = self._per_tool_counts.get(tool_name, 0) + 1
        if self._per_tool_counts[tool_name] > self._config.poll_tool_budget:
            self._emit_triggered("poll_budget", tool_name)
            return LoopVerdict(
                blocked=True,
                cycle_key="poll-budget",
                reason=(
                    f"Tool '{tool_name}' exceeded poll budget "
                    f"({self._config.poll_tool_budget})."
                ),
            )

        # 3. Ping-pong detection
        self._tool_sequence.append(tool_name)
        if len(self._tool_sequence) >= self._config.ping_pong_window:
            if self._detect_ping_pong():
                self._emit_triggered("ping_pong", tool_name)
                return LoopVerdict(
                    blocked=True,
                    cycle_key="ping-pong",
                    reason="Repetitive tool-calling pattern detected (ping-pong).",
                )

        return LoopVerdict()

    def check_response(self, content: str) -> LoopVerdict:
        """Check whether an agent response indicates a loop. Reserved for future use."""
        return LoopVerdict()

    @staticmethod
    def _is_system(msg: object) -> bool:
        """Check if a message has role == system."""
        return getattr(msg, "role", None) == "system"

    @staticmethod
    def _is_tool(msg: object) -> bool:
        """Check if a message has role == tool."""
        return getattr(msg, "role", None) == "tool"

    def compress_context(self, messages: list) -> list:
        """Apply 4-stage context overflow recovery to message list.

        Stages:
        1. Summarize old tool results (replace content with "[Tool result truncated]")
        2. Sliding window — keep only recent messages
        3. Drop tool call/result pairs from the middle
        4. Truncate to system + last 2 exchanges
        """
        if len(messages) <= self._config.max_context_messages:
            return messages

        # Stage 1: Truncate old tool result messages
        threshold = len(messages) // 2
        compressed = []
        for i, msg in enumerate(messages):
            if i < threshold and self._is_tool(msg):
                from openjarvis.core.types import Message, Role

                compressed.append(
                    Message(
                        role=Role.TOOL,
                        content="[Tool result truncated]",
                        tool_call_id=getattr(
                            msg,
                            "tool_call_id",
                            None,
                        ),
                        name=getattr(msg, "name", None),
                    )
                )
            else:
                compressed.append(msg)

        if len(compressed) <= self._config.max_context_messages:
            return compressed

        # Stage 2: Sliding window — keep system + recent
        system_msgs = [m for m in compressed if self._is_system(m)]
        non_system = [m for m in compressed if not self._is_system(m)]
        window_size = self._config.max_context_messages - len(system_msgs)
        if len(non_system) > window_size:
            non_system = non_system[-window_size:]
        compressed = system_msgs + non_system

        if len(compressed) <= self._config.max_context_messages:
            return compressed

        # Stage 3: Drop tool call/result pairs from middle
        keep_start = max(
            len(system_msgs),
            len(compressed) // 10,
        )
        keep_end = len(compressed) // 2
        compressed = compressed[:keep_start] + compressed[-keep_end:]

        if len(compressed) <= self._config.max_context_messages:
            return compressed

        # Stage 4: Extreme — system + last 2 exchanges
        sys_final = [m for m in compressed if self._is_system(m)]
        tail = [m for m in compressed if not self._is_system(m)]
        return sys_final + tail[-4:]

    def reset(self) -> None:
        """Reset all tracking state — always via Rust backend."""
        self._call_counts.clear()
        self._tool_sequence.clear()
        self._per_tool_counts.clear()
        self._warned_cycles.clear()
        self._triggered_cycles.clear()
        if self._rust_impl is not None:
            self._rust_impl.reset()

    def _detect_ping_pong(self) -> bool:
        """Detect repeating patterns in tool call sequence."""
        seq = list(self._tool_sequence)
        n = len(seq)
        # Check for period-2 pattern (A-B-A-B)
        for period in (2, 3):
            if n >= period * 2:
                tail = seq[-period * 2 :]
                pattern = tail[:period]
                if all(tail[i] == pattern[i % period] for i in range(len(tail))):
                    return True
        return False

    def _emit_triggered(self, reason_type: str, tool_name: str) -> None:
        """Publish a LOOP_GUARD_TRIGGERED event."""
        if self._bus:
            self._bus.publish(
                EventType.LOOP_GUARD_TRIGGERED,
                {"reason_type": reason_type, "tool": tool_name},
            )


__all__ = ["LoopGuard", "LoopGuardConfig", "LoopVerdict"]
