"""Tests de la memoire de perception et de la boucle d'observation.

⚠ CE QUI EST VERROUILLE ICI. La persistance de l'etat est ce qui separe cette
  perception du defaut d'`ai_analysis` : sa ligne de base vivait dans des variables
  globales, effacees a chaque redemarrage — **64 fois en 29 jours** cote control plane,
  **60 fois le 04/08** cote Ava. Un module qui perd sa baseline est aveugle au
  changement precisement au moment ou l'infrastructure vient de bouger, puisqu'un
  redemarrage suit souvent un deploiement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.perception.qualification import Changement, Niveau


@pytest.fixture(autouse=True)
def _base_isolee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from ava_extensions.perception import memoire_perception as mp

    monkeypatch.setattr(mp, "CHEMIN", tmp_path / "perception.db")
    return mp


# ══ L'etat survit-il vraiment ? ═════════════════════════════════════════════════


def test_l_etat_survit_a_un_redemarrage(_base_isolee: Any, tmp_path: Path) -> None:
    """⚠ LE TEST QUI JUSTIFIE TOUT CE FICHIER. On simule le redemarrage en rechargeant
    le module : l'etat doit revenir du disque, pas d'une variable."""
    import importlib

    mp = _base_isolee
    mp.ecrire_dernier_etat({"score": {"global": 97}})

    recharge = importlib.reload(mp)
    recharge.CHEMIN = tmp_path / "perception.db"
    assert recharge.lire_dernier_etat() == {"score": {"global": 97}}


def test_un_premier_demarrage_rend_un_etat_VIDE(_base_isolee: Any) -> None:
    assert _base_isolee.lire_dernier_etat() == {}


def test_une_base_ILLISIBLE_ne_leve_pas(_base_isolee: Any, tmp_path: Path) -> None:
    """⚠ Un etat corrompu doit produire le comportement du PREMIER demarrage — silence
    et prise de ligne de base — pas une exception. Le contraire ferait taire Ava
    definitivement sur une simple corruption, et son silence serait indistinguable
    d'une infrastructure saine."""
    (tmp_path / "perception.db").write_bytes(b"ceci n'est pas une base sqlite")
    assert _base_isolee.lire_dernier_etat() == {}


def test_un_etat_NON_SERIALISABLE_est_refuse_sans_lever(_base_isolee: Any) -> None:
    assert _base_isolee.ecrire_dernier_etat({"objet": object()}) is False


# ══ Le journal des faits ════════════════════════════════════════════════════════


def _faits() -> list[Changement]:
    return [
        Changement("Aurélie", "Aurélie est rentré·e", Niveau.NOTABLE, "maison"),
        Changement("exterieur", "exterieur : 31.4 °C", Niveau.MEMOIRE, "maison"),
        Changement(
            "Sonde", "la pile de « Sonde » est faible", Niveau.INTERRUPT, "maison"
        ),
    ]


def test_tous_les_niveaux_sont_ENREGISTRES(_base_isolee: Any) -> None:
    """⚠ Y COMPRIS `MEMOIRE`, et c'est tout l'interet. La temperature d'hier apres-midi
    ne vaut rien prise isolement, et tout quand on demande « il faisait quoi hier
    soir ? ». Filtrer a l'ECRITURE rendrait ces questions definitivement insolubles ;
    filtrer a la lecture ne coute rien."""
    assert _base_isolee.enregistrer(_faits()) == 3
    assert len(_base_isolee.relire()) == 3


def test_la_relecture_filtre_par_niveau(_base_isolee: Any) -> None:
    _base_isolee.enregistrer(_faits())
    interruptions = _base_isolee.relire(niveau_min=Niveau.INTERRUPT)
    assert [f["sujet"] for f in interruptions] == ["Sonde"]


def test_la_relecture_est_du_plus_RECENT_au_plus_ancien(_base_isolee: Any) -> None:
    maintenant = time.time()
    _base_isolee.enregistrer(
        [Changement("vieux", "vieux fait", Niveau.NOTABLE, "infra")], maintenant - 3600
    )
    _base_isolee.enregistrer(
        [Changement("recent", "fait recent", Niveau.NOTABLE, "infra")], maintenant
    )
    assert [f["sujet"] for f in _base_isolee.relire()] == ["recent", "vieux"]


def test_la_relecture_est_BORNEE(_base_isolee: Any) -> None:
    """⚠ C'est cette fonction que l'outil de memoire episodique appellera. Un modele qui
    demanderait « tout » doit recevoir une reponse de taille finie. La lecon vient de
    Loki, ou une requete plausible et ruineuse est acceptee sans broncher."""
    _base_isolee.enregistrer(
        [Changement(f"s{i}", f"fait {i}", Niveau.MEMOIRE, "infra") for i in range(50)]
    )
    assert len(_base_isolee.relire(limite=10)) == 10
    assert len(_base_isolee.relire(limite=99999)) <= 1000


def test_la_fenetre_temporelle_est_respectee(_base_isolee: Any) -> None:
    maintenant = time.time()
    _base_isolee.enregistrer(
        [Changement("hier", "fait d hier", Niveau.NOTABLE, "infra")],
        maintenant - 2 * 86400,
    )
    _base_isolee.enregistrer(
        [Changement("aujourdhui", "fait du jour", Niveau.NOTABLE, "infra")], maintenant
    )
    recents = _base_isolee.relire(depuis_secondes=86400)
    assert [f["sujet"] for f in recents] == ["aujourdhui"]


