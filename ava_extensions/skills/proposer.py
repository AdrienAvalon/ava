"""Outil `proposer` — Ava propose une modification du depot, jamais elle ne l'applique.

⚠ C'EST LE BARREAU 2 DE L'ECHELLE D'AUTONOMIE, et son interet tient a ce qu'il NE fait
  pas : aucune ecriture sur `main`, aucune fusion. Une proposition devient une merge
  request que l'administrateur lit, amende ou ferme. Le retour arriere est la fermeture
  d'un onglet.

⚠ AVA NE PARLE PAS A GITLAB. Elle demande au control plane, qui tourne sur AVA et detient
  le jeton. Celui-ci ne franchit JAMAIS la frontiere vers la DMZ — meme doctrine que le
  jeton Home Assistant et que le jeton Matrix de la parole. Une Ava compromise ne peut
  donc pas s'en servir ailleurs.

⚠ TROIS BORNES INDEPENDANTES, et c'est leur cumul qui compte :
  1. le DOMAINE est ferme (documentation Markdown seule, 5 fichiers, 64 000 octets), et
     les chemins sont normalises AVANT comparaison — sans quoi `docs/../CLAUDE.md`
     passerait ;
  2. le JETON est Developer (30) quand `main` exige Instance admins (60) pour pousser ET
     pour fusionner. Verifie contre GitLab le 2026-08-05, pas suppose : une tentative de
     fusion rend 401, une ecriture directe sur `main` rend 403 « You are not allowed to
     push into this branch ». Un defaut du code ne peut donc pas devenir une ecriture sur
     la branche protegee ;
  3. le TITRE doit respecter `type(scope): description` — la convention du depot est
     appliquee cote control plane, pas laissee a la bonne volonte du modele.

⚠ CE QU'IL FAUT ECRIRE DANS `contenu` : le fichier ENTIER, tel qu'il doit exister apres
  la modification. Ce n'est pas un correctif ni un extrait. Un fichier existant est
  REMPLACE par ce contenu.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

CP_BASE = os.environ.get("AVA_CP_BASE", "http://192.168.100.31:8100")
CHEMIN_JETON = Path(
    os.environ.get(
        "AVA_VOICE_TOKEN_FILE", str(Path.home() / ".openjarvis" / "cp_voice_token")
    )
).expanduser()
_TIMEOUT_S = 45.0


def _jeton() -> str:
    try:
        return CHEMIN_JETON.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@ToolRegistry.register("proposer")
class ProposerTool(BaseTool):
    """Ouvre une merge request de documentation pour le compte d'Adrien."""

    tool_id = "proposer"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="proposer",
            description=(
                "Propose une modification de la DOCUMENTATION du depot Avalon en ouvrant "
                "une merge request. Tu ne modifies rien : Adrien lira, amendera ou "
                "fermera. A utiliser quand tu constates qu'une page est fausse, perimee "
                "ou manquante — par exemple apres avoir verifie une donnee et trouve que "
                "la doc dit autre chose. "
                "N'ouvre PAS de proposition pour un simple avis : il faut un ecart "
                "constate entre ce que dit un document et ce que tu as mesure. "
                "Le titre DOIT suivre « type(scope): description » (ex. « docs(hass): "
                "corriger les bornes de consigne des vannes »). Le contenu est le fichier "
                "ENTIER apres modification, pas un extrait."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "titre": {
                        "type": "string",
                        "description": "Titre au format « type(scope): description ».",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Pourquoi cette modification : ce que tu as mesure, ou, et ce "
                            "que le document dit a la place. C'est ce qu'Adrien lira en "
                            "premier."
                        ),
                    },
                    "chemin": {
                        "type": "string",
                        "description": "Chemin du fichier, sous docs/ et en .md.",
                    },
                    "contenu": {
                        "type": "string",
                        "description": "Le fichier ENTIER apres modification.",
                    },
                },
                "required": ["titre", "description", "chemin", "contenu"],
            },
            category="infra",
            latency_estimate=6.0,
            timeout_seconds=60.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        jeton = _jeton()
        if not jeton:
            return ToolResult(
                tool_name=self.tool_id,
                content="Je ne peux pas proposer : jeton absent.",
                success=False,
            )
        corps = json.dumps(
            {
                "domaine": "documentation",
                "titre": params.get("titre") or "",
                "description": params.get("description") or "",
                "fichiers": [
                    {
                        "chemin": params.get("chemin") or "",
                        "contenu": params.get("contenu") or "",
                    }
                ],
            }
        ).encode("utf-8")
        requete = urllib.request.Request(
            f"{CP_BASE}/api/v1/propositions",
            data=corps,
            headers={"Content-Type": "application/json", "X-CP-Voice-Token": jeton},
        )
        try:
            with urllib.request.urlopen(requete, timeout=_TIMEOUT_S) as reponse:
                d = json.loads(reponse.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # ⚠ ON REND LE MOTIF DU CONTROL PLANE, pas un message generique : c'est lui
            #   qui sait si le chemin sort du domaine ou si le titre viole la convention,
            #   et sans ce detail le modele reessaie a l'aveugle.
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:  # noqa: BLE001
                detail = ""
            logger.warning("proposer: refus (%s) %s", exc.code, detail)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Proposition refusee ({exc.code}) : {detail or 'sans detail'}.",
                success=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("proposer: control plane injoignable (%s)", type(exc).__name__)
            return ToolResult(
                tool_name=self.tool_id,
                content="Le control plane ne repond pas — je n'ai rien propose.",
                success=False,
            )

        if not d.get("ok"):
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Proposition refusee : {d.get('erreur') or 'motif inconnu'}.",
                success=False,
                metadata=d,
            )
        return ToolResult(
            tool_name=self.tool_id,
            content=(
                f"Proposition ouverte : merge request !{d.get('iid')} — {d.get('url')}\n"
                f"Branche {d.get('branche')}, fichier {', '.join(d.get('fichiers') or [])}.\n"
                "Rien n'est applique : Adrien decide."
            ),
            success=True,
            metadata=d,
        )


