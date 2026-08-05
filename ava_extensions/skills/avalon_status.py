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

# ⚠ DEUX ENDPOINTS, ET C'EST STRUCTUREL. `/dashboard` porte le score et l'état des
#   modules ; il ne porte NI les alertes NI les hôtes. La version précédente lisait
#   `data.get("alerts")` et `data.get("hosts")` sur cette réponse : les deux clés
#   n'existent pas, `.get()` rendait `None`, et l'outil annonçait « Alertes actives: 0 »
#   et « Hosts: 0 » avec assurance — pendant qu'une alerte Grafana tirait réellement.
#   C'est le piège central de cette infrastructure : une source qui ne porte pas la
#   donnée répond « rien » SANS ERREUR. Un zéro faux est pire qu'une absence : Ava
#   répondait « non, aucune alerte » à une question dont elle n'avait pas la réponse.
#   Vérifié le 2026-08-03 — clés réelles de /dashboard : module_data, module_health,
#   networks, score, version, widgets.
CP_V2_BASE = "http://192.168.100.31:8100/api/v1"
_TIMEOUT_S = 8.0


def _get(chemin: str) -> Any:
    req = urllib.request.Request(
        f"{CP_V2_BASE}{chemin}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch() -> dict[str, Any]:
    """Le tableau de bord seul — score et modules."""
    return _get("/dashboard")


def _fetch_hosts() -> list[dict[str, Any]]:
    """Les hôtes, depuis leur PROPRE endpoint.

    ⚠ Ne jamais faire échouer l'outil entier si cet appel échoue : le score reste utile
    même sans le détail des hôtes. Une panne partielle ne doit pas produire un silence
    total — sinon Ava dit « je ne peux pas savoir » alors qu'elle sait l'essentiel.
    """
    try:
        d = _get("/hosts")
    except Exception:
        return []
    return d if isinstance(d, list) else (d.get("hosts") or [])


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

    # ⚠ Les « alertes » du control plane SONT les déductions des modules : il n'existe
    #   pas de liste d'alertes séparée. Les compter à partir des déductions dit la vérité ;
    #   lire une clé `alerts` inexistante disait « 0 » quoi qu'il arrive.
    nb_deductions = sum(
        len(info.get("deductions") or [])
        for info in modules.values()
        if isinstance(info, dict)
    )
    lines.append(f"Déductions actives: {nb_deductions}")

    # Les hôtes viennent de leur propre endpoint (cf. commentaire en tête de fichier).
    hosts = _fetch_hosts()
    if hosts:
        inactifs = [
            h
            for h in hosts
            if isinstance(h, dict)
            and (h.get("active") is False or h.get("maintenance"))
        ]
        detail = ""
        if inactifs:
            # ⚠ LE MOTIF ET L'ANCIENNETÉ, PAS SEULEMENT LE NOM. Le control plane les expose
            #   (`maintenance: {mode, reason, age_days}`) et cet outil n'en gardait que le
            #   nom, en écrivant « hors service OU en maintenance ». Ava reprenait cette
            #   ambiguïté et la renvoyait à l'admin en question — « tu l'as mis
            #   volontairement ou c'est une surprise ? » — alors que la réponse est écrite
            #   dans le registre. Mesuré le 2026-08-05 sur pve-02.
            # ⚠ `age_days` compte AUTANT que le motif : ce champ existe pour rendre visible
            #   un arrêt « temporaire » qui dure depuis des semaines. Le taire vide le champ
            #   de sa raison d'être.
            morceaux = []
            for h in inactifs[:4]:
                nom = str(h.get("display_name") or h.get("id"))
                m = h.get("maintenance") or {}
                motif = str(m.get("reason") or "").strip()
                jours = m.get("age_days")
                if motif and jours is not None:
                    morceaux.append(f"{nom} ({motif}, depuis {jours:.0f} j)")
                elif motif:
                    morceaux.append(f"{nom} ({motif})")
                elif m.get("mode"):
                    morceaux.append(f"{nom} ({m['mode']})")
                else:
                    # ⚠ Sans entrée de maintenance, l'hôte est inactif SANS raison déclarée
                    #   — ce qui n'est pas la même chose qu'un arrêt volontaire, et mérite
                    #   d'être dit tel quel plutôt que fondu dans un « ou ».
                    morceaux.append(f"{nom} (inactif, aucune maintenance déclarée)")
            detail = " — " + " ; ".join(morceaux)
        lines.append(f"Hôtes: {len(hosts)} déclarés, {len(inactifs)} inactifs{detail}")
    else:
        # ⚠ On dit qu'on ne sait pas, plutôt que d'écrire « 0 hôte » — c'est exactement
        #   l'erreur qu'on corrige ici.
        lines.append("Hôtes: information indisponible (endpoint /hosts injoignable)")

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
                # ⚠ Etait `CP_V2_URL` — un nom QUI N'EXISTE PAS (la constante s'appelle
                #   `CP_V2_BASE`). Ce chemin ne s'emprunte que si le control plane est
                #   injoignable : le jour ou il l'aurait ete, l'outil aurait leve un
                #   `NameError` AU LIEU d'afficher son message d'erreur. Un gestionnaire
                #   d'erreur casse ne se voit jamais tant que l'erreur ne survient pas —
                #   c'est-a-dire jamais avant le pire moment.
                #   Trouve par `ruff` (F821) le 2026-08-04, en branchant la CI du fork.
                content=f"Impossible de joindre le Control Plane v2 ({CP_V2_BASE}): {exc}",
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
