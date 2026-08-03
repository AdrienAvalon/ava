#!/usr/bin/env bash
# Assistant d'identifiants git pour les DEUX miroirs d'Ava — jetons lus dans SOPS.
#
# POURQUOI CE FICHIER PLUTOT QU'UN GIT_ASKPASS PONCTUEL
# ------------------------------------------------------
# Le miroir GitLab d'Ava a gele le 2026-04-18 et n'a ete rattrape que le 2026-08-03,
# avec 16 commits de retard — dont la totalite du travail de reprise du projet. Le
# depot local n'avait meme plus GitLab dans ses remotes : la synchro n'etait pas
# "en panne", elle n'existait plus, et rien ne pouvait le signaler.
# C'est la meme histoire que le miroir GitHub d'infra_avalon (475 commits perdus de
# vue en juin-juillet 2026). Un mecanisme qu'il faut penser a declencher n'est pas
# un mecanisme. Celui-ci s'applique a chaque `git push`, sans geste supplementaire.
#
# ⚠️ LES JETONS NE PASSENT NI PAR argv NI PAR .git/config. Git parle a cet assistant
#    par un protocole texte sur stdin/stdout : le secret ne transite que par ce canal,
#    jamais par une ligne de commande (`/proc/<pid>/cmdline` est world-readable) ni par
#    un fichier de configuration qu'un `git config -l` ou une sauvegarde exposerait.
#
# ⚠️ DEUX HOTES, DEUX JETONS — et c'est pour ça que l'assistant LIT `host=` sur stdin
#    au lieu de renvoyer un secret fixe. Repondre le jeton GitHub a une demande GitLab
#    produirait un echec d'authentification incomprehensible ; pire, cela enverrait un
#    jeton a un service qui n'a rien a en faire.
#
# ⚠️ IL NE PEUT RIEN FORCER. Il authentifie, c'est tout. Le push reste ORDINAIRE : il
#    avance en fast-forward ou il echoue. Toute reparation apres divergence
#    (`--force-with-lease`) demeure une action humaine deliberee.
#
# Installation : scripts/setup-mirrors.sh
set -euo pipefail

# Git n'appelle un assistant qu'avec `get`, `store` ou `erase`. On ne repond qu'a `get` :
# rien a stocker (la source de verite est SOPS) et rien a effacer.
[ "${1:-}" = "get" ] || exit 0

# ⚠️ Le coffre vit dans le depot infra_avalon, PAS ici : Ava est un fork public d'un
#    projet tiers et n'a aucune raison de porter des secrets d'infrastructure.
#    Surchargeable pour un clone range ailleurs.
COFFRE="${AVALON_SOPS_VAULT:-$HOME/Documents/projets/infra_avalon/secrets/infra.yml}"
[ -f "$COFFRE" ] || exit 0

# Git ecrit `protocol=…`, `host=…`, `path=…` sur stdin, une paire par ligne.
HOTE=""
while IFS='=' read -r cle valeur; do
  [ -n "$cle" ] || break          # ligne vide = fin de la requete
  [ "$cle" = "host" ] && HOTE="$valeur"
done

extraire() { sops -d --extract "$1" "$COFFRE" 2>/dev/null || true; }

case "$HOTE" in
  github.com)
    JETON="$(extraire '["github"]["token"]')"
    UTILISATEUR="AdrienAvalon"
    ;;
  gitlab.avalon-network.com)
    JETON="$(extraire '["gitlab"]["infra_automation_token"]')"
    # `oauth2` est le nom d'utilisateur attendu par GitLab pour un jeton d'acces.
    UTILISATEUR="oauth2"
    ;;
  *)
    exit 0
    ;;
esac

# Sortie muette si le dechiffrement echoue (cle age absente, coffre verrouille) : git
# demandera alors les identifiants au lieu de partir en erreur obscure.
[ -n "$JETON" ] || exit 0

printf 'username=%s\n' "$UTILISATEUR"
printf 'password=%s\n' "$JETON"