@ToolRegistry.register("lire_doc")
class LireDocTool(BaseTool):
    """Lit un document du depot — le MEME perimetre que ce qu'elle peut proposer.

    ⚠ IL VIT DANS LE MEME FICHIER QUE `proposer`, DELIBEREMENT. Lire et proposer sont
      bornes par la meme fonction cote control plane (`chemin_valide`) ; les separer ici
      inviterait a les faire diverger la-bas. Ce qu'on peut lire est exactement ce qu'on
      peut reproposer, ni plus ni moins.
    ⚠ SANS LUI, `proposer` N'ETAIT QU'A MOITIE UN OUTIL : Ava pouvait CREER une page, pas
      en AMENDER une. Invitee a corriger sa propre page le 2026-08-05, elle a refuse
      d'improviser un contenu qu'elle n'avait pas vu — bonne reaction, et cul-de-sac.
    """

    tool_id = "lire_doc"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="lire_doc",
            description=(
                "Lit un document de la documentation du depot Avalon (docs/*.md), ou "
                "LISTE les documents disponibles si aucun chemin n'est donne. "
                "A utiliser AVANT `proposer` des que tu modifies une page existante : "
                "le contenu que tu enverras REMPLACE le fichier entier, donc il faut "
                "partir du texte reel, jamais de ton souvenir. "
                "Meme perimetre que `proposer` : la documentation, et rien d'autre."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "chemin": {
                        "type": "string",
                        "description": (
                            "Chemin du document, sous docs/ et en .md. Omettre pour "
                            "obtenir la liste des documents disponibles."
                        ),
                    }
                },
                "required": [],
            },
            category="infra",
            latency_estimate=1.0,
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        jeton = _jeton()
        if not jeton:
            return ToolResult(
                tool_name=self.tool_id,
                content="Je ne peux pas lire la documentation : jeton absent.",
                success=False,
            )
        chemin = str(params.get("chemin") or "").strip()
        if chemin:
            url = f"{CP_BASE}/api/v1/propositions/fichier?" + urllib.parse.urlencode(
                {"chemin": chemin}
            )
        else:
            url = f"{CP_BASE}/api/v1/propositions/fichiers"
        requete = urllib.request.Request(url, headers={"X-CP-Voice-Token": jeton})
        try:
            with urllib.request.urlopen(requete, timeout=_TIMEOUT_S) as reponse:
                d = json.loads(reponse.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # ⚠ ON REND LE MOTIF DU CONTROL PLANE. Un 404 (« faute de frappe ») et un 403
            #   (« hors perimetre ») demandent deux reactions opposees ; un message
            #   generique ferait reessayer au hasard.
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:  # noqa: BLE001
                detail = ""
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Lecture refusee ({exc.code}) : {detail or 'sans detail'}.",
                success=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lire_doc: control plane injoignable (%s)", type(exc).__name__)
            return ToolResult(
                tool_name=self.tool_id,
                content="Le control plane ne repond pas — je n'ai rien pu lire.",
                success=False,
            )

        if not chemin:
            fichiers = d.get("fichiers") or []
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"{len(fichiers)} document(s) lisibles et proposables :\n"
                    + "\n".join(f"  - {f}" for f in fichiers)
                ),
                success=True,
                metadata=d,
            )
        return ToolResult(
            tool_name=self.tool_id,
            content=(
                f"{d.get('chemin')} ({d.get('octets')} octets) :\n\n{d.get('contenu', '')}"
            ),
            success=True,
            metadata={"chemin": d.get("chemin"), "octets": d.get("octets")},
        )
