"""Sonde de routage — mesurer AVANT de router.

POURQUOI CETTE SONDE EXISTE (2026-08-04). L'objectif est un routage Haiku / Sonnet /
Opus selon la tâche. Le construire tout de suite reviendrait à régler des seuils sur des
suppositions : Ava n'a pas servi depuis trois mois, personne ne sait quelle proportion de
ses échanges appelle un outil, ni combien de jetons coûte une conversation ordinaire.
Un routeur mal réglé ne produit pas une panne — il produit **de mauvaises réponses**, ce
qui est bien plus difficile à diagnostiquer qu'un service qui tombe.

Cette sonde ne change RIEN au comportement. Elle observe, elle écrit une ligne JSON par
échange, et dans deux semaines les seuils se choisiront sur des données.

⚠ ELLE NE PATCHE PAS L'AMONT, ET C'EST SA PROPRIÉTÉ LA PLUS IMPORTANTE. Elle s'abonne à
  l'`EventBus` d'OpenJarvis (`INFERENCE_END`, `TOOL_CALL_START`, `CHAT_EXCHANGE_COMPLETED`)
  — un point d'extension prévu pour ça. Le dépôt compte déjà 4 fichiers amont patchés, et
  chacun est un conflit garanti à chaque synchronisation ; en ajouter un cinquième pour de
  la mesure temporaire serait un mauvais échange.

⚠ CE QU'ELLE MESURE, ET POURQUOI CES CHAMPS-LÀ :
  · `outil_appele` — **le signal décisif, et il n'est PAS la complexité du texte.**
    « Il fait quoi dehors ? » a un score de complexité très bas (court, une question, pas
    de code) et déclenche pourtant un appel d'outil vers le control plane. Or c'est
    exactement là que les petits modèles échouent : ils oublient l'outil ou inventent la
    réponse. Router sur la longueur enverrait donc les questions sur la maison au modèle
    le moins capable d'y répondre. On mesure les deux pour pouvoir le démontrer.
  · `complexite` — le score de l'amont (`score_complexity`), réutilisé tel quel. Inutile
    d'en écrire un autre : celui-ci pondère longueur, questions multiples, sous-tâches et
    domaine (code, maths, raisonnement).
  · `jetons_entree` / `jetons_sortie` — sans eux, « ça coûte cher » reste une impression.
  · `modele` — pour comparer ce qui a réellement servi, pas ce qu'on croit configuré.

⚠ AUCUN CONTENU DE CONVERSATION N'EST ÉCRIT. Ni la question, ni la réponse : ce journal
  vivrait sur une VM exposée et Ava parle de la maison, de la présence des personnes, de
  l'infrastructure. On garde la LONGUEUR de la question, pas la question.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ⚠ Sous `/var/log` on n'aurait pas le droit d'écrire (le daemon tourne en `avalon`), et
#   le fichier disparaîtrait au prochain nettoyage sous `/tmp`. Le répertoire de données
#   d'OpenJarvis est sauvegardé avec la VM.
CHEMIN = Path(
    os.environ.get(
        "AVA_ROUTING_LOG", str(Path.home() / ".openjarvis" / "routing-probe.jsonl")
    )
)

# ⚠ Un verrou : l'`EventBus` appelle ses abonnés « synchronously […] within the publishing
#   thread » (son propre docstring). Deux échanges concurrents écriraient donc en même
#   temps, et des lignes JSON entrelacées sont illisibles — donc inutilisables, ce qui
#   ruinerait la seule raison d'être de cette sonde.
_verrou = threading.Lock()

# État de l'échange en cours. Réinitialisé à chaque `CHAT_EXCHANGE_COMPLETED`.
# ⚠ UN ÉTAT PAR ÉCHANGE, ET NON UN DICTIONNAIRE DE MODULE (corrigé le 2026-08-04).
#   `_courant` était un unique dict partagé, muté par `_sur_debut_outil` et
#   `_sur_fin_inference`, puis vidé par `.clear()`. Rien n'y distinguait un échange
#   d'un autre. Or le daemon sert plusieurs clients, et la sonde s'abonne à TOUT bus
#   créé. Deux échanges qui se chevauchent — A appelle `home_assistant` et consomme
#   3 200 jetons, B n'appelle rien — donnaient : B termine le premier et **emporte les
#   outils et les jetons de A**, puis `clear()` ; A sortait ensuite avec
#   `outil_appele: false` et `jetons_entree: null`.
#   Ce n'est pas un détail cosmétique : `outil_appele` est présenté en tête de ce
#   fichier comme LE signal sur lequel se régleront les seuils Haiku/Sonnet/Opus. Une
#   sonde qui attribue les mesures au mauvais échange ne produit pas un bruit — elle
#   produit une CORRÉLATION INVERSÉE, sur laquelle on réglerait le routage à l'envers.
#
# ⚠ LA CORRÉLATION NE PEUT PAS ÊTRE FAITE PAR LE CONTEXTE D'EXÉCUTION — mesuré en
#   production le 2026-08-04, après avoir cru le contraire. Sur un échange réel avec
#   appel d'outil, les clés relevées sont :
#       inference  cle=125651746027200
#       outil      cle=125651746027200
#       inference  cle=125651746027200
#       FIN        cle=125650636716160   ← AUTRE THREAD
#   `CHAT_EXCHANGE_COMPLETED` est publié depuis `_record_completed_exchange`, hors du
#   contexte qui a exécuté les outils. Un `contextvars.ContextVar` a été essayé et ne
#   traverse pas davantage (`ctxvar=None` côté FIN) : le contexte n'est pas hérité, il
#   est indépendant.
#   ⚠ C'est POUR CETTE RAISON que l'état global d'origine « marchait » : il marchait par
#   accident, et au prix du mélange entre échanges concurrents. Le corriger naïvement
#   par une clé de contexte ne fait donc pas que déplacer le problème — cela **perd
#   toutes les mesures d'outils**, ce qu'une première version de ce correctif a fait
#   pendant quelques minutes en production.
#
# ⚠ D'OÙ CE COMPROMIS, ET SON HONNÊTETÉ EST LE POINT ESSENTIEL. La collecte reste
#   indexée par contexte (outils et jetons, eux, SONT bien émis ensemble). À la fin, on
#   consomme l'état actif — et l'on ENREGISTRE le niveau de confiance de ce
#   rapprochement dans la ligne elle-même :
#     · `certaine` : un seul échange était en cours, aucune ambiguïté possible ;
#     · `ambigue`  : plusieurs échanges se chevauchaient, on prend le plus ancien (ils
#                    se terminent approximativement dans l'ordre) — la ligne reste
#                    exploitable mais se filtre à l'analyse ;
#     · `aucune`   : aucun état à rapprocher (échange sans inférence tracée).
#   L'état global d'avant produisait des lignes `ambigue` SANS LE DIRE. Une mesure qui
#   annonce son incertitude vaut infiniment mieux qu'une mesure fausse d'apparence
#   propre — c'est tout l'enjeu, puisque ces lignes serviront à régler les seuils
#   Haiku/Sonnet/Opus.
_etats: dict[int, dict[str, Any]] = {}
_verrou_etats = threading.Lock()

# ⚠ BORNE ANTI-FUITE. Un échange interrompu (exception pendant l'inférence, client qui
#   raccroche) ne publie jamais son événement de fin : son entrée resterait
#   indéfiniment. Au-delà de cette borne on purge les plus anciennes — une sonde ne
#   doit jamais faire grossir la mémoire du processus qu'elle observe.
_ETATS_MAX = 64


def _cle_echange() -> int:
    """Identifiant du contexte d'exécution courant (tâche asyncio, sinon thread)."""
    try:
        import asyncio

        tache = asyncio.current_task()
        if tache is not None:
            return id(tache)
    except Exception:  # noqa: BLE001 — hors boucle d'événements
        pass
    return threading.get_ident()


