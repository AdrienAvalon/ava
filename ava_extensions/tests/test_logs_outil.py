"""Tests de l'outil `logs` — l'acces d'Ava aux journaux, par le control plane.

⚠ CE QUI EST EN JEU. Loki n'ecoute que sur `127.0.0.1` et son proxy DMZ est write-only
  depuis le 2026-07-30, a la suite d'un incident reel (mot de passe Keycloak lisible
  dans `auth.log` depuis une VM web compromise). Cet outil donne a Ava une lecture — il
  n'a de valeur que si son perimetre tient.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def outil(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import openjarvis.core.registry as reg

    original = reg.ToolRegistry.register
    reg.ToolRegistry.register = staticmethod(lambda cle: lambda c: c)  # type: ignore[assignment]
    try:
        chemin = Path(__file__).resolve().parents[1] / "skills" / "logs.py"
        spec = importlib.util.spec_from_file_location("_test_logs", chemin)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        reg.ToolRegistry.register = original  # type: ignore[assignment]
    jeton = tmp_path / "j"
    jeton.write_text("secret")
    monkeypatch.setattr(m, "CHEMIN_JETON", jeton)
    return m


def _reponse(m: Any, monkeypatch: pytest.MonkeyPatch, charge: dict) -> None:
    class _R:
        def read(self) -> bytes:
            return json.dumps(charge).encode()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _R())


def test_une_question_HORS_CATALOGUE_est_refusee_localement(outil: Any) -> None:
    """⚠ On refuse AVANT l'appel reseau : inutile de deranger le control plane pour une
    valeur que le modele a inventee, et le message lui dit quoi demander."""
    r = outil.LogsTool().execute(question="tout ce que tu as")
    assert r.success is False
    assert "redemarrages" in r.content


def test_le_modele_ne_peut_composer_AUCUNE_requete(outil: Any) -> None:
    """⚠ L'INVARIANT CENTRAL. Preuve empirique du besoin : « combien de fois le
    disjoncteur est tombe » pose en LogQL libre renvoie 310 occurrences qui sont TOUTES
    des erreurs de connectivite `tuya_local` — plausible, alarmant, entierement faux."""
    props = outil.LogsTool().spec.parameters["properties"]
    assert set(props) <= {"question", "fenetre", "hote"}
    assert props["question"]["enum"] == list(outil.QUESTIONS)
    source = Path(outil.__file__).read_text(encoding="utf-8")
    for interdit in ("logql", "|~", '|= "'):
        assert interdit not in source.split('"""', 2)[2], (
            f"{interdit} present dans le code"
        )


