"""Tool avalon_status — interroge lAPI Control Plane v2 et résume létat de linfra.

Enregistré comme tool OpenJarvis via @ToolRegistry.register("avalon_status").
Ava peut linvoquer quand Adrien demande "comment va linfra", "quel est le
score", etc.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

# ⚠ DEUX ENDPOINTS, ET C'EST STRUCTUREL. `/dashboard` porte le score et l'état des
#   modules ; il ne porte NI les alertes NI les hôtes. La version précédente lisait
#   `data.get("alerts")` et `data.get("hosts")` sur cette réponse : les deux clés
#   n'existent pas, `.get()` rendait `None`, et l'outil annonçait « Alertes actives: 0 »
#   et « Hosts: 0 » avec assurance — pendant qu'une alerte Grafana tirait réellement.
#   C'est le piège central de cette infrastructure : une source qui ne porte pas la
#   donnée répond « rien » SANS ERREUR. Un zéro faux est pire qu'une absence : Ava
#   répondait « non, aucune alerte » à une question dont elle n'avait pas la réponse.
#   Vérifié le 2026-08-03 — clés réelles de /dashboard : module_data, module_health,
#   networks, score, version, widgets.
CP_V2_BASE = "http://192.168.100.31:8100/api/v1"
_TIMEOUT_S = 8.0


def _get(chemin: str) -> Any:
    req = urllib.request.Request(
        f"{CP_V2_BASE}{chemin}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch() -> dict[str, Any]:
    """Le tableau de bord seul — score et modules."""
    return _get("/dashboard")


def _fetch_hosts() -> list[dict[str, Any]]:
    """Les hôtes, depuis leur PROPRE endpoint.

    ⚠ Ne jamais faire échouer l'outil entier si cet appel échoue : le score reste utile
    même sans le détail des hôtes. Une panne partielle ne doit pas produire un silence
    total — sinon Ava dit « je ne peux pas savoir » alors qu'elle sait l'essentiel.
    """
    try:
        d = _get("/hosts")
    except Exception:
        return []
    return d if isinstance(d, list) else (d.get("hosts") or [])


def _rendre_couverture_sauvegarde(px: dict[str, Any]) -> list[str]:
    """Quelles VM sont reellement protegees par vzdump/PBS — la question qu'elle ne
    pouvait pas trancher.

    ⚠ POURQUOI CE RENDU EXISTE. Le 2026-08-06, a la question « combien de VM sont
      reellement protegees ? », la reponse honnete a ete « je n'ai pas la vue » : l'etat
      `backups` du control plane ne parle que de restic (volumes Docker d'AVA), jamais des
      machines virtuelles. Le champ `backup_coverage` a ete ajoute au module `proxmox`
      pour ca — et le collecter sans le RELAYER ici l'aurait laisse hors de sa portee,
      exactement comme les cinq fois precedentes.

    ⚠ TROIS ETATS DISTINCTS, et les confondre serait pire que se taire :
      · cle ABSENTE  -> le control plane deploye est anterieur : on ne dit rien.
      · valeur None  -> l'endpoint Proxmox n'a pas repondu : on le DIT. « je ne sais pas
        quels jobs existent » n'est pas « aucune VM n'est sauvegardee ».
      · dictionnaire -> on rend le compte, les jobs, les orphelins et les exemptions.
    """
    if "backup_coverage" not in px:
        return []
    cv = px.get("backup_coverage")
    if cv is None:
        return [
            "  sauvegardes des VM : non mesurable (Proxmox n'a pas repondu sur ses jobs)"
        ]

    lignes = [
        f"  sauvegardes des VM : {cv.get('total_couverts', 0)} / {cv.get('total_a_proteger', 0)} "
        "couvertes par vzdump/PBS (les gabarits sont exclus du compte)"
    ]
    for j in cv.get("jobs") or []:
        cibles = j.get("vmids")
        cibles = (
            "tous les guests" if cibles == "tous" else f"{len(cibles or [])} guests"
        )
        etat = "actif" if j.get("enabled") else "DESACTIVE"
        lignes.append(
            f"    job {j.get('id')} ({j.get('storage')}, {j.get('schedule')}) : {etat}, {cibles}"
        )
    orphelins = cv.get("non_couverts") or []
    if orphelins:
        lignes.append(
            "    SANS AUCUNE SAUVEGARDE : "
            + ", ".join(f"{g.get('vmid')} {g.get('name')}" for g in orphelins)
        )
    # ⚠ Une exemption se rend AVEC SA RAISON. Annoncer « 1 exemptee » sans dire pourquoi
    #   invite a la traiter comme un oubli — ce qui est precisement ce que l'ecriture de
    #   la raison sert a empecher.
    for e in cv.get("exemptes") or []:
        lignes.append(
            f"    exemptee : {e.get('vmid')} {e.get('name')} — {e.get('raison')}"
        )
    return lignes


def _rendre_proxmox(px: dict[str, Any]) -> str:
    """Vue par machine virtuelle et par noeud — ce qu'Ava disait ne pas avoir."""
    lignes = []
    for n in px.get("nodes") or []:
        etat = n.get("status")
        detail = (
            f"CPU {float(n.get('cpu', 0)) * 100:.0f} %, RAM {n.get('mem_percent')} %"
            if etat == "online"
            else "hors ligne"
        )
        lignes.append(f"  noeud {n.get('name')} : {etat} — {detail}")
    vms = px.get("vms") or []
    actives = [v for v in vms if v.get("status") == "running"]
    arretees = [v for v in vms if v.get("status") != "running"]
    lignes.append(f"  {len(actives)} VM en marche sur {len(vms)} declarees")
    for v in actives:
        lignes.append(f"    - {v.get('vmid')} {v.get('name')} ({v.get('node')})")
    if arretees:
        lignes.append(
            "  arretees : "
            + ", ".join(f"{v.get('vmid')} {v.get('name')}" for v in arretees)
        )
    ha = px.get("ha") or {}
    # ⚠ `expectation` est une PHRASE ECRITE PAR LE CONTROL PLANE qui explique pourquoi le
    #   HA est desarme. La relayer telle quelle evite qu'Ava reconstruise un raisonnement
    #   a partir d'un booleen — et se trompe.
    if ha.get("expectation"):
        lignes.append(f"  haute disponibilite : {ha['expectation']}")
    elif "enabled" in ha:
        lignes.append(
            f"  haute disponibilite : {'armee' if ha['enabled'] else 'desarmee'}"
        )
    lignes.extend(_rendre_couverture_sauvegarde(px))
    rep = px.get("replication") or {}
    jobs = rep.get("status") or []
    if jobs:
        en_panne = [j for j in jobs if j.get("fail_count")]
        suspendus = [j for j in jobs if j.get("disabled")]
        lignes.append(
            f"  replication : {len(jobs)} taches, {len(suspendus)} suspendues, "
            f"{len(en_panne)} en echec"
        )
    return "\n".join(lignes)


def _rendre_generique(donnees: Any) -> str:
    """Repli pour tout module sans rendu dedie.

    ⚠ C'EST CE REPLI QUI CASSE LA CHAINE DES HUIT OCCURRENCES : un module ajoute au
      control plane demain devient joignable sans modifier ce fichier. Un rendu soigne
      par domaine serait plus lisible et laisserait le neuvieme cas se reproduire.
    """
    if isinstance(donnees, dict):
        lignes = []
        for cle, val in donnees.items():
            if cle.startswith("_"):
                continue
            lignes.append(f"  {cle} : {_abreger(val)}")
        reste = len(lignes) - 25
        lignes = lignes[:25]
        if reste > 0:
            lignes.append(f"  (… et {reste} autres champs non affiches)")
        return "\n".join(lignes)
    return f"  {_abreger(donnees, budget=800)}"


def _abreger(val: Any, budget: int = 300) -> str:
    """Rend une valeur en DISANT ce qui manque.

    ⚠ UNE TRONCATURE QUI NE S'ANNONCE PAS SE LIT COMME UNE REPONSE COMPLETE — c'est la
      meme lecon que le comptage plafonne cote control plane, qui faisait repondre
      « au moins 50 » pour 74. Mesure du 2026-08-05 : interrogee sur les certificats TLS,
      Ava a vu la liste coupee a 300 caracteres et a repondu « le cert le plus proche est
      a 51 jours, pas identifie nommement dans l'extrait ». Elle s'en est bien tiree parce
      qu'elle a REMARQUE la coupure — mais rien ne la lui signalait, et sur une liste ou
      l'element important n'est pas le premier, elle aurait conclu a tort.
    """
    if isinstance(val, list):
        rendu, gardes = [], 0
        for element in val:
            texte = json.dumps(element, ensure_ascii=False)
            if sum(len(x) for x in rendu) + len(texte) > budget:
                break
            rendu.append(texte)
            gardes += 1
        restants = len(val) - gardes
        suffixe = f" … et {restants} autres sur {len(val)}" if restants > 0 else ""
        return "[" + ", ".join(rendu) + "]" + suffixe
    texte = json.dumps(val, ensure_ascii=False) if isinstance(val, dict) else str(val)
    if len(texte) <= budget:
        return texte
    return texte[:budget] + f" … (tronque, {len(texte)} caracteres au total)"


def _format_domaine(data: dict[str, Any], domaine: str) -> str:
    """Le detail d'UN module, a la demande."""
    modules = data.get("module_data") or {}
    cle = domaine.strip().lower()
    if cle not in modules:
        # ⚠ On NOMME les domaines disponibles au lieu de dire « inconnu » : sans cette
        #   liste, le modele reessaie au hasard ou conclut que la donnee n'existe pas.
        return f"Domaine « {domaine} » inconnu. Domaines disponibles : " + ", ".join(
            sorted(k for k in modules if not k.startswith("_"))
        )
    donnees = modules[cle] or {}
    sante = donnees.get("_health") if isinstance(donnees, dict) else None
    entete = f"{cle} — sante : {sante or 'inconnue'}"
    corps = _rendre_proxmox(donnees) if cle == "proxmox" else _rendre_generique(donnees)
    return f"{entete}\n{corps}"


def _resume_sauvegardes(data: dict[str, Any]) -> str:
    """Une ligne sur l'etat des sauvegardes, construite depuis `module_data.backups`.

    Champs REELS mesures le 2026-08-05 sur `/api/v1/dashboard` (pas devines) :
    `restic.age_hours`, `restic.status`, `restic.check_age_days`, `restic.restore_test`
    (un dict destination -> 1/0), et `nfs_mounted`.
    ⚠ Rendre la chaine vide plutot qu'une phrase creuse si le module est absent : dire
      « sauvegardes inconnues » quand on n'a pas regarde est un mensonge de plus.
    """
    b = (data.get("module_data") or {}).get("backups") or {}
    r = b.get("restic") or {}
    if not r:
        return ""
    bouts = []
    age = r.get("age_hours")
    if age is not None:
        bouts.append(f"dernier backup il y a {age:.0f} h ({r.get('status', '?')})")
    controle = r.get("check_age_days")
    if controle is not None:
        bouts.append(f"controle d'integrite il y a {controle:.1f} j")
    tests = r.get("restore_test") or {}
    if tests:
        # ⚠ Le test de RESTAURATION est le seul qui prouve qu'une sauvegarde sert a
        #   quelque chose. Une sauvegarde qui s'ecrit et ne se relit pas est une
        #   sauvegarde qui n'existe pas — on nomme donc les destinations en echec.
        rates = sorted(d for d, ok in tests.items() if not ok)
        if rates:
            bouts.append(f"test de restauration EN ECHEC sur : {', '.join(rates)}")
        else:
            bouts.append(f"test de restauration OK sur les {len(tests)} copies")
    if b.get("nfs_mounted") is False:
        bouts.append("montage NFS du NAS ABSENT")
    return "Sauvegardes: " + " ; ".join(bouts) if bouts else ""


def _format_summary(data: dict[str, Any]) -> str:
    score_info = data.get("score", {}) or {}
    global_score = score_info.get("global", "?")
    max_score = score_info.get("max", 100)
    status = score_info.get("status", "?")
    modules = score_info.get("modules", {}) or {}

    # Modules en dégradation (deductions non vides OU score < max)
    issues = []
    for name, info in modules.items():
        if not isinstance(info, dict):
            continue
        deductions = info.get("deductions") or []
        mscore = info.get("score", 0)
        mmax = info.get("max", 0)
        if deductions or (mmax and mscore < mmax):
            detail = f"{name} {mscore}/{mmax}"
            if deductions:
                top = deductions[0]
                if isinstance(top, dict):
                    reason = top.get("reason", top.get("msg", "?"))
                    detail += f" ({reason})"
            issues.append(detail)

    lines = [f"Score Avalon: {global_score}/{max_score} — {status}"]
    if issues:
        lines.append("Modules en dégradation:")
        for it in issues[:8]:
            lines.append(f"  - {it}")
    else:
        lines.append("Tous les modules sont OK.")

    # ⚠ Les « alertes » du control plane SONT les déductions des modules : il n'existe
    #   pas de liste d'alertes séparée. Les compter à partir des déductions dit la vérité ;
    #   lire une clé `alerts` inexistante disait « 0 » quoi qu'il arrive.
    nb_deductions = sum(
        len(info.get("deductions") or [])
        for info in modules.values()
        if isinstance(info, dict)
    )
    lines.append(f"Déductions actives: {nb_deductions}")

    # Les hôtes viennent de leur propre endpoint (cf. commentaire en tête de fichier).
    hosts = _fetch_hosts()
    if hosts:
        inactifs = [
            h
            for h in hosts
            if isinstance(h, dict)
            and (h.get("active") is False or h.get("maintenance"))
        ]
        detail = ""
        if inactifs:
            # ⚠ LE MOTIF ET L'ANCIENNETÉ, PAS SEULEMENT LE NOM. Le control plane les expose
            #   (`maintenance: {mode, reason, age_days}`) et cet outil n'en gardait que le
            #   nom, en écrivant « hors service OU en maintenance ». Ava reprenait cette
            #   ambiguïté et la renvoyait à l'admin en question — « tu l'as mis
            #   volontairement ou c'est une surprise ? » — alors que la réponse est écrite
            #   dans le registre. Mesuré le 2026-08-05 sur pve-02.
            # ⚠ `age_days` compte AUTANT que le motif : ce champ existe pour rendre visible
            #   un arrêt « temporaire » qui dure depuis des semaines. Le taire vide le champ
            #   de sa raison d'être.
            morceaux = []
            for h in inactifs[:4]:
                nom = str(h.get("display_name") or h.get("id"))
                m = h.get("maintenance") or {}
                motif = str(m.get("reason") or "").strip()
                jours = m.get("age_days")
                if motif and jours is not None:
                    morceaux.append(f"{nom} ({motif}, depuis {jours:.0f} j)")
                elif motif:
                    morceaux.append(f"{nom} ({motif})")
                elif m.get("mode"):
                    morceaux.append(f"{nom} ({m['mode']})")
                else:
                    # ⚠ Sans entrée de maintenance, l'hôte est inactif SANS raison déclarée
                    #   — ce qui n'est pas la même chose qu'un arrêt volontaire, et mérite
                    #   d'être dit tel quel plutôt que fondu dans un « ou ».
                    morceaux.append(f"{nom} (inactif, aucune maintenance déclarée)")
            detail = " — " + " ; ".join(morceaux)
        lines.append(f"Hôtes: {len(hosts)} déclarés, {len(inactifs)} inactifs{detail}")
    else:
        # ⚠ On dit qu'on ne sait pas, plutôt que d'écrire « 0 hôte » — c'est exactement
        #   l'erreur qu'on corrige ici.
        lines.append("Hôtes: information indisponible (endpoint /hosts injoignable)")

    # ⚠ LES SAUVEGARDES SE DISENT MEME QUAND TOUT VA BIEN — et c'est tout l'interet.
    #   Ce resume ne listait que les modules EN DEGRADATION : un module au maximum n'y
    #   figure nulle part. Le 2026-08-05, a la question « est-ce que je peux dormir
    #   tranquille », Ava a donc repondu « pas de module backup dans le Control Plane »,
    #   alors qu'il expose l'age du dernier backup, celui du controle d'integrite et le
    #   resultat des tests de RESTAURATION sur les trois copies du 3-2-1.
    # ⚠ La lecon depasse ce module : certaines questions demandent une preuve POSITIVE,
    #   pas l'absence de plainte. « Rien ne va mal » ne repond pas a « est-ce protege ».
    sauvegardes = _resume_sauvegardes(data)
    if sauvegardes:
        lines.append(sauvegardes)

    return "\n".join(lines)


@ToolRegistry.register("avalon_status")
class AvalonStatusTool(BaseTool):
    """Interroge le Control Plane v2 et résume létat global de linfra."""

    tool_id = "avalon_status"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="avalon_status",
            description=(
                "Etat de l'infrastructure Avalon depuis le Control Plane v2. "
                "Sans argument : score 0-100, modules en degradation, hotes "
                "inactifs avec le motif et l'anciennete, etat des sauvegardes. "
                "Avec `domaine` : le DETAIL d'un module — machines virtuelles et "
                "noeuds Proxmox, evenements de la camera, securite reseau, "
                "sauvegardes, certificats, conteneurs... "
                "A utiliser des qu'une question porte sur l'infra, y compris sur "
                "un point precis : le detail par domaine EXISTE, ne reponds jamais "
                "que tu ne l'as pas sans avoir essaye."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domaine": {
                        "type": "string",
                        "description": (
                            "Optionnel. Nom du module du control plane dont on veut le "
                            "DETAIL : proxmox (machines virtuelles, noeuds, haute "
                            "disponibilite, replication), frigate (camera et evenements), "
                            "nsm (securite reseau), backups, wazuh, tls, docker, "
                            "home_assistant, gitlab, http_health... Sans ce parametre, "
                            "rend le resume global."
                        ),
                    }
                },
                "required": [],
            },
            category="infra",
            latency_estimate=1.0,
            timeout_seconds=10.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            data = _fetch()
        except urllib.error.URLError as exc:
            return ToolResult(
                tool_name=self.tool_id,
                # ⚠ Etait `CP_V2_URL` — un nom QUI N'EXISTE PAS (la constante s'appelle
                #   `CP_V2_BASE`). Ce chemin ne s'emprunte que si le control plane est
                #   injoignable : le jour ou il l'aurait ete, l'outil aurait leve un
                #   `NameError` AU LIEU d'afficher son message d'erreur. Un gestionnaire
                #   d'erreur casse ne se voit jamais tant que l'erreur ne survient pas —
                #   c'est-a-dire jamais avant le pire moment.
                #   Trouve par `ruff` (F821) le 2026-08-04, en branchant la CI du fork.
                content=f"Impossible de joindre le Control Plane v2 ({CP_V2_BASE}): {exc}",
                success=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Erreur inattendue: {exc}",
                success=False,
            )
        domaine = str(params.get("domaine") or "").strip()
        summary = _format_domaine(data, domaine) if domaine else _format_summary(data)
        return ToolResult(
            tool_name=self.tool_id,
            content=summary,
            success=True,
            metadata={"global_score": data.get("score", {}).get("global")},
        )