def _etat() -> dict[str, Any]:
    """L'état de l'échange en cours, créé au besoin."""
    cle = _cle_echange()
    with _verrou_etats:
        etat = _etats.get(cle)
        if etat is None:
            if len(_etats) >= _ETATS_MAX:
                # purge des plus anciennes (dict ordonné par insertion)
                for vieille in list(_etats)[: len(_etats) - _ETATS_MAX + 1]:
                    _etats.pop(vieille, None)
            etat = {"_ne_le": time.time(), "_a_coexiste": bool(_etats)}
            _etats[cle] = etat
            # ⚠ LA CONTAMINATION SE PROPAGE DANS LES DEUX SENS, et l'oublier produit une
            #   fausse assurance — trouvé par le test, pas par raisonnement. Si un second
            #   échange démarre, l'ANCIEN devient lui aussi douteux : à sa fin, il ne
            #   restera peut-être qu'un seul état, et le rapprochement se déclarerait
            #   « certain » alors que les deux se sont chevauchés. Compter les états
            #   présents AU MOMENT DE LA FIN ne suffit donc pas ; il faut se souvenir
            #   qu'un chevauchement a eu lieu.
            if len(_etats) > 1:
                for autre in _etats.values():
                    autre["_a_coexiste"] = True
        return etat


