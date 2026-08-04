"""Ce qu'Ava fait réellement — la boucle qui transforme l'usage en feuille de route.

⚠ CE MODULE EST DÉLIBÉRÉMENT PLUS MODESTE QUE PRÉVU, ET IL FAUT DIRE POURQUOI.
  L'intention était un détecteur d'échecs : repérer les questions auxquelles Ava n'a pas
  su répondre, pour en déduire ce qu'il lui manque. La mesure a invalidé l'approche.

  Corpus au 2026-08-04 (51 traces, 133 étapes) :
    · **zéro** formule d'échec — ni « je ne sais pas », ni « je n'ai pas accès »,
      ni « aucune idée ». Le corpus n'en contient AUCUNE ;
    · 43 « questions enchaînées à moins de 600 s », signal censé détecter une
      reformulation — mais ce sont en écrasante majorité des tests lancés en rafale.

  Écrire un détecteur de formules sur un corpus qui n'en contient pas, c'est **inventer
  les motifs**. On obtiendrait un outil qui ne trouve jamais rien et qu'on croirait
  fonctionnel — soit exactement le défaut que ce projet documente partout ailleurs. Ce
  fichier ne mesure donc que ce qui est objectivement mesurable, et le dira quand il n'a
  pas de quoi conclure.

⚠ CE QU'IL MESURE, ET POURQUOI CHAQUE CHIFFRE DÉSIGNE UNE ACTION :
  · **outils réellement appelés** — un outil activé mais jamais utilisé est soit inutile,
    soit mal décrit. Les deux se corrigent, mais pas de la même façon.
  · **questions sans aucun outil** — candidates à un outil manquant. C'est le signal le
    plus riche, mais il faut le lire avec la restriction ci-dessous.
  · **échecs techniques** (`outcome='error'`) — traçables depuis le 2026-08-04 seulement ;
    les 8 HTTP 500 du 03/08 n'existent nulle part.

⚠ RESTRICTION DE LECTURE, LOAD-BEARING. Le chemin de streaming produit des traces
  **mono-étape** : ni `generate`, ni `tool_call`. Les compter comme « questions sans
  outil » gonflerait le signal d'un tiers avec des cas où l'agent n'a simplement pas été
  sollicité. On ne raisonne donc que sur les traces portant au moins une étape
  `generate` — 36 sur 51 à la mesure.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHEMIN_TRACES = Path(
    os.environ.get("AVA_TRACES_DB", str(Path.home() / ".openjarvis" / "traces.db"))
).expanduser()

# ⚠ Un échange n'est retenu comme « sans outil » que s'il a vraiment fait raisonner
#   l'agent. Cf. la restriction de lecture ci-dessus.
_ETAPE_AGENT = "generate"


def _lire(
    depuis_secondes: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Les traces récentes et leurs étapes. Rend ([], {}) si la base est illisible."""
    borne = time.time() - max(0.0, depuis_secondes)
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{CHEMIN_TRACES}?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            traces = [
                dict(r)
                for r in conn.execute(
                    "SELECT trace_id, query, result, outcome, metadata, started_at, "
                    "total_tokens, model FROM traces WHERE started_at >= ?",
                    (borne,),
                )
            ]
            etapes: dict[str, dict[str, int]] = {}
            for r in conn.execute(
                "SELECT trace_id, step_type, COUNT(*) n FROM trace_steps GROUP BY trace_id, step_type"
            ):
                etapes.setdefault(r[0], {})[r[1]] = r[2]
            outils: Counter[str] = Counter()
            for r in conn.execute(
                "SELECT input FROM trace_steps WHERE step_type = 'tool_call'"
            ):
                try:
                    charge = json.loads(r[0]) if r[0] else {}
                    nom = charge.get("tool") or charge.get("name")
                    if nom:
                        outils[str(nom)] += 1
                except Exception:  # noqa: BLE001
                    continue
        for t in traces:
            t["_outils"] = etapes.get(t["trace_id"], {}).get("tool_call", 0)
            t["_agent"] = etapes.get(t["trace_id"], {}).get(_ETAPE_AGENT, 0)
        return traces, {"outils": dict(outils)}  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        logger.warning("apprentissage: traces illisibles (%s)", exc)
        return [], {}


