"""Tool home_assistant — donne à Ava la perception de son environnement physique.

C'est la source qui distingue Ava d'un assistant générique : elle seule sait qui est à la
maison, quelle température il fait dans quelle pièce, ce qui consomme, si le chauffage
tourne. Les autres outils décrivent l'infrastructure ; celui-ci décrit le monde réel.

⚠ LECTURE SEULE, DÉLIBÉRÉMENT. Le jeton dont Ava dispose autorise techniquement l'écriture
  (allumer, chauffer, ouvrir). Cet outil ne l'expose pas : une IA qui INTERPRÈTE une
  demande ambiguë et agit sur le chauffage d'une maison où vivent des personnes âgées est
  un risque d'une autre nature que se tromper dans une réponse. Le pilotage viendra
  éventuellement plus tard, avec des garde-fous explicites et un arbitrage séparé.

⚠ RÉSEAU : Ava est en DMZ (192.168.100.15), Home Assistant sur le LAN (192.168.2.41).
  La séparation de zones interdit ce trajet par défaut ; une règle nftables NOMMÉE
  l'autorise (une IP, une destination, un port — cf. host_vars/firewall.yml du dépôt
  infra_avalon). Sans elle, les appels échouent en TIMEOUT et non en erreur claire :
  si cet outil ne répond plus, vérifier le pare-feu AVANT de suspecter Home Assistant.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

HA_URL = os.environ.get("HA_URL", "http://192.168.2.41:8123")
_TIMEOUT_S = 10.0

# ⚠ CE QUE L'ON EXPOSE EST UNE SÉLECTION, PAS UN DÉVERSEMENT. Home Assistant compte plus
#   de 650 entités, dont l'immense majorité est du réglage (seuils d'alarme, minuteurs,
#   luminosité d'écran des prises). Tout envoyer au modèle coûterait cher, noierait le
#   signal, et l'exposerait à des valeurs qu'il interpréterait de travers.
#   Chaque entrée ci-dessous répond à une question qu'on pose réellement à voix haute.
DOMAINES = {
    "presence": {
        "titre": "Qui est là",
        "entites": [
            ("Adrien", "person.adrien_cros"),
            ("Aurélie", "person.aurelie_ruffray"),
            ("Annie", "person.annie_cros"),
            ("Jean-Pierre", "person.jean_pierre_cros"),
        ],
        # ⚠ `zone.home` EST le nombre de personnes présentes, calculé par HA sur les
        #   coordonnées. Ne jamais recompter en comparant les états textuels : une app
        #   companion publie parfois le nom d'une zone supprimée, et le compte tombe faux
        #   (mesuré : 4 réels contre 2 comptés à la main).
        "compteur": ("À la maison", "zone.home"),
    },
    "climat": {
        "titre": "Températures",
        "entites": [
            ("Extérieur", "sensor.temperature_exterieure"),
            ("Grange (moyenne)", "sensor.temperature_moyenne_grange"),
            ("Parents (moyenne)", "sensor.temperature_moyenne_parents"),
            ("Salon", "sensor.temp_salon_temperature"),
            ("Cuisine", "sensor.temp_cuisine_temperature"),
            ("Chambre", "sensor.temp_chambre_temperature"),
            ("Salle de bain", "sensor.temp_salle_de_bain_temperature"),
            ("Salle de bain étage (parents)", "sensor.salle_de_bain_etage_temperature"),
        ],
    },
    "chauffage": {
        "titre": "Chauffage des parents",
        # ⚠ On lit les `sensor.*_mode` et NON les `climate.*` : ces derniers sont en
        #   `unknown` permanent (l'intégration Tuya ne sait traduire ni « Standby », ni
        #   « Comfort », ni « Anti_forst »). Interroger le climate rendrait « inconnu »
        #   sur un chauffage qui fonctionne parfaitement.
        "entites": [
            ("Radiateur salon", "sensor.radiateur_salon_mode"),
            ("Radiateur salle à manger", "sensor.radiateur_salle_a_manger_mode"),
            ("Radiateur véranda", "sensor.radiateur_salon_veranda_mode"),
            ("Thermostat maison", "sensor.thermostat_maison_mode"),
            ("Ballon eau chaude", "sensor.ballon_eau_chaude_local_temperature"),
            ("Disjoncteur chaufferie", "sensor.disjoncteur_chaufferie_local_etat"),
        ],
    },
    "energie": {
        "titre": "Consommation",
        "entites": [
            ("Baie serveur", "sensor.baie_serveur_local_puissance"),
            ("Température baie", "sensor.baie_serveur_local_temperature"),
            ("Chaufferie", "sensor.disjoncteur_chaufferie_local_puissance"),
            ("Tension secteur", "sensor.baie_serveur_local_tension_b"),
        ],
    },
    "maison": {
        "titre": "État de la maison",
        "entites": [
            ("Lumière salon", "light.salon_local"),
            ("Lumière cuisine", "light.cuisine_local"),
            ("Lumière chambre gauche", "light.chambre_gauche_local"),
            ("Lumière bureau", "light.bureau_local"),
            ("Projecteur parking", "light.parking_projecteur"),
            ("Détection personne (parking)", "binary_sensor.parking_personne"),
            ("Météo", "weather.meteo_france"),
        ],
    },
}

# Vocabulaire des appareils traduit — le modèle recevrait sinon « Standby » ou
# « remote_on », qu'il restituerait tels quels à l'oral.
_LISIBLE = {
    "Standby": "arrêt", "Comfort": "confort", "Anti_forst": "hors-gel",
    "eco": "éco", "auto": "auto", "home": "normal", "temporary": "dérogation",
    "on": "allumé", "off": "éteint", "remote_on": "sous tension",
    "remote_off": "coupé à distance", "normal": "normal",
    "home_zone": "à la maison", "not_home": "absent",
    "unknown": "inconnu", "unavailable": "indisponible",
    # ⚠ LES 15 CONDITIONS MÉTÉO, pas seulement celles déjà rencontrées. Sans cette liste
    #   complète, le modèle recevait « lightning » (constaté au premier test réel) et
    #   l'aurait restitué tel quel à l'oral. Même défaut que la page d'accueil Home
    #   Assistant, corrigé le même jour : on ne traduit pas ce qu'on a vu passer, on
    #   traduit ce que la source peut produire.
    "clear-night": "nuit claire", "cloudy": "nuageux",
    "exceptional": "conditions exceptionnelles", "fog": "brouillard",
    "hail": "grêle", "lightning": "orage", "lightning-rainy": "orage et pluie",
    "partlycloudy": "éclaircies", "pouring": "fortes pluies", "rainy": "pluie",
    "snowy": "neige", "snowy-rainy": "pluie et neige", "sunny": "ensoleillé",
    "windy": "venteux", "windy-variant": "venteux",
}


def _jeton() -> str:
    """Jeton DÉDIÉ à Ava, distinct de celui de l'admin (donc révocable seul)."""
    return os.environ.get("HA_TOKEN", "")


