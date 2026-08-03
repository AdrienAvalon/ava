#!/usr/bin/env bash
# Configure `git push` pour alimenter LES DEUX miroirs d'Ava : GitHub et GitLab.
#
# ⚠️ POURQUOI UN SCRIPT ET PAS UNE LIGNE DE DOCUMENTATION
# --------------------------------------------------------
# Cette configuration vit dans `.git/config`, qui n'est PAS versionne. Un nouveau
# clone repart donc sans elle, et le miroir recommence a geler en silence — ce qui
# s'est exactement produit : GitLab est reste fige du 2026-04-18 au 2026-08-03, et
# le depot local n'avait meme plus GitLab dans ses remotes. Une consigne qu'on peut
# oublier d'appliquer n'est pas une protection ; un script qu'on relance sur un
# clone neuf en est une.
#
# ⚠️ LES `pushurl` REMPLACENT L'URL DE PUSH. GitHub doit donc etre redeclare
#    explicitement, sinon on ne pousserait plus QUE vers GitLab — en croyant
#    alimenter les deux pendant que le miroir public ne recoit plus rien.
#
# ⚠️ CONTREPARTIE ASSUMEE : `git push` depend desormais de la joignabilite des DEUX
#    services. Si l'un est indisponible, la commande signale un echec meme si l'autre
#    a reussi. Sans dommage, mais a savoir pour ne pas se tromper de diagnostic.
set -euo pipefail

RACINE="$(git rev-parse --show-toplevel)"
cd "$RACINE"

GITHUB="https://github.com/AdrienAvalon/ava.git"
GITLAB="https://gitlab.avalon-network.com/avalon/ava.git"
ASSISTANT="$RACINE/scripts/git-credential-avalon-sops.sh"

[ -x "$ASSISTANT" ] || { echo "assistant d'identifiants introuvable ou non executable : $ASSISTANT" >&2; exit 1; }

# `--replace-all` rend le script IDEMPOTENT : le relancer ne empile pas les entrees.
# Sans lui, une seconde execution ajouterait un doublon de pushurl et git pousserait
# deux fois vers la meme destination.
git config remote.origin.url "$GITHUB"
git config --unset-all remote.origin.pushurl 2>/dev/null || true
git config --add remote.origin.pushurl "$GITHUB"
git config --add remote.origin.pushurl "$GITLAB"
git config --replace-all credential.helper "$ASSISTANT"

echo "origin configure — un seul \`git push\` alimente :"
git config --get-all remote.origin.pushurl | sed 's/^/  · /'
echo "identifiants : $ASSISTANT (jetons lus dans SOPS, jamais dans .git/config)"
echo
echo "Controle recommande — les deux miroirs doivent porter le meme SHA :"
echo "  git ls-remote origin ava-main"
