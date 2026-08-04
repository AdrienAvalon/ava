"""Tests de la qualification — ce qu'Ava percoit, et surtout ce qu'elle TAIT.

⚠ CES TESTS PORTENT SUR LE SILENCE AUTANT QUE SUR LE SIGNAL, et c'est volontaire.
  Un module de perception se juge a deux choses : ce qu'il attrape, et ce qu'il laisse
  passer. Avalon a paye plusieurs fois la seconde — le digest qui criait chaque matin
  sur une sonde au DP batterie fige, l'alerte TLS vue trois fois dans la meme soiree.
  Le resultat est toujours le meme : on apprend a ignorer, et une alerte ignoree est
  pire qu'une alerte absente parce qu'on se croit couvert.

  D'ou la structure : pour chaque signal verifie, un contre-test verifie qu'une
  variation ORDINAIRE du meme champ ne dit rien.
"""

from __future__ import annotations

from typing import Any

import pytest

from ava_extensions.perception import Niveau, a_dire, qualifier
from ava_extensions.perception.qualification import qualifier_infra, qualifier_maison


def _maison(**champs: Any) -> dict[str, Any]:
    """Un etat HA minimal, complete par les champs donnes."""
    base: dict[str, Any] = {
        "presence": {},
        "temperatures": {},
        "pieces": {},
        "chauffage": {},
        "energie": {},
        "maison": {},
        "piles_faibles": [],
        "entites_absentes": [],
    }
    base.update(champs)
    return base


def _faits(avant: dict[str, Any], apres: dict[str, Any]) -> list[Any]:
    return qualifier_maison(_maison(**avant), _maison(**apres))


# ══ Le premier demarrage ne doit RIEN annoncer ═══════════════════════════════════


def test_un_etat_initial_VIDE_ne_produit_aucun_changement() -> None:
    """⚠ LE DEFAUT D'`ai_analysis`, QU'ON NE VEUT PAS HERITER.

    Sa ligne de base vivait dans des variables globales, effacees a chaque redemarrage
    — 64 fois en 29 jours cote CP, 60 fois le 04/08 cote Ava. Sans cette garde, chaque
    demarrage ferait annoncer a Ava l'arrivee des quatre habitants et la panne de tout
    ce qui est deja en panne. Elle serait la plus bavarde precisement au moment ou elle
    vient de perdre la memoire.
    """
    apres = {
        "score": {"global": 40},
        "module_health": {"nsm": "degraded"},
        "module_data": {
            "home_assistant": _maison(
                presence={"Adrien": "présent"},
                piles_faibles=[{"nom": "Sonde", "niveau": 3}],
                entites_absentes=["Ballon"],
            )
        },
    }
    assert qualifier({}, apres) == []


# ══ Ce qui merite d'interrompre ══════════════════════════════════════════════════


def test_une_pile_faible_qui_APPARAIT_interrompt() -> None:
    faits = _faits({}, {"piles_faibles": [{"nom": "Sonde salon", "niveau": 11}]})
    assert [f.niveau for f in faits] == [Niveau.INTERRUPT]
    assert "Sonde salon" in faits[0].fait


def test_une_pile_faible_DEJA_connue_se_TAIT() -> None:
    """⚠ LE CONTRE-TEST QUI COMPTE LE PLUS. Une pile faible le reste des jours durant.
    La redire a chaque releve — toutes les 5 minutes — serait exactement le digest qui
    criait chaque matin, et qu'on a fini par ignorer. On ne signale qu'a l'APPARITION.
    """
    pile = [{"nom": "Sonde salon", "niveau": 11}]
    assert _faits({"piles_faibles": pile}, {"piles_faibles": pile}) == []


def test_une_entite_qui_DISPARAIT_interrompt() -> None:
    """C'est ce signal qui a revele le radiateur muet depuis deux mois et la panne du
    point d'acces de la chaufferie. Le plus utile de cette maison."""
    faits = _faits({}, {"entites_absentes": ["Ballon eau chaude"]})
    assert a_dire(faits) and "ne répond plus" in faits[0].fait


