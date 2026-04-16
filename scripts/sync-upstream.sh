#!/usr/bin/env bash
# Sync main sur upstream/main + merge dans ava-main
set -euo pipefail
git fetch upstream
git checkout main
git merge --ff-only upstream/main
git push origin main
git checkout ava-main
git merge main
echo "OK : ava-main contient maintenant upstream/main. Pousse avec scripts/git-push-all.sh"
