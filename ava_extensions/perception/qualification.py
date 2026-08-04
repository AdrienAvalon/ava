"""Qualifier ce qui change dans Avalon — et surtout, ce qui ne mérite PAS d'être dit.

⚠ CE FICHIER EST LE CŒUR DE LA PERCEPTION, ET SON SUJET EST LE SILENCE.
  Brancher Ava sur un flux d'événements est facile. Le difficile, et ce qui décide si
  elle devient un organe d'Avalon ou une source de bruit de plus, c'est de savoir se
  taire.

  L'infrastructure a déjà payé cette leçon plusieurs fois, et chaque fois de la même
  façon : un digest qui criait chaque matin sur une sonde au DP batterie figé, une
  alerte TLS vue trois fois dans la même soirée, une règle Wazuh qu'il ne faut surtout
  pas museler. Le résultat est toujours le même — **on apprend à ignorer**, et une
  alerte ignorée est pire qu'une alerte absente, parce qu'on se croit couvert.

⚠ LA DISTINCTION QUI STRUCTURE TOUT : PERCEVOIR ≠ DIRE.
  Ava doit TOUT percevoir — c'est ce qui lui permet de répondre « Aurélie est rentrée
  il y a vingt minutes » quand on le lui demande. Mais elle ne doit PARLER que de ce
  qui change la journée de quelqu'un. Trois niveaux, et un seul autorise à
  interrompre :

    · `MEMOIRE`   — enregistré, jamais dit spontanément. La matière du contexte et de
                    la mémoire épisodique : températures, lumières, consommation.
    · `NOTABLE`   — enregistré, dit SI on lui parle, ou dans un point d'étape.
                    Une arrivée, un départ, un changement de mode de chauffage.
    · `INTERRUPT` — mérite de couper quelqu'un dans ce qu'il fait. Réservé à ce qui
                    est **actionnable maintenant** et que la personne ignore.

⚠ POURQUOI LE SCORER DU CP IGNORE VOLONTAIREMENT CE QUI VARIE, et pourquoi on reprend
  sa doctrine : température, présence, lumières et consommation bougent par nature. Les
  transformer en signaux ferait chuter le score chaque été — donc apprendrait à
  l'ignorer. Le module `home_assistant` du control plane ne retient que trois
  défaillances, toutes actionnables : entité suivie **disparue**, **pile** sous son
  seuil, **baie serveur** chaude. On ne fait pas mieux ici, on fait pareil.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Niveau(str, Enum):
    """Ce qu'Ava a le droit de faire d'un changement."""

    MEMOIRE = "memoire"
    NOTABLE = "notable"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class Changement:
    """Un fait observé, avec ce qu'Ava a le droit d'en faire."""

    sujet: str
    """De quoi il s'agit, en langage humain (« Aurélie », « pve-02 »)."""
    fait: str
    """Ce qui a changé, formulé pour être dit tel quel."""
    niveau: Niveau
    domaine: str
    """`infra` ou `maison` — sert au routage et au filtrage, pas à la décision."""
    details: dict[str, Any] = field(default_factory=dict)


# ⚠ SEUILS — chacun a une raison, aucun n'est rond par hasard.
#
#   `BAIE_CHAUDE_C` : la baie serveur porte AVA, donc GitLab, Keycloak, les sauvegardes
#   et ce control plane. C'est la seule température de la maison dont la dérive soit une
#   urgence, et c'est déjà le choix du module CP.
BAIE_CHAUDE_C = 35.0

#   `SCORE_CHUTE_INTERRUPT` : en dessous, une chute de score est un incident. La valeur
#   reprend le seuil déjà utilisé par `matrix_alerting` pour router vers #ops-critical —
#   on ne réinvente pas un seuil concurrent, deux seuils qui divergent produisent deux
#   vérités.
SCORE_CHUTE_INTERRUPT = 10

#   `SCORE_CHUTE_NOTABLE` : plus petit qu'une déduction de module isolée. En dessous,
#   c'est du bruit de mesure.
SCORE_CHUTE_NOTABLE = 3