def test_une_entite_qui_REVIENT_est_notable_sans_interrompre() -> None:
    """Savoir qu'une panne est terminee evite d'aller la reparer. Mais cela ne justifie
    pas de couper quelqu'un dans ce qu'il fait."""
    faits = _faits({"entites_absentes": ["Ballon"]}, {"entites_absentes": []})
    assert [f.niveau for f in faits] == [Niveau.NOTABLE]
    assert "répond de nouveau" in faits[0].fait


def test_la_baie_serveur_chaude_interrompt() -> None:
    """⚠ LA SEULE TEMPERATURE DE LA MAISON QUI INTERROMPT, et pour une raison precise :
    cette baie porte AVA, donc GitLab, Keycloak, le control plane et les sauvegardes."""
    faits = _faits(
        {"energie": {"baie_temperature_c": 25.0}},
        {"energie": {"baie_temperature_c": 36.2}},
    )
    assert [f.niveau for f in faits] == [Niveau.INTERRUPT]


def test_la_baie_DEJA_chaude_ne_redit_rien() -> None:
    """Au FRANCHISSEMENT du seuil, jamais tant qu'on reste au-dessus."""
    chaud = {"energie": {"baie_temperature_c": 36.2}}
    assert _faits(chaud, {"energie": {"baie_temperature_c": 37.0}}) == []


def test_une_temperature_de_piece_elevee_ne_dit_RIEN() -> None:
    """⚠ CONTRE-TEST STRUCTURANT. Il fait 31 °C dehors aujourd'hui. Si la chaleur
    interrompait, Ava crierait tout l'ete — donc on apprendrait a l'ignorer, y compris
    le jour ou la baie serveur chaufferait vraiment. C'est exactement pour cela que le
    scorer du CP ignore volontairement tout ce qui varie par nature."""
    faits = _faits(
        {"temperatures": {"exterieur": 28.0}}, {"temperatures": {"exterieur": 34.5}}
    )
    assert a_dire(faits) == []
    assert [f.niveau for f in faits] == [Niveau.MEMOIRE]


# ══ La presence : notable, jamais interruptive ═══════════════════════════════════


def test_une_arrivee_est_NOTABLE_et_n_interrompt_pas() -> None:
    """⚠ Ava doit SAVOIR qu'Aurelie est rentree — c'est ce qui lui permet de repondre
    « il y a vingt minutes » quand on le lui demande. Mais cela n'a aucune raison de
    couper quelqu'un. C'est la distinction PERCEVOIR / DIRE, appliquee au cas le plus
    frequent de la maison."""
    faits = _faits(
        {"presence": {"Aurélie": "absent"}}, {"presence": {"Aurélie": "présent"}}
    )
    assert [f.niveau for f in faits] == [Niveau.NOTABLE]
    assert "Aurélie" in faits[0].fait
    assert a_dire(faits) == []


def test_un_depart_est_percu() -> None:
    faits = _faits(
        {"presence": {"Adrien": "présent"}}, {"presence": {"Adrien": "absent"}}
    )
    assert len(faits) == 1 and "parti" in faits[0].fait


def test_une_presence_INCHANGEE_ne_produit_rien() -> None:
    p = {"presence": {"Adrien": "présent", "Aurélie": "présent"}}
    assert _faits(p, p) == []


def test_une_personne_INCONNUE_au_depart_n_est_pas_une_arrivee() -> None:
    """⚠ Une personne qui apparait dans le releve sans y figurer avant n'est pas
    forcement rentree : c'est peut-etre un nouveau compte, ou le module qui la voit
    pour la premiere fois. Sans cette garde, l'ajout d'un membre de la famille
    produirait une fausse arrivee."""
    assert _faits({}, {"presence": {"Annie": "présent"}}) == []