def analyser(depuis_secondes: float = 604800) -> dict[str, Any]:
    """Ce qu'Ava a fait sur la période, et ce que cela désigne comme travail.

    ⚠ Rend `suffisant: False` quand le corpus est trop mince pour conclure. C'est le
      point central de ce module : un rapport qui affirmerait « aucun outil ne manque »
      sur douze échanges serait faux, et on le croirait.
    """
    traces, extra = _lire(depuis_secondes)
    if not traces:
        return {
            "suffisant": False,
            "motif": "aucune trace lisible sur la période",
            "traces": 0,
        }

    analysables = [t for t in traces if t.get("_agent")]
    sans_outil = [t for t in analysables if not t.get("_outils")]
    erreurs = [t for t in traces if t.get("outcome") == "error"]

    # ⚠ SEUIL DE SUFFISANCE. En dessous, on ne conclut pas — on dit qu'on ne sait pas.
    #   30 échanges analysables est arbitraire mais explicite : mieux vaut un seuil
    #   discutable et visible qu'une conclusion tirée de trois observations.
    suffisant = len(analysables) >= 30

    types_erreur = Counter()
    for t in erreurs:
        try:
            meta = json.loads(t.get("metadata") or "{}")
            types_erreur[str(meta.get("error_type", "?"))] += 1
        except Exception:  # noqa: BLE001
            types_erreur["?"] += 1

    return {
        "suffisant": suffisant,
        "traces": len(traces),
        "analysables": len(analysables),
        "mono_etape": len(traces) - len(analysables),
        "avec_outil": len(analysables) - len(sans_outil),
        "sans_outil": len(sans_outil),
        "outils_utilises": extra.get("outils", {}),
        "erreurs": len(erreurs),
        "types_erreur": dict(types_erreur),
        "questions_sans_outil": [
            (t.get("query") or "")[:120] for t in sans_outil[-15:] if t.get("query")
        ],
    }


def formuler(rapport: dict[str, Any], outils_actifs: list[str] | None = None) -> str:
    """Le rapport en français, avec ce qu'il désigne comme action."""
    if not rapport.get("traces"):
        return "Aucune trace lisible — la boucle d'apprentissage n'a rien à analyser."

    lignes = [
        f"{rapport['traces']} échanges, dont {rapport['analysables']} analysables "
        f"({rapport['mono_etape']} mono-étape, chemin streaming — non exploitables).",
        f"Outils appelés dans {rapport['avec_outil']} échanges, aucun outil dans "
        f"{rapport['sans_outil']}.",
    ]

    utilises = rapport.get("outils_utilises") or {}
    if utilises:
        detail = ", ".join(
            f"{k} ×{v}" for k, v in sorted(utilises.items(), key=lambda x: -x[1])
        )
        lignes.append(f"Répartition : {detail}.")

    # ⚠ UN OUTIL ACTIF ET JAMAIS APPELÉ EST UN SIGNAL, pas un détail : soit il ne sert à
    #   rien, soit sa description ne dit pas au modèle quand l'employer. Les deux se
    #   corrigent, mais pas de la même façon — et l'ignorer laisse un outil mort en place.
    if outils_actifs:
        jamais = [o for o in outils_actifs if o not in utilises]
        if jamais:
            lignes.append(
                "Jamais appelés sur la période : "
                + ", ".join(jamais)
                + " — soit inutiles, soit mal décrits."
            )

    if rapport.get("erreurs"):
        lignes.append(
            f"⚠ {rapport['erreurs']} échange(s) en erreur : "
            + ", ".join(
                f"{k}×{v}" for k, v in (rapport.get("types_erreur") or {}).items()
            )
        )

    if not rapport.get("suffisant"):
        # ⚠ LA PHRASE QUI ÉVITE LA CONCLUSION FAUSSE. Sans elle, un rapport tiré de
        #   douze échanges se lirait comme un verdict.
        lignes.append(
            "⚠ Corpus trop mince pour conclure quoi que ce soit sur ce qui manque à Ava "
            "(moins de 30 échanges analysables). Les chiffres ci-dessus décrivent "
            "l'usage, ils ne désignent pas encore un besoin."
        )
    elif rapport.get("questions_sans_outil"):
        lignes.append("Questions sans aucun outil (candidates à un outil manquant) :")
        lignes += [f"  · {q}" for q in rapport["questions_sans_outil"][-8:]]

    return "\n".join(lignes)
