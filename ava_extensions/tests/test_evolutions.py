"""Tests de l'outil `evolutions`.

⚠ POURQUOI IL EXISTE. Constat de l'admin le 2026-08-06 : « elle ne sait pas trop à
  chaque fois ce qui a changé ». Son code est modifié plusieurs fois par jour et rien ne
  le lui dit — elle se décrit donc d'après sa persona, texte figé au jour de sa
  rédaction. Le symptôme observé : elle récitait « je n'ai aucune initiative » pendant
  que sa veille documentaire tournait.

L'essentiel de ce qui suit porte sur les deux façons dont cet outil pourrait NUIRE :
en rendant du bruit d'entretien à la place des vraies capacités, et en faisant passer
« je n'ai pas pu regarder » pour « rien n'a changé ».
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
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
    """⚠ `chore` et `docs` ne changent pas ce qu'elle sait faire.

    Les inclure noierait les vraies capacités sous des re-baselines d'empreinte — et
    un outil qui rend surtout du bruit est un outil qu'on cesse d'appeler.
    """
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
    """⚠ LE CONTRE-TEST QUI PORTE LE RISQUE.

    Les deux phrases se ressemblent et une seule est vraie. Conclure à l'absence de
    changement parce qu'on n'a pas pu regarder est exactement la classe de défaut que
    ce système passe son temps à corriger.
    """
    _git("", code=128)
    r = evolutions.EvolutionsTool().execute()
    assert r.success is False
    assert "n'arrive pas à lire" in r.content
    assert "Aucun changement" not in r.content


def test_une_release_sans_git_lit_son_historique_atteste(
    _git, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git("", code=128)
    historique = tmp_path / "evolutions-v1.json"
    historique.write_text(
        json.dumps(
            {
                "schema": 1,
                "git_sha": "a" * 40,
                "truncated": False,
                "entries": [
                    {
                        "date": date.today().isoformat(),
                        "subject": "feat(release): historique immuable",
                        "body": "Le journal suit le commit livré.",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(evolutions, "HISTORIQUE_RELEASE", historique)

    resultat = evolutions.EvolutionsTool().execute()

    assert resultat.success is True
    assert "historique immuable" in resultat.content


def test_un_historique_release_symbolique_ou_corrompu_est_refuse(
    _git, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git("", code=128)
    cible = tmp_path / "outside.json"
    cible.write_text("{}")
    lien = tmp_path / "evolutions-v1.json"
    lien.symlink_to(cible)
    monkeypatch.setattr(evolutions, "HISTORIQUE_RELEASE", lien)

    resultat = evolutions.EvolutionsTool().execute()

    assert resultat.success is False
    assert "n'arrive pas à lire" in resultat.content


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
    """⚠ Sans cette phrase, elle répondrait encore d'après sa persona figée.

    Ce serait le défaut d'origine, simplement déplacé d'un cran.
    """
    _git(_bloc("2026-08-06", "feat: une capacite"))
    r = evolutions.EvolutionsTool().execute()
    assert "c'est lui qui a raison" in r.content


def test_contre_le_VRAI_depot_git() -> None:
    """⚠ Les tests ci-dessus simulent git. Celui-ci l'appelle POUR DE VRAI sur le dépôt
    d'Ava : un format `--pretty` invalide ou un chemin faux ne se verrait pas autrement.

    ⚠ IL NE DOIT PAS DÉPENDRE DE L'HISTORIQUE — leçon payée le 2026-08-06 : écrit avec
      un `trouves >= 1` sec, il passait en local (clone complet) et échouait sur le
      runner, qui fait un clone SUPERFICIEL d'un seul commit. Un test vert chez soi et
      rouge en intégration est le pire des deux : il fait douter du code au lieu du
      test. On mesure donc d'abord ce que le dépôt CONTIENT, et on n'affirme que ce qui
      en découle. Le `skip` est explicite : mieux vaut un test franchement sauté qu'un
      test affaibli en silence jusqu'à ne plus rien vérifier.
    """
    r = evolutions.EvolutionsTool().execute(depuis="toujours", nombre=3)
    assert r.success is True, r.content  # git joignable, sortie exploitable

    brut = subprocess.run(  # noqa: S603
        ["git", "-C", str(evolutions.RACINE), "log", "--pretty=format:%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    eligibles = [
        ligne
        for ligne in brut.stdout.splitlines()
        if any(ligne.startswith(t) for t in evolutions._TYPES)
    ]
    if not eligibles:
        pytest.skip("clone sans commit de capacite (superficiel) — rien a analyser")
    assert r.metadata["trouves"] >= 1


# ══ La seconde moitié — l'angle mort trouvé en lui parlant ════════════════════════


def _infra(monkeypatch, valeur):
    monkeypatch.setattr(evolutions, "_cote_infra", lambda *_a, **_k: valeur)


def test_les_DEUX_sources_sont_fusionnees_et_ETIQUETEES(_git, monkeypatch) -> None:
    """⚠ L'ANGLE MORT TROUVÉ EN LUI PARLANT le 2026-08-06. Son journal ne lisait que SON
    dépôt, alors que la moitié de ce qui la change vit dans `infra_avalon` : ses outils
    côté control plane, le relais, sa veille, sa liste blanche. Interrogée sur ses
    propres échecs, elle a classé « inexpliqué » un refus dont la cause était un
    correctif livré une heure plus tôt dans l'autre dépôt.
    ⚠ L'étiquette compte autant que la fusion : « on m'a changée » et « on a changé mes
    outils » ne se diagnostiquent pas au même endroit."""
    _git(_bloc("2026-08-06", "feat(memoire): peremption"))
    _infra(monkeypatch, [("2026-08-06", "fix(logs): enum sur hote", "")])
    r = evolutions.EvolutionsTool().execute()
    assert "[moi] feat(memoire)" in r.content
    assert "[infrastructure] fix(logs)" in r.content
    assert r.metadata["depuis_moi"] == 1
    assert r.metadata["depuis_infra"] == 1


def test_une_vue_PARTIELLE_est_ANNONCEE(_git, monkeypatch) -> None:
    """⚠ LE CONTRE-TEST QUI PORTE TOUT. Ce correctif existe parce qu'une vue amputée
    SILENCIEUSE lui faisait conclure « inexpliqué » à tort. Reproduire ce défaut ici —
    en rendant la moitié locale sans dire que l'autre manque — serait exactement la même
    faute, une couche plus bas."""
    _git(_bloc("2026-08-06", "feat(memoire): peremption"))
    _infra(monkeypatch, None)
    r = evolutions.EvolutionsTool().execute()
    assert "n'ai PAS pu lire les changements côté infrastructure" in r.content
    assert r.metadata["infra_lue"] is False


def test_une_vue_COMPLETE_n_avertit_de_RIEN(_git, monkeypatch) -> None:
    """Un avertissement permanent est un avertissement qu'on cesse de lire."""
    _git(_bloc("2026-08-06", "feat(memoire): peremption"))
    _infra(monkeypatch, [])
    r = evolutions.EvolutionsTool().execute()
    assert "n'ai PAS pu lire" not in r.content
    assert r.metadata["infra_lue"] is True


