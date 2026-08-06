"""Tests de l'outil `evolutions`.

⚠ POURQUOI IL EXISTE. Constat de l'admin le 2026-08-06 : « elle ne sait pas trop à chaque
  fois ce qui a changé ». Son code est modifié plusieurs fois par jour et rien ne le lui
  dit — elle se décrit donc d'après sa persona, texte figé au jour de sa rédaction. Le
  symptôme observé : elle récitait « je n'ai aucune initiative » pendant que sa veille
  documentaire tournait.

L'essentiel de ce qui suit porte sur les deux façons dont cet outil pourrait NUIRE :
en rendant du bruit d'entretien à la place des vraies capacités, et en faisant passer
« je n'ai pas pu regarder » pour « rien n'a changé ».
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ava_extensions.skills import evolutions


def _bloc(date: str, sujet: str, corps: str = "") -> str:
    return f"{date}\x1f{sujet}\x1f{corps}\x1e"


class _Faux:
    def __init__(self, sortie: str, code: int = 0) -> None:
        self.stdout, self.returncode, self.stderr = sortie, code, ""


@pytest.fixture
def _git(monkeypatch: pytest.MonkeyPatch):
    def poser(sortie: str, code: int = 0) -> dict[str, Any]:
        vu: dict[str, Any] = {}

        def faux_run(cmd, **kw):  # noqa: ANN001, ANN003
            vu["cmd"] = cmd
            return _Faux(sortie, code)

        monkeypatch.setattr(subprocess, "run", faux_run)
        return vu

    return poser


def test_les_changements_sont_RENDUS_avec_leur_date(_git) -> None:
    _git(
        _bloc(
            "2026-08-06",
            "feat(memoire): un fait perime est MARQUE et CONSERVE",
            "corps",
        )
        + _bloc("2026-08-05", "fix(securite): fermer file_write par symetrie")
    )
    r = evolutions.EvolutionsTool().execute()
    assert r.success is True
    assert "2026-08-06" in r.content
    assert "un fait perime est MARQUE" in r.content
    assert r.metadata["trouves"] == 2


def test_le_BRUIT_D_ENTRETIEN_est_ECARTE(_git) -> None:
    """⚠ `chore` et `docs` ne changent pas ce qu'elle sait faire. Les inclure noierait les
    vraies capacités sous des re-baselines d'empreinte — et un outil qui rend surtout du
    bruit est un outil qu'on cesse d'appeler."""
    _git(
        _bloc("2026-08-06", "chore(integrity): re-baseline CP v2")
        + _bloc("2026-08-06", "docs: corriger un compteur")
        + _bloc("2026-08-05", "feat(outils): nouvel outil camera")
    )
    r = evolutions.EvolutionsTool().execute()
    assert "re-baseline" not in r.content
    assert "nouvel outil camera" in r.content
    assert r.metadata["trouves"] == 1


def test_git_MUET_n_est_pas_RIEN_N_A_CHANGE(_git) -> None:
    """⚠ LE CONTRE-TEST QUI PORTE LE RISQUE. Les deux phrases se ressemblent et une seule
    est vraie. Conclure à l'absence de changement parce qu'on n'a pas pu regarder est
    exactement la classe de défaut que ce système passe son temps à corriger."""
    _git("", code=128)
    r = evolutions.EvolutionsTool().execute()
    assert r.success is False
    assert "n'arrive pas à lire" in r.content
    assert "Aucun changement" not in r.content


def test_une_periode_VIDE_le_dit_franchement(_git) -> None:
    _git(_bloc("2026-08-06", "chore: rien qui la concerne"))
    r = evolutions.EvolutionsTool().execute()
    assert r.success is True
    assert "Aucun changement" in r.content


def test_le_nombre_demande_est_BORNE(_git) -> None:
    """Une valeur aberrante ne doit ni lever ni rendre un pavé."""
    _git("".join(_bloc("2026-08-06", f"feat: capacite {i}") for i in range(60)))
    r = evolutions.EvolutionsTool().execute(nombre=9999)
    assert r.metadata["trouves"] <= evolutions._MAX


def test_AUCUNE_chaine_du_modele_n_atteint_la_ligne_de_commande(_git) -> None:
    """⚠ Le modèle choisit parmi des questions nommées, il ne compose pas de commande.
    La fenêtre est un `enum`, le nombre un entier borné, le répertoire est fixe."""
    vu = _git(_bloc("2026-08-06", "feat: quelque chose"))
    evolutions.EvolutionsTool().execute(depuis="; rm -rf /", nombre="8; echo")
    cmd = vu["cmd"]
    assert all(isinstance(a, str) for a in cmd)
    assert not any("rm -rf" in a or "echo" in a for a in cmd)
    assert cmd[:3] == ["git", "-C", str(evolutions.RACINE)]


def test_la_fenetre_INCONNUE_retombe_sur_le_defaut(_git) -> None:
    vu = _git(_bloc("2026-08-06", "feat: quelque chose"))
    evolutions.EvolutionsTool().execute(depuis="depuis toujours et a jamais")
    assert any("--since=7 days ago" in a for a in vu["cmd"])


def test_le_corps_est_TRONQUE_et_sans_pied_de_commit(_git) -> None:
    """⚠ Les messages de ce dépôt font souvent trente lignes : tout rendre coûterait des
    milliers de jetons pour une question à laquelle une phrase répond. Et la signature
    de commit n'apprend rien à Ava sur elle-même."""
    _git(
        _bloc(
            "2026-08-06",
            "feat: une capacite",
            "Co-Authored-By: quelqu'un\nLa vraie explication du changement\n"
            + "x" * 500,
        )
    )
    r = evolutions.EvolutionsTool().execute()
    assert "La vraie explication" in r.content
    assert "Co-Authored-By" not in r.content
    assert len(r.content) < 1200


def test_le_rendu_dit_au_modele_de_PREFERER_le_journal(_git) -> None:
    """⚠ Sans cette phrase, elle lit ses évolutions puis répond d'après sa persona figée —
    c'est le défaut d'origine, déplacé d'un cran."""
    _git(_bloc("2026-08-06", "feat: une capacite"))
    r = evolutions.EvolutionsTool().execute()
    assert "c'est lui qui a raison" in r.content


def test_contre_le_VRAI_depot_git(tmp_path) -> None:  # noqa: ARG001
    """⚠ Les tests ci-dessus simulent git. Celui-ci l'appelle POUR DE VRAI sur le dépôt
    d'Ava : un format `--pretty` invalide ou un chemin faux ne se verrait pas autrement."""
    r = evolutions.EvolutionsTool().execute(depuis="toujours", nombre=3)
    assert r.success is True, r.content
    assert r.metadata["trouves"] >= 1
