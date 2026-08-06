"""Outil `introspection` — comment ELLE se comporte, mesuré sur ses propres traces.

⚠ IL COMPLÈTE `evolutions`, IL NE LE DOUBLE PAS, et la distinction est nette :
    · `evolutions`    → ce qui a CHANGÉ dans son code (journal git, écrit par d'autres) ;
    · `introspection` → comment elle S'EN SORT (ses traces, produites par elle-même).
  La première répond à « qu'est-ce qu'on m'a fait ? », la seconde à « est-ce que je fais
  bien mon travail ? ». Sans la seconde, elle ne peut pas répondre à « où est-ce que tu
  coinces ? » autrement qu'en devinant — et deviner, sur soi, produit des réponses
  agréables et fausses.

⚠ C'EST LA BRIQUE QUI MANQUE À LA VISION DE L'ADMIN : « qu'elle s'analyse, crée, teste,
  améliore et corrige Avalon d'elle-même ». Sa veille documentaire compare déjà un
  document à la mesure ; elle ne pouvait pas comparer SON PROPRE comportement à quoi que
  ce soit, faute d'y avoir accès.

⚠ CE QUE LES TRACES NE PORTENT PAS, et qu'il ne faut donc pas promettre : `metadata` et
  `messages` sont VIDES sur les 204 traces (mesuré le 2026-08-06). Le détail par OUTIL —
  quel outil a échoué, avec quel argument — n'existe pas en base. Cet outil rend donc le
  taux d'aboutissement, la latence, le coût et les questions fautives ; jamais « c'est
  `logs` qui a planté ». Annoncer plus serait rendre une donnée absente sous forme de
  chiffre faux, le défaut le plus fréquent de ce système.

⚠ `tool_failure` NE VEUT PAS DIRE « MAUVAISE RÉPONSE », et le confondre ferait conclure à
  une dégradation là où il y a eu un bon réflexe. Exemple mesuré du 2026-08-06 : interrogée
  sur un hôte inexistant, elle a REFUSÉ d'inventer un chiffre — bonne réponse — et la trace
  porte `tool_failure` parce que l'outil `logs`, lui, a bien refusé. Le rendu le dit.

⚠ LECTURE SEULE, SUR SA PROPRE BASE, EN LOCAL. Aucune requête composée par le modèle : les
  fenêtres sont un `enum`, la vue est un `enum`, le nombre est un entier borné. Même
  doctrine que `journal`, `logs` et `evolutions`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

CHEMIN_TRACES = Path(
    os.environ.get("AVA_TRACES_PATH", str(Path.home() / ".openjarvis" / "traces.db"))
)

#: Fenêtres offertes. `enum` fermé : aucune expression de date ne vient du modèle.
_FENETRES = {"24h": 86400.0, "7j": 604800.0, "30j": 2592000.0}

#: Verdicts qui comptent comme un aboutissement. `success` est l'ancien libellé, conservé
#: parce que d'anciennes traces le portent encore — l'ignorer ferait chuter le taux sans
#: qu'aucune dégradation réelle ne se soit produite.
_ABOUTIS = frozenset({"completed", "success"})

_MAX_LIGNES = 12


def _traces(depuis_s: float) -> list[dict[str, Any]] | None:
    """Les traces de la fenêtre, ou None si la base est illisible.

    ⚠ `None` DIT « JE N'AI PAS PU REGARDER », ce qui n'est pas « je n'ai rien fait ». Les
      deux se ressemblent et une seule est vraie — c'est la distinction que ce système
      passe son temps à rétablir ailleurs.
    """
    cx: sqlite3.Connection | None = None
    try:
        # ⚠ OUVERTURE EN LECTURE SEULE (`mode=ro`), et ce n'est pas décoratif : c'est le
        #   seul garde qui empêche une erreur de code de corrompre la base de traces
        #   pendant qu'Ava y écrit ses échanges en cours.
        cx = sqlite3.connect(f"file:{CHEMIN_TRACES}?mode=ro", uri=True, timeout=5)
        cx.row_factory = sqlite3.Row
        lignes = cx.execute(
            "select query, outcome, started_at, total_tokens, total_latency_seconds "
            "from traces where started_at > ? order by started_at desc",
            (time.time() - depuis_s,),
        ).fetchall()
        return [dict(r) for r in lignes]
    except Exception as exc:  # noqa: BLE001
        logger.warning("introspection: traces illisibles (%s)", type(exc).__name__)
        return None
    finally:
        if cx is not None:
            cx.close()


def _centile(valeurs: list[float], part: float) -> float:
    if not valeurs:
        return 0.0
    tri = sorted(valeurs)
    return tri[min(len(tri) - 1, int(len(tri) * part))]


def _resume(traces: list[dict[str, Any]]) -> list[str]:
    total = len(traces)
    aboutis = sum(1 for t in traces if str(t.get("outcome")) in _ABOUTIS)
    lat = [float(t.get("total_latency_seconds") or 0) for t in traces]
    jet = [float(t.get("total_tokens") or 0) for t in traces]
    return [
        f"  {total} échanges, {aboutis} aboutis ({100 * aboutis / total:.0f} %)",
        f"  Temps de réponse : médiane {_centile(lat, 0.5):.0f} s, "
        f"9 sur 10 sous {_centile(lat, 0.9):.0f} s, pire {max(lat or [0]):.0f} s",
        f"  Coût : médiane {_centile(jet, 0.5):.0f} jetons, "
        f"9 sur 10 sous {_centile(jet, 0.9):.0f}, pire {max(jet or [0]):.0f}",
    ]


def _echecs(traces: list[dict[str, Any]]) -> list[str]:
    rates = [t for t in traces if str(t.get("outcome")) not in _ABOUTIS]
    if not rates:
        return ["  Aucun échec sur la période."]
    lignes = [
        "  ⚠ « tool_failure » = un OUTIL a refusé, pas forcément une mauvaise réponse :",
        "    refuser d'inventer un chiffre quand l'outil dit non EST le bon réflexe.",
    ]
    for t in rates[:_MAX_LIGNES]:
        quand = time.strftime(
            "%d/%m %H:%M", time.localtime(float(t.get("started_at") or 0))
        )
        lignes.append(
            f"  · {quand} [{t.get('outcome')}] {str(t.get('query') or '')[:90]}"
        )
    if len(rates) > _MAX_LIGNES:
        lignes.append(f"  … et {len(rates) - _MAX_LIGNES} autre(s) non montré(s).")
    return lignes


def _couteux(traces: list[dict[str, Any]]) -> list[str]:
    tri = sorted(traces, key=lambda t: float(t.get("total_tokens") or 0), reverse=True)
    lignes = ["  Les questions qui m'ont coûté le plus cher :"]
    for t in tri[:_MAX_LIGNES]:
        quand = time.strftime(
            "%d/%m %H:%M", time.localtime(float(t.get("started_at") or 0))
        )
        lignes.append(
            f"  · {quand} — {float(t.get('total_tokens') or 0):.0f} jetons, "
            f"{float(t.get('total_latency_seconds') or 0):.0f} s : "
            f"{str(t.get('query') or '')[:70]}"
        )
    return lignes


_VUES = {"resume": _resume, "echecs": _echecs, "couteux": _couteux}


@ToolRegistry.register("introspection")
class IntrospectionTool(BaseTool):
    """Comment Ava se comporte, mesuré sur ses propres traces."""

    tool_id = "introspection"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="introspection",
            description=(
                "Mesure le comportement d'Ava elle-même sur ses échanges passés : taux "
                "d'aboutissement, temps de réponse, coût en jetons, et les questions sur "
                "lesquelles elle a échoué. À utiliser quand on lui demande comment elle "
                "s'en sort, où elle coince, ce qu'elle pourrait améliorer chez elle, ou "
                "avant d'affirmer quoi que ce soit sur ses propres performances — c'est "
                "mesuré, pas ressenti. Ne dit PAS quel outil a échoué : ce détail n'est "
                "pas enregistré."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "vue": {
                        "type": "string",
                        "enum": list(_VUES),
                        "description": (
                            "'resume' (défaut) : chiffres d'ensemble. 'echecs' : les "
                            "questions qui n'ont pas abouti. 'couteux' : les plus chères."
                        ),
                    },
                    "depuis": {
                        "type": "string",
                        "enum": list(_FENETRES),
                        "description": "Fenêtre à mesurer. Défaut : 7j.",
                    },
                },
                "required": [],
            },
            category="memoire",
            latency_estimate=0.2,
            timeout_seconds=10.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        vue = str(params.get("vue") or "resume")
        rendu = _VUES.get(vue, _resume)
        depuis = _FENETRES.get(str(params.get("depuis") or "7j"), _FENETRES["7j"])

        traces = _traces(depuis)
        if traces is None:
            return ToolResult(
                tool_name=self.tool_id,
                # ⚠ Dire que la SOURCE est muette, jamais « je n'ai rien fait » : les deux
                #   se ressemblent et une seule est vraie.
                content="Je n'arrive pas à lire mes propres traces.",
                success=False,
                metadata={"traces": 0},
            )
        if not traces:
            return ToolResult(
                tool_name=self.tool_id,
                content="Aucun échange enregistré sur cette période.",
                success=True,
                metadata={"traces": 0},
            )
        return ToolResult(
            tool_name=self.tool_id,
            content="<mes_mesures>\n" + "\n".join(rendu(traces)) + "\n</mes_mesures>",
            success=True,
            metadata={"traces": len(traces), "vue": vue},
        )