def _etats() -> dict[str, dict[str, Any]]:
    req = urllib.request.Request(
        f"{HA_URL}/api/states",
        headers={"Authorization": f"Bearer {_jeton()}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        brut = json.loads(resp.read().decode("utf-8"))
    return {e["entity_id"]: e for e in brut}


def _valeur(etats: dict[str, dict[str, Any]], eid: str) -> str | None:
    """Valeur lisible d'une entité, ou None si elle ne dit rien d'exploitable.

    ⚠ On rend `None` — et non « 0 » ou « inconnu » — quand la donnée manque. Un zéro
      inventé se propage : le modèle l'affirmerait à l'oral avec assurance. C'est le
      défaut exact qui faisait dire à l'outil `avalon_status` « aucune alerte » alors
      qu'une alerte tirait.
    """
    e = etats.get(eid)
    if not e:
        return None
    etat = str(e.get("state", "")).strip()
    if etat.lower() in ("unknown", "unavailable", "none", ""):
        return None
    attrs = e.get("attributes") or {}
    unite = attrs.get("unit_of_measurement")
    if eid.startswith("weather."):
        cond = _LISIBLE.get(etat, etat)
        t = attrs.get("temperature")
        return f"{cond}, {t} °C" if t is not None else cond
    if eid.startswith("person."):
        return "à la maison" if etat not in ("not_home",) else "absent"
    lisible = _LISIBLE.get(etat, etat)
    return f"{lisible} {unite}".strip() if unite else lisible


def _resume(etats: dict[str, dict[str, Any]], domaine: str | None) -> str:
    demandes = [domaine] if domaine and domaine in DOMAINES else list(DOMAINES)
    lignes: list[str] = []
    for cle in demandes:
        d = DOMAINES[cle]
        bloc: list[str] = []
        compteur = d.get("compteur")
        if compteur:
            v = _valeur(etats, compteur[1])
            if v is not None:
                bloc.append(f"  {compteur[0]} : {v}")
        for nom, eid in d["entites"]:
            v = _valeur(etats, eid)
            if v is not None:
                bloc.append(f"  {nom} : {v}")
        if bloc:
            lignes.append(f"{d['titre']} :")
            lignes.extend(bloc)
    if not lignes:
        return "Aucune donnée exploitable renvoyée par Home Assistant."
    return "\n".join(lignes)


@ToolRegistry.register("home_assistant")
class HomeAssistantTool(BaseTool):
    """Lit l'état de la maison depuis Home Assistant (lecture seule)."""

    tool_id = "home_assistant"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="home_assistant",
            description=(
                "Lit l'état RÉEL de la maison depuis Home Assistant : qui est présent, "
                "températures intérieures et extérieure, état du chauffage des parents, "
                "consommation électrique, lumières allumées, météo locale, détection au "
                "parking. À utiliser dès qu'Adrien pose une question sur la maison, la "
                "température, le chauffage, la présence de quelqu'un, la consommation, ou "
                "ce qui se passe chez lui. Lecture seule : cet outil ne pilote rien."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domaine": {
                        "type": "string",
                        "enum": list(DOMAINES),
                        "description": (
                            "Restreint la réponse à un domaine : presence, climat, "
                            "chauffage, energie, maison. Omettre pour tout obtenir."
                        ),
                    }
                },
                "required": [],
            },
            category="maison",
            latency_estimate=1.0,
            timeout_seconds=12.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        if not _jeton():
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Jeton Home Assistant absent (HA_TOKEN). Il est déployé dans le .env "
                    "d'Ava depuis SOPS (clé homeassistant.ava_token)."
                ),
                success=False,
            )
        try:
            etats = _etats()
        except urllib.error.HTTPError as exc:
            # ⚠ 401 = jeton refusé ; c'est une cause DIFFÉRENTE d'un réseau coupé, et le
            #   dire évite de partir chercher le pare-feu quand c'est le jeton.
            detail = "jeton refusé (401)" if exc.code == 401 else f"HTTP {exc.code}"
            return ToolResult(tool_name=self.tool_id,
                              content=f"Home Assistant a refusé la requête : {detail}",
                              success=False)
        except urllib.error.URLError as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"Home Assistant injoignable ({HA_URL}) : {exc}. "
                    "Vérifier la règle pare-feu DMZ→LAN avant de suspecter HA — sans "
                    "elle l'appel expire sans erreur explicite."
                ),
                success=False,
            )
        except Exception as exc:  # pragma: no cover - défensif
            return ToolResult(tool_name=self.tool_id,
                              content=f"Erreur inattendue : {exc}", success=False)

        domaine = params.get("domaine")
        return ToolResult(
            tool_name=self.tool_id,
            content=_resume(etats, domaine if isinstance(domaine, str) else None),
            success=True,
            metadata={"entites_lues": len(etats), "domaine": domaine or "tous"},
        )