def test_RIEN_des_deux_cotes_avec_infra_MUETTE_ne_conclut_PAS_a_l_absence(
    _git, monkeypatch
) -> None:
    """⚠ Le pire cas : aucun changement local ET l'infrastructure illisible. Répondre
    « aucun changement » serait affirmer ce qu'on n'a pas pu vérifier."""
    _git(_bloc("2026-08-06", "chore: rien qui la concerne"))
    _infra(monkeypatch, None)
    r = evolutions.EvolutionsTool().execute()
    assert "je ne peux donc pas dire que rien n'a changé" in r.content


def test_l_infra_SEULE_suffit_a_rendre_un_resultat(_git, monkeypatch) -> None:
    """Elle voit les changements d'outils même sans évolution de son dépôt."""
    _git(_bloc("2026-08-06", "chore: rien qui la concerne"))
    _infra(monkeypatch, [("2026-08-06", "feat(ava_app): nouvel outil", "")])
    r = evolutions.EvolutionsTool().execute()
    assert "[infrastructure] feat(ava_app)" in r.content
    assert r.metadata["depuis_moi"] == 0


# ══ La troisième amputation — trouvée par un agent d'évaluation le 2026-08-07 ═══════


def test_une_liste_TRONQUEE_le_DIT(_git, monkeypatch) -> None:
    """⚠ LE DÉFAUT MESURÉ. Interrogée sur les changements du 5 août, Ava a répondu « un
    seul changement » là où il y en avait des dizaines : elle rendait les N plus récents
    sans qu'un mot ne signale les autres. Ce fichier corrigeait déjà l'amputation
    silencieuse sur l'axe INFRASTRUCTURE, en écrivant qu'« il serait absurde de
    reproduire
    ce défaut ici » — et le reproduisait sur l'axe du NOMBRE, vingt lignes plus haut."""
    _git("".join(_bloc("2026-08-05", f"feat: capacite {i}") for i in range(40)))
    _infra(monkeypatch, [])
    r = evolutions.EvolutionsTool().execute(nombre=5)
    assert "au moins" in r.content
    assert "PAS la liste complète" in r.content
    assert r.metadata["tronque"] is True


def test_une_liste_COMPLETE_n_avertit_de_RIEN(_git, monkeypatch) -> None:
    """⚠ LE CONTRE-TEST QUI PORTE LE RISQUE DE LA CORRECTION. Un avertissement permanent
    est un avertissement qu'on cesse de lire — et il ferait douter d'une réponse
    exhaustive
    et juste. Le remède ne doit pas coûter la confiance dans le cas normal."""
    _git(_bloc("2026-08-06", "feat: une seule capacite"))
    _infra(monkeypatch, [])
    r = evolutions.EvolutionsTool().execute(nombre=10)
    assert "au moins" not in r.content
    assert r.metadata["tronque"] is False


def test_la_troncature_cote_INFRASTRUCTURE_compte_aussi(_git, monkeypatch) -> None:
    """⚠ Les deux sources peuvent déborder indépendamment. Ne compter que la sienne
    laisserait la moitié du défaut en place — c'est la faute d'origine, déplacée d'un
    cran
    pour la troisième fois."""
    _git(_bloc("2026-08-06", "feat: une capacite"))
    _infra(monkeypatch, [("2026-08-06", f"feat(cp): outil {i}", "") for i in range(30)])
    r = evolutions.EvolutionsTool().execute(nombre=3)
    assert r.metadata["tronque"] is True
    assert "au moins" in r.content


def test_le_total_annonce_n_est_JAMAIS_presente_comme_EXACT(_git, monkeypatch) -> None:
    """⚠ `git log` est lui-même borné en amont : on ne CONNAÎT pas le total. Annoncer un
    chiffre sec serait remplacer une omission par une affirmation fausse — le contraire
    d'une correction. « au moins N » est la seule formulation vraie."""
    _git("".join(_bloc("2026-08-05", f"feat: capacite {i}") for i in range(40)))
    _infra(monkeypatch, [])
    r = evolutions.EvolutionsTool().execute(nombre=5)
    assert "au moins" in r.content
    assert "exactement" not in r.content