def _score_complexite(texte: str) -> float | None:
    """Score de l'amont, ou None s'il n'est pas disponible.

    ⚠ `None` et non `0.0` : un zéro se lirait comme « question triviale » et fausserait
      toute la statistique dans le sens qui nous intéresse. Une mesure absente doit rester
      absente — c'est le défaut récurrent de ce projet.
    """
    try:
        from openjarvis.learning.routing.complexity import score_complexity

        return float(score_complexity(texte).score)
    except Exception as exc:  # noqa: BLE001 — une sonde ne casse jamais son hôte
        logger.debug("score_complexity indisponible: %s", exc)
        return None


def _ecrire(ligne: dict[str, Any]) -> None:
    try:
        CHEMIN.parent.mkdir(parents=True, exist_ok=True)
        with _verrou, CHEMIN.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        # ⚠ Une sonde qui fait tomber le service qu'elle observe est pire que pas de sonde.
        logger.debug("routing-probe: écriture impossible (%s)", exc)


def _sur_debut_outil(evenement: Any) -> None:
    d = getattr(evenement, "data", {}) or {}
    _etat().setdefault("outils", []).append(d.get("tool") or d.get("name") or "?")


def _sur_fin_inference(evenement: Any) -> None:
    brut = getattr(evenement, "data", {}) or {}
    # ⚠ LES JETONS SONT DANS UN SOUS-OBJET `usage`, PAS À PLAT — la sonde écrivait
    #   `jetons_entree: null` à chaque échange alors que l'événement les portait. Elle
    #   cherchait `prompt_tokens` à la racine ; l'amont publie
    #   `{"model": …, "usage": {"prompt_tokens": …}}` (`agents/_stubs.py`).
    #   Une sonde qui lit la mauvaise clé rend « rien » sans erreur — le défaut récurrent
    #   de ce projet, sous une forme de plus.
    usage = brut.get("usage") if isinstance(brut.get("usage"), dict) else {}
    d = {**usage, **{k: v for k, v in brut.items() if k != "usage"}}
    for cle, champs in (
        ("jetons_entree", ("prompt_tokens", "input_tokens", "tokens_in")),
        ("jetons_sortie", ("completion_tokens", "output_tokens", "tokens_out")),
        ("modele", ("model", "model_name")),
    ):
        for champ in champs:
            if d.get(champ) is not None:
                # ⚠ On ACCUMULE les jetons : un échange avec appel d'outil produit
                #   PLUSIEURS inférences (une pour décider de l'outil, une pour rédiger la
                #   réponse). Garder la dernière sous-estimerait le coût réel — soit
                #   exactement la grandeur qu'on cherche à mesurer.
                etat = _etat()
                if cle.startswith("jetons") and isinstance(d[champ], int):
                    etat[cle] = etat.get(cle, 0) + d[champ]
                else:
                    etat[cle] = d[champ]
                break


def _consommer_etat() -> tuple[dict[str, Any], str]:
    """L'état de l'échange qui se termine, avec le niveau de confiance du rapprochement.

    ⚠ On ne peut PAS se fier au contexte courant : cet événement est publié depuis un
      autre thread que les outils (mesuré, cf. l'en-tête). On consomme donc l'état actif
      le plus ancien, en disant à quel point ce rapprochement est sûr.
    """
    with _verrou_etats:
        if not _etats:
            return {}, "aucune"
        cle = next(iter(_etats))  # dict ordonné par insertion → le plus ancien
        etat = _etats.pop(cle)
        # `certaine` exige les DEUX conditions : aucun autre état en cours maintenant,
        # ET aucun chevauchement depuis la naissance de celui-ci.
        douteux = bool(_etats) or etat.get("_a_coexiste", False)
        return etat, "ambigue" if douteux else "certaine"


