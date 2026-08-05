"""Outil `logs` — les journaux d'Avalon, par questions nommées.

⚠ AVA NE LIT PAS LOKI DIRECTEMENT, ET C'EST DELIBERE. Loki n'ecoute que sur
  `127.0.0.1:3100` et son proxy DMZ est **write-only depuis le 2026-07-30**, a la suite
  d'un incident reel : un mot de passe Keycloak lisible dans `auth.log` depuis une VM web
  compromise. Ava — VM DMZ executant du code communautaire et un LLM — est exactement la
  menace que ce controle modelise.
  Elle passe donc par `GET /api/v1/logs/{question}` du control plane, deja lecteur
  legitime de Loki. Meme doctrine que pour la maison et pour la parole : un service
  dual-home sert d'intermediaire, et le pouvoir reste de son cote.

⚠ ELLE NE COMPOSE AUCUNE REQUETE. Le control plane n'accepte que des questions NOMMEES.
  La preuve du besoin est empirique : « combien de fois le disjoncteur est tombe » pose
  en LogQL libre renvoie 310 occurrences qui sont TOUTES des erreurs de connectivite
  `tuya_local` — un chiffre plausible, alarmant et entierement faux.

⚠ CERTAINES SOURCES NE RENDENT QU'UN COMPTAGE (`authlog`, alertes reseau) : elles
  portent des IP et des noms d'utilisateurs. Ava peut dire « 12 echecs SSH cette nuit »
  sans jamais lire une ligne.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.parse
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
_TIMEOUT_S = 30.0

# ⚠ Miroir du catalogue du CP. Duplique volontairement : la description doit etre dans
#   le `ToolSpec` pour que le modele sache quoi demander AVANT d'appeler. Un test verifie
#   que les deux listes ne divergent pas — deux vocabulaires qui s'ecartent produiraient
#   des questions systematiquement refusees.
QUESTIONS = (
    "redemarrages",
    "erreurs_services",
    "deploiements",
    "activite_ava",
    "echecs_ssh",
    "alertes_reseau",
)
FENETRES = ("1h", "6h", "24h", "7j", "30j")


def _jeton() -> str:
    try:
        return CHEMIN_JETON.read_text().strip()
    except Exception:  # noqa: BLE001
        return ""


@ToolRegistry.register("logs")
class LogsTool(BaseTool):
    """Les journaux de l'infrastructure, par questions nommees."""

    tool_id = "logs"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="logs",
            description=(
                "Interroge les journaux de l'infrastructure Avalon (180 jours de "
                "retention) par QUESTIONS NOMMEES : services redemarres, erreurs dans "
                "les conteneurs, deploiements de stacks, journaux d'Ava elle-meme, "
                "tentatives SSH echouees, alertes du capteur reseau. "
                "A utiliser quand la question porte sur ce qui s'est passe SUR LES "
                "MACHINES (« qu'est-ce qui a redemarre cette nuit ? », « il y a eu des "
                "erreurs ? », « quand a-t-on deploye pour la derniere fois ? »). "
                "⚠ Different de `journal`, qui porte ce qu'AVA a observe (presence, "
                "temperatures, score) : ici ce sont les journaux systeme."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "enum": list(QUESTIONS),
                        "description": "Ce qu'on cherche. Aucune autre valeur n'est acceptee.",
                    },
                    "fenetre": {
                        "type": "string",
                        "enum": list(FENETRES),
                        "description": "Periode. Defaut 24h. Au-dela de 30j Loki refuse.",
                    },
                    "hote": {
                        "type": "string",
                        "description": "Limiter a une machine (ava, firewall, ws-02...). Optionnel.",
                    },
                },
                "required": ["question"],
            },
            category="infra",
            latency_estimate=2.0,
            timeout_seconds=35.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        question = params.get("question")
        if question not in QUESTIONS:
            return ToolResult(
                tool_name=self.tool_id,
                content="Question inconnue. Disponibles : " + ", ".join(QUESTIONS),
                success=False,
                metadata={"questions": list(QUESTIONS)},
            )
        jeton = _jeton()
        if not jeton:
            return ToolResult(
                tool_name=self.tool_id,
                content="Je ne peux pas interroger les journaux : jeton absent.",
                success=False,
            )

        args = {"fenetre": params.get("fenetre") or "24h"}
        if isinstance(params.get("hote"), str) and params["hote"]:
            args["hote"] = params["hote"]
        url = f"{CP_BASE}/api/v1/logs/{question}?" + urllib.parse.urlencode(args)
        requete = urllib.request.Request(url, headers={"X-CP-Voice-Token": jeton})
        try:
            with urllib.request.urlopen(requete, timeout=_TIMEOUT_S) as reponse:
                d = json.loads(reponse.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.warning("logs: refus (%s)", exc.code)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Le control plane a refuse la requete ({exc.code}).",
                success=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("logs: control plane injoignable (%s)", type(exc).__name__)
            return ToolResult(
                tool_name=self.tool_id,
                content="Le control plane ne repond pas — je ne peux pas lire les journaux.",
                success=False,
            )

        if d.get("erreur"):
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Requete refusee : {d['erreur']}."
                + (
                    f" Volume estime {d['volume_estime_mo']} Mo."
                    if d.get("volume_estime_mo")
                    else ""
                ),
                success=False,
                metadata=d,
            )

        n = d.get("occurrences", 0)
        # ⚠ LE COMPTE EST DESORMAIS EXACT (control plane, 2026-08-05) : il ne vient plus
        #   du nombre de lignes rendues, donc il n'est plus plafonne par la limite
        #   d'affichage. Avant ce correctif, la reponse a « combien de deploiements sur
        #   7 jours » etait « au moins 50 » — il y en avait 74.
        # ⚠ ON GARDE LA NUANCE POUR LE CAS DE REPLI. Si le comptage exact echoue, le
        #   control plane retombe sur le plancher et le DIT dans `comptage`. Lire ce champ
        #   plutot que de supposer l'exactitude : c'est precisement en supposant qu'une
        #   cle porte ce qu'on croit qu'on fabrique des zeros faux.
        # ⚠ Le repli sur `tronque` couvre un control plane plus ancien que cet outil —
        #   sans lui, un CP non deploye rendrait un plancher presente comme un total.
        comptage = d.get("comptage")
        plancher = (
            comptage.startswith("plancher")
            if isinstance(comptage, str)
            else bool(d.get("tronque"))
        )
        prefixe = "au moins " if plancher else ""
        entete = f"{d.get('libelle', question)} — {prefixe}{n} sur {d.get('fenetre')}"
        if not n:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"{entete} : rien.",
                success=True,
                metadata=d,
            )
        lignes = d.get("lignes") or []
        if not lignes:
            # Source sensible : comptage seul, et on dit pourquoi.
            return ToolResult(
                tool_name=self.tool_id,
                content=f"{entete}.\n({d.get('detail', 'comptage seul')})",
                success=True,
                metadata=d,
            )
        rendu = []
        for x in lignes[:20]:
            h = datetime.datetime.fromtimestamp(x["horodatage"]).strftime("%d/%m %H:%M")
            rendu.append(f"  · {h} — {x['texte'][:150]}")
        return ToolResult(
            tool_name=self.tool_id,
            content=f"{entete} :\n" + "\n".join(rendu),
            success=True,
            metadata={k: v for k, v in d.items() if k != "lignes"},
        )
