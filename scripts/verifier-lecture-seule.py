#!/usr/bin/env python3
"""Vérifie qu'un module Python ne contient AUCUN chemin d'écriture HTTP.

⚠ POURQUOI CE FICHIER REMPLACE UN `grep`, ET POURQUOI ÇA COMPTE.
  L'invariant « l'outil Home Assistant reste en LECTURE SEULE » était appliqué par
  `grep -nE '"POST"|requests\\.post|method=.POST'` — dans le workflow CI ET dans
  `test_ha_est_en_lecture_seule`. Or `home_assistant.py` n'importe ni `requests` ni
  n'écrit de `method=` : il fait exclusivement du `urllib`.

  Et **dans urllib, il suffit de passer `data=` pour basculer en POST** :

      urllib.request.Request(url, data=json.dumps(charge).encode())

  Aucun des trois motifs n'y apparaît. Mesuré : le grep rend 1 (aucune
  correspondance) sur ce code, donc la CI resterait VERTE en présence d'une écriture
  bien réelle. L'invariant portait le titre « INVARIANT DE SÉCURITÉ » et ne gardait
  rien — c'est-à-dire pire qu'un garde-fou absent, parce qu'on cesse de vérifier à la
  main ce qu'on le croit vérifier.

  L'enjeu n'est pas théorique : le jeton HA autorise l'ÉCRITURE (allumer, chauffer,
  ouvrir la porte). Ava exécute du code communautaire et vit en DMZ. Tout le montage
  d'accès — le CP v2 dual-homé en intermédiaire plutôt qu'une règle DMZ → LAN — n'a
  de valeur que si cet outil reste incapable de commander quoi que ce soit.

⚠ POURQUOI L'AST PLUTÔT QU'UN MOTIF PLUS MALIN. Une expression régulière raisonne sur
  du texte : elle ne sait pas qu'`urlopen(req, corps)` passe `corps` en `data` par
  POSITION, ni qu'un alias `from urllib.request import Request as R` change le nom
  affiché. L'AST voit la STRUCTURE de l'appel. On reste néanmoins conservateur :
  ce détecteur signale tout ce qui ressemble à une écriture, quitte à demander une
  justification explicite. Un faux positif coûte une ligne de commentaire ; un faux
  négatif coûte le contrôle de la maison.

⚠ UN SEUL DÉTECTEUR, DEUX CONSOMMATEURS — la CI (`ava-ci.yml`) et la suite de tests
  (`test_skills.py`) l'appellent tous les deux. Deux implémentations du même invariant
  divergent : c'était déjà le cas (le grep existait en double, et les deux copies
  étaient aveugles de la même façon).

Usage :
    python3 scripts/verifier-lecture-seule.py <fichier.py> [<fichier.py> ...]
Sortie : 0 si aucun chemin d'écriture, 1 sinon (avec le détail sur stderr).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Verbes HTTP qui MODIFIENT l'état distant. `GET` et `HEAD` sont les seuls admis.
VERBES_ECRITURE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Méthodes de commodité des bibliothèques HTTP courantes (`requests`, `httpx`,
# `aiohttp`…). On teste l'ATTRIBUT appelé, pas le nom du module : `requests.post`,
# `httpx.post`, `session.post`, `client.put`… se ramènent tous à un attribut d'écriture.
ATTRIBUTS_ECRITURE = frozenset({"post", "put", "patch", "delete"})


class DetecteurEcriture(ast.NodeVisitor):
    """Relève les appels HTTP susceptibles de modifier l'état distant."""

    def __init__(self, source: str) -> None:
        self.constats: list[tuple[int, str]] = []
        self._lignes = source.splitlines()
        # ⚠ ALIAS D'IMPORT — trou trouvé par les tests de ce détecteur, pas par
        #   raisonnement. `from urllib.request import Request as R` puis `R(url, data=c)`
        #   est une écriture bien réelle dont le nom appelé est « R » : sans cette table,
        #   le détecteur reproduisait le défaut du grep qu'il remplace, en plus subtil.
        #   Renommer suffisait à le contourner — et ce n'aurait même pas eu besoin d'être
        #   intentionnel : `import requests as rq` est une convention répandue.
        self._alias: dict[str, str] = {}

    def _signaler(self, noeud: ast.AST, raison: str) -> None:
        ligne = getattr(noeud, "lineno", 0)
        extrait = (
            self._lignes[ligne - 1].strip() if 0 < ligne <= len(self._lignes) else ""
        )
        self.constats.append((ligne, f"{raison} — {extrait[:100]}"))

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for a in node.names:
            self._alias[a.asname or a.name.split(".")[0]] = a.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for a in node.names:
            self._alias[a.asname or a.name] = f"{node.module or ''}.{a.name}"
        self.generic_visit(node)

    def _nom_appele(self, noeud: ast.Call) -> str:
        """Nom canonique de la fonction appelée, alias d'import résolus.

        `R(…)` après `from urllib.request import Request as R` rend
        `urllib.request.Request` — c'est ce qui permet de raisonner sur ce qui est
        RÉELLEMENT appelé plutôt que sur le nom qu'on a choisi de lui donner.
        """
        cible: ast.AST = noeud.func
        morceaux: list[str] = []
        while isinstance(cible, ast.Attribute):
            morceaux.append(cible.attr)
            cible = cible.value
        if isinstance(cible, ast.Name):
            morceaux.append(self._alias.get(cible.id, cible.id))
        return ".".join(reversed(morceaux))

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (nom impose par ast)
        nom = self._nom_appele(node)
        dernier = nom.rsplit(".", 1)[-1] if nom else ""
        nommes = {kw.arg for kw in node.keywords if kw.arg}

        # ⚠ LE CAS QUI ÉCHAPPAIT AU GREP : `Request(url, data=…)`. Un corps sur une
        #   Request bascule la méthode en POST — sans qu'aucun verbe n'apparaisse.
        #   `data` peut aussi arriver en 2e POSITION : `Request(url, corps)`.
        if dernier == "Request" and ("data" in nommes or len(node.args) >= 2):
            self._signaler(node, "Request avec un corps (`data`) → POST implicite")

        # Idem pour `urlopen(req, data)` : 2e argument positionnel = corps.
        if dernier == "urlopen" and ("data" in nommes or len(node.args) >= 2):
            self._signaler(node, "urlopen avec un corps (`data`) → POST implicite")

        # Méthodes de commodité : requests.post, session.put, httpx.patch…
        if dernier in ATTRIBUTS_ECRITURE and isinstance(node.func, ast.Attribute):
            self._signaler(node, f"appel `.{dernier}(…)` d'une bibliothèque HTTP")

        # Verbe explicite, quel que soit son emplacement : `method="POST"`,
        # `Request(…, method=verbe)`, `conn.request("PUT", …)`.
        for kw in node.keywords:
            if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                if str(kw.value.value).upper() in VERBES_ECRITURE:
                    self._signaler(node, f"method={kw.value.value!r}")
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.upper() in VERBES_ECRITURE:
                    self._signaler(node, f"verbe {arg.value!r} en argument")

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        # ⚠ `req.method = "POST"` après construction : forme légale d'urllib, invisible
        #   à l'analyse des seuls appels.
        for cible in node.targets:
            if isinstance(cible, ast.Attribute) and cible.attr == "method":
                v = node.value
                if (
                    isinstance(v, ast.Constant)
                    and str(v.value).upper() in VERBES_ECRITURE
                ):
                    self._signaler(node, f"affectation .method = {v.value!r}")
        self.generic_visit(node)


