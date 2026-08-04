"""Tests du détecteur de chemins d'écriture HTTP.

⚠ CE FICHIER TESTE UN GARDE-FOU, ce qui est une catégorie à part : un garde-fou non
  testé se comporte exactement comme un garde-fou qui marche — il ne dit rien. C'est
  précisément ce qui s'est produit avec le `grep` qu'il remplace : il était en place,
  affiché « INVARIANT DE SÉCURITÉ », vert à chaque exécution, et **aveugle à la seule
  bibliothèque HTTP du fichier qu'il gardait**.

  D'où la forme de ces tests : chaque cas d'écriture est un extrait de code que
  **l'ancien grep laissait passer**, et qui doit désormais être détecté. Les
  contre-tests (`test_lecture_pure_*`) comptent autant : un détecteur qui refuse tout
  finirait désactivé à la première fausse alerte.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ⚠ Le détecteur arrive par la fixture `verificateur_lecture_seule` (conftest local) :
#   il est chargé PAR CHEMIN depuis `scripts/`, donc ces tests portent sur le fichier
#   que la CI exécute réellement — pas sur une copie qui pourrait dériver.


# ══ Les formes que l'ANCIEN GREP laissait passer ═════════════════════════════════
#
# ⚠ Chacun de ces extraits a été vérifié comme NON DÉTECTÉ par
#   `grep -nE '"POST"|requests\.post|method=.POST'`. Ce sont donc des écritures
#   réelles qui auraient traversé la CI en la laissant verte.


@pytest.mark.parametrize(
    ("libelle", "code"),
    [
        (
            "data= nommé sur Request (la forme idiomatique d'urllib)",
            "req = urllib.request.Request(url, data=json.dumps(charge).encode())",
        ),
        (
            "data en 2e POSITION — invisible à toute recherche du mot « data »",
            "req = urllib.request.Request(url, corps)",
        ),
        (
            "corps passé à urlopen plutôt qu'à Request",
            "urllib.request.urlopen(req, data=corps)",
        ),
        (
            "corps positionnel sur urlopen",
            "urllib.request.urlopen(req, corps, timeout=5)",
        ),
        (
            "alias d'import — le nom du module n'apparaît plus",
            "from urllib.request import Request as R\nr = R(url, data=c)",
        ),
        (
            "méthode posée APRÈS construction",
            'req = urllib.request.Request(url)\nreq.method = "PUT"',
        ),
        (
            "verbe en argument positionnel (http.client)",
            'conn.request("DELETE", "/api/states/light.salon")',
        ),
    ],
)
def test_une_ecriture_est_DETECTEE(
    libelle: str, code: str, verificateur_lecture_seule: Any
) -> None:
    """⚠ Le jeton HA autorise l'écriture : allumer, chauffer, ouvrir. Ava exécute du
    code communautaire depuis la DMZ. Tout l'arbitrage d'accès (le CP v2 dual-homé
    comme intermédiaire, plutôt qu'une règle DMZ → LAN) ne vaut que si cet outil reste
    incapable de commander quoi que ce soit."""
    assert verificateur_lecture_seule.analyser(code), f"NON détecté : {libelle}"


@pytest.mark.parametrize(
    "code",
    [
        "requests.post(url, json=charge)",
        "session.put(url, data=c)",
        "httpx.patch(url)",
        "client.delete(url)",
    ],
)
def test_les_bibliotheques_de_commodite_sont_detectees(
    code: str, verificateur_lecture_seule: Any
) -> None:
    """Ces formes-là, le grep en voyait UNE (`requests.post`). Les trois autres non —
    et rien n'empêche une future contribution d'ajouter `httpx`."""
    assert verificateur_lecture_seule.analyser(code)


# ══ Contre-tests — un détecteur qui refuse tout serait désactivé ═════════════════


@pytest.mark.parametrize(
    "code",
    [
        "urllib.request.Request(url, headers=entetes)",
        "urllib.request.urlopen(req, timeout=5)",
        "with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r: pass",
        "requests.get(url, timeout=5)",
        "session.head(url)",
        "reponse = json.loads(r.read().decode())",
    ],
)
def test_lecture_pure_NON_signalee(code: str, verificateur_lecture_seule: Any) -> None:
    """⚠ AUSSI IMPORTANT QUE LE RESTE. Un garde-fou qui crie sur du code légitime finit
    contourné — commenté « temporairement », puis oublié. `urlopen(req, timeout=…)` est
    la forme employée par le fichier réel : la signaler serait fatal à l'invariant."""
    assert verificateur_lecture_seule.analyser(code) == []


def test_le_fichier_home_assistant_REEL_est_en_lecture_seule(
    verificateur_lecture_seule: Any,
) -> None:
    """L'invariant lui-même, appliqué à la source de production.

    ⚠ C'est le test qui doit rougir le jour où quelqu'un ajoute un chemin d'écriture.
    Il est volontairement séparé des cas synthétiques ci-dessus : ceux-là valident le
    DÉTECTEUR, celui-ci valide le CODE.
    """
    source = (
        Path(__file__).resolve().parents[1] / "skills" / "home_assistant.py"
    ).read_text(encoding="utf-8")
    constats = verificateur_lecture_seule.analyser(source)
    assert constats == [], f"chemin(s) d'écriture dans home_assistant.py : {constats}"


def test_le_detecteur_rend_la_ligne_du_constat(verificateur_lecture_seule: Any) -> None:
    """Un constat sans numéro de ligne oblige à relire tout le fichier — sur un échec
    de CI, c'est la différence entre corriger et abandonner."""
    code = "a = 1\nb = 2\nreq = urllib.request.Request(url, data=c)\n"
    constats = verificateur_lecture_seule.analyser(code)
    assert constats and constats[0][0] == 3
