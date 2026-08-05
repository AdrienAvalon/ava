"""`file_read` qui dit OU chercher quand il ne trouve pas.

⚠ MESURE DU 2026-08-05, sur les traces reelles : **12 appels a `file_read`, 11 echecs**.
  Neuf portaient sur le MEME fichier — `docs/ava-perimetre.md` — essaye en sept
  orthographes successives (`docs/x.md`, `./docs/x.md`, `/docs/x.md`, `x.md`,
  `/home/avalon/ava/docs/x.md`, `/home/avalon/Documents/gitlab/ava/docs/x.md`, `docs`).

  Aucune ne pouvait aboutir : **le depot d'infrastructure n'est pas sur cette machine**.
  Le message « File not found » est exact et sans issue — il invite a reessayer un
  chemin de plus, ce qu'elle a fait sept fois. Un outil qui echoue en boucle sur une
  intention legitime coute des tours de conversation ET apprend au modele a se mefier
  d'un outil qui, lui, fonctionne.

⚠ ON NE RETIRE PAS `file_read` — j'ai essaye, c'etait le mauvais geste (cf. l'en-tete de
  `roles/ava_app/defaults/main.yml`) : c'est le seul outil du systeme de fichiers qui
  consulte la garde des fichiers sensibles. On rend son echec UTILE, ce qui coute une
  phrase et resout le probleme mesure.

⚠ PATCH PLUTOT QUE MODIFICATION AMONT : le message est propre a Avalon (il nomme
  `lire_doc`, qui n'existe que chez nous). Le mettre dans `src/openjarvis/tools/` ferait
  diverger un fichier amont pour une raison locale.
"""

from __future__ import annotations

import logging
from typing import Any

from openjarvis.tools.file_read import FileReadTool

logger = logging.getLogger(__name__)

#: Ce que le modele cherchait dans les 11 echecs mesures : de la documentation du depot.
_INDICE = (
    " ⚠ Le depot d'infrastructure Avalon n'est PAS sur cette machine — aucun chemin ne "
    "marchera. Pour la documentation (docs/*.md), utilise l'outil `lire_doc` : sans "
    "argument il liste les documents, avec `chemin` il rend le contenu."
)

_execute_amont = FileReadTool.execute


def _execute_oriente(self: FileReadTool, **params: Any) -> Any:
    resultat = _execute_amont(self, **params)
    if getattr(resultat, "success", True):
        return resultat
    chemin = str(params.get("path") or "")
    # ⚠ On n'oriente QUE sur le cas mesure : une demande qui ressemble a de la
    #   documentation du depot. Ajouter cette phrase a tout echec la rendrait du bruit,
    #   et le modele cesserait de la lire — exactement le sort d'une alerte qui crie trop.
    if chemin.endswith(".md") or "docs/" in chemin or chemin.rstrip("/").endswith("docs"):
        resultat.content = f"{resultat.content}{_INDICE}"
    return resultat


FileReadTool.execute = _execute_oriente  # type: ignore[method-assign]
logger.info("ava: file_read oriente vers lire_doc sur echec documentaire")
