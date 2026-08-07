"""Perte de mise à jour concurrente sur la SEULE capacité d'écriture d'Ava.

⚠ DÉFAUT MESURÉ EN PRODUCTION LE 2026-08-07, pas imaginé. Deux `mark_stale` émis dans
  la même seconde ont TOUS DEUX rendu `True` et TOUS DEUX journalisé « fait perime » —
  un seul a survécu sur le disque. Ava a donc annoncé de bonne foi « les deux sont
  périmés », et c'était faux : un succès rapporté sans effet.

⚠ POURQUOI LE VERROU EN MÉMOIRE NE SUFFISAIT PAS, et pourquoi la relecture ne le
  montrait pas : `LocalFactStore` possède bien un `self._lock` qui fonctionne. Mais
  l'appelant construit une instance NEUVE à chaque appel — donc deux verrous distincts
  qui ne se voient pas. Et la CLI (`openjarvis memory revive`) écrit depuis un AUTRE
  processus, où aucun verrou mémoire ne peut porter.

Mesure de la contre-preuve, 8 marquages simultanés, trois essais :
    avec verrou  : 8/8, 8/8, 8/8
    sans verrou  : 2/8, 3/8, 2/8   (+ des FileNotFoundError, voir ci-dessous)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from openjarvis.memory.store import LocalFactStore

_N = 8


def _semer(dossier: Path) -> Path:
    chemin = dossier / "faits.jsonl"
    chemin.write_text(
        "".join(
            json.dumps(
                {
                    "text": f"fait numero {i}",
                    "source": "auto",
                    "created_at": 0.0,
                    "perime_le": 0.0,
                    "perime_par": "",
                },
                ensure_ascii=False,
            )
            + "\n"
            for i in range(_N)
        ),
        encoding="utf-8",
    )
    return chemin


def _marquer_tous(chemin: Path) -> int:
    """Lance _N marquages EXACTEMENT simultanés et rend le nombre de survivants."""
    barriere = threading.Barrier(_N)

    def travail(i: int) -> None:
        barriere.wait()  # la barrière est ce qui rend la course reproductible
        LocalFactStore(chemin).mark_stale(
            f"fait numero {i}", "raison de test suffisamment longue"
        )

    fils = [threading.Thread(target=travail, args=(i,)) for i in range(_N)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    return sum(
        1
        for L in chemin.read_text(encoding="utf-8").splitlines()
        if json.loads(L).get("perime_le")
    )


def test_AUCUN_marquage_simultane_n_est_PERDU(tmp_path: Path) -> None:
    """⚠ LE TEST QUI PORTE LE DÉFAUT. Sans le verrou fichier, 2 à 3 marquages sur 8
    survivaient — les autres étaient écrasés par le dernier écrivain, en silence et avec
    un `True` rendu à l'appelant."""
    assert _marquer_tous(_semer(tmp_path)) == _N


def test_le_fichier_TEMPORAIRE_ne_se_marche_pas_dessus(tmp_path: Path) -> None:
    """⚠ SECOND DÉFAUT RÉVÉLÉ PAR LA MÊME COURSE, et il est plus brutal que la perte :
    `_flush` écrit toujours dans le MÊME `.tmp` avant son `os.replace`. Deux écrivains
    simultanés → l'un renomme le fichier pendant que l'autre s'apprête à le renommer →
    `FileNotFoundError` remontée jusqu'à l'appelant. Le verrou sérialise le cycle
    complet, donc il ferme les deux d'un coup."""
    chemin = _semer(tmp_path)
    _marquer_tous(chemin)
    assert not (tmp_path / "faits.jsonl.tmp").exists(), (
        "un temporaire est resté sur le carreau"
    )
    # Le fichier reste lisible et complet : ni tronqué, ni corrompu.
    lignes = chemin.read_text(encoding="utf-8").splitlines()
    assert len(lignes) == _N
    for ligne in lignes:
        json.loads(ligne)
