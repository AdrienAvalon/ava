#!/usr/bin/env bash
# Push la branche courante sur GitHub + GitLab (les deux pushURL configurés sur origin)
set -euo pipefail
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ]; then
  echo "ERROR: ne pas push sur main (miroir strict upstream). Utilise ava-main ou une branche feature." >&2
  exit 1
fi
git push origin "$BRANCH"
