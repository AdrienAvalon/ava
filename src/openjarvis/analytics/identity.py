"""Anonymous identity for external analytics.

One UUID v4 per install, persisted to disk on first use. The same file
is referenced by ``scripts/install/install.sh`` so install-time beacon
events tie back to the same person across the install→first-run funnel.

No email, no name, no hardware fingerprint — just an opaque UUID.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from openjarvis.core.config import AnalyticsConfig

_OPT_OUT_ENV_VARS = ("DO_NOT_TRACK", "OPENJARVIS_NO_ANALYTICS")


def get_or_create_anon_id(path: Path | str) -> str:
    """Return the persisted anon ID, generating one on first call.

    Idempotent across processes — if the file already exists with a
    non-empty value, return it; otherwise generate a fresh UUID v4 and
    write atomically (rename-after-write so a crashed write leaves no
    half-file).
    """
    p = Path(path)
    if p.exists():
        existing = p.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    new_id = str(uuid.uuid4())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(new_id + "\n", encoding="utf-8")
    tmp.replace(p)
    return new_id


def reset_anon_id(path: Path | str) -> str:
    """Delete the persisted ID and generate a fresh one (privacy reset)."""
    p = Path(path)
    if p.exists():
        p.unlink()
    return get_or_create_anon_id(p)


def _env_opt_out() -> bool:
    """Return whether an explicit process-level analytics opt-out is active."""

    for name in _OPT_OUT_ENV_VARS:
        raw = os.environ.get(name)
        if raw and raw.strip().lower() not in ("", "0", "false", "no", "off"):
            return True
    return False


def is_analytics_enabled(cfg: AnalyticsConfig) -> bool:
    """Return True only when the operator explicitly allows analytics."""

    if _env_opt_out():
        return False
    return cfg.enabled
