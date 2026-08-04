"""La boucle de perception — le WebSocket du CP sert de cloche, Ava compare elle-même.

⚠ POURQUOI UNE « CLOCHE » ET NON UN FLUX D'ÉVÉNEMENTS. Mesuré le 2026-08-04 : le
  WebSocket `/api/v1/ws` du control plane ne porte **qu'un seul type de message**,
  `{"type":"score_update","score":97,"status":"ok","modules":25}`, une fois toutes les
  **5 min 04 s**. Il n'a ni souscription, ni filtre, ni topic. Son bus interne compte
  3 producteurs et 1 abonné, pour 25 modules qui collectent en permanence et ne
  publient rien.

  On ne peut donc pas s'abonner à « ce qui change » : ça n'existe pas encore côté CP.
  En revanche le WS dit fidèlement **quand** le CP a fini un cycle. Ava s'en sert pour
  se réveiller, relit `/api/v1/dashboard` (37 Ko, **3,5 ms** depuis la DMZ) et fait le
  diff. C'est exactement le motif déjà en production dans `matrix_alerting` et
  `ai_analysis` : on copie la doctrine maison plutôt que d'en inventer une.

⚠ TROIS EXIGENCES MESURÉES, dont aucune ne se devine :
  1. le chemin est `/api/v1/ws` — `/ws` et `/ws/events` répondent **500** ;
  2. il faut répondre **PONG**, sinon uvicorn ferme à **40 s** (PING à +20 s, close
     1011 « keepalive ping timeout »). Une sonde naïve conclut à une instabilité
     réseau. La bibliothèque `websockets` y répond d'elle-même — c'est pour ça qu'on
     l'utilise plutôt que de cadrer du RFC6455 à la main ;
  3. reconnexion avec **backoff** : le CP est recréé environ deux fois par jour
     (64 démarrages en 29 jours), et chaque recréation coupe la connexion.

⚠ CE QUE CE CHEMIN COÛTE, ET QU'IL FAUT ASSUMER PAR ÉCRIT. Le WS est joignable sans
  authentification parce que `AuthMiddleware` et `RateLimitMiddleware` héritent de
  `BaseHTTPMiddleware`, qui ne traite que le scope `http` — contre-épreuve :
  `GET /api/v1/webhooks` répond bien 401 sur la même instance. Ce n'est donc pas un
  privilège accordé à Ava, c'est une **absence de contrôle sur le port 8100**. La
  documentation d'Avalon dit déjà « le durcissement se décide au port, pas au
  chemin » : le jour où ce durcissement arrivera, il cassera cette perception. Mieux
  vaut l'avoir écrit ici que le découvrir.

⚠ LA PERCEPTION NE PARLE PAS. Elle observe, qualifie et enregistre. Ce qu'Ava a le
  droit de DIRE est décidé ailleurs (`qualification.Niveau`), et le canal par lequel
  elle le dirait n'existe pas encore — c'est une décision de sécurité à prendre, pas
  une implémentation à glisser.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any

from ava_extensions.perception import memoire_perception as memoire
from ava_extensions.perception import parole
from ava_extensions.perception.qualification import qualifier

logger = logging.getLogger(__name__)

# ⚠ L'adresse DMZ du CP, pas l'adresse LAN : Ava vit en DMZ et son egress n'autorise
#   nommément que ce port (« for avalon_status tool » dans host_vars). Le même choix
#   que `skills/avalon_status.py` — deux constantes divergentes produiraient deux
#   comportements selon le chemin emprunté.
CP_BASE = os.environ.get("AVA_CP_BASE", "http://192.168.100.31:8100")
CP_WS = os.environ.get("AVA_CP_WS", CP_BASE.replace("http", "ws", 1) + "/api/v1/ws")

# ⚠ Filet quand le WebSocket est muet. Le CP émet toutes les 5 min ; au-delà de deux
#   périodes sans rien, on relit quand même. Sans ce filet, une connexion WS ouverte
#   mais silencieuse (cas vécu côté sondes : une fenêtre d'écoute plus courte que la
#   période d'émission mesure l'absence, pas le flux) figerait la perception sans
#   qu'aucune erreur n'apparaisse.
PERIODE_FILET_S = float(os.environ.get("AVA_PERCEPTION_FILET_S", "660"))

BACKOFF_INITIAL_S = 5.0
BACKOFF_MAX_S = 120.0
_TIMEOUT_HTTP_S = 10.0

_fil: threading.Thread | None = None
_arret = threading.Event()


def _dashboard() -> dict[str, Any] | None:
    """Le tableau de bord du CP, ou None s'il est injoignable."""
    requete = urllib.request.Request(
        f"{CP_BASE}/api/v1/dashboard", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(requete, timeout=_TIMEOUT_HTTP_S) as reponse:
            charge = json.loads(reponse.read().decode("utf-8"))
        return charge if isinstance(charge, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("perception: dashboard injoignable (%s)", exc)
        return None


def observer_une_fois() -> int:
    """Relit le tableau de bord, qualifie l'écart, journalise. Rend le nombre de faits.

    ⚠ SÉPARÉE DE LA BOUCLE EXPRÈS : c'est cette fonction qu'on teste, et c'est elle
      qu'on peut appeler à la main pour vérifier la perception sans attendre cinq
      minutes. Une boucle qu'on ne peut pas exécuter pas à pas ne se débogue qu'en
      production.
    """
    apres = _dashboard()
    if apres is None:
        return 0
    avant = memoire.lire_dernier_etat()
    faits = qualifier(avant, apres)
    # ⚠ On écrit la nouvelle ligne de base MÊME si rien n'a changé, et AVANT de
    #   journaliser : si le processus meurt entre les deux, on préfère perdre des faits
    #   plutôt que de les rejouer en boucle au redémarrage suivant. Un doublon d'alerte
    #   coûte plus cher qu'un fait manquant — c'est ce qui use la confiance.
    memoire.ecrire_dernier_etat(apres)
    if faits:
        memoire.enregistrer(faits)
        interruptions = [f for f in faits if f.niveau.value == "interrupt"]
        logger.info(
            "perception: %d fait(s), dont %d a signaler", len(faits), len(interruptions)
        )
        for f in interruptions:
            # ⚠ Journalisé en WARNING **en plus** d'être dit : le journal part vers Loki,
            #   donc reste cherchable et daté même si la parole échoue (jeton absent,
            #   Matrix injoignable). Une trace locale rend la perception vérifiable
            #   indépendamment du canal — c'est ce qui permet de distinguer « Ava n'a
            #   rien vu » de « Ava n'a pas pu le dire ».
            logger.warning("perception: %s — %s", f.sujet, f.fait)
        # ⚠ La parole décide seule de se taire (anti-répétition, plafond horaire) : on
        #   lui passe TOUS les faits, elle filtre. Centraliser la discipline à un seul
        #   endroit évite que deux règles de silence divergent.
        parole.dire(faits)
    return len(faits)


def _boucle() -> None:
    """Écoute la cloche, avec reconnexion et filet temporel. Ne lève jamais."""
    try:
        import asyncio

        import websockets
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: websockets indisponible (%s) — filet seul", exc)
        _boucle_sans_ws()
        return

    async def _ecouter() -> None:
        attente = BACKOFF_INITIAL_S
        while not _arret.is_set():
            try:
                # `ping_interval`/`ping_timeout` : la bibliothèque répond seule aux PING
                # du serveur, ce qui évite la fermeture à 40 s.
                async with websockets.connect(
                    CP_WS, ping_interval=20, ping_timeout=20, close_timeout=5
                ) as ws:
                    logger.info("perception: connectee a %s", CP_WS)
                    attente = BACKOFF_INITIAL_S
                    observer_une_fois()  # ligne de base a la connexion
                    while not _arret.is_set():
                        try:
                            await asyncio.wait_for(ws.recv(), timeout=PERIODE_FILET_S)
                        except TimeoutError:
                            # ⚠ Le filet : une connexion ouverte mais muette ne doit pas
                            #   figer la perception en silence.
                            logger.debug(
                                "perception: cloche muette, relecture de filet"
                            )
                        observer_une_fois()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "perception: lien perdu (%s), nouvelle tentative dans %.0f s",
                    type(exc).__name__,
                    attente,
                )
                if _arret.wait(attente):
                    return
                attente = min(attente * 2, BACKOFF_MAX_S)

    try:
        asyncio.run(_ecouter())
    except Exception as exc:  # noqa: BLE001
        logger.warning("perception: boucle arretee (%s)", exc)


def _boucle_sans_ws() -> None:
    """Repli par relecture périodique, si la bibliothèque WebSocket manque.

    ⚠ Volontairement conservé : le jour où `websockets` disparaîtrait d'une montée de
      version, la perception doit se dégrader — pas s'éteindre. Une fonctionnalité qui
      s'arrête complètement sur une dépendance manquante se remarque des semaines plus
      tard.
    """
    while not _arret.is_set():
        observer_une_fois()
        if _arret.wait(PERIODE_FILET_S / 2):
            return


def demarrer() -> bool:
    """Lance la perception en tâche de fond. Idempotent.

    ⚠ Un thread `daemon` : il ne doit jamais retarder l'arrêt du service. Et il ne
      remonte aucune exception dans le démarrage d'Ava — une perception qui empêcherait
      Ava de parler serait exactement contraire à son but.
    """
    global _fil
    if os.environ.get("AVA_PERCEPTION", "1") not in ("1", "true", "yes"):
        logger.info("perception: desactivee par AVA_PERCEPTION")
        return False
    if _fil is not None and _fil.is_alive():
        return True
    _arret.clear()
    _fil = threading.Thread(target=_boucle, name="ava-perception", daemon=True)
    _fil.start()
    logger.info("perception: demarree (cloche %s)", CP_WS)
    return True


def arreter(delai: float = 5.0) -> None:
    """Demande l'arrêt de la boucle (utilisé par les tests)."""
    _arret.set()
    if _fil is not None and _fil.is_alive():
        _fil.join(timeout=delai)