# ══ L'infrastructure ════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("avant", "apres", "attendu"),
    [
        (97, 85, Niveau.INTERRUPT),  # chute de 12 → incident
        (97, 93, Niveau.NOTABLE),  # chute de 4 → a savoir
        (97, 96, Niveau.MEMOIRE),  # chute de 1 → bruit de mesure
    ],
)
def test_la_gravite_d_une_chute_de_score_suit_son_ampleur(
    avant: int, apres: int, attendu: Niveau
) -> None:
    faits = qualifier_infra({"score": {"global": avant}}, {"score": {"global": apres}})
    assert [f.niveau for f in faits] == [attendu]


def test_un_score_STABLE_ne_produit_rien() -> None:
    assert qualifier_infra({"score": {"global": 97}}, {"score": {"global": 97}}) == []


def test_une_remontee_de_score_est_notable() -> None:
    """Savoir qu'un incident s'est resolu tout seul evite d'aller chercher une panne
    terminee — c'est ce que fait deja le routage `resolved` de Grafana."""
    faits = qualifier_infra({"score": {"global": 80}}, {"score": {"global": 97}})
    assert [f.niveau for f in faits] == [Niveau.NOTABLE]
    assert "remonté" in faits[0].fait


def test_un_module_qui_BASCULE_est_signale_une_seule_fois() -> None:
    """⚠ Un module qui bascule, pas un module qui EST en panne. Sans cette distinction
    on redirait la meme chose toutes les 5 minutes — la definition du bruit."""
    a, b = {"module_health": {"nsm": "ok"}}, {"module_health": {"nsm": "degraded"}}
    assert len(qualifier_infra(a, b)) == 1
    assert qualifier_infra(b, b) == []


def test_un_module_INCONNU_au_depart_n_est_pas_une_degradation() -> None:
    """Un module qu'on active apparait `ok` ou `degraded` : ce n'est pas un changement
    d'etat, c'est une premiere mesure."""
    assert qualifier_infra({}, {"module_health": {"nouveau": "degraded"}}) == []


# ══ Robustesse — la perception ne doit jamais casser sur des donnees abimees ═════


@pytest.mark.parametrize(
    "apres",
    [
        {"score": {"global": None}},
        {"score": {"global": "n/a"}},
        {"score": None},
        {"module_health": None},
        {},
    ],
)
def test_des_donnees_ABIMEES_ne_levent_pas(apres: dict[str, Any]) -> None:
    """⚠ Le tableau de bord est produit par 25 modules dont certains tombent. Une
    perception qui leverait sur un champ manquant se tairait definitivement — et son
    silence serait indistinguable d'une infrastructure saine."""
    qualifier_infra({"score": {"global": 97}}, apres)


def test_un_NaN_ne_devient_pas_une_chute_de_score() -> None:
    """⚠ `float('nan')` passe `float()` sans erreur et rend toute comparaison fausse.
    Le meme piege a deja ete ferme dans `conversation.py` — un NaN y faisait echouer
    l'ecriture atomique de tout un envoi."""
    faits = qualifier_infra(
        {"score": {"global": 97}}, {"score": {"global": float("nan")}}
    )
    assert faits == []


def test_qualifier_complet_separe_bien_les_deux_domaines() -> None:
    avant = {
        "score": {"global": 97},
        "module_data": {"home_assistant": _maison(presence={"Adrien": "absent"})},
    }
    apres = {
        "score": {"global": 80},
        "module_data": {"home_assistant": _maison(presence={"Adrien": "présent"})},
    }
    faits = qualifier(avant, apres)
    domaines = {f.domaine for f in faits}
    assert domaines == {"infra", "maison"}


def test_les_faits_sont_formules_pour_etre_DITS() -> None:
    """⚠ Le champ `fait` est destine a etre prononce tel quel par Ava. Un libelle
    technique (`presence.Aurelie: absent→present`) obligerait le modele a le
    reformuler, donc a l'interpreter — et c'est la qu'on invente."""
    faits = _faits(
        {"presence": {"Aurélie": "absent"}}, {"presence": {"Aurélie": "présent"}}
    )
    assert faits[0].fait == "Aurélie est rentré·e"
    assert "→" not in faits[0].fait and "presence." not in faits[0].fait
