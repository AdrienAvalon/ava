"""Tests de la memoire de conversation serveur.

⚠ CE QUI EST TESTE ICI EST AVANT TOUT LE CLOISONNEMENT. Une fuite entre utilisateurs
  ne se voit pas : elle se lit comme « Ava a confondu deux conversations », et personne
  ne pense a un defaut de requete SQL. C'est le genre de bug qu'on ne trouve qu'en le
  cherchant explicitement — donc ici.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.server import conversation as conv


@pytest.fixture(autouse=True)
def _base_temporaire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    chemin = tmp_path / "conv.db"
    monkeypatch.setattr(conv, "CHEMIN_BASE", chemin)
    return chemin


def _jeton(charge: dict[str, Any]) -> str:
    """Fabrique un JWT plausible — en-tete et signature bidons, corps reel."""
    corps = base64.urlsafe_b64encode(json.dumps(charge).encode()).decode().rstrip("=")
    return f"entete.{corps}.signature"


# ══ Cloisonnement — le coeur du sujet ══════════════════════════════════════════════


def test_deux_utilisateurs_ne_se_voient_pas() -> None:
    """⚠ L'INVARIANT CENTRAL. Une fuite ici se presenterait comme « Ava confond les
    conversations », jamais comme une erreur — d'ou ce test explicite."""
    conv.ajouter("adrien", [{"role": "user", "texte": "mon secret a moi"}])
    conv.ajouter("aurelie", [{"role": "user", "texte": "le sien"}])

    chez_adrien = [x["texte"] for x in conv.lire("adrien")]
    chez_aurelie = [x["texte"] for x in conv.lire("aurelie")]

    assert chez_adrien == ["mon secret a moi"]
    assert chez_aurelie == ["le sien"]


def test_effacer_n_efface_QUE_son_proprietaire() -> None:
    """⚠ Un DELETE sans clause `utilisateur` viderait la memoire de tout le monde — et
    le symptome serait « Ava a tout oublie », sans coupable evident."""
    conv.ajouter("adrien", [{"role": "user", "texte": "a"}])
    conv.ajouter("aurelie", [{"role": "user", "texte": "b"}])

    conv.effacer("adrien")

    assert conv.lire("adrien") == []
    assert len(conv.lire("aurelie")) == 1


def test_le_plafond_s_applique_PAR_utilisateur(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Un plafond GLOBAL laisserait un bavard effacer la memoire des autres."""
    monkeypatch.setattr(conv, "MAX_LIGNES", 5)
    conv.ajouter("bavard", [{"role": "user", "texte": f"m{i}"} for i in range(20)])
    conv.ajouter("discret", [{"role": "user", "texte": "unique"}])

    assert len(conv.lire("bavard")) == 5
    assert [x["texte"] for x in conv.lire("discret")] == ["unique"]
    # …et ce sont bien les PLUS RECENTES qui restent.
    assert [x["texte"] for x in conv.lire("bavard")] == [
        "m15",
        "m16",
        "m17",
        "m18",
        "m19",
    ]


# ══ Identite ═══════════════════════════════════════════════════════════════════════


def test_l_identite_vient_du_sub_pas_de_l_email() -> None:
    """⚠ `sub` est le SEUL identifiant stable. L'infra Avalon a migre l'adresse de
    `acros` le 2026-08-01 et deux consommateurs qui indexaient sur l'e-mail ont casse en
    silence. Indexer la memoire dessus la ferait disparaitre au prochain changement.
    """
    entetes = {
        "X-Ava-Identity": _jeton(
            {"sub": "abc-123", "email": "a@b.c", "preferred_username": "acros"}
        )
    }
    assert conv.identite(entetes) == "abc-123"


def test_repli_sur_le_nom_puis_l_email() -> None:
    assert (
        conv.identite({"X-Ava-Identity": _jeton({"preferred_username": "acros"})})
        == "acros"
    )
    assert conv.identite({"X-Ava-Identity": _jeton({"email": "a@b.c"})}) == "a@b.c"


def test_le_prefixe_Bearer_est_tolere() -> None:
    entetes = {"X-Ava-Identity": "Bearer " + _jeton({"sub": "x"})}
    assert conv.identite(entetes) == "x"


@pytest.mark.parametrize(
    "valeur",
    ["", "pas-un-jwt", "a.b", "a.###.c", "Bearer ", "a." + "!" * 10 + ".c"],
)
def test_un_jeton_illisible_donne_anonyme_sans_lever(valeur: str) -> None:
    """⚠ Un jeton absent ou casse ne doit pas casser la conversation — seulement priver
    du cloisonnement. Lever ici ferait tomber la page pour un en-tete mal forme."""
    assert conv.identite({"X-Ava-Identity": valeur}) == "anonyme"


def test_entete_absent_donne_anonyme() -> None:
    assert conv.identite({}) == "anonyme"


def test_le_base64_sans_remplissage_est_accepte() -> None:
    """⚠ Le corps d'un JWT est du base64url SANS `=` final. `urlsafe_b64decode` leve sur
    une longueur non multiple de 4 : sans completion manuelle, un jeton sur trois serait
    rejete — de façon parfaitement intermittente, donc introuvable."""
    for taille in range(1, 12):
        charge = {"sub": "u" * taille}
        assert conv.identite({"X-Ava-Identity": _jeton(charge)}) == "u" * taille


# ══ Contrat de stockage ════════════════════════════════════════════════════════════


def test_l_ordre_chronologique_est_preserve() -> None:
    """La requete trie en DESC pour appliquer la limite, puis reinverse. Une erreur ici
    rendrait la conversation a l'envers — visible, mais seulement une fois affichee."""
    conv.ajouter("u", [{"role": "user", "texte": f"m{i}"} for i in range(5)])
    assert [x["texte"] for x in conv.lire("u")] == ["m0", "m1", "m2", "m3", "m4"]


def test_les_lignes_vides_sont_ignorees() -> None:
    ecrites = conv.ajouter(
        "u", [{"role": "user", "texte": ""}, {"role": "user", "texte": "ok"}]
    )
    assert ecrites == 1
    assert len(conv.lire("u")) == 1


def test_une_base_illisible_ne_leve_pas(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Une memoire indisponible doit degrader la conversation, pas l'interrompre."""
    monkeypatch.setattr(conv, "CHEMIN_BASE", Path("/proc/interdit/x.db"))
    assert conv.lire("u") == []
    assert conv.ajouter("u", [{"role": "user", "texte": "x"}]) == 0
    assert conv.effacer("u") == 0


def test_l_horodatage_est_pose_s_il_manque() -> None:
    avant = time.time()
    conv.ajouter("u", [{"role": "user", "texte": "x"}])
    assert conv.lire("u")[0]["horodatage"] >= avant


def test_l_horodatage_fourni_est_respecte() -> None:
    """Le client envoie l'heure locale de l'echange : la reecrire ferait apparaitre
    toute une conversation importee a la seconde de son import."""
    conv.ajouter("u", [{"role": "user", "texte": "x", "horodatage": 1000.0}])
    assert conv.lire("u")[0]["horodatage"] == 1000.0