def test_le_comptage_permet_de_dire_ENCORE(_base_isolee: Any) -> None:
    """⚠ C'est ce qui permettra « encore ce disjoncteur » plutot que de le constater a
    neuf chaque fois. La complicite vient de la connaissance partagee d'un historique,
    pas d'un prompt plus drole."""
    for _ in range(3):
        _base_isolee.enregistrer(
            [
                Changement(
                    "Disjoncteur chaufferie",
                    "ne répond plus",
                    Niveau.INTERRUPT,
                    "maison",
                )
            ]
        )
    assert _base_isolee.compter("Disjoncteur") == 3
    assert _base_isolee.compter("inexistant") == 0


def test_les_details_survivent_a_l_aller_retour(_base_isolee: Any) -> None:
    _base_isolee.enregistrer(
        [
            Changement(
                "baie", "chaude", Niveau.INTERRUPT, "maison", {"temperature": 36.2}
            )
        ]
    )
    assert _base_isolee.relire()[0]["details"]["temperature"] == 36.2


def test_la_purge_retire_les_faits_anciens(_base_isolee: Any) -> None:
    """Une base qui grossit sans limite finit supprimee a la main, donc tout est perdu.
    Mieux vaut une retention choisie qu'une purge subie."""
    _base_isolee.enregistrer(
        [Changement("vieux", "fait", Niveau.MEMOIRE, "infra")],
        time.time() - 200 * 86400,
    )
    _base_isolee.enregistrer([Changement("neuf", "fait", Niveau.MEMOIRE, "infra")])
    assert _base_isolee.purger(retention_jours=90) == 1
    assert [f["sujet"] for f in _base_isolee.relire(depuis_secondes=10**9)] == ["neuf"]


def test_enregistrer_une_liste_VIDE_ne_fait_rien(_base_isolee: Any) -> None:
    assert _base_isolee.enregistrer([]) == 0


# ══ La boucle d'observation ═════════════════════════════════════════════════════


def test_la_premiere_observation_pose_la_LIGNE_DE_BASE_sans_rien_dire(
    monkeypatch: pytest.MonkeyPatch, _base_isolee: Any
) -> None:
    """⚠ Au premier demarrage, tout parait nouveau. Sans cette garde, Ava annoncerait
    l'arrivee des quatre habitants et la panne de tout ce qui est deja en panne."""
    from ava_extensions.perception import collecteur

    tableau = {"score": {"global": 97}, "module_health": {"nsm": "degraded"}}
    monkeypatch.setattr(collecteur, "_dashboard", lambda: tableau)
    monkeypatch.setattr(collecteur, "memoire", _base_isolee)

    assert collecteur.observer_une_fois() == 0
    assert _base_isolee.lire_dernier_etat() == tableau


def test_la_seconde_observation_VOIT_le_changement(
    monkeypatch: pytest.MonkeyPatch, _base_isolee: Any
) -> None:
    from ava_extensions.perception import collecteur

    monkeypatch.setattr(collecteur, "memoire", _base_isolee)
    etats = [
        {"score": {"global": 97}, "module_health": {"nsm": "ok"}},
        {"score": {"global": 80}, "module_health": {"nsm": "degraded"}},
    ]
    monkeypatch.setattr(collecteur, "_dashboard", lambda: etats.pop(0))

    assert collecteur.observer_une_fois() == 0  # ligne de base
    assert collecteur.observer_une_fois() == 2  # chute + module
    faits = _base_isolee.relire(niveau_min=Niveau.INTERRUPT)
    assert [f["sujet"] for f in faits] == ["score Avalon"]


def test_un_CP_INJOIGNABLE_ne_casse_pas_la_ligne_de_base(
    monkeypatch: pytest.MonkeyPatch, _base_isolee: Any
) -> None:
    """⚠ Le control plane est recree environ deux fois par jour. Une lecture ratee ne
    doit ni lever, ni ECRASER l'etat connu — sinon la reprise ferait rejouer tous les
    changements comme s'ils venaient d'arriver."""
    from ava_extensions.perception import collecteur

    monkeypatch.setattr(collecteur, "memoire", _base_isolee)
    _base_isolee.ecrire_dernier_etat({"score": {"global": 97}})
    monkeypatch.setattr(collecteur, "_dashboard", lambda: None)

    assert collecteur.observer_une_fois() == 0
    assert _base_isolee.lire_dernier_etat() == {"score": {"global": 97}}


def test_la_ligne_de_base_est_ecrite_MEME_sans_changement(
    monkeypatch: pytest.MonkeyPatch, _base_isolee: Any
) -> None:
    """⚠ Ecrire l'etat avant de journaliser : si le processus meurt entre les deux, on
    prefere perdre un fait plutot que le rejouer en boucle au redemarrage. Un doublon
    d'alerte coute plus cher qu'un fait manquant — c'est ce qui use la confiance."""
    from ava_extensions.perception import collecteur

    monkeypatch.setattr(collecteur, "memoire", _base_isolee)
    monkeypatch.setattr(collecteur, "_dashboard", lambda: {"score": {"global": 97}})
    collecteur.observer_une_fois()
    collecteur.observer_une_fois()
    assert _base_isolee.lire_dernier_etat() == {"score": {"global": 97}}


def test_un_dashboard_NON_JSON_ne_leve_pas(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava_extensions.perception import collecteur

    class _Reponse:
        def read(self) -> bytes:
            return b"<html>erreur</html>"

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(
        collecteur.urllib.request, "urlopen", lambda *a, **k: _Reponse()
    )
    assert collecteur._dashboard() is None


def test_le_json_de_details_reste_lisible_apres_relecture(_base_isolee: Any) -> None:
    """Garde-fou de serialisation : un `details` non serialisable ne doit pas faire
    perdre le fait lui-meme."""
    _base_isolee.enregistrer(
        [Changement("x", "fait", Niveau.MEMOIRE, "infra", {"objet": object()})]
    )
    ligne = _base_isolee.relire()[0]
    assert ligne["fait"] == "fait"
    assert isinstance(json.dumps(ligne["details"]), str)
