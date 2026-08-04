"""Outil `memoire` — ce qu'Ava a appris, et qu'elle sait retrouver.

⚠ CE MODULE COMBLE UN CHAÎNON MANQUANT DE L'AMONT, et il faut le dire clairement :
  OpenJarvis EXTRAIT bien des faits durables de chaque conversation
  (`memory/service.py` → `~/.openjarvis/memory_facts.jsonl`, vérifié : le fait apparaît
  dans le fichier quelques secondes après l'échange) — mais **rien côté serveur ne les
  relit jamais**. Le seul consommateur de `facts_path` dans tout le dépôt est une
  commande CLI (`cli/memory_cmd.py`).

  Conséquence mesurée le 2026-08-04 : on dit à Ava « le disjoncteur est derrière la porte
  verte », le fait est correctement extrait et écrit, et à la question suivante elle
  répond « aucune idée ». La mémoire fonctionnait à moitié — celle qui ne se voit pas.
  C'est la même classe de défaut que le `WebhookManager.dispatch()` du control plane :
  la table existe, les routes existent, le chaînon d'appel n'existe pas.

⚠ POURQUOI UN OUTIL PLUTÔT QU'UNE INJECTION AUTOMATIQUE DANS LE PROMPT. Injecter les N
  faits « les plus proches » à chaque requête paraît plus simple, mais :
  · il faudrait une mesure de similarité — et sans elle, on injecte les N plus récents,
    qui n'ont aucun rapport avec la question posée ;
  · le prompt grossit à chaque échange, donc le coût aussi, y compris quand la question
    ne demande aucun souvenir ;
  · le modèle ne peut pas dire « je ne me souviens pas » : il voit toujours des faits, et
    un modèle qui voit du contexte a tendance à s'en servir même hors sujet.
  Un outil laisse Ava DÉCIDER quand chercher, et rend visible ce qu'elle a trouvé. C'est
  cohérent avec le reste de son outillage (`avalon_status`, `home_assistant`).

⚠ MÉMOIRE CENTRALE, PAS PERSONNELLE — arbitrage explicite de l'admin. Ces faits sont
  partagés entre tous les interlocuteurs : Ava apprend de tout le monde. L'HISTORIQUE,
  lui, reste cloisonné par personne (`ava_extensions/server/conversation.py`).
  Le prix, accepté en connaissance de cause : l'extracteur ne distingue pas un fait
  d'intérêt général d'un propos personnel. Le fichier est du JSONL lisible — une ligne
  qui n'a rien à y faire s'enlève à la main.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

CHEMIN_FAITS = Path(
    os.environ.get(
        "AVA_FACTS_PATH", str(Path.home() / ".openjarvis" / "memory_facts.jsonl")
    )
)

MAX_RENDUS = 8


def _sans_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _mots(s: str) -> set[str]:
    """Mots significatifs d'une chaîne, normalisés.

    ⚠ Les mots de moins de 4 lettres sont écartés : « le », « la », « est », « pour »
      apparaissent dans presque tous les faits et feraient tout correspondre à tout —
      une recherche qui rend toujours quelque chose ne rend aucune information.
    """
    propre = _sans_accents(s).lower()
    return {
        m.strip(".,;:!?'\"()")
        for m in propre.split()
        if len(m.strip(".,;:!?'\"()")) >= 4
    }


def charger_faits() -> list[str]:
    """Les faits enregistrés, du plus récent au plus ancien.

    ⚠ Ne lève JAMAIS : une mémoire illisible doit priver Ava de souvenirs, pas la faire
      planter au milieu d'une conversation.
    """
    try:
        lignes = CHEMIN_FAITS.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    faits: list[str] = []
    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            objet = json.loads(ligne)
        except Exception:  # noqa: BLE001
            # ⚠ Une ligne corrompue ne doit pas emporter le fichier entier : un JSONL
            #   écrit en continu peut se terminer par une ligne partielle.
            continue
        texte = objet.get("text") if isinstance(objet, dict) else None
        if isinstance(texte, str) and texte.strip():
            faits.append(texte.strip())
    return list(reversed(faits))


def chercher(question: str, faits: list[str] | None = None) -> list[str]:
    """Faits pertinents pour `question`, les plus proches d'abord.

    ⚠ Recherche par mots communs, volontairement simple. Un index vectoriel serait plus
      fin, mais il ajouterait un modèle d'embedding à charger et à tenir à jour pour un
      corpus qui plafonne à 1000 faits. À reconsidérer si le corpus grossit beaucoup.
    """
    corpus = charger_faits() if faits is None else faits
    if not corpus:
        return []
    cles = _mots(question)
    if not cles:
        # ⚠ Question sans mot significatif (« et alors ? ») : on rend les plus RÉCENTS
        #   plutôt que rien — c'est le comportement attendu d'un « de quoi on parlait ? ».
        return corpus[:MAX_RENDUS]
    notes = [(len(cles & _mots(f)), f) for f in corpus]
    retenus = [f for n, f in sorted(notes, key=lambda x: -x[0]) if n > 0]
    return retenus[:MAX_RENDUS]


@ToolRegistry.register("memoire")
class MemoireTool(BaseTool):
    """Cherche dans ce qu'Ava a appris au fil des conversations."""

    tool_id = "memoire"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memoire",
            description=(
                "Cherche dans la mémoire à long terme d'Ava — les faits qu'elle a retenus "
                "des conversations passées (emplacements, habitudes, préférences, "
                "particularités de la maison et de l'infrastructure). À utiliser dès "
                "qu'une question porte sur quelque chose qui a pu être dit auparavant, ou "
                "quand Adrien demande de se souvenir. Cette mémoire est COMMUNE à tous les "
                "interlocuteurs : Ava apprend de tout le monde."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sujet": {
                        "type": "string",
                        "description": (
                            "Ce qu'on cherche à retrouver, en quelques mots. Omettre pour "
                            "obtenir les souvenirs les plus récents."
                        ),
                    }
                },
                "required": [],
            },
            category="memoire",
            latency_estimate=0.1,
            timeout_seconds=5.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        sujet = params.get("sujet")
        faits = charger_faits()
        if not faits:
            return ToolResult(
                tool_name=self.tool_id,
                # ⚠ Distinguer « rien en mémoire » de « rien sur CE sujet » : la première
                #   phrase invite à vérifier que l'extraction tourne, la seconde non.
                content="Aucun souvenir enregistré pour l'instant.",
                success=True,
                metadata={"total": 0},
            )
        trouves = chercher(sujet if isinstance(sujet, str) else "", faits)
        if not trouves:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Rien en mémoire sur ce sujet ({len(faits)} souvenirs au total).",
                success=True,
                metadata={"total": len(faits), "trouves": 0},
            )
        lignes = "\n".join(f"  · {f}" for f in trouves)
        return ToolResult(
            tool_name=self.tool_id,
            content=f"Ce dont je me souviens :\n{lignes}",
            success=True,
            metadata={"total": len(faits), "trouves": len(trouves)},
        )
