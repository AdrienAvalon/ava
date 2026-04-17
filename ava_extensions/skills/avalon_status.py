"""Tool avalon_status — interroge lAPI Control Plane v2 et résume létat de linfra.

Enregistré comme tool OpenJarvis via @ToolRegistry.register("avalon_status").
Ava peut linvoquer quand Adrien demande "comment va linfra", "quel est le
score", etc.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

CP_V2_URL = "http://192.168.100.31:8100/api/v1/dashboard"
_TIMEOUT_S = 8.0


def _fetch() -> dict[str, Any]:
    req = urllib.request.Request(CP_V2_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format_summary(data: dict[str, Any]) -> str:
    score_info = data.get("score", {}) or {}
    global_score = score_info.get("global", "?")
    max_score = score_info.get("max", 100)
    status = score_info.get("status", "?")
    modules = score_info.get("modules", {}) or {}

    # Modules en dégradation (deductions non vides OU score < max)
    issues = []
    for name, info in modules.items():
        if not isinstance(info, dict):
            continue
        deductions = info.get("deductions") or []
        mscore = info.get("score", 0)
        mmax = info.get("max", 0)
        if deductions or (mmax and mscore < mmax):
            detail = f"{name} {mscore}/{mmax}"
            if deductions:
                top = deductions[0]
                if isinstance(top, dict):
                    reason = top.get("reason", top.get("msg", "?"))
                    detail += f" ({reason})"
            issues.append(detail)

    lines = [f"Score Avalon: {global_score}/{max_score} — {status}"]
    if issues:
        lines.append("Modules en dégradation:")
        for it in issues[:8]:
            lines.append(f"  - {it}")
    else:
        lines.append("Tous les modules sont OK.")

    # Stats utiles
    alerts = data.get("alerts") or []
    if isinstance(alerts, list):
        lines.append(f"Alertes actives: {len(alerts)}")
    hosts = data.get("hosts") or []
    if isinstance(hosts, list):
        down = [h for h in hosts if isinstance(h, dict) and h.get("status") not in ("up", "ok", None)]
        lines.append(f"Hosts: {len(hosts)} ({len(down)} down/maintenance)")

    return "\n".join(lines)


@ToolRegistry.register("avalon_status")
class AvalonStatusTool(BaseTool):
    """Interroge le Control Plane v2 et résume létat global de linfra."""

    tool_id = "avalon_status"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="avalon_status",
            description=(
                "Récupère létat global de linfrastructure Avalon depuis le "
                "Control Plane v2 (score 0-100, modules en dégradation, "
                "alertes actives, hosts down). À utiliser quand Adrien "
                "demande comment va linfra, quel est le score, sil y a des "
                "problèmes."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            category="infra",
            latency_estimate=1.0,
            timeout_seconds=10.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            data = _fetch()
        except urllib.error.URLError as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Impossible de joindre le Control Plane v2 ({CP_V2_URL}): {exc}",
                success=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Erreur inattendue: {exc}",
                success=False,
            )
        summary = _format_summary(data)
        return ToolResult(
            tool_name=self.tool_id,
            content=summary,
            success=True,
            metadata={"global_score": data.get("score", {}).get("global")},
        )
