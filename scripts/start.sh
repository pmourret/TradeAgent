#!/usr/bin/env sh
# Bot(s) + interface(s) web ensemble, dans ce terminal. Ctrl+C arrête tout proprement. PAPER TRADING uniquement.
#
#   scripts/start.sh               profil hold (la référence, gratuit)
#   scripts/start.sh llm           le vrai LLM (clé requise, l'API est facturée, plafonnée par le code)
#   scripts/start.sh hold llm      les deux côte à côte, pour comparer l'agent à « ne rien faire »
#   scripts/start.sh demo          hors ligne, pour voir l'interface s'animer
#
# Options : --no-ui (bots seulement), --no-open (n'ouvre pas le navigateur)
set -eu
. "$(dirname "$0")/_common.sh"
exec "$PY" -m tradeagent up "$@"