def analyser(source: str) -> list[tuple[int, str]]:
    """Constats d'écriture dans `source`. Liste vide = lecture seule."""
    arbre = ast.parse(source)
    detecteur = DetecteurEcriture(source)
    # ⚠ PRÉ-PASSE SUR LES IMPORTS, et ce n'est pas de la précaution gratuite. Le
    #   parcours de `visit()` suit l'ordre du fichier : un import placé APRÈS l'appel
    #   (dans une fonction, ou en bas du module pour casser un cycle — forme courante)
    #   ne serait pas encore connu au moment de résoudre l'alias. Le détecteur
    #   marcherait alors par CHANCE, selon la mise en page du fichier analysé, et
    #   échouerait précisément sur le code écrit pour le contourner.
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            detecteur.visit_Import(noeud)
        elif isinstance(noeud, ast.ImportFrom):
            detecteur.visit_ImportFrom(noeud)
    detecteur.constats.clear()  # la pré-passe ne doit rien signaler par elle-même
    detecteur.visit(arbre)
    return sorted(set(detecteur.constats))


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: verifier-lecture-seule.py <fichier.py> [...]", file=sys.stderr)
        return 2
    echec = False
    for chemin in argv:
        p = Path(chemin)
        try:
            constats = analyser(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"{chemin}: illisible ({e})", file=sys.stderr)
            return 2
        for ligne, raison in constats:
            # Format reconnu par GitHub Actions : le constat s'affiche sur la ligne.
            print(
                f"::error file={chemin},line={ligne}::chemin d'écriture HTTP — {raison}"
            )
            echec = True
        if not constats:
            print(f"  ✓ {chemin} — lecture seule")
    return 1 if echec else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
