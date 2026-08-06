"""Tool home_assistant — donne à Ava la perception de son environnement physique.

C'est la source qui distingue Ava d'un assistant générique : elle seule sait qui est à la
maison, quelle température il fait dans quelle pièce, ce qui consomme, si le chauffage
tourne. Les autres outils décrivent l'infrastructure ; celui-ci décrit le monde réel.

⚠ IL NE PARLE PAS À HOME ASSISTANT — IL LIT LE CONTROL PLANE. C'est la décision de
  conception la plus importante de ce fichier, et elle n'est pas un détour technique.

  Home Assistant vit sur le LAN (192.168.2.41) : un hub domotique DOIT partager le segment
  de ses objets, mDNS et SSDP étant du multicast qui ne route pas. Ava vit en DMZ
  (192.168.100.15) parce qu'elle exécute du code tiers et parle à des API externes. Les
  deux placements sont justes, et pris isolément ils semblent imposer une règle pare-feu
  DMZ → LAN — la première de cette infrastructure, dont l'asymétrie (LAN → DMZ, jamais
  l'inverse) est le cœur de la séparation de zones.

  Cette règle a été posée le 2026-08-03, puis RETIRÉE le jour même. Le control plane
  tourne sur AVA, machine DUAL-HOMED : il atteint HA sans traverser la moindre frontière,
  et Ava l'interroge par un chemin déjà ouvert (celui de `avalon_status`). Zéro règle
  nouvelle.

  ⚠ ET LE GAIN N'EST PAS QUE COMPTABLE. Le jeton HA autorise l'ÉCRITURE : allumer,
    chauffer, ouvrir. L'accès direct plaçait ce pouvoir dans une VM qui exécute du code
    tiers. Le module du control plane est en lecture seule et ne renvoie que des valeurs :
    même compromise, Ava ne peut rien commander. C'est une réduction de portée, pas
    seulement une économie de règle.

⚠ SI CET OUTIL NE RÉPOND PLUS, ce n'est pas Home Assistant qu'il faut regarder en premier
  mais le module `home_assistant` du control plane (`GET /api/v1/dashboard`). Deux pannes
  distinctes se présentent identiquement côté Ava : le CP injoignable, et le CP joignable
  dont le module est dégradé. Le code ci-dessous les distingue explicitement — c'est ce qui
  évite de chercher une heure du mauvais côté.
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

# Patte DMZ d'AVA. ⚠ Pas le hostname public `control.avalon-network.com` : il est derrière
# Cloudflare Access, une requête serveur s'y ferait rediriger vers un portail de connexion
# et échouerait sans dire pourquoi. Même piège que l'endpoint interne de Renovate.
CP_URL = os.environ.get("CP_URL", "http://192.168.100.31:8100")
_TIMEOUT_S = 10.0

# ⚠ Les domaines sont des VUES sur la donnée du CP, pas des requêtes distinctes. Un seul
#   appel rapporte tout ; le filtre sert à ne pas noyer le modèle quand la question est
#   précise (« il fait combien dans la chambre ? » n'a pas besoin de la consommation).
DOMAINES = ("presence", "climat", "chauffage", "energie", "maison", "surveillance")


def _dashboard() -> dict[str, Any]:
    req = urllib.request.Request(
        f"{CP_URL}/api/v1/dashboard", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _nombre(v: Any, unite: str = "") -> str | None:
    """Formate une mesure, ou rend None si elle manque.

    ⚠ Jamais « 0 » ni « inconnu » par défaut : un zéro inventé se propage et le modèle
      l'affirmerait à l'oral avec assurance. C'est le défaut exact qui faisait dire à
      l'outil `avalon_status` « aucune alerte » pendant qu'une alerte tirait.
    """
    if v is None:
        return None
    if isinstance(v, float):
        # Virgule décimale : le modèle lit le texte tel quel, et « 25.4 » se dit
        # « vingt-cinq point quatre » par un moteur vocal français.
        return f"{v:.1f}".replace(".", ",") + unite
    return f"{v}{unite}"


def _presence(d: dict[str, Any]) -> list[str]:
    lignes: list[str] = []
    n = d.get("a_la_maison")
    if n is not None:
        lignes.append(f"  À la maison : {n} personne(s)")
    for nom, etat in (d.get("presence") or {}).items():
        lignes.append(f"  {nom} : {etat}")
    return lignes


def _climat(d: dict[str, Any]) -> list[str]:
    lignes: list[str] = []
    t = d.get("temperatures") or {}
    for cle, libelle in (
        ("exterieur", "Extérieur"),
        ("grange", "Grange (moyenne)"),
        ("parents", "Parents (moyenne)"),
    ):
        v = _nombre(t.get(cle), " °C")
        if v:
            lignes.append(f"  {libelle} : {v}")
    for piece, valeur in (d.get("pieces") or {}).items():
        v = _nombre(valeur, " °C")
        if v:
            lignes.append(f"  {piece} : {v}")
    meteo = d.get("meteo")
    if meteo:
        tm = _nombre(d.get("meteo_temperature"), " °C")
        lignes.append(f"  Météo : {meteo}" + (f", {tm}" if tm else ""))
    return lignes


def _chauffage(d: dict[str, Any]) -> list[str]:
    lignes: list[str] = []
    for nom, info in (d.get("chauffage") or {}).items():
        etat = info.get("mode", "?")
        # ⚠ `chauffe` répond à la vraie question — « est-ce que ça chauffe MAINTENANT » —
        #   qui est distincte du mode. Une vanne en « confort » avec `chauffe: false` a
        #   atteint sa consigne ; dire seulement « confort » laisserait croire l'inverse.
        actif = " (chauffe actuellement)" if info.get("chauffe") else ""
        ouv = info.get("ouverture")
        detail = f", vanne à {ouv} %" if isinstance(ouv, (int, float)) else ""
        lignes.append(f"  {nom} : {etat}{actif}{detail}")
    return lignes


def _energie(d: dict[str, Any]) -> list[str]:
    e = d.get("energie") or {}
    paires = (
        ("baie_serveur_kw", "Baie serveur", " kW"),
        ("chaufferie_kw", "Chaufferie", " kW"),
        ("baie_temperature_c", "Température baie", " °C"),
        ("tension_v", "Tension secteur", " V"),
    )
    return [f"  {lib} : {v}" for cle, lib, u in paires if (v := _nombre(e.get(cle), u))]


def _maison(d: dict[str, Any]) -> list[str]:
    return [f"  {nom} : {val}" for nom, val in (d.get("maison") or {}).items()]


def _surveillance(d: dict[str, Any]) -> list[str]:
    """La caméra du parking : ce qu'elle voit, et dans quelles conditions.

    ⚠ CE DOMAINE EXISTE PARCE QUE `DOMAINES` EST UNE LISTE BLANCHE — et c'est le défaut le
      plus récurrent de tout ce projet, rencontré ici pour la troisième fois : le control
      plane a beau collecter un champ, s'il n'est relayé par aucune vue, Ava répond « je ne
      sais pas » sur une donnée qu'elle possède. Écrire la collecte et la RELIER sont deux
      gestes distincts, et le second se fait oublier parce que le premier « marche ».
    """
    c = d.get("camera_parking") or {}
    if not c:
        return []
    lignes = []
    # ⚠ LA PRÉSENCE D'ABORD, ET C'EST LA CORRECTION D'UNE ERREUR RÉELLE. Traces du
    #   2026-08-04 : l'admin écrit « le parking est pas vide, tu peux vérifier ? », Ava
    #   répond « parking vide » puis soupçonne le capteur d'être en panne. Le capteur allait
    #   bien — elle lisait des capteurs ÉVÉNEMENTIELS (« off » = rien ne se DÉCLENCHE), pas
    #   des compteurs de PRÉSENCE. Une voiture garée ne produit aucun événement.
    #   ⚠ Ces lignes passent EN PREMIER exprès : elles répondent à « qu'y a-t-il là ? », et
    #   doivent être lues avant les détections, qui répondent à « que s'est-il passé ? ».
    if lisible := c.get("parking_lisible"):
        lignes.append(f"  Parking, en ce moment : {lisible}")
    for zone, z in (c.get("presence_par_zone") or {}).items():
        if zone == "parking":
            continue  # déjà dit par la phrase ci-dessus
        detail = []
        if v := z.get("voitures_presentes"):
            detail.append(f"{v} voiture(s)")
        if pers := z.get("personnes_presentes"):
            detail.append(f"{pers} personne(s)")
        if detail:
            lignes.append(f"  Zone « {zone} » : {', '.join(detail)} présente(s)")
    if moment := c.get("moment"):
        lignes.append(f"  Il fait {moment} sur le parking")
    if sd := c.get("carte_sd"):
        lignes.append(f"  Carte SD : {sd}")
    # ⚠ Une IA à 0 se comporte EXACTEMENT comme un capteur sain qui ne voit rien. C'est ce
    #   qui a rendu la détection véhicule invisible du 25 au 28 juillet.
    if muettes := c.get("detections_muettes"):
        lignes.append(
            f"  ⚠ Détection désactivée (sensibilité à 0) : {', '.join(muettes)}"
        )
    return lignes


_VUES = {
    "presence": ("Qui est là", _presence),
    "climat": ("Températures", _climat),
    "chauffage": ("Chauffage des parents", _chauffage),
    "energie": ("Consommation", _energie),
    "maison": ("État de la maison", _maison),
    "surveillance": ("Caméra du parking", _surveillance),
}


def _resume(d: dict[str, Any], domaine: str | None) -> str:
    cles = [domaine] if domaine in _VUES else list(_VUES)
    sortie: list[str] = []
    for cle in cles:
        titre, rendu = _VUES[cle]
        bloc = rendu(d)
        if bloc:
            sortie.append(f"{titre} :")
            sortie.extend(bloc)
    # Les piles se disent partout : c'est une action à prendre, pas un domaine.
    piles = d.get("piles_faibles") or []
    if piles and (domaine is None or domaine == "maison"):
        noms = ", ".join(f"{p['nom']} ({p['niveau']:.0f} %)" for p in piles)
        sortie.append(f"À changer : pile(s) faible(s) — {noms}")
    if not sortie:
        return "Aucune donnée exploitable dans le relevé de la maison."
    return "\n".join(sortie)


@ToolRegistry.register("home_assistant")
class HomeAssistantTool(BaseTool):
    """Lit l'état de la maison via le control plane Avalon (lecture seule)."""

    tool_id = "home_assistant"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="home_assistant",
            description=(
                "Lit l'état RÉEL de la maison : qui est présent, températures par pièce et "
                "à l'extérieur, état du chauffage des parents, consommation électrique, "
                "lumières allumées, météo locale, détection au parking, piles à changer. "
                "À utiliser dès qu'Adrien pose une question sur la maison, la température, "
                "le chauffage, la présence de quelqu'un, la consommation, ou ce qui se "
                "passe chez lui. Lecture seule : cet outil ne pilote rien."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domaine": {
                        "type": "string",
                        "enum": list(DOMAINES),
                        "description": (
                            "Restreint la réponse à un domaine : presence, climat, "
                            "chauffage, energie, maison, surveillance. Omettre pour tout obtenir."
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
        try:
            dash = _dashboard()
        except urllib.error.HTTPError as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Le control plane a refusé la requête : HTTP {exc.code}.",
                success=False,
            )
        except urllib.error.URLError as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"Control plane injoignable ({CP_URL}) : {exc}. La donnée de la maison "
                    "transite par lui — ce n'est donc pas Home Assistant qu'il faut "
                    "regarder en premier."
                ),
                success=False,
            )
        except Exception as exc:  # pragma: no cover - défensif
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Erreur inattendue : {exc}",
                success=False,
            )

        # ⚠ LA CLÉ EST `module_health`, PAS `modules`. Le dashboard n'expose que
        #   `module_data`, `module_health`, `networks`, `score`, `version`, `widgets`.
        #   J'ai écrit `modules` la première fois : la clé n'existant pas, `.get()` rendait
        #   un dictionnaire vide et l'outil répondait « module non déployé » — un message
        #   parfaitement clair et parfaitement faux, sur un module qui tournait. C'est le
        #   défaut récurrent de ce projet : *une source qui ne porte pas la donnée répond
        #   « rien » sans erreur.* Vérifier les clés réelles d'une réponse, ne pas les
        #   supposer.
        sante = (dash.get("module_health") or {}).get("home_assistant")
        if sante is None:
            # ⚠ Cause DISTINCTE des précédentes : le CP répond, mais le module n'est pas
            #   déployé ou est désactivé en configuration. Sans ce cas, on lirait un
            #   dictionnaire vide et on répondrait « la maison n'a rien à dire » — une
            #   phrase fausse qui ferait chercher du côté des capteurs.
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Le control plane répond mais ne publie aucun module `home_assistant` "
                    "(non déployé, ou `enabled: false` dans config.production.yml)."
                ),
                success=False,
            )

        # ⚠ `module_health` est un dict de CHAÎNES (`{"home_assistant": "ok"}`), pas
        #   d'objets : `sante.get("health")` lèverait un AttributeError sur une str.
        data = (dash.get("module_data") or {}).get("home_assistant") or {}
        if sante != "ok" or not data:
            raison = data.get("_error") or sante or "état inconnu"
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Le relevé de la maison est indisponible : {raison}.",
                success=False,
            )

        domaine = params.get("domaine")
        return ToolResult(
            tool_name=self.tool_id,
            content=_resume(data, domaine if isinstance(domaine, str) else None),
            success=True,
            metadata={
                "source": "control-plane",
                "entites_lues": data.get("entites_totales"),
                "domaine": domaine or "tous",
            },
        )