def test_les_lignes_sont_rendues_quand_la_source_l_autorise(
    outil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reponse(
        outil,
        monkeypatch,
        {
            "libelle": "services redémarrés",
            "fenetre": "24h",
            "occurrences": 2,
            "lignes": [{"horodatage": 1785860000, "texte": "Started docker.service"}],
        },
    )
    r = outil.LogsTool().execute(question="redemarrages")
    assert r.success is True and "docker.service" in r.content


def test_une_source_SENSIBLE_ne_rend_qu_un_comptage(
    outil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ `authlog` porte les IP sources et les noms d'utilisateurs. Ava doit pouvoir
    dire « 12 echecs cette nuit » sans jamais lire une ligne."""
    _reponse(
        outil,
        monkeypatch,
        {
            "libelle": "tentatives SSH échouées",
            "fenetre": "24h",
            "occurrences": 12,
            "detail": "comptage seul — cette source porte des données personnelles",
        },
    )
    r = outil.LogsTool().execute(question="echecs_ssh")
    assert "12" in r.content and "comptage seul" in r.content


def test_un_resultat_TRONQUE_le_dit(
    outil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Sans cette nuance, « 200 » se lirait comme un total exact et l'on croirait
    avoir tout vu. C'est un PLANCHER, pas un compte."""
    _reponse(
        outil,
        monkeypatch,
        {
            "libelle": "erreurs",
            "fenetre": "24h",
            "occurrences": 200,
            "tronque": True,
            "lignes": [{"horodatage": 1785860000, "texte": "ERROR x"}],
        },
    )
    assert (
        "au moins 200" in outil.LogsTool().execute(question="erreurs_services").content
    )


def test_un_refus_du_CP_est_dit_avec_sa_raison(
    outil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reponse(
        outil, monkeypatch, {"erreur": "fenetre trop large", "volume_estime_mo": 4200}
    )
    r = outil.LogsTool().execute(question="erreurs_services", fenetre="30j")
    assert r.success is False and "4200 Mo" in r.content


def test_un_CP_INJOIGNABLE_ne_leve_pas(
    outil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boum(*a: Any, **k: Any) -> Any:
        raise OSError("refuse")

    monkeypatch.setattr(outil.urllib.request, "urlopen", _boum)
    r = outil.LogsTool().execute(question="redemarrages")
    assert r.success is False and "control plane" in r.content.lower()


def test_le_JETON_absent_rend_muet_sans_lever(
    outil: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outil, "CHEMIN_JETON", tmp_path / "absent")
    r = outil.LogsTool().execute(question="redemarrages")
    assert r.success is False and "jeton" in r.content.lower()


def test_le_vocabulaire_est_le_MEME_que_celui_du_control_plane() -> None:
    """⚠ Deux vocabulaires qui divergent produiraient des questions systematiquement
    refusees — et le modele, lui, croirait avoir bien demande."""
    cp = Path.home() / "Documents/projets/infra_avalon/control-plane-v2/core/logs.py"
    if not cp.exists():
        pytest.skip("depot infra_avalon absent")
    import re

    cles_cp = set(re.findall(r'cle="([a-z_]+)"', cp.read_text(encoding="utf-8")))
    ava = Path(__file__).resolve().parents[1] / "skills" / "logs.py"
    bloc = re.search(
        r"QUESTIONS = \((.*?)\)", ava.read_text(encoding="utf-8"), re.S
    ).group(1)
    cles_ava = set(re.findall(r'"([a-z_]+)"', bloc))
    assert cles_ava == cles_cp, f"divergence : {cles_ava ^ cles_cp}"


# ══ Le relais — douzième « collecté mais non relayé » ═════════════════════════════


def test_la_REPARTITION_atteint_le_MODELE_et_pas_seulement_metadata(outil: Any) -> None:
    """⚠ LE DÉFAUT LE PLUS INSTRUCTIF DE LA SÉRIE. Le control plane calculait la
    répartition, elle était livrée, déployée, testée de son côté — et elle finissait
    dans `metadata`, que le modèle ne lit pas.

    Mesuré le 2026-08-07 : après déploiement du correctif côté CP, Ava a redonné
    EXACTEMENT le même classement faux qu'avant. Une correction qui n'atteint pas son
    consommateur se lit comme une correction qui ne marche pas — et on va la chercher
    au mauvais endroit, c'est-à-dire dans le code qu'on vient de réparer.
    """
    rendu = outil._repartition_rendue(
        {
            "repartition": [
                {"nom": "systemd-timedated", "occurrences": 300},
                {"nom": "lynis", "occurrences": 12},
            ]
        }
    )
    assert "systemd-timedated : 300" in rendu
    assert "lynis : 12" in rendu


def test_une_repartition_ABSENTE_ne_rend_RIEN(outil: Any) -> None:
    """⚠ Un en-tête « Répartition : » suivi du vide apprend au modèle à ignorer la
    section — et il l'ignorera aussi le jour où elle portera quelque chose."""
    assert outil._repartition_rendue({}) == ""
    assert outil._repartition_rendue({"repartition": []}) == ""


def test_la_TRONCATURE_de_la_repartition_est_ANNONCEE(outil: Any) -> None:
    """⚠ Remplacer une extrapolation fausse par une vue amputée silencieuse serait le
    même défaut déplacé d'un cran."""
    rendu = outil._repartition_rendue(
        {
            "repartition": [{"nom": "a", "occurrences": 1}],
            "repartition_tronquee": "les 8 premiers seulement",
        }
    )
    assert "les 8 premiers seulement" in rendu