def _sur_echange_termine(evenement: Any) -> None:
    d = getattr(evenement, "data", {}) or {}
    # ⚠ LE CHAMP S'APPELLE `user_text` — `publish_completed_exchange` (memory/service.py)
    #   publie `{"user_text": …, "assistant_text": …, "source": …}`. La sonde cherchait
    #   `user_message`, donc écrivait `longueur_question: 0` sur CHAQUE échange : la
    #   corrélation entre longueur de question et appel d'outil, seule raison d'être de
    #   cette mesure, était nulle par construction.
    #   Les autres noms sont conservés en repli : une montée amont peut renommer le champ,
    #   et une sonde muette ne se signale pas.
    question = str(
        d.get("user_text")
        or d.get("user_message")
        or d.get("query")
        or d.get("prompt")
        or ""
    )
    etat, confiance = _consommer_etat()
    outils = etat.get("outils") or []

    _ecrire(
        {
            "horodatage": round(time.time(), 3),
            # ⚠ La LONGUEUR, jamais le texte — cf. l'en-tête de ce fichier.
            "longueur_question": len(question),
            "complexite": _score_complexite(question) if question else None,
            "outil_appele": bool(outils),
            "outils": outils,
            "modele": etat.get("modele"),
            "jetons_entree": etat.get("jetons_entree"),
            "jetons_sortie": etat.get("jetons_sortie"),
            # ⚠ Sans ce champ, une ligne issue d'un rapprochement douteux serait
            #   indiscernable d'une mesure exacte — c'est précisément ce que faisait
            #   l'état global d'avant.
            "correlation": confiance,
        }
    )


def brancher(bus: Any) -> bool:
    """Abonne la sonde au bus. Rend True si elle est active.

    ⚠ Ne lève JAMAIS : si les noms d'événements changent lors d'une synchronisation amont,
      Ava doit continuer de fonctionner sans sa sonde — pas s'arrêter parce qu'un outil de
      mesure n'a pas trouvé son point d'accroche.
    """
    try:
        from openjarvis.core.events import EventType

        bus.subscribe(EventType.TOOL_CALL_START, _sur_debut_outil)
        bus.subscribe(EventType.INFERENCE_END, _sur_fin_inference)
        bus.subscribe(EventType.CHAT_EXCHANGE_COMPLETED, _sur_echange_termine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("routing-probe non branchée: %s", exc)
        return False
    logger.info("routing-probe active → %s", CHEMIN)
    return True


def brancher_bus_serveur() -> bool:
    """Branche la sonde sur le bus RÉELLEMENT utilisé par le serveur.

    ⚠ LA SONDE ÉCOUTAIT UN BUS QUE PERSONNE N'UTILISE — mesuré le 2026-08-04 : elle
      était « active » depuis des heures et son journal était resté VIDE.
      `boot.py` la branchait sur `get_event_bus()`, le singleton global. Or `cli/serve.py`
      construit **son propre** `EventBus(record_history=False)` et ne le publie nulle part.
      Deux bus distincts : les événements partaient dans l'un, la sonde écoutait l'autre.

      ⚠ Le défaut est invisible par construction : `brancher()` réussissait, journalisait
        « routing-probe active », et rien ne pouvait signaler que le bus était le mauvais.
        C'est la même famille que le HUD qui affirmait une configuration qu'il ne lisait
        pas — un composant qui rend compte de son intention, jamais de son effet.

    ⚠ ON S'ABONNE À LA CRÉATION DE TOUT `EventBus`, et c'est délibérément direct. Le bus
      de `serve.py` est une variable LOCALE, passée à une dizaine de composants mais
      publiée nulle part : tenter de la retrouver après coup reviendrait à deviner un
      chemin qu'une montée amont déplacerait. Le constructeur, lui, est le seul point par
      lequel tout bus passe forcément.
      Le marqueur d'idempotence évite les abonnements en double si plusieurs bus naissent
      (tests, rechargements), et le patch est silencieux en cas d'échec : une sonde ne
      doit jamais empêcher un démarrage.
    """
    try:
        from openjarvis.core import events as _ev

        if getattr(_ev.EventBus, "_ava_sonde", False):
            return True
        _init_origine = _ev.EventBus.__init__

        def _init_instrumente(self: Any, *args: Any, **kwargs: Any) -> None:
            _init_origine(self, *args, **kwargs)
            try:
                brancher(self)
            except Exception:  # noqa: BLE001
                pass  # une sonde ne fait jamais échouer la création d'un bus

        _ev.EventBus.__init__ = _init_instrumente  # type: ignore[method-assign]
        _ev.EventBus._ava_sonde = True  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("routing-probe: instrumentation du bus impossible (%s)", exc)
        return False
    return True
