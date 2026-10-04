#!/usr/bin/env sh
# L'état d'un profil (ou de tous ceux qui ont déjà tourné) : equity, résultat net après coût de l'API, soldes.
#
#   scripts/status.sh           tous les profils qui ont déjà tourné
#   scripts/status.sh llm       un seul
set -eu
. "$(dirname "$0")/_common.sh"
if [ $# -gt 0 ]; then
  exec "$PY" -m tradeagent status --profile "$1"
fi
exec "$PY" -m tradeagent status --all
