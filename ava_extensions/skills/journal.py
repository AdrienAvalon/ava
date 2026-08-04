"""Outil `journal` — ce qu'Ava a VU se passer, et quand.

⚠ POURQUOI CET OUTIL EXISTE, ET POURQUOI SON ABSENCE ÉTAIT LE MÊME DÉFAUT DEUX FOIS.
  La perception continue enregistre depuis le 2026-08-04 : arrivées et départs,
  températures, modules qui basculent, appareils qui cessent de répondre. Mesuré le soir
  même : trois faits en deux heures, dont « le score est passé de 98 à 96 ».
  Et **aucun outil ne les relisait**. Ava percevait, mémorisait — et restait incapable de
  répondre à « il s'est passé quoi cet après-midi ? », alors que la réponse était dans sa
  propre base.

  C'est exactement le défaut corrigé le matin même sur l'outil `memoire` : l'amont
  extrayait des faits durables que rien ne consommait. Reproduit une couche plus haut, le
  même jour. La leçon est structurelle — **écrire une mémoire et la relier au modèle sont
  deux gestes distincts**, et le second se fait oublier parce que le premier « marche ».

⚠ POURQUOI UN OUTIL SÉPARÉ DE `memoire`, ET NON UNE EXTENSION. Les deux mémoires n'ont
  pas la même nature, et les confondre produirait des réponses fausses :
    · `memoire`  → ce qui est VRAI, sans date. « Le disjoncteur est derrière la porte
                   verte. » Partagé entre tous les interlocuteurs.
    · `journal`  → ce qui est ARRIVÉ, daté. « Aurélie est rentrée à 18 h 12. »
  Une question sur un emplacement n'a rien à faire dans le journal ; une question sur la
  nuit dernière n'a rien à faire dans les faits. Deux outils aux descriptions nettes
  laissent le modèle choisir juste.

⚠ TOUT EST BORNÉ PAR CONSTRUCTION. La leçon vient de Loki, où une requête plausible et
  ruineuse est acceptée sans broncher : `max_query_bytes_read = 0B`, et un balayage de
  30 jours coûterait ~112 Go. Ici la fenêtre est un `enum`, le nombre de lignes est
  plafonné, et le modèle ne compose aucune requête. Il choisit parmi des questions
  nommées — jamais un langage.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from ava_extensions.perception import memoire_perception as memoire
from ava_extensions.perception.qualification import Niveau

logger = logging.getLogger(__name__)

# ⚠ FENÊTRES NOMMÉES, jamais une durée libre. Un modèle qui pourrait écrire « 900 jours »
#   obtiendrait une réponse vide et en conclurait qu'il ne s'est rien passé — alors que
#   la rétention est de 90 jours. Une borne explicite se dit ; une borne implicite se
#   découvre au pire moment.
FENETRES: dict[str, float] = {
    "dernière heure": 3600,
    "6 heures": 21600,
    "aujourd'hui": 86400,
    "semaine": 604800,
    "mois": 2592000,
}

MAX_LIGNES = 40


def _quand(horodatage: float) -> str:
    """Un repère temporel dit à l'oral, pas un horodatage lu à la machine.

    ⚠ Ava répond à la VOIX. « 2026-08-04T18:47:12 » se prononce atrocement et n'aide
      personne ; « il y a vingt minutes » est ce qu'un humain dirait.
    """
    ecart = max(0.0, datetime.datetime.now().timestamp() - horodatage)
    if ecart < 90:
        return "à l'instant"
    if ecart < 3600:
        return f"il y a {int(ecart // 60)} min"
    if ecart < 86400:
        h = datetime.datetime.fromtimestamp(horodatage)
        return f"à {h.strftime('%H:%M')}"
    jours = int(ecart // 86400)
    h = datetime.datetime.fromtimestamp(horodatage)
    if jours == 1:
        return f"hier à {h.strftime('%H:%M')}"
    return f"il y a {jours} jours ({h.strftime('%d/%m %H:%M')})"


@ToolRegistry.register("journal")
class JournalTool(BaseTool):
    """Ce qu'Ava a vu se passer sur l'infrastructure et dans la maison."""

    tool_id = "journal"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="journal",
            description=(
                "Ce qu'Ava a OBSERVÉ se passer, avec les dates : arrivées et départs des "
                "personnes, températures, modules de l'infrastructure qui se dégradent ou "
                "se rétablissent, appareils qui cessent de répondre, piles faibles. "
                "À utiliser pour toute question sur le PASSÉ ou sur une évolution : "
                "« il s'est passé quoi cette nuit ? », « quand est rentrée Aurélie ? », "
                "« c'est la combientième fois que le disjoncteur tombe ? », « le score a "
                "bougé aujourd'hui ? ». "
                "⚠ À ne pas confondre avec l'outil `memoire`, qui porte des faits durables "
                "SANS date (emplacements, habitudes, préférences). Ici, tout est daté."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "periode": {
                        "type": "string",
                        "enum": list(FENETRES),
                        "description": "Sur quelle période regarder. Défaut : aujourd'hui.",
                    },
                    "sujet": {
                        "type": "string",
                        "description": (
                            "Filtrer sur une personne, une pièce, un appareil ou un module "
                            "(« Aurélie », « Ballon », « nsm »). Omettre pour tout voir."
                        ),
                    },
                    "seulement_important": {
                        "type": "boolean",
                        "description": (
                            "Ne garder que ce qui méritait une alerte (pannes, piles). "
                            "Par défaut, tout est rendu, y compris les températures."
                        ),
                    },
                },
                "required": [],
            },
            category="memoire",
            latency_estimate=0.1,
            timeout_seconds=5.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        periode = params.get("periode")
        fenetre = FENETRES.get(periode if isinstance(periode, str) else "", 86400)
        libelle = periode if periode in FENETRES else "aujourd'hui"
        sujet = params.get("sujet") if isinstance(params.get("sujet"), str) else None
        # ⚠ `INTERRUPT` seulement quand on demande l'important : `NOTABLE` inclurait les
        #   allées et venues, qui noieraient une vraie panne dans une question du type
        #   « qu'est-ce qui a cloché ? ».
        niveau = Niveau.INTERRUPT if params.get("seulement_important") else None

        # ⚠ ON VÉRIFIE LA BASE AVANT DE CONCLURE. `relire()` ne lève jamais — c'est
        #   voulu — mais elle rend `[]` aussi bien quand rien ne s'est passé que quand le
        #   fichier est corrompu. Sans ce contrôle, Ava répondrait « rien de noté » sur
        #   une mémoire illisible, ce qui est la réponse la plus trompeuse possible :
        #   l'admin conclurait que sa maison a été calme.
        if not memoire.lisible():
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Je n'arrive pas à relire mon journal — la base est illisible. "
                    "Ce n'est pas la même chose que « rien ne s'est passé »."
                ),
                success=False,
                metadata={"erreur": "base_illisible"},
            )
        try:
            faits = memoire.relire(
                depuis_secondes=fenetre,
                niveau_min=niveau,
                sujet=sujet,
                limite=MAX_LIGNES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("journal: relecture impossible (%s)", exc)
            return ToolResult(
                tool_name=self.tool_id,
                content="Je n'arrive pas à relire mon journal pour l'instant.",
                success=False,
                metadata={"erreur": type(exc).__name__},
            )

        if not faits:
            # ⚠ DEUX PHRASES DIFFÉRENTES POUR DEUX SITUATIONS DIFFÉRENTES, et c'est ce qui
            #   évite la réponse la plus trompeuse de tout ce projet : « rien ne s'est
            #   passé » alors qu'on n'a simplement rien observé. Un filtre trop étroit doit
            #   s'entendre comme tel.
            precision = f" concernant « {sujet} »" if sujet else ""
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"Rien de noté sur la période « {libelle} »{precision}. "
                    "Soit il ne s'est rien passé, soit ce n'était pas dans ce que je surveille."
                ),
                success=True,
                metadata={"periode": libelle, "trouves": 0},
            )

        lignes = []
        for f in faits:
            marque = "⚠ " if f["niveau"] == Niveau.INTERRUPT.value else ""
            lignes.append(f"  · {_quand(f['horodatage'])} — {marque}{f['fait']}")

        # ⚠ On donne aussi le COMPTE sur 30 jours quand un sujet est demandé : c'est ce
        #   qui permet de dire « c'est la troisième fois ce mois-ci » plutôt que de
        #   constater à neuf. La complicité vient de là, pas d'un prompt plus drôle.
        recurrence = ""
        if sujet:
            total = memoire.compter(sujet, depuis_secondes=2592000)
            if total > len(faits):
                recurrence = (
                    f"\n(sur les 30 derniers jours : {total} occurrences au total)"
                )

        return ToolResult(
            tool_name=self.tool_id,
            content=f"Ce que j'ai vu ({libelle}) :\n" + "\n".join(lignes) + recurrence,
            success=True,
            metadata={"periode": libelle, "trouves": len(faits), "sujet": sujet},
        )
