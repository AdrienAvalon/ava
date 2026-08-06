"""Tests de l'outil `camera` — ce qui empeche de croire le reste.

⚠ CE FICHIER N'EXISTAIT PAS. L'outil `camera` etait couvert par les invariants generaux de
  `test_skills.py` (il est charge, il est dans la liste blanche) et par rien d'autre : sa
  fonction `_sante()`, qui decide si Ava doit AVERTIR que « rien detecte » n'est pas une
  observation fiable, n'avait aucun test. C'est pourtant la seule partie du module dont
  une erreur produit une reponse rassurante et fausse.
"""

from __future__ import annotations

from ava_extensions.skills import camera

# ══ Le disque — collecté depuis toujours, jamais rendu ════════════════════════════


def test_un_disque_PRESQUE_PLEIN_est_SIGNALE() -> None:
    """⚠ MÊME FAMILLE QUE LE FLUX MORT, et il était TU. Le control plane publie
    `stockage_libre_pct` depuis toujours ; `_sante()` ne le regardait pas. Or un disque
    plein arrête l'enregistrement — à partir de là « rien détecté » cesse d'être une
    observation pour devenir un mensonge. Et rien d'autre ne le montre : le conteneur
    reste `healthy`, la caméra filme, la réponse est rassurante."""
    lignes = camera._sante({"camera_fps": 5.0, "stockage_libre_pct": 3.0})
    assert any("disque" in x.lower() for x in lignes)
    assert any("pu ne pas être enregistrés" in x for x in lignes)


def test_un_disque_SAIN_ne_dit_RIEN() -> None:
    """⚠ LE CONTRE-TEST. Un avertissement permanent est un avertissement qu'on cesse de
    lire — c'est ce qui a coûté l'alerte Aruba pendant des semaines."""
    assert camera._sante({"camera_fps": 5.0, "stockage_libre_pct": 49.6}) == []


def test_un_disque_BAS_est_mentionne_SANS_alarmer() -> None:
    lignes = camera._sante({"camera_fps": 5.0, "stockage_libre_pct": 15.0})
    assert lignes and "à surveiller" in lignes[0]
    assert not any("⚠" in x for x in lignes)


def test_un_stockage_ILLISIBLE_ne_leve_pas_et_ne_crie_pas() -> None:
    """Une valeur corrompue ne doit ni planter la réponse ni inventer une alerte."""
    assert camera._sante({"camera_fps": 5.0, "stockage_libre_pct": "inconnu"}) == []


def test_un_stockage_ABSENT_reste_silencieux() -> None:
    """Un control plane plus ancien n'envoie pas le champ : ne rien dire vaut mieux que
    supposer un disque plein."""
    assert camera._sante({"camera_fps": 5.0}) == []
