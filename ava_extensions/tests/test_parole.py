"""Tests de la parole d'Ava — et surtout de son silence.

⚠ CE FICHIER TESTE MAJORITAIREMENT DES NON-EMISSIONS, et c'est la mesure de reussite du
  module. Decider d'emettre est trivial ; decider de se TAIRE est ce qui separe un
  organe d'une source de bruit. Avalon a paye cette lecon plusieurs fois — le digest
  qui criait chaque matin sur une sonde au DP batterie fige, l'alerte TLS vue trois fois
  dans la meme soiree. Le signal etait juste ; sa repetition l'a rendu invisible.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from ava_extensions.perception import parole
from ava_extensions.perception.qualification import Changement, Niveau


@pytest.fixture(autouse=True)
def _isole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Un jeton present et un transport simule : on teste la DECISION, pas le reseau."""
    jeton = tmp_path / "cp_voice_token"
    jeton.write_text("secret-de-parole\n")
    monkeypatch.setattr(parole, "CHEMIN_JETON", jeton)
    parole.reinitialiser()
    envoyes: list[str] = []
    monkeypatch.setattr(
        parole, "_emettre", lambda texte: (envoyes.append(texte), True)[1]
    )
    return envoyes


@pytest.fixture
def _vrai_emettre(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Restaure le VRAI `_emettre`, que la fixture `_isole` remplace.

    ⚠ Sans cela, les tests de robustesse mesureraient le simulacre au lieu du transport
      reel — ils passeraient toujours, y compris si `_emettre` levait sur un jeton
      absent. Un test qui ne peut pas echouer ne prouve rien.
    """
    monkeypatch.setattr(parole, "_emettre", parole._emettre_reel)
    return parole._emettre_reel


def _fait(sujet: str, niveau: Niveau = Niveau.INTERRUPT) -> Changement:
    return Changement(sujet, f"probleme sur {sujet}", niveau, "maison")


# ══ Ce qu'Ava dit ═══════════════════════════════════════════════════════════════


def test_un_fait_INTERRUPT_est_dit(_isole: list[str]) -> None:
    assert parole.dire([_fait("Ballon eau chaude")]) == 1
    assert _isole == ["probleme sur Ballon eau chaude"]


# ══ Ce qu'Ava TAIT — l'essentiel ════════════════════════════════════════════════


@pytest.mark.parametrize("niveau", [Niveau.MEMOIRE, Niveau.NOTABLE])
def test_les_faits_NON_interruptifs_sont_TUS(_isole: list[str], niveau: Niveau) -> None:
    """⚠ Les temperatures, les lumieres et la presence sont percues et memorisees,
    jamais annoncees. Sans cette regle Ava parlerait plusieurs fois par heure — et on
    la couperait au bout d'une journee, y compris le jour ou elle aurait raison."""
    assert parole.dire([_fait("Salon", niveau)]) == 0
    assert _isole == []


def test_le_MEME_sujet_n_est_pas_redit_tout_de_suite(_isole: list[str]) -> None:
    """⚠ LE CONTRE-TEST CENTRAL. Une entite muette le reste des heures durant, et la
    perception relit toutes les 5 minutes : sans anti-repetition, Ava enverrait
    12 messages par heure sur le meme sujet. C'est exactement le digest des piles."""
    assert parole.dire([_fait("Ballon")]) == 1
    assert parole.dire([_fait("Ballon")]) == 0
    assert len(_isole) == 1


def test_le_meme_sujet_est_redit_APRES_le_delai(
    _isole: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Et le contre-test du contre-test : un silence definitif serait pire que la
    repetition. Une panne qui dure doit revenir a l'esprit."""
    assert parole.dire([_fait("Ballon")]) == 1
    vrai_temps = time.time
    monkeypatch.setattr(
        parole.time, "time", lambda: vrai_temps() + parole.REPETITION_S + 1
    )
    assert parole.dire([_fait("Ballon")]) == 1


def test_des_sujets_DIFFERENTS_ne_se_bloquent_pas(_isole: list[str]) -> None:
    """L'anti-repetition est par SUJET : deux pannes distinctes doivent toutes deux
    etre dites, sinon la seconde serait masquee par la premiere."""
    assert parole.dire([_fait("Ballon"), _fait("Disjoncteur")]) == 2


def test_le_plafond_horaire_arrete_une_CASCADE(_isole: list[str]) -> None:
    """⚠ Une panne en cascade produit des dizaines de faits. Les envoyer tous
    transformerait #ops en journal d'application — ce qu'il n'est pas, et ce qui ferait
    perdre les alertes du control plane dans le flot."""
    faits = [_fait(f"appareil-{i}") for i in range(parole.PLAFOND_HORAIRE + 5)]
    emis = parole.dire(faits)
    assert emis == parole.PLAFOND_HORAIRE
    assert len(_isole) == parole.PLAFOND_HORAIRE


def test_une_liste_VIDE_ne_declenche_rien(_isole: list[str]) -> None:
    assert parole.dire([]) == 0


# ══ Robustesse — Ava ne doit jamais tomber a cause de sa parole ═════════════════


def test_un_JETON_ABSENT_rend_muet_sans_lever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _vrai_emettre: Any
) -> None:
    """⚠ Le montage est fail-closed cote CP ; cote Ava il doit etre silencieux mais
    VISIBLE. Le journal part vers Loki depuis ce matin : une Ava muette parce qu'un
    secret manque doit pouvoir se diagnostiquer sans lire le code."""
    monkeypatch.setattr(parole, "CHEMIN_JETON", tmp_path / "absent")
    parole.reinitialiser()
    assert parole._emettre("bonjour") is False


def test_un_CP_INJOIGNABLE_ne_leve_pas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _vrai_emettre: Any
) -> None:
    """Le control plane est recree environ deux fois par jour. Une parole ratee ne doit
    jamais faire tomber la perception qui l'a declenchee."""
    jeton = tmp_path / "j"
    jeton.write_text("x")
    monkeypatch.setattr(parole, "CHEMIN_JETON", jeton)

    def _boum(*a: Any, **k: Any) -> Any:
        raise OSError("connexion refusee")

    monkeypatch.setattr(parole.urllib.request, "urlopen", _boum)
    assert parole._emettre("bonjour") is False


