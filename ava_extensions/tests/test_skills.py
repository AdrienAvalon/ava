"""Tests des outils d'Ava — la couche par laquelle elle percoit Avalon et la maison.

⚠ POURQUOI CE FICHIER EXISTE. `ava_extensions/` comptait **908 lignes et zero test**.
  Le cout a ete paye le meme jour : l'outil `home_assistant`, ecrit le 2026-08-03,
  lisait `dash["modules"]` — une cle qui **n'existe pas** dans la reponse du control
  plane, qui expose `module_data` / `module_health`. `.get()` rendait un dictionnaire
  vide, et l'outil repondait « le control plane ne publie aucun module home_assistant »
  sur un module qui tournait parfaitement.

  Le message d'erreur etait clair, actionnable — et faux. J'ai moi-meme conclu deux fois
  « pas encore deploye » en le lisant. C'est la signature du defaut recurrent de ce
  projet : *une source qui ne porte pas la donnee repond « rien » sans erreur.*

  Un test de trois lignes l'aurait attrape avant la livraison. Les voici.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest

RACINE = Path(__file__).resolve().parents[1]


def _charger(nom: str) -> Any:
    """Charge un outil SANS passer par le registre.

    ⚠ Un import direct echouerait : `@ToolRegistry.register` leve « already has an entry »
      si le daemon a deja charge le module. On neutralise donc le decorateur le temps du
      chargement — la classe reste intacte, seul l'enregistrement est saute.
    """
    import openjarvis.core.registry as reg

    original = reg.ToolRegistry.register
    reg.ToolRegistry.register = staticmethod(lambda cle: lambda c: c)  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location(
            f"_test_{nom}", RACINE / "skills" / f"{nom}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        reg.ToolRegistry.register = original  # type: ignore[assignment]


# ── La forme REELLE de /api/v1/dashboard, mesuree le 2026-08-03 ────────────────────
# ⚠ Ces cles sont la source de verite du test. Elles ont ete relevees sur l'API de
#   production, pas supposees — c'est precisement la supposition qui a produit le bug.
DASHBOARD = {
    "version": "2.0.39",
    "score": {"global": 98, "max": 100, "status": "ok"},
    "module_health": {"home_assistant": "ok", "prometheus": "ok"},
    "module_data": {
        "home_assistant": {
            "_health": "ok",
            "entites_totales": 658,
            "a_la_maison": 4,
            "presence": {"Adrien": "présent", "Aurélie": "absent"},
            "temperatures": {"exterieur": 28.6, "grange": 25.8, "parents": 22.1},
            "pieces": {"Salon": 25.1, "Chambre": 27.4},
            "chauffage": {
                "Radiateur salon": {
                    "mode": "hors-gel",
                    "chauffe": False,
                    "ouverture": 0,
                },
            },
            "energie": {"baie_serveur_kw": 0.738, "baie_temperature_c": 25.0},
            "maison": {
                "Lumière salon": "éteint",
                "Ballon eau chaude": "31 °C",
                "Détection véhicule (parking)": "aucune détection",
            },
            "camera_parking": {
                "moment": "jour",
                "carte_sd_pourcent": 95.22,
                "carte_sd": "occupée à 95 % (enregistrement en boucle, normal)",
                "sensibilite_ia": {"personne": 70.0, "véhicule": 50.0, "animal": 50.0},
            },
            "piles_faibles": [{"nom": "Sonde salon", "niveau": 12.0}],
            "entites_absentes": [],
            "meteo": "orage",
            "meteo_temperature": 28.6,
        }
    },
}


# ══ home_assistant ═════════════════════════════════════════════════════════════════


@pytest.fixture
def ha(monkeypatch: pytest.MonkeyPatch) -> Any:
    m = _charger("home_assistant")
    monkeypatch.setattr(m, "_dashboard", lambda: DASHBOARD)
    return m


def test_ha_lit_les_bonnes_cles_du_dashboard(ha: Any) -> None:
    """⚠ LE TEST QUI AURAIT EVITE LE BUG DU 2026-08-03.

    L'outil cherchait `dash["modules"]`. Cette cle n'existe pas : le dashboard expose
    `module_health` (un dict de CHAINES) et `module_data`. Avec le mauvais nom, `.get()`
    rend `{}` et l'outil annonce « module non deploye » sur un module parfaitement sain.
    """
    r = ha.HomeAssistantTool().execute()
    assert r.success is True, r.content
    assert "4 personne" in r.content


def test_ha_module_absent_donne_un_message_actionnable(
    ha: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le CP repond mais ne publie pas le module : cause DISTINCTE d'un reseau coupe."""
    monkeypatch.setattr(
        ha, "_dashboard", lambda: {"module_health": {}, "module_data": {}}
    )
    r = ha.HomeAssistantTool().execute()
    assert r.success is False
    assert "enabled: false" in r.content or "non déployé" in r.content


