"""Strict, server-owned temporal context for Matrix prompts.

The Matrix bridge may attach event timestamps to a request, but it must never
turn a client-authored ``system`` message into trusted prompt context.  This
module accepts only a closed integer payload associated with a Matrix service
``Principal`` already established by the request boundary, then renders one
small system-prompt fragment containing no client text or identifier.

No credential is verified here: :class:`Principal` is the boundary-issued
type, and its ``provenance`` is attribution rather than authentication.  A
caller must therefore pass the object returned by the existing cryptographic
principal resolver, never construct one from request fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ava_extensions.server.principal import Principal

TEMPORAL_CONTEXT_MARKER = "[AVA_TEMPORAL_CONTEXT:v1]"

# Matrix was publicly announced in 2014.  This absolute lower bound rejects
# Unix-epoch/default values without making replay validity depend on today's
# date.  In particular, there is intentionally no sliding maximum event age.
MATRIX_EPOCH_TS_MS = 1_409_529_600_000  # 2014-09-01T00:00:00Z
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

_MAX_SUPPORTED_TS_MS = 253_402_214_399_999  # 9999-12-30T23:59:59.999Z
_EXPECTED_KEYS = frozenset({"version", "current_event_ts_ms", "previous_event_ts_ms"})
_MATRIX_SUBJECT_RE = re.compile(r"matrix:@[^:\s]+:[^\s]+")
_SERVICE_PROVENANCE_RE = re.compile(r"principal:service:sha256:[0-9a-f]{64}")
_PARIS = ZoneInfo("Europe/Paris")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TemporalContextValidationError(ValueError):
    """A temporal payload is not trusted, bounded or internally coherent."""


def _strict_int(value: Any, field: str) -> int:
    if type(value) is not int:
        raise TemporalContextValidationError(f"{field} must be a strict integer")
    return value


def _bounded_timestamp(value: Any, field: str) -> int:
    timestamp = _strict_int(value, field)
    if not MATRIX_EPOCH_TS_MS <= timestamp <= _MAX_SUPPORTED_TS_MS:
        raise TemporalContextValidationError(f"{field} is outside supported bounds")
    return timestamp


def _require_verified_matrix_principal(principal: Principal | None) -> Principal:
    """Accept only the existing boundary type for a Matrix service identity.

    ``Principal.provenance`` is checked for structural consistency only.  It is
    deliberately not accepted on its own because the property is pseudonymous
    attribution, not a bearer credential or a new authentication mechanism.
    """

    if (
        type(principal) is not Principal
        or principal.provider != "service"
        or not isinstance(principal.issuer, str)
        or not principal.issuer
        or len(principal.issuer) > 512
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in principal.issuer)
        or not isinstance(principal.subject, str)
        or not principal.subject
        or len(principal.subject) > 512
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in principal.subject)
        or _MATRIX_SUBJECT_RE.fullmatch(principal.subject) is None
        or _SERVICE_PROVENANCE_RE.fullmatch(principal.provenance) is None
    ):
        raise TemporalContextValidationError(
            "temporal context requires a verified Matrix service principal"
        )
    return principal


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalContextV1:
    """Closed v1 transport metadata; every field is integer or ``None``."""

    version: Literal[1] = 1
    current_event_ts_ms: int
    previous_event_ts_ms: int | None

    def __post_init__(self) -> None:
        version = _strict_int(self.version, "version")
        if version != 1:
            raise TemporalContextValidationError("unsupported temporal context version")
        current = _bounded_timestamp(
            self.current_event_ts_ms,
            "current_event_ts_ms",
        )
        previous = self.previous_event_ts_ms
        if previous is not None:
            previous = _bounded_timestamp(previous, "previous_event_ts_ms")
            if previous >= current:
                raise TemporalContextValidationError(
                    "previous_event_ts_ms must precede current_event_ts_ms"
                )


def _validate_against_request(
    context: TemporalContextV1,
    *,
    principal: Principal | None,
    now_ms: int,
) -> None:
    _require_verified_matrix_principal(principal)
    current_now = _bounded_timestamp(now_ms, "now_ms")
    # Reconstructing through the public type also protects the renderer if a
    # caller somehow bypassed the frozen dataclass constructor.
    TemporalContextV1(
        version=context.version,
        current_event_ts_ms=context.current_event_ts_ms,
        previous_event_ts_ms=context.previous_event_ts_ms,
    )
    if context.current_event_ts_ms > current_now + MAX_FUTURE_SKEW_MS:
        raise TemporalContextValidationError(
            "current_event_ts_ms is too far in the future"
        )


def parse_matrix_temporal_context_v1(
    payload: object,
    *,
    principal: Principal | None,
    now_ms: int,
) -> TemporalContextV1:
    """Validate an exact Matrix timestamp payload from the trusted bridge.

    A concrete ``dict`` is required so foreign mapping implementations cannot
    add behavior or perform I/O during validation.  Extra keys are rejected
    here; duplicate JSON members are already collapsed by the standard HTTP
    decoder and are therefore not claimed as a property of this validator.
    """

    if type(payload) is not dict or set(payload) != _EXPECTED_KEYS:
        raise TemporalContextValidationError(
            "temporal context must contain exactly the v1 fields"
        )
    context = TemporalContextV1(
        version=payload["version"],
        current_event_ts_ms=payload["current_event_ts_ms"],
        previous_event_ts_ms=payload["previous_event_ts_ms"],
    )
    _validate_against_request(context, principal=principal, now_ms=now_ms)
    return context


def _paris_iso8601(timestamp_ms: int) -> str:
    instant = _UNIX_EPOCH + timedelta(milliseconds=timestamp_ms)
    local = instant.astimezone(_PARIS).isoformat(timespec="milliseconds")
    return f"{local}[Europe/Paris]"


def _duration_iso8601(duration_ms: int) -> str:
    days, remainder = divmod(duration_ms, 86_400_000)
    hours, remainder = divmod(remainder, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    day_part = f"{days}D" if days else ""
    return f"P{day_part}T{hours}H{minutes}M{seconds}.{milliseconds:03d}S"


def render_temporal_context_system_fragment(
    context: TemporalContextV1,
    *,
    principal: Principal | None,
    now_ms: int,
) -> str:
    """Render one canonical system fragment without free-form client data."""

    if type(context) is not TemporalContextV1:
        raise TemporalContextValidationError("invalid temporal context type")
    _validate_against_request(context, principal=principal, now_ms=now_ms)

    current = _paris_iso8601(context.current_event_ts_ms)
    if context.previous_event_ts_ms is None:
        previous = "null"
        elapsed_ms = "null"
        elapsed_iso8601 = "null"
    else:
        previous = _paris_iso8601(context.previous_event_ts_ms)
        elapsed = context.current_event_ts_ms - context.previous_event_ts_ms
        elapsed_ms = str(elapsed)
        elapsed_iso8601 = _duration_iso8601(elapsed)

    return "\n".join(
        (
            TEMPORAL_CONTEXT_MARKER,
            f"current={current}",
            f"previous={previous}",
            f"elapsed_ms={elapsed_ms};elapsed_iso8601={elapsed_iso8601}",
        )
    )


__all__ = [
    "MATRIX_EPOCH_TS_MS",
    "MAX_FUTURE_SKEW_MS",
    "TEMPORAL_CONTEXT_MARKER",
    "TemporalContextV1",
    "TemporalContextValidationError",
    "parse_matrix_temporal_context_v1",
    "render_temporal_context_system_fragment",
]
