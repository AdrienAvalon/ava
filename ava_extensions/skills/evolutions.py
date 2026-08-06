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

import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

#: Racine de son propre dépôt, dérivée de l'emplacement de ce fichier. Jamais un paramètre.
RACINE = Path(__file__).resolve().parents[2]

#: ⚠ LA MOITIÉ DE CE QUI CHANGE CHEZ ELLE N'EST PAS DANS SON DÉPÔT. Ses outils côté control
#: plane, le relais de conversation, sa veille et sa liste blanche d'outils vivent dans
#: `infra_avalon`. Angle mort trouvé EN LUI PARLANT le 2026-08-06 : interrogée sur ses
#: propres échecs, elle a classé « inexpliqué » un refus dont la cause était un correctif
#: livré une heure plus tôt dans l'AUTRE dépôt. Huit commits du jour lui étaient invisibles,
#: et rien ne le lui disait — sa vue de sa propre évolution était juste sur ce qu'elle
#: voyait, et amputée de moitié.
#: ⚠ Elle n'a PAS ce dépôt sur sa machine, et il n'a rien à y faire. C'est le control plane,
#: dual-homé et déjà porteur d'un jeton GitLab, qui le lit pour elle. Même doctrine que
#: pour Home Assistant, la parole et les journaux.
CP_BASE = os.environ.get("AVA_CP_BASE", "http://192.168.100.31:8100")
CHEMIN_JETON = Path(
    os.environ.get(
        "AVA_VOICE_TOKEN_FILE", str(Path.home() / ".openjarvis" / "cp_voice_token")
    )
).expanduser()

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


#: Correspondance fenêtre → jours, pour l'appel au control plane.
_JOURS = {"7j": 7, "30j": 30, "toujours": 30}


def _cote_infra(fenetre: str, nombre: int) -> list[tuple[str, str, str]] | None:
    """Les changements d'infrastructure qui la concernent, via le control plane.

    ⚠ Rend `None` si le control plane est muet — « je n'ai pas pu regarder » n'est pas
      « rien n'a changé », et c'est le rendu qui doit porter la différence.
    ⚠ NE FAIT PAS ÉCHOUER L'OUTIL : si cette moitié manque, on rend quand même le journal
      local en DISANT qu'il est partiel. Une vue amputée annoncée vaut mieux qu'aucune vue,
      et infiniment mieux qu'une vue amputée SILENCIEUSE — c'est précisément le défaut que
      cet ajout corrige.
    """
    try:
        jeton = CHEMIN_JETON.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return None
    jours = _JOURS.get(fenetre, 7)
    url = f"{CP_BASE}/api/v1/evolutions?jours={jours}&limite={nombre}"
    req = urllib.request.Request(url, headers={"X-CP-Voice-Token": jeton})
    try:
        with urllib.request.urlopen(req, timeout=12) as rep:  # noqa: S310
            d = json.loads(rep.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("evolutions: control plane muet (%s)", type(exc).__name__)
        return None
    lignes = d.get("changements")
    if lignes is None:
        return None
    return [(str(x.get("date", "")), str(x.get("titre", "")), "") for x in lignes]


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

        # ⚠ LA SECONDE MOITIÉ. Sans elle, ce journal était juste sur ce qu'il voyait et
        #   amputé de moitié — sans le dire. Les deux sources sont FUSIONNÉES et chaque
        #   ligne porte sa provenance : « on m'a changée » et « on a changé mes outils
        #   côté infrastructure » ne se diagnostiquent pas au même endroit.
        fenetre = str(params.get("depuis") or "7j")
        infra = _cote_infra(fenetre, nombre)

        if not resumes and not infra:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Aucun changement de capacité sur cette période."
                    if infra is not None
                    else "Aucun changement dans mon dépôt, et je n'ai pas pu consulter "
                    "l'infrastructure — je ne peux donc pas dire que rien n'a changé."
                ),
                success=True,
                metadata={"trouves": 0, "infra_lue": infra is not None},
            )

        lignes = [
            f"  · {date} — [moi] {sujet}" + (f"\n      {corps}" if corps else "")
            for date, sujet, corps in resumes
        ]
        lignes += [
            f"  · {date} — [infrastructure] {sujet}" for date, sujet, _ in (infra or [])
        ]
        lignes.sort(reverse=True)

        # ⚠ ON DIT QUAND LA VUE EST PARTIELLE. Une vue amputée annoncée vaut mieux qu'aucune
        #   vue, et infiniment mieux qu'une vue amputée SILENCIEUSE — c'est exactement le
        #   défaut que cet ajout corrige, il serait absurde de le reproduire ici.
        avertissement = (
            ""
            if infra is not None
            else "\n⚠ Je n'ai PAS pu lire les changements côté infrastructure : cette liste "
            "est donc incomplète, et une absence n'y prouve rien.\n"
        )
        return ToolResult(
            tool_name=self.tool_id,
            content=(
                "<mes_evolutions>\n"
                "Changements me concernant, du plus récent au plus ancien. [moi] = mon "
                "propre code ; [infrastructure] = mes outils côté control plane, ma liste "
                "d'outils, le relais de conversation. Si l'un d'eux contredit ce que je "
                "crois savoir de moi, c'est lui qui a raison.\n"
                + avertissement
                + "\n".join(lignes)
                + "\n</mes_evolutions>"
            ),
            success=True,
            metadata={
                "trouves": len(resumes) + len(infra or []),
                "depuis_moi": len(resumes),
                "depuis_infra": len(infra or []),
                "infra_lue": infra is not None,
            },
        )