def test_ha_module_degrade_reprend_le_motif_du_cp(
    ha: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ha,
        "_dashboard",
        lambda: {
            "module_health": {"home_assistant": "degraded"},
            "module_data": {
                "home_assistant": {
                    "_health": "degraded",
                    "_error": "jeton refusé (401)",
                }
            },
        },
    )
    r = ha.HomeAssistantTool().execute()
    assert r.success is False
    assert "401" in r.content


def test_ha_cp_injoignable_ne_leve_pas(
    ha: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Et le message doit dire de regarder le CP, PAS Home Assistant — la donnee
    transite par lui depuis qu'on a supprime la regle pare-feu DMZ → LAN."""
    import urllib.error

    def _boum() -> None:
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(ha, "_dashboard", _boum)
    r = ha.HomeAssistantTool().execute()
    assert r.success is False
    assert "control plane" in r.content.lower()


def test_ha_les_nombres_sont_en_virgule_decimale(ha: Any) -> None:
    """⚠ Un moteur vocal français lit « 25.4 » « vingt-cinq POINT quatre »."""
    r = ha.HomeAssistantTool().execute(domaine="climat")
    assert "28,6 °C" in r.content
    assert "28.6" not in r.content


def test_ha_dit_si_le_chauffage_TOURNE_pas_seulement_son_mode(ha: Any) -> None:
    """`chauffe` repond a la vraie question. Une vanne en « confort » ayant atteint sa
    consigne ne chauffe pas : ne publier que le mode laisserait croire l'inverse."""
    r = ha.HomeAssistantTool().execute(domaine="chauffage")
    assert "hors-gel" in r.content
    assert "chauffe actuellement" not in r.content  # chauffe: False


def test_ha_un_domaine_filtre_vraiment(ha: Any) -> None:
    r = ha.HomeAssistantTool().execute(domaine="presence")
    assert "Adrien" in r.content
    assert "Baie serveur" not in r.content


def test_ha_les_piles_faibles_remontent(ha: Any) -> None:
    """Une pile a changer est une ACTION : elle doit etre dite, pas noyee."""
    r = ha.HomeAssistantTool().execute()
    assert "Sonde salon" in r.content


def test_ha_ne_fabrique_pas_de_valeur_absente(
    ha: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Le coeur du contrat : une mesure absente DISPARAIT, elle ne vaut pas 0.

    Un zero invente se propage et le modele l'affirme a l'oral avec assurance.
    """
    creux = {
        "module_health": {"home_assistant": "ok"},
        "module_data": {
            "home_assistant": {"_health": "ok", "temperatures": {"exterieur": None}}
        },
    }
    monkeypatch.setattr(ha, "_dashboard", lambda: creux)
    r = ha.HomeAssistantTool().execute(domaine="climat")
    assert "0" not in r.content.replace("°C", "")


def test_ha_est_en_lecture_seule(verificateur_lecture_seule: Any) -> None:
    """⚠ INVARIANT DE SECURITE. Le jeton HA autorise l'ECRITURE (allumer, chauffer,
    ouvrir). Cet outil ne doit exposer AUCUN chemin d'ecriture : Ava execute du code
    tiers, et une IA qui interprete de travers une demande ambigue commanderait le
    chauffage d'une maison habitee.

    ⚠ CE TEST CHERCHAIT QUATRE LITTERAUX JUSQU'AU 2026-08-04 — `requests.post`,
      `urlopen(req, data=`, `"POST"`, `method='POST'` — et **aucun ne couvrait la seule
      bibliotheque HTTP du fichier**. `home_assistant.py` fait exclusivement du
      `urllib`, ou `Request(url, data=…)` bascule en POST sans qu'aucun verbe
      n'apparaisse : le test etait vert par construction, quoi qu'on ajoute.
      Il delegue desormais a `scripts/verifier-lecture-seule.py` (analyse AST, alias
      d'import resolus), qui est le MEME detecteur que la CI — pour qu'il n'existe pas
      deux definitions de l'invariant qui divergent. Elles divergeaient deja : le grep
      existait en double, et les deux copies etaient aveugles de la meme facon.
      Le detecteur est lui-meme couvert par `test_lecture_seule.py`.
    """
    source = (RACINE / "skills" / "home_assistant.py").read_text()
    constats = verificateur_lecture_seule.analyser(source)
    assert constats == [], f"chemin(s) d'ecriture dans home_assistant.py : {constats}"


def _code_sans_commentaires(chemin: Path) -> str:
    """Le CODE seul — commentaires et docstrings retires.

    ⚠ Necessaire, et la premiere version de ce test s'y est cassee : chercher un motif
      dans le fichier BRUT le trouve dans les commentaires qui expliquent pourquoi on
      ne l'emploie PAS. Un test qui echoue sur sa propre documentation est pire
      qu'inutile — il pousse a supprimer l'explication pour faire passer le test.
    """
    import ast

    arbre = ast.parse(chemin.read_text())
    for noeud in ast.walk(arbre):
        if isinstance(
            noeud, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(noeud, clean=False)
            if doc and noeud.body and isinstance(noeud.body[0], ast.Expr):
                noeud.body.pop(0)  # retire la docstring
    return ast.unparse(arbre)


def test_ha_vise_l_endpoint_interne_pas_le_hostname_public() -> None:
    """⚠ `control.avalon-network.com` est derriere Cloudflare Access : une requete
    serveur s'y ferait rediriger vers un portail de connexion et echouerait sans dire
    pourquoi. Meme piege que l'endpoint GitLab interne de Renovate."""
    code = _code_sans_commentaires(RACINE / "skills" / "home_assistant.py")
    assert "192.168.100.31:8100" in code
    assert "control.avalon-network.com" not in code


# ══ avalon_status ══════════════════════════════════════════════════════════════════


def test_avalon_status_ne_confond_pas_les_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ DEUX ENDPOINTS, ET C'EST STRUCTUREL. `/dashboard` porte le score et les
    modules ; il ne porte NI `alerts` NI `hosts`. Les y chercher rendait « 0 alerte »
    pendant qu'une alerte tirait — un mensonge tranquille, c'est-a-dire le pire.
    """
    m = _charger("avalon_status")
    appels: list[str] = []

    def _get(chemin: str) -> Any:
        appels.append(chemin)
        if chemin == "/dashboard":
            return DASHBOARD
        if chemin == "/hosts":
            return [{"id": "ava", "active": True}]
        return {}

    monkeypatch.setattr(m, "_get", _get)
    r = m.AvalonStatusTool().execute()
    assert r.success is True
    assert "/hosts" in appels, "les hotes doivent venir de /hosts, jamais de /dashboard"
    assert "98/100" in r.content


# ══ boot.py — l'isolation des groupes ══════════════════════════════════════════════


def test_un_groupe_casse_n_emporte_pas_les_autres(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠ LE DEFAUT STRUCTUREL CORRIGE LE 2026-08-04.

    `openjarvis/__init__.py` importe `ava_extensions.boot` dans un
    `try: ... except ImportError: pass`. Avec des imports NUS au niveau du module, un
    seul echec faisait donc disparaitre TOUT `ava_extensions` — STT, outils, TTS — sans
    un mot. Ce n'est pas theorique : le 2026-08-03, un `uv sync` aux extras incomplets a
    retire le SDK `anthropic`, precisement ce qu'importe `patches/`.
    Il aurait suffi que le moteur demarre par ailleurs pour qu'Ava reponde normalement,
    **sans aucun de ses outils**. Une panne qui ressemble a un fonctionnement normal est
    la pire de toutes.
    """
    from ava_extensions import boot

    appels: list[str] = []

    def _casse() -> None:
        raise ModuleNotFoundError("No module named 'anthropic'")

    def _sain() -> None:
        appels.append("charge")

    with caplog.at_level("WARNING"):
        boot._charger("groupe casse", _casse)
        boot._charger("groupe sain", _sain)

    assert appels == ["charge"], "un groupe en echec a empeche le suivant de se charger"
    # ⚠ L'echec doit LAISSER UNE TRACE : une extension qui disparait sans journal est
    #   indistinguable d'une extension qui n'a jamais existe.
    assert "groupe casse" in caplog.text
    assert "anthropic" in caplog.text


def test_charger_attrape_AUSSI_les_erreurs_d_execution() -> None:
    """⚠ Pas seulement `ImportError` : un module d'extension peut echouer a l'execution
    de son propre corps (constante mal formee, fichier de configuration absent). Le
    resultat serait identique — tout le reste perdu — pour une cause qui n'est pas un
    import manquant.
    """
    from ava_extensions import boot

    def _explose() -> None:
        raise ValueError("configuration illisible")

    boot._charger("groupe qui explose", _explose)  # ne doit pas lever


# ══ Enregistrement dans le registre — transpose du STT le 2026-08-04 ══════════════


@pytest.mark.parametrize(
    "cle", ["avalon_status", "home_assistant", "memoire", "journal", "logs"]
)
def test_l_outil_est_REELLEMENT_enregistre(cle: str) -> None:
    """⚠ AUCUN TEST NE VERIFIAIT CECI, et c'est le seul qui attraperait la panne.

    Tous les tests ci-dessus chargent les outils PAR CHEMIN DE FICHIER, en neutralisant
    `@ToolRegistry.register` (`_charger`, en haut de ce fichier) pour contourner le
    nettoyage de registres du conftest amont. Consequence : la suite entiere pouvait
    etre verte alors que le modele ne voyait AUCUN outil.

    Il suffit que la cle change lors d'une resynchro amont (`home_assistant` →
    `homeassistant`), ou que le decorateur saute sur un conflit de fusion : Ava repond
    « je n'ai pas acces a la maison » sur une infra parfaitement saine, et rien ne le
    signale — le decorateur qui ne s'execute pas ne leve aucune erreur, il laisse juste
    le registre vide.

    C'est la classe exacte de l'incident du 2026-04-27, pour lequel
    `test_le_backend_est_enregistre` a ete ecrit cote STT et jamais transpose ici.
    """
    from openjarvis.core.registry import ToolRegistry

    assert ToolRegistry.contains(cle), (
        f"outil {cle!r} absent du ToolRegistry — le decorateur s'est-il execute ?"
    )


def test_la_voix_kokoro_fr_est_REELLEMENT_enregistree() -> None:
    """Meme raisonnement pour le TTS — et il porte desormais la voix par defaut d'Ava
    (bascule du 2026-08-04 : `openai_tts` → `kokoro-fr`). Un registre TTS sans
    `kokoro-fr` rend Ava MUETTE, chaque phrase repondant 404."""
    from openjarvis.core.registry import TTSRegistry

    assert TTSRegistry.contains("kokoro-fr")


def test_ha_relaie_la_CAMERA_du_parking(ha: Any) -> None:
    """⚠ LE MEME DEFAUT POUR LA TROISIEME FOIS, ET C'EST CE QUI JUSTIFIE CE TEST.

    Le 2026-08-04 l'admin signale « Ava ne detecte pas les voitures sur le parking ».
    Mesure : la camera detecte (32 declenchements en 24 h cote Home Assistant) — le
    maillon manquant etait la liste `MAISON` du collecteur. Corrige.
    Mais `DOMAINES` cote Ava est **aussi** une liste blanche : un champ collecte que
    n'expose aucune vue reste invisible, sans erreur nulle part. Collecter et RELIER
    sont deux gestes distincts ; le second se fait oublier parce que le premier marche.
    """
    sortie = ha.HomeAssistantTool().execute(domaine="surveillance").content
    assert "parking" in sortie.lower()
    assert "jour" in sortie


def test_ha_dit_qu_une_detection_est_DESACTIVEE(ha: Any, monkeypatch: Any) -> None:
    """⚠ Une sensibilite IA a 0 se comporte EXACTEMENT comme un capteur sain qui ne voit
    rien — c'est ce qui a rendu la detection vehicule invisible du 25 au 28 juillet. Ava
    doit pouvoir trancher en une phrase au lieu d'envoyer enqueter."""
    dash = {**DASHBOARD}
    dash["module_data"] = {
        "home_assistant": {
            **DASHBOARD["module_data"]["home_assistant"],
            "camera_parking": {"moment": "nuit", "detections_muettes": ["véhicule"]},
        }
    }
    monkeypatch.setattr(ha, "_dashboard", lambda: dash)
    sortie = ha.HomeAssistantTool().execute(domaine="surveillance").content
    assert "véhicule" in sortie and "0" in sortie


def test_les_domaines_annonces_ont_TOUS_une_vue(ha: Any) -> None:
    """Un domaine propose au modele mais sans vue rendrait une reponse vide — et le
    modele conclurait a une maison sans donnee plutot qu'a un outil incomplet."""
    spec = ha.HomeAssistantTool().spec
    annonces = spec.parameters["properties"]["domaine"]["enum"]
    assert set(annonces) == set(ha._VUES), "enum et _VUES ont divergé"


# ══ camera — l'historique qualifie de l'exterieur ══════════════════════════════════

DASH_CAM = {
    "module_health": {"frigate": "ok"},
    "module_data": {
        "frigate": {
            "_health": "ok",
            "camera": "parking",
            "camera_fps": 5.1,
            "detection_active": True,
            "historique_lisible": True,
            "evenements_24h": {
                "total": 32,
                "par_objet": {"voiture": 30, "personne": 2},
                "par_zone": {"la cour": 30, "le portail": 2},
            },
            "derniers": [
                {"objet": "personne", "zones": ["le portail"], "debut": 1.0, "en_cours": True},
                {"objet": "voiture", "zones": ["la cour"], "debut": 1.0, "en_cours": False},
            ],
        }
    },
}


@pytest.fixture
def cam(monkeypatch: pytest.MonkeyPatch) -> Any:
    m = _charger("camera")
    monkeypatch.setattr(m, "_dashboard", lambda: DASH_CAM)
    return m


def test_camera_dit_que_ce_sont_des_DETECTIONS_pas_des_vehicules(cam: Any) -> None:
    """⚠ LE TEST QUI EMPECHE LA REPONSE FAUSSE QUI A LANCE CE CHANTIER.

    « 32 detections de voiture » s'entend « 32 voitures » — alors qu'il y en a QUATRE,
    dont trois garees en permanence dans le champ. Une meme voiture qui part et revient
    compte deux fois. Sans cette precision, Ava donnerait un chiffre exact et une
    reponse fausse, ce qui est pire qu'un « je ne sais pas »."""
    sortie = cam.CameraTool().execute().content
    assert "32" in sortie
    assert "DÉTECTIONS" in sortie or "détections" in sortie
    assert "pas des véhicules distincts" in sortie


def test_camera_signale_ce_qui_est_EN_COURS(cam: Any) -> None:
    """« Il y a quelqu'un au portail EN CE MOMENT » n'est pas « quelqu'un est venu » —
    et c'est la seule des deux qui demande de se lever."""
    sortie = cam.CameraTool().execute().content
    assert "EN CE MOMENT" in sortie
    assert "personne" in sortie


def test_camera_AVEUGLE_ne_dit_pas_qu_il_ne_s_est_rien_passe(
    cam: Any, monkeypatch: Any
) -> None:
    """⚠ LE CAS LE PLUS IMPORTANT. Sans flux RTSP, « rien detecte » ne veut PAS dire
    « rien ne s'est passe ». Ava doit le dire, pas le taire — sinon elle rassure sur une
    camera morte."""
    dash = {
        "module_health": {"frigate": "ok"},
        "module_data": {
            "frigate": {
                **DASH_CAM["module_data"]["frigate"],
                "camera_fps": 0.0,
                "evenements_24h": {"total": 0, "par_objet": {}, "par_zone": {}},
                "derniers": [],
            }
        },
    }
    monkeypatch.setattr(cam, "_dashboard", lambda: dash)
    sortie = cam.CameraTool().execute().content
    assert "n'envoie plus d'image" in sortie


def test_camera_distingue_le_CP_injoignable(cam: Any, monkeypatch: Any) -> None:
    """Deux pannes distinctes se presentent identiquement : le CP injoignable, et la
    camera qui ne voit rien. Chercher du mauvais cote coute une heure."""
    import urllib.error

    def _boum() -> Any:
        raise urllib.error.URLError("refuse")

    monkeypatch.setattr(cam, "_dashboard", _boum)
    r = cam.CameraTool().execute()
    assert r.success is False
    assert "Control plane injoignable" in r.content


def test_camera_est_DISTINCTE_de_home_assistant(cam: Any) -> None:
    """Les deux parlent du parking mais repondent a des questions differentes :
    l'un donne l'etat instantane, l'autre l'historique date. La description doit le dire
    au modele — c'est elle qui guide le choix, comme pour `journal` vs `memoire`."""
    d = cam.CameraTool().spec.description
    assert "home_assistant" in d
    assert "INSTANTANÉ" in d or "instantané" in d


def test_camera_ACCORDE_au_pluriel(cam: Any) -> None:
    """⚠ Ava DIT ces phrases a voix haute. « 3 voiture » s'entend, et s'entend mal.
    Defaut constate sur la sortie reelle, pas en relisant le code."""
    sortie = cam.CameraTool().execute().content
    assert "30 voitures" in sortie
    assert "30 voiture," not in sortie and "30 voiture " not in sortie


def test_camera_dit_AUCUNE_ALERTE_quand_le_bruit_domine(cam: Any, monkeypatch: Any) -> None:
    """⚠ « 5 detections » et « 5 detections, aucune alerte » decrivent la MEME realite,
    l'une de facon inquietante et l'autre rassurante. Mesure du 2026-08-04 : les 5
    evenements nocturnes etaient tous des voitures GAREES redetectees par salves, et
    Frigate les classait correctement en `detection` — zero alerte. Ava doit le dire."""
    dash = {**DASH_CAM, "module_data": {"frigate": {
        **DASH_CAM["module_data"]["frigate"],
        "revue": {"alertes": 0, "detections": 5, "dernieres_alertes": []},
    }}}
    monkeypatch.setattr(cam, "_dashboard", lambda: dash)
    assert "Aucune alerte" in cam.CameraTool().execute().content


def test_camera_RACONTE_les_alertes_quand_il_y_en_a(cam: Any, monkeypatch: Any) -> None:
    dash = {**DASH_CAM, "module_data": {"frigate": {
        **DASH_CAM["module_data"]["frigate"],
        "revue": {"alertes": 1, "detections": 2, "dernieres_alertes": [
            {"objets": ["personne"], "zones": ["le portail"], "debut": 1.0}]},
    }}}
    monkeypatch.setattr(cam, "_dashboard", lambda: dash)
    s = cam.CameraTool().execute().content
    assert "1 alerte" in s and "le portail" in s


def test_camera_dit_la_PHRASE_COMPLETE_avec_sa_preposition(cam: Any) -> None:
    """⚠ SORTIE REELLE, non retouchee, avant correction : « EN CE MOMENT : voiture la
    cour. » Les libelles du control plane portent l'article mais pas la preposition —
    corrects pour un decompte (« la cour (8) »), fautifs des qu'on les concatene.
    Ava DIT ces phrases a voix haute.
    ⚠ Ce test assere la PHRASE ENTIERE et non un fragment : c'est le controle par
    sous-chaine qui avait laisse passer « 3 voiture » PUIS « voiture la cour »."""
    sortie = cam.CameraTool().execute().content
    assert "personne au portail" in sortie
    assert "voiture dans la cour" in sortie
    assert "voiture la cour" not in sortie
    assert "personne le portail" not in sortie


def _outils_importes_par_boot() -> set[str]:
    """Les modules que `_skills()` importe reellement, lus par PARCOURS D'AST.

    ⚠ La premiere version de ce test decoupait le fichier a la chaine et cherchait la
      parenthese fermante apres « def _skills() » — elle tombait sur celle de la
      SIGNATURE, donc lisait un bloc vide et declarait les sept outils manquants. Un test
      faux qui echoue est moins couteux qu'un test faux qui passe, mais c'est le meme
      defaut : on lit une source qui ne porte pas la donnee.
    """
    arbre = ast.parse((RACINE / "boot.py").read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.FunctionDef) and noeud.name == "_skills":
            return {
                alias.name
                for sous in ast.walk(noeud)
                if isinstance(sous, ast.ImportFrom)
                for alias in sous.names
            }
    raise AssertionError("`_skills()` introuvable dans boot.py")


def test_TOUT_outil_pose_sur_le_disque_est_IMPORTE_par_boot() -> None:
    """⚠ LE GARDE-FOU QUE `boot.py` ANNONCAIT SANS QU'IL EXISTE.

    Son commentaire dit « TOUT NOUVEL OUTIL DOIT ETRE AJOUTE ICI (et la CI le verifie) ».
    La CI ne verifiait rien. Paye le 2026-08-05 avec l'outil `proposer` : le fichier etait
    depose, la liste blanche Ansible le nommait, le service redemarrait sans la moindre
    erreur — et Ava repondait « je n'ai pas d'outil `proposer` dans ma liste ». Rien
    n'etait casse ; personne n'importait le module, donc le decorateur
    `@ToolRegistry.register` ne s'executait jamais.

    C'est le defaut recurrent de ce projet sous une forme de plus : *une source qui ne
    porte pas la donnee repond « rien » sans erreur.* Ici la source est la liste d'imports.
    """
    poses = {f.stem for f in (RACINE / "skills").glob("*.py") if not f.stem.startswith("_")}
    manquants = sorted(poses - _outils_importes_par_boot())
    assert not manquants, (
        "Outils presents sur le disque mais JAMAIS importes par `boot.py` — ils "
        f"n'existeront pas pour le modele, en silence : {manquants}"
    )


def test_le_garde_fou_precedent_examine_VRAIMENT_quelque_chose() -> None:
    """Contre-test indispensable : un test dont la lecture echoue silencieusement reste
    vert pour toujours. On verifie donc que les DEUX cotes de la comparaison sont peuples.
    """
    poses = {f.stem for f in (RACINE / "skills").glob("*.py") if not f.stem.startswith("_")}
    importes = _outils_importes_par_boot()
    assert len(poses) >= 5, f"la decouverte des outils ne rend que {poses}"
    assert len(importes) >= 5, f"la lecture de boot.py ne rend que {importes}"


# ── Garde des fichiers sensibles (2026-08-05) ───────────────────────────────────


def test_un_JETON_est_traite_comme_un_fichier_SENSIBLE() -> None:
    """⚠ TROU MESURE, PAS THEORIQUE. `file_read` refusait bien `.env`, `*.pem`, `id_rsa`
    — mais AUCUN motif ne couvrait un fichier de jeton, qui n'a ni extension ni nom en
    « credentials ». Le 2026-08-05, `~/.openjarvis/cp_voice_token` etait rendu EN CLAIR
    au modele : c'est le jeton qui autorise `logs` et `proposer`. Le vecteur n'est pas
    theorique — `web_search` ramene du texte ecrit par des tiers.
    """
    from openjarvis.security.file_policy import is_sensitive_file

    for chemin in (
        "/home/avalon/.openjarvis/cp_voice_token",
        "/tmp/gitlab.token",
        "/tmp/un_api_key_quelconque",
        "/tmp/machin_secret",
    ):
        assert is_sensitive_file(chemin), f"devrait etre refuse : {chemin}"


def test_un_fichier_ORDINAIRE_reste_lisible() -> None:
    """Le jumeau : une garde qui refuse tout serait pire qu'aucune garde — l'outil
    cesserait de servir sans que rien ne le signale."""
    from openjarvis.security.file_policy import is_sensitive_file

    for chemin in ("/etc/hostname", "/tmp/README.md", "/tmp/config.toml"):
        assert not is_sensitive_file(chemin), f"ne devrait pas etre refuse : {chemin}"


def test_la_garde_consulte_les_DEUX_implementations() -> None:
    """⚠ LE PIEGE QUI A COUTE UNE PASSE. `is_sensitive_file` rendait la reponse RUST des
    qu'elle etait disponible et ne consultait Python qu'en `ImportError` : un motif ajoute
    a `DEFAULT_SENSITIVE_PATTERNS` etait DU CODE MORT en production. On croit durcir, rien
    ne change, aucune erreur ne le signale.
    L'union doit aller dans le sens du REFUS — un desaccord entre deux gardes doit fermer,
    jamais ouvrir."""
    import inspect

    from openjarvis.security import file_policy

    source = inspect.getsource(file_policy.is_sensitive_file)
    assert "return _is_sensitive_file_py" in source, (
        "le repli Python doit etre atteint MEME quand Rust est disponible"
    )


# ── file_read oriente vers lire_doc (2026-08-05) ────────────────────────────────


def test_un_echec_DOCUMENTAIRE_oriente_vers_lire_doc() -> None:
    """⚠ MESURE : 12 appels a `file_read`, 11 echecs, dont NEUF sur le meme fichier
    essaye en sept orthographes. Aucune ne pouvait aboutir — le depot d'infrastructure
    n'est pas sur cette machine. « File not found » est exact et sans issue : il invite a
    reessayer un chemin de plus, ce qu'elle a fait sept fois."""
    import ava_extensions.patches.file_read_oriente  # noqa: F401
    from openjarvis.tools.file_read import FileReadTool

    r = FileReadTool().execute(path="docs/ava-perimetre.md")
    assert r.success is False
    assert "lire_doc" in r.content, "l'echec doit NOMMER l'outil qui, lui, fonctionne"


def test_un_echec_ORDINAIRE_n_est_PAS_pollue() -> None:
    """Le jumeau : ajouter cette phrase a TOUT echec la rendrait du bruit, et le modele
    cesserait de la lire — le sort de toute alerte qui crie trop."""
    import ava_extensions.patches.file_read_oriente  # noqa: F401
    from openjarvis.tools.file_read import FileReadTool

    r = FileReadTool().execute(path="/tmp/inexistant-quelconque.bin")
    assert r.success is False
    assert "lire_doc" not in r.content


def test_une_lecture_REUSSIE_reste_intacte() -> None:
    """Un patch d'orientation ne doit rien changer au chemin nominal."""
    import ava_extensions.patches.file_read_oriente  # noqa: F401
    from openjarvis.tools.file_read import FileReadTool

    r = FileReadTool().execute(path="/etc/hostname")
    assert r.success is True
    assert "lire_doc" not in r.content


def test_TOUT_patch_pose_sur_le_disque_est_IMPORTE_par_boot() -> None:
    """Meme garde-fou que pour les outils, etendu aux patches : un module pose et jamais
    importe ne s'execute pas, et rien ne le signale. C'est ainsi que `proposer` est reste
    invisible au modele le 2026-08-05 alors que tout semblait en place."""
    poses = {f.stem for f in (RACINE / "patches").glob("*.py") if not f.stem.startswith("_")}
    arbre = ast.parse((RACINE / "boot.py").read_text(encoding="utf-8"))
    importes: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.FunctionDef) and noeud.name == "_patches":
            importes = {
                alias.name
                for sous in ast.walk(noeud)
                if isinstance(sous, ast.ImportFrom)
                for alias in sous.names
            }
    assert poses, "aucun patch decouvert — lecture cassee"
    manquants = sorted(poses - importes)
    assert not manquants, f"patches jamais importes par boot.py : {manquants}"
