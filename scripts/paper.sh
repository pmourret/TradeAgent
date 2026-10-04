#!/usr/bin/env sh
# Le bot en PAPER TRADING (argent fictif), au premier plan, sans interface. Ctrl+C pour arrêter.
#
#   scripts/paper.sh            profil hold : vrais prix, l'agent ne fait rien (la référence, gratuit)
#   scripts/paper.sh llm        vrais prix + vrai LLM Anthropic (clé requise, l'API est facturée)
#   scripts/paper.sh demo       hors ligne : prix simulés + agent aléatoire, un cycle toutes les 2 s
#
# Les options suivantes sont transmises à `tradeagent run`, ex. :  scripts/paper.sh hold --max-cycles 1
# Chaque profil a sa propre base (data/paper-<profil>.db) : ils ne se mélangent jamais.
set -eu
. "$(dirname "$0")/_common.sh"
profile="hold"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then profile="$1"; shift; fi
exec "$PY" -m tradeagent run --profile "$profile" "$@"
