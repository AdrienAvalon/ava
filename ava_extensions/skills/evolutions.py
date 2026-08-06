"""Outil `evolutions` — ce qui a changé CHEZ ELLE, et quand.

⚠ POURQUOI CET OUTIL EXISTE. Constat de l'admin, le 2026-08-06 : « j'ai l'impression
  qu'elle ne sait pas trop à chaque fois ce qui a changé ». Il a raison, et le défaut est
  structurel : son code est modifié plusieurs fois par jour — mémoire, outils, garde-fous,
  identité Matrix — et **rien ne le lui dit**. Elle décrit donc ses propres capacités
  d'après sa persona, c'est-à-dire d'après un texte figé au jour de sa rédaction.

  Le symptôme se voit à l'œil : elle récitait encore « je n'ai aucune initiative » alors
  que sa veille documentaire tournait depuis des heures. Une IA qui se décrit à partir
  d'un souvenir périmé se trompe sur elle-même **avec assurance** — et refuse des tâches
  qu'elle sait faire.

⚠ LA SOURCE EST LE JOURNAL GIT, PAS UN FICHIER RECOPIÉ. C'est le point de conception.
  Un CHANGELOG écrit à la main dériverait dès la première session pressée, et il dérive
  toujours dans le sens qui trompe : on oublie d'y retirer ce qui a été défait. Le journal
  git ne peut pas mentir sur ce qui a été fait, parce qu'il EST ce qui a été fait.

⚠ AUCUNE CHAÎNE DU MODÈLE N'ATTEINT LA LIGNE DE COMMANDE. Le nombre est un entier borné,
  la fenêtre est un `enum`, le répertoire est fixe. Le modèle ne compose pas de commande :
  il choisit parmi des questions nommées. Même doctrine que `journal` et `logs`, et pour
  la même raison — une requête plausible et ruineuse est acceptée sans broncher par les
  outils qui acceptent un langage.

⚠ LECTURE SEULE, ET SUR SON PROPRE DÉPÔT UNIQUEMENT. `git log` ne modifie rien, ne sort
  pas sur le réseau, et le chemin est dérivé de l'emplacement de ce module — il ne peut
  donc pas désigner un autre dépôt.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

#: Racine de son propre dépôt, dérivée de l'emplacement de ce fichier. Jamais un paramètre.
RACINE = Path(__file__).resolve().parents[2]

#: Fenêtres offertes. `enum` fermé : aucune expression de date ne vient du modèle.
_FENETRES = {"7j": "7 days ago", "30j": "30 days ago", "toujours": ""}

#: Ce qui compte comme un changement LA CONCERNANT. `chore` et `docs` sont écartés : un
#: re-baseline d'empreinte ou une correction de documentation ne changent pas ce qu'elle
#: sait faire, et les inclure noierait les vrais changements sous du bruit d'entretien.
_TYPES = ("feat", "fix", "perf", "refactor")

_MAX = 20
_DEFAUT = 8


def _lignes_git(depuis: str, nombre: int) -> list[str] | None:
    """Le journal brut, ou None si git est injoignable. Ne lève jamais."""
    commande = [
        "git",
        "-C",
        str(RACINE),
        "log",
        f"-{nombre * 4}",  # on filtre ensuite : prendre large pour ne pas rendre vide
        "--date=short",
        "--pretty=format:%ad\x1f%s\x1f%b\x1e",
    ]
    if depuis:
        commande.insert(5, f"--since={depuis}")
    try:
        r = subprocess.run(  # noqa: S603 — argv fixe, aucune chaîne du modèle n'y entre
            commande, capture_output=True, text=True, timeout=8, check=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("evolutions: git injoignable (%s)", type(exc).__name__)
        return None
    if r.returncode != 0:
        logger.warning("evolutions: git a refusé (%s)", r.stderr[:120])
        return None
    return [bloc for bloc in r.stdout.split("\x1e") if bloc.strip()]


def _resumer(bloc: str) -> tuple[str, str, str] | None:
    """(date, sujet, première phrase du corps) — ou None si ce n'est pas pour elle."""
    morceaux = bloc.strip().split("\x1f")
    if len(morceaux) < 2:
        return None
    date, sujet = morceaux[0].strip(), morceaux[1].strip()
    if not any(sujet.startswith(t) for t in _TYPES):
        return None
    corps = morceaux[2].strip() if len(morceaux) > 2 else ""
    # ⚠ On ne garde que la PREMIÈRE ligne utile du corps : les messages de ce dépôt font
    #   souvent trente lignes. Tout rendre ferait un appel de plusieurs milliers de jetons
    #   pour une question à laquelle une phrase répond.
    premiere = next(
        (
            ligne.strip()
            for ligne in corps.splitlines()
            if ligne.strip() and not ligne.startswith(("Co-Authored", "Claude-Session"))
        ),
        "",
    )
    return date, sujet, premiere[:200]


@ToolRegistry.register("evolutions")
class EvolutionsTool(BaseTool):
    """Ce qui a récemment changé dans le code d'Ava elle-même."""

    tool_id = "evolutions"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="evolutions",
            description=(
                "Ce qui a récemment changé dans le code d'Ava elle-même : nouvelles "
                "capacités, corrections, garde-fous. À utiliser quand on lui demande ce "
                "qui a changé chez elle, ce qu'elle sait faire de nouveau, ou pourquoi "
                "quelque chose se comporte différemment d'avant. À consulter aussi avant "
                "d'affirmer une de ses propres limites : sa description d'elle-même est "
                "figée au jour où elle a été écrite, ce journal ne l'est pas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "depuis": {
                        "type": "string",
                        "enum": list(_FENETRES),
                        "description": "Fenêtre à regarder. Défaut : 7j.",
                    },
                    "nombre": {
                        "type": "integer",
                        "description": f"Combien de changements rendre (1 à {_MAX}).",
                    },
                },
                "required": [],
            },
            category="memoire",
            latency_estimate=0.3,
            timeout_seconds=12.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        depuis = _FENETRES.get(str(params.get("depuis") or "7j"), _FENETRES["7j"])
        try:
            nombre = int(params.get("nombre") or _DEFAUT)
        except (TypeError, ValueError):
            nombre = _DEFAUT
        nombre = max(1, min(_MAX, nombre))

        blocs = _lignes_git(depuis, nombre)
        if blocs is None:
            return ToolResult(
                tool_name=self.tool_id,
                # ⚠ Dire que la SOURCE est muette, jamais « rien n'a changé » : les deux
                #   se ressemblent et une seule est vraie. Conclure à l'absence de
                #   changement parce qu'on n'a pas pu regarder est le défaut que ce
                #   système passe son temps à corriger ailleurs.
                content="Je n'arrive pas à lire mon journal de modifications.",
                success=False,
                metadata={"trouves": 0},
            )

        resumes = [r for r in (_resumer(b) for b in blocs) if r][:nombre]
        if not resumes:
            return ToolResult(
                tool_name=self.tool_id,
                content="Aucun changement de capacité sur cette période.",
                success=True,
                metadata={"trouves": 0},
            )
        lignes = [
            f"  · {date} — {sujet}" + (f"\n      {corps}" if corps else "")
            for date, sujet, corps in resumes
        ]
        return ToolResult(
            tool_name=self.tool_id,
            content=(
                "<mes_evolutions>\n"
                "Changements apportés à mon propre code, du plus récent au plus ancien. "
                "Si l'un d'eux contredit ce que je crois savoir de moi, c'est lui qui a "
                "raison.\n" + "\n".join(lignes) + "\n</mes_evolutions>"
            ),
            success=True,
            metadata={"trouves": len(resumes)},
        )