def test_un_echec_d_emission_ne_CONSOMME_pas_l_anti_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ SUBTIL ET IMPORTANT. Si un envoi rate marquait quand meme le sujet comme
    « deja dit », une panne survenue pendant une indisponibilite du control plane
    resterait tue pendant deux heures — precisement quand on en a le plus besoin."""
    jeton = tmp_path / "j"
    jeton.write_text("x")
    monkeypatch.setattr(parole, "CHEMIN_JETON", jeton)
    parole.reinitialiser()
    monkeypatch.setattr(parole, "_emettre", lambda _t: False)
    assert parole.dire([_fait("Ballon")]) == 0

    envoyes: list[str] = []
    monkeypatch.setattr(parole, "_emettre", lambda t: (envoyes.append(t), True)[1])
    assert parole.dire([_fait("Ballon")]) == 1, "le sujet a ete bloque par un echec"


def test_le_jeton_ne_passe_PAS_par_l_environnement() -> None:
    """⚠ Un secret en variable d'environnement se retrouve dans `/proc/<pid>/environ`,
    dans les dumps de plantage et dans les journaux de demarrage. Sur cette
    infrastructure les secrets sont des FICHIERS — c'est la meme raison qui a fait
    corriger le push GitHub le 2026-07-25 (jeton dans argv)."""
    source = Path(parole.__file__).read_text(encoding="utf-8")
    # ⚠ On cherche le NOM de la variable, pas une forme d'appel : le formateur peut
    #   repartir l'expression sur plusieurs lignes, et un test qui casse au reformatage
    #   finit par etre supprime plutot que compris.
    assert "AVA_VOICE_TOKEN_FILE" in source, (
        "le chemin du jeton n'est plus configurable"
    )
    assert '"AVA_VOICE_TOKEN"' not in source, (
        "le jeton lui-meme viendrait de l'environnement"
    )
    assert "CHEMIN_JETON.read_text()" in source


def test_le_jeton_n_apparait_JAMAIS_dans_un_journal() -> None:
    """⚠ Un jeton journalise est un jeton compromis : Loki garde 180 jours, et le
    journal d'Ava y monte depuis ce matin."""
    source = Path(parole.__file__).read_text(encoding="utf-8")
    for ligne in source.splitlines():
        if "logger." in ligne:
            assert "jeton)" not in ligne and "_jeton()" not in ligne, ligne
