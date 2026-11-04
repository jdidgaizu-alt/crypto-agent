#!/usr/bin/env bash
# Un cycle sur le livre versionné, puis commit et push du livre.
# À utiliser pour les cycles planifiés : les sessions cloud sont éphémères, seul GitHub garde l'historique.
#
# Le livre est une base SQLite binaire : git ne sait pas fusionner deux versions. Le script part donc
# toujours de la dernière version publiée, et si quelqu'un d'autre a publié entre-temps, il s'arrête
# sans rien fusionner : le cycle local est abandonné, l'historique publié reste intact.
set -euo pipefail
cd "$(dirname "$0")/.."

branch="${CYCLE_BRANCH:-main}"
git fetch --quiet origin "$branch"
if ! git diff --quiet HEAD -- ledger/book.db; then
  echo "le livre local contient des modifications non publiées : publiez-les ou annulez-les d'abord" >&2
  exit 1
fi
git merge --ff-only --quiet "origin/$branch"

python -m crypto_agent.cycle

git add ledger/book.db
if git diff --cached --quiet; then
  echo "livre inchangé : rien à publier"
  exit 0
fi
git commit --quiet -m "Cycle du $(date -u +%Y-%m-%dT%H:%MZ)"
if ! git push --quiet origin "HEAD:$branch"; then
  echo "publication refusée : un autre cycle a publié le livre entre-temps." >&2
  echo "Ce cycle n'est PAS publié (pas de fusion possible d'une base binaire). Annulez-le avec :" >&2
  echo "  git reset --hard origin/$branch" >&2
  exit 1
fi
echo "livre publié"
