"""Outil `camera` — ce qui s'est passé DEHORS, avec les lieux et les heures.

⚠ POURQUOI UN OUTIL SÉPARÉ DE `home_assistant`. Les deux parlent du parking, mais ils
  répondent à des questions différentes, et les confondre produirait des réponses fausses :
    · `home_assistant` → l'ÉTAT INSTANTANÉ. « Est-ce qu'il y a quelqu'un là, maintenant ? »
      Il lit des capteurs binaires qui valent `on` ou `off` à la seconde où on regarde.
    · `camera` (ici)   → l'HISTORIQUE QUALIFIÉ. « Combien de voitures aujourd'hui ?
      Quelqu'un est-il venu au portail cette nuit ? »
  Le premier ne peut PAS répondre au second, et c'est une limite de nature : le control
  plane échantillonne toutes les 60 s alors qu'une détection dure quelques secondes.
  Compter par échantillonnage rendrait « 3 » là où il y en a eu 32. Frigate tient le
  registre des événements ; cet outil le lit.

⚠ IL LIT LE CONTROL PLANE, JAMAIS FRIGATE EN DIRECT. L'API de Frigate est non
  authentifiée et sait supprimer des enregistrements — elle n'écoute donc que sur la
  boucle locale d'AVA. Ava est en DMZ ; elle passe par le CP, déjà autorisé. Même
  doctrine que pour Home Assistant : un service dual-homé sert d'intermédiaire, et le
  pouvoir reste de son côté.

⚠ LE NOMBRE D'ÉVÉNEMENTS N'EST PAS UN NOMBRE DE VÉHICULES. Une voiture qui se gare
  produit un événement ; la même qui repart en produit un second. C'est exactement la
  confusion qui a fait lire « 32 véhicules » là où il y en a quatre — l'outil le dit
  plutôt que de laisser le modèle conclure.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

CP_URL = os.environ.get("CP_URL", "http://192.168.100.31:8100")
_TIMEOUT_S = 10.0


def _dashboard() -> dict[str, Any]:
    req = urllib.request.Request(
        f"{CP_URL}/api/v1/dashboard", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _quand(horodatage: Any) -> str:
    """Un repère temporel dit à l'oral — Ava répond à la voix, pas à l'écran."""
    try:
        ts = float(horodatage)
    except (TypeError, ValueError):
        return "à un moment inconnu"
    ecart = max(0.0, datetime.datetime.now().timestamp() - ts)
    if ecart < 90:
        return "à l'instant"
    if ecart < 3600:
        return f"il y a {int(ecart // 60)} min"
    h = datetime.datetime.fromtimestamp(ts)
    if ecart < 86400:
        return f"à {h.strftime('%H:%M')}"
    return f"hier à {h.strftime('%H:%M')}" if ecart < 172800 else h.strftime("%d/%m à %H:%M")


def _sante(d: dict[str, Any]) -> list[str]:
    """Ce qui empêcherait de croire le reste."""
    lignes = []
    fps = d.get("camera_fps")
    if fps is not None and float(fps) <= 0:
        # ⚠ Le cas le plus important de tout ce fichier : sans flux, « rien détecté » ne
        #   veut PAS dire « rien ne s'est passé ». Ava doit le dire, pas le taire.
        lignes.append("  ⚠ La caméra n'envoie plus d'image — je ne vois rien du tout.")
    if not d.get("detection_active", True):
        lignes.append("  ⚠ La détection d'objets est désactivée sur la caméra.")
    if not d.get("historique_lisible", True):
        lignes.append("  ⚠ Je n'arrive pas à relire l'historique — ce n'est pas « rien ne s'est passé ».")
    return lignes


def _resume(d: dict[str, Any]) -> str:
    lignes = _sante(d)

    derniers = d.get("derniers") or []
    en_cours = [x for x in derniers if x.get("en_cours")]
    if en_cours:
        # ⚠ « Encore là » et « est venu » ne demandent pas la même chose de l'auditeur.
        for x in en_cours:
            ou = " et ".join(x.get("zones") or []) or "dans le champ"
            lignes.append(f"  EN CE MOMENT : {x['objet']} {ou}.")

    e = d.get("evenements_24h")
    if e and e.get("total"):
        # ⚠ ACCORD AU PLURIEL : Ava DIT ces phrases à voix haute. « 3 voiture » s'entend,
        #   et s'entend mal. Les noms retenus ici prennent tous un `s` simple (voiture,
        #   personne, moto, vélo, chat, chien, camion) — pas de cas particulier à traiter.
        objets = ", ".join(
            f"{v} {k}{'s' if v > 1 else ''}"
            for k, v in sorted(e["par_objet"].items(), key=lambda x: -x[1])
        )
        lignes.append(f"  Sur 24 h : {e['total']} détections — {objets}.")
        if e.get("par_zone"):
            zones = ", ".join(f"{k} ({v})" for k, v in sorted(e["par_zone"].items(), key=lambda x: -x[1]))
            lignes.append(f"  Par endroit : {zones}.")
        # ⚠ LA PHRASE QUI EVITE LA REPONSE FAUSSE. Sans elle, « 32 détections de voiture »
        #   s'entend « 32 voitures » — alors qu'il y en a quatre, dont trois garées en
        #   permanence dans le champ. C'est l'erreur exacte que ce chantier a corrigée.
        lignes.append(
            "  (ce sont des DÉTECTIONS, pas des véhicules distincts : une même voiture "
            "qui part et revient compte deux fois)"
        )
    elif e is not None:
        lignes.append("  Rien de détecté sur les dernières 24 h.")

    passes = [x for x in derniers if not x.get("en_cours")]
    if passes:
        lignes.append("  Derniers passages :")
        for x in passes[:4]:
            ou = " et ".join(x.get("zones") or []) or "dans le champ"
            lignes.append(f"    · {_quand(x.get('debut'))} — {x['objet']} {ou}")

    if not lignes:
        return "Je n'ai pas de relevé exploitable de la caméra."
    return "\n".join(lignes)


@ToolRegistry.register("camera")
class CameraTool(BaseTool):
    """Historique qualifié de la caméra du parking, via le control plane (lecture seule)."""

    tool_id = "camera"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="camera",
            description=(
                "Ce que la caméra du parking a VU, avec les lieux et les heures : "
                "combien de voitures ou de personnes sont passées, où (la cour, le portail, "
                "le jardin, le mur ouest, le passage est), et s'il y a quelqu'un EN CE MOMENT. "
                "À utiliser pour toute question sur l'extérieur, la surveillance, une "
                "arrivée ou un passage : « combien de voitures aujourd'hui ? », « quelqu'un "
                "est venu cette nuit ? », « il y a quelqu'un dehors ? ». "
                "⚠ À ne pas confondre avec l'outil `home_assistant`, qui donne l'état "
                "INSTANTANÉ des capteurs de la maison — il ne sait pas compter ni dater."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="maison",
            latency_estimate=0.3,
            timeout_seconds=12.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            dash = _dashboard()
        except urllib.error.URLError as exc:
            # ⚠ Distinguer « le CP est injoignable » de « la caméra ne voit rien » : les
            #   deux se présentent comme une absence de réponse, et chercher du mauvais
            #   côté coûte une heure.
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Control plane injoignable ({CP_URL}) : {exc}. Je ne peux pas lire la caméra.",
                success=False,
                metadata={"erreur": "cp_injoignable"},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_name=self.tool_id,
                content="Je n'arrive pas à lire le relevé de la caméra.",
                success=False,
                metadata={"erreur": type(exc).__name__},
            )

        # ⚠ LA CLÉ EST `module_data`, PAS `modules` — piège déjà payé le 2026-08-03 sur
        #   l'outil `home_assistant` : avec le mauvais nom, `.get()` rend `{}` et l'outil
        #   annonce « module non déployé » sur un module parfaitement sain.
        d = (dash.get("module_data") or {}).get("frigate") or {}
        if not d:
            sante = (dash.get("module_health") or {}).get("frigate")
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Le control plane ne publie aucune donnée de caméra"
                    + (f" (module « {sante} »)." if sante else " — module absent.")
                ),
                success=False,
                metadata={"erreur": "module_absent", "sante": sante},
            )
        if d.get("_health") != "ok":
            return ToolResult(
                tool_name=self.tool_id,
                content=f"La caméra est en défaut côté control plane : {d.get('_error', 'raison inconnue')}",
                success=False,
                metadata={"erreur": "module_degrade"},
            )

        return ToolResult(
            tool_name=self.tool_id,
            content=_resume(d),
            success=True,
            metadata={"camera": d.get("camera"), "evenements": (d.get("evenements_24h") or {}).get("total")},
        )
