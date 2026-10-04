#!/usr/bin/env sh
# L'interface web seule (lecture seule, locale), pour regarder un bot lancé ailleurs. Ctrl+C pour arrêter.
#
#   scripts/ui.sh [hold|llm|demo]      ports : hold 8765, llm 8766, demo 8767
#
# Pour lancer bot ET interface d'un coup : scripts/start.sh
set -eu
. "$(dirname "$0")/_common.sh"
profile="hold"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then profile="$1"; shift; fi
exec "$PY" -m tradeagent web --profile "$profile" --open "$@"
