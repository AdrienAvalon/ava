"""Patches d'observabilité des traces — rendre les ÉCHECS d'Ava visibles.

⚠ POURQUOI CE FICHIER EXISTE, ET POURQUOI IL PASSE AVANT TOUT LE RESTE.
  On s'apprête à donner à Ava une perception continue de l'infrastructure, puis une
  forme d'autonomie. Le précédent à ne pas rejouer est dans le control plane :
  `modules/ai_analysis` a enchaîné **151 tentatives et 0 analyse délivrée sur 29 jours**
  en annonçant `_health: "ok"` pendant toute la période, parce que son gestionnaire
  d'erreur journalisait le code HTTP sans le corps et que son état de santé ne se
  dégradait jamais.

  Une IA qu'on rebranche sur une infrastructure sans que ses propres échecs soient
  visibles reproduit ce défaut — **en plus grand**, puisqu'elle agira.

  Deux bugs mesurés le 2026-08-04 rendaient les échecs d'Ava introuvables :

  1. **`TraceStore` n'étend pas le `~`.** `store.py:84` fait `str(db_path)` là où
     `TelemetryStore` fait bien un `expanduser()`. Le fichier réel est donc
     `/home/avalon/ava/~/.openjarvis/traces.db` — un répertoire littéralement nommé
     `~` dans l'arbre git — tandis que le chemin documenté contient **0 trace**.
     Mesuré : 43 traces + 106 étapes du mauvais côté, 0 du bon.
     ⚠ Et ce répertoire est **ignoré par git** via la règle `*~` écrite pour les
     sauvegardes d'éditeur : un `git clean -fdx` effacerait tout le corpus, sans que
     rien ne le signale. C'est le seul substrat d'apprentissage dont on dispose.

  2. **`TraceCollector.run` n'enregistre rien quand l'agent lève.** L'appel à
     `store.save()` a lieu APRÈS l'agent : une exception court-circuite l'écriture.
     Conséquence mesurée : les 8 réponses en HTTP 500 du 2026-08-03 (`temperature`
     dépréciée, puis crédit épuisé) n'apparaissent **nulle part** — ni dans
     `traces.db`, ni dans `routing-probe.jsonl`, ni dans Loki. Or ce sont précisément
     les événements qu'une boucle d'apprentissage doit voir : un échec silencieux est
     un échec qu'on ne corrigera jamais.

⚠ POURQUOI DES PATCHES ET NON UNE MODIFICATION DE L'AMONT. `src/openjarvis/` est
  synchronisé depuis OpenJarvis — 353 commits absorbés le 2026-08-03. Chaque fichier
  amont modifié devient un conflit à résoudre à chaque montée. `ava_extensions/` a
  encaissé ces 353 commits sans une seule collision, précisément parce qu'il ne touche
  à rien. Ces correctifs devraient remonter en amont ; en attendant, ils vivent ici.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MARQUEUR = "_ava_observabilite"


def _corriger_chemin_traces() -> bool:
    """Fait développer le `~` par `TraceStore`, comme le fait déjà `TelemetryStore`."""
    try:
        from openjarvis.traces import store as _store
    except Exception as exc:  # noqa: BLE001
        log.debug("traces: module introuvable (%s)", exc)
        return False

    cls = getattr(_store, "TraceStore", None)
    if cls is None or getattr(cls, _MARQUEUR, False):
        return True

    origine = cls.__init__

    def __init__(self: Any, db_path: str | Path, *args: Any, **kwargs: Any) -> None:  # noqa: N807
        # ⚠ `expanduser()` AVANT l'appel d'origine : celui-ci crée le fichier et le
        #   répertoire parent. Corriger après coup laisserait un répertoire `~` vide
        #   derrière nous à chaque démarrage.
        if isinstance(db_path, (str, Path)) and str(db_path) != ":memory:":
            db_path = Path(str(db_path)).expanduser()
        origine(self, db_path, *args, **kwargs)

    cls.__init__ = __init__  # type: ignore[method-assign]
    setattr(cls, _MARQUEUR, True)
    return True


def _tracer_les_echecs() -> bool:
    """Enregistre les echecs ordinaires pour l'observabilite operationnelle.

    L'apprentissage automatique reste desactive. Ce journal ne promeut aucune
    connaissance et ne contourne jamais une frontiere de publication : les appels
    filtres ou a persistance differee ne passent pas par ce chemin de secours.

    ⚠ `TraceCollector.run` construit sa `Trace` APRES l'appel de l'agent (collector.py:84) :
      il n'y a donc aucune trace en cours a completer quand l'exception survient. Le
      wrapper doit la CONSTRUIRE lui-meme, a partir de la question et des etapes deja
      collectees — que l'original conserve, son `_unsubscribe` etant dans un `finally`.
    """
    try:
        from openjarvis.traces import collector as _collector
    except Exception as exc:  # noqa: BLE001
        log.debug("traces: collecteur introuvable (%s)", exc)
        return False

    cls = getattr(_collector, "TraceCollector", None)
    if cls is None or getattr(cls, _MARQUEUR, False):
        return True

    origine = getattr(cls, "run", None)
    if origine is None:
        log.debug("traces: TraceCollector.run absent — patch sans objet")
        return False

    import functools

    @functools.wraps(origine)
    def run(self: Any, entree: str, *args: Any, **kwargs: Any) -> Any:
        debut = time.time()
        try:
            return origine(self, entree, *args, **kwargs)
        except Exception as exc:
            # A filtered or deferred run belongs to the request's publication
            # boundary. Its failure (including a late cancellation) must not
            # acquire a second persistence or logging path through this wrapper.
            # Ordinary, unfiltered failures remain observable below.
            if kwargs.get("content_filter") is None and not getattr(
                self, "_defer_persistence", False
            ):
                _consigner_echec(self, entree, debut, exc)
            raise

    cls.run = run  # type: ignore[method-assign]
    setattr(cls, _MARQUEUR, True)
    return True


def _consigner_echec(
    collecteur: Any, question: str, debut: float, exc: BaseException
) -> None:
    """Ecrit une trace d'echec, sans jamais masquer l'exception d'origine.

    ⚠ CE CHEMIN NE DOIT JAMAIS LEVER. Il s'execute dans un gestionnaire d'exception :
      une erreur ici remplacerait la cause reelle par une erreur d'instrumentation, et
      l'on chercherait le mauvais defaut. C'est le pire resultat possible pour du code
      dont l'unique raison d'etre est de rendre les pannes lisibles.
    """
    try:
        store = getattr(collecteur, "_store", None)
        if store is None or not hasattr(store, "save"):
            # On journalise quand meme : depuis le 2026-08-04 le journal part vers Loki.
            log.error(
                "ava: echange en echec, trace non enregistrable (%s: %s)",
                type(exc).__name__,
                exc,
            )
            return

        from openjarvis.core.types import Trace

        trace = Trace(
            query=question,
            agent=getattr(getattr(collecteur, "_agent", None), "agent_id", "unknown"),
            model=getattr(collecteur, "_current_model", "") or "",
            engine=getattr(collecteur, "_current_engine", "") or "",
            steps=list(getattr(collecteur, "_current_steps", []) or []),
            result="",
            # ⚠ `outcome` est le champ que la boucle d'apprentissage interrogera : il
            #   vaut `None` sur les 43 traces existantes, donc « echec » y est
            #   aujourd'hui inexprimable.
            outcome="error",
            started_at=debut,
            ended_at=time.time(),
            # ⚠ Le TYPE d'exception, pas seulement le message. C'est lui qui distingue
            #   les deux causes des 8 HTTP 500 — un `BadRequestError` de parametre
            #   deprecie et un epuisement de credit rendent le meme code HTTP.
            metadata={
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )
        store.save(trace)
        setattr(collecteur, "_last_trace", trace)
        log.warning(
            "ava: echange en echec trace (%s: %s)", type(exc).__name__, str(exc)[:200]
        )
    except Exception as interne:  # noqa: BLE001
        log.debug("ava: consignation de l'echec impossible (%s)", interne)


def appliquer() -> None:
    """Applique les deux correctifs. Idempotent, ne lève jamais."""
    ok_chemin = _corriger_chemin_traces()
    ok_echecs = _tracer_les_echecs()
    log.info(
        "ava: observabilite des traces — chemin=%s echecs=%s",
        "ok" if ok_chemin else "non applique",
        "ok" if ok_echecs else "non applique",
    )


appliquer()