def _nombre(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def qualifier_infra(avant: dict[str, Any], apres: dict[str, Any]) -> list[Changement]:
    """Changements de l'infrastructure entre deux lectures du tableau de bord."""
    faits: list[Changement] = []

    a = _nombre((avant.get("score") or {}).get("global"))
    b = _nombre((apres.get("score") or {}).get("global"))
    if a is not None and b is not None and b < a:
        chute = a - b
        if chute >= SCORE_CHUTE_INTERRUPT:
            niveau = Niveau.INTERRUPT
        elif chute >= SCORE_CHUTE_NOTABLE:
            niveau = Niveau.NOTABLE
        else:
            niveau = Niveau.MEMOIRE
        faits.append(
            Changement(
                sujet="score Avalon",
                fait=f"le score est passé de {a:.0f} à {b:.0f}",
                niveau=niveau,
                domaine="infra",
                details={"avant": a, "apres": b, "chute": chute},
            )
        )
    elif a is not None and b is not None and b > a and (b - a) >= SCORE_CHUTE_NOTABLE:
        # ⚠ LA REMONTÉE COMPTE AUSSI, et elle est NOTABLE et non INTERRUPT : savoir
        #   qu'un incident s'est résolu tout seul évite d'aller chercher une panne
        #   terminée. C'est exactement ce que fait le routage `resolved` de Grafana.
        faits.append(
            Changement(
                sujet="score Avalon",
                fait=f"le score est remonté de {a:.0f} à {b:.0f}",
                niveau=Niveau.NOTABLE,
                domaine="infra",
                details={"avant": a, "apres": b},
            )
        )

    # ⚠ Un module qui BASCULE, pas un module qui est en panne : sans cette distinction
    #   on redirait la même chose à chaque lecture, ce qui est la définition du bruit.
    sante_a = avant.get("module_health") or {}
    sante_b = apres.get("module_health") or {}
    for nom, etat in sante_b.items():
        precedent = sante_a.get(nom)
        if precedent is None or precedent == etat:
            continue
        if etat in ("degraded", "error", "down") and precedent == "ok":
            faits.append(
                Changement(
                    sujet=f"module {nom}",
                    fait=f"le module {nom} est passé de {precedent} à {etat}",
                    niveau=Niveau.NOTABLE,
                    domaine="infra",
                    details={"module": nom, "avant": precedent, "apres": etat},
                )
            )
        elif etat == "ok" and precedent in ("degraded", "error", "down"):
            faits.append(
                Changement(
                    sujet=f"module {nom}",
                    fait=f"le module {nom} est revenu à la normale",
                    niveau=Niveau.MEMOIRE,
                    domaine="infra",
                    details={"module": nom},
                )
            )
    return faits


def qualifier_maison(avant: dict[str, Any], apres: dict[str, Any]) -> list[Changement]:
    """Changements de la maison entre deux lectures du module `home_assistant`.

    ⚠ CE QUI EST DÉLIBÉRÉMENT CLASSÉ `MEMOIRE` : températures, lumières, consommation.
      Ces valeurs bougent par nature — les dire ferait d'Ava un bavard qu'on couperait
      au bout d'une journée. Elles sont **enregistrées**, donc disponibles quand on
      pose une question ou quand elle construit un point d'étape. C'est la différence
      entre savoir et raconter.
    """
    faits: list[Changement] = []

    # ── Présence ────────────────────────────────────────────────────────────────
    # ⚠ NOTABLE et jamais INTERRUPT : qu'Aurélie rentre est un fait utile — Ava doit
    #   pouvoir dire « elle est rentrée il y a vingt minutes » — mais cela n'a aucune
    #   raison de couper quelqu'un dans ce qu'il fait.
    pres_a = avant.get("presence") or {}
    pres_b = apres.get("presence") or {}
    for qui, etat in pres_b.items():
        precedent = pres_a.get(qui)
        if precedent is None or precedent == etat:
            continue
        verbe = "est rentré·e" if etat == "présent" else "est parti·e"
        faits.append(
            Changement(
                sujet=qui,
                fait=f"{qui} {verbe}",
                niveau=Niveau.NOTABLE,
                domaine="maison",
                details={"personne": qui, "avant": precedent, "apres": etat},
            )
        )

    # ── Piles faibles ───────────────────────────────────────────────────────────
    # ⚠ ACTIONNABLE, donc INTERRUPT — mais seulement à l'APPARITION. Une pile faible
    #   le reste des jours durant : la redire à chaque lecture serait exactement le
    #   digest qui criait chaque matin, et qu'on a fini par ignorer.
    piles_a = {p.get("nom") for p in (avant.get("piles_faibles") or [])}
    for pile in apres.get("piles_faibles") or []:
        nom = pile.get("nom")
        if nom and nom not in piles_a:
            niv = _nombre(pile.get("niveau"))
            faits.append(
                Changement(
                    sujet=str(nom),
                    fait=f"la pile de « {nom} » est faible"
                    + (f" ({niv:.0f} %)" if niv is not None else ""),
                    niveau=Niveau.INTERRUPT,
                    domaine="maison",
                    details={"entite": nom, "niveau": niv},
                )
            )

    # ── Entités disparues ───────────────────────────────────────────────────────
    # ⚠ Un appareil qui cesse de répondre est le signal le plus utile de cette maison :
    #   c'est ainsi qu'on a découvert le radiateur muet depuis deux mois, et la panne du
    #   point d'accès de la chaufferie. Même règle : à l'apparition seulement.
    abs_a = set(avant.get("entites_absentes") or [])
    for entite in apres.get("entites_absentes") or []:
        if entite not in abs_a:
            faits.append(
                Changement(
                    sujet=str(entite),
                    fait=f"« {entite} » ne répond plus",
                    niveau=Niveau.INTERRUPT,
                    domaine="maison",
                    details={"entite": entite},
                )
            )
    # Et le retour, qui évite de partir réparer une panne terminée.
    abs_b = set(apres.get("entites_absentes") or [])
    for entite in abs_a - abs_b:
        faits.append(
            Changement(
                sujet=str(entite),
                fait=f"« {entite} » répond de nouveau",
                niveau=Niveau.NOTABLE,
                domaine="maison",
                details={"entite": entite},
            )
        )

    # ── Baie serveur ────────────────────────────────────────────────────────────
    # ⚠ LA SEULE TEMPÉRATURE QUI INTERROMPT, et pour une raison précise : cette baie
    #   porte AVA, donc GitLab, Keycloak, le control plane et les sauvegardes. Toutes
    #   les autres températures de la maison sont `MEMOIRE`.
    #   Au FRANCHISSEMENT du seuil, jamais tant qu'on reste au-dessus.
    e_a = avant.get("energie") or {}
    e_b = apres.get("energie") or {}
    t_a = _nombre(e_a.get("baie_temperature_c"))
    t_b = _nombre(e_b.get("baie_temperature_c"))
    if (
        t_b is not None
        and t_b >= BAIE_CHAUDE_C
        and (t_a is None or t_a < BAIE_CHAUDE_C)
    ):
        faits.append(
            Changement(
                sujet="baie serveur",
                fait=f"la baie serveur est à {t_b:.1f} °C",
                niveau=Niveau.INTERRUPT,
                domaine="maison",
                details={"temperature": t_b, "seuil": BAIE_CHAUDE_C},
            )
        )
    elif (
        t_a is not None
        and t_a >= BAIE_CHAUDE_C
        and t_b is not None
        and t_b < BAIE_CHAUDE_C
    ):
        faits.append(
            Changement(
                sujet="baie serveur",
                fait=f"la baie serveur est redescendue à {t_b:.1f} °C",
                niveau=Niveau.NOTABLE,
                domaine="maison",
                details={"temperature": t_b},
            )
        )

    # ── Chauffage ───────────────────────────────────────────────────────────────
    # ⚠ Le MODE change rarement et sur décision humaine : c'est notable. Le fait de
    #   chauffer ou non alterne en permanence — c'est de la mémoire.
    ch_a = avant.get("chauffage") or {}
    ch_b = apres.get("chauffage") or {}
    for appareil, etat in ch_b.items():
        if not isinstance(etat, dict):
            continue
        precedent = ch_a.get(appareil)
        if not isinstance(precedent, dict):
            continue
        if precedent.get("mode") != etat.get("mode"):
            faits.append(
                Changement(
                    sujet=str(appareil),
                    fait=f"{appareil} est passé en mode « {etat.get('mode')} »",
                    niveau=Niveau.NOTABLE,
                    domaine="maison",
                    details={
                        "appareil": appareil,
                        "avant": precedent.get("mode"),
                        "apres": etat.get("mode"),
                    },
                )
            )

    # ── Ce qui est MÉMOIRE : enregistré, jamais dit spontanément ────────────────
    faits.extend(_memoire_ambiance(avant, apres))
    return faits


def _memoire_ambiance(avant: dict[str, Any], apres: dict[str, Any]) -> list[Changement]:
    """Températures, lumières, consommation — la matière du contexte.

    ⚠ CES FAITS EXISTENT POUR ÊTRE RETROUVÉS, PAS POUR ÊTRE DITS. C'est ce qui permet
      à Ava de répondre « il faisait 31 °C à 15 h » ou « la lumière du salon est
      restée allumée toute la nuit » — et, à terme, de dire « encore ce disjoncteur »
      parce qu'elle sait que c'est la troisième fois. Sans cet enregistrement, elle ne
      peut que constater le présent, et un assistant qui n'a pas de passé ne peut pas
      être complice.
    """
    faits: list[Changement] = []

    for cle, libelle in (("temperatures", ""), ("pieces", "")):
        a, b = avant.get(cle) or {}, apres.get(cle) or {}
        for endroit, val in b.items():
            v, w = _nombre(val), _nombre(a.get(endroit))
            # ⚠ Seuil de 0,5 °C : sans lui, chaque relève produirait un « changement »
            #   (les sondes oscillent au dixième) et la mémoire se remplirait de rien.
            if v is not None and (w is None or abs(v - w) >= 0.5):
                faits.append(
                    Changement(
                        sujet=str(endroit),
                        fait=f"{endroit} : {v:.1f} °C{libelle}",
                        niveau=Niveau.MEMOIRE,
                        domaine="maison",
                        details={"endroit": endroit, "temperature": v},
                    )
                )

    maison_a = avant.get("maison") or {}
    for objet, etat in (apres.get("maison") or {}).items():
        if maison_a.get(objet) != etat:
            faits.append(
                Changement(
                    sujet=str(objet),
                    fait=f"{objet} : {etat}",
                    niveau=Niveau.MEMOIRE,
                    domaine="maison",
                    details={"objet": objet, "etat": etat},
                )
            )
    return faits


def qualifier(avant: dict[str, Any], apres: dict[str, Any]) -> list[Changement]:
    """Tous les changements entre deux lectures du tableau de bord.

    ⚠ `avant` VIDE N'EST PAS UN CHANGEMENT. Au premier démarrage — ou après un
      redémarrage si l'état n'avait pas été persisté — tout paraîtrait nouveau : Ava
      annoncerait l'arrivée des quatre habitants et la panne de tout ce qui est déjà
      en panne. C'est exactement le défaut d'`ai_analysis`, dont la baseline en
      variables globales était effacée à chaque redémarrage (64 fois en 29 jours).
      On rend une liste vide et on se contente d'enregistrer la ligne de base.
    """
    if not avant:
        return []
    ha_a = (avant.get("module_data") or {}).get("home_assistant") or {}
    ha_b = (apres.get("module_data") or {}).get("home_assistant") or {}
    return qualifier_infra(avant, apres) + qualifier_maison(ha_a, ha_b)


def a_dire(faits: list[Changement]) -> list[Changement]:
    """Ce qui mérite d'interrompre quelqu'un. Rien d'autre."""
    return [f for f in faits if f.niveau is Niveau.INTERRUPT]
