#!/usr/bin/env sh
# MODE RÉEL : refusé, volontairement. Ce script existe pour que la réponse soit claire et au même endroit.
# Le code ne sait pas passer d'ordre sur un vrai compte : voir le README, « Chemin vers l'argent réel ».
cd "$(dirname "$0")/.." || exit 1
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m tradeagent run --profile live
fi
echo "Le mode réel n'existe pas encore dans ce projet (voir README, « Chemin vers l'argent réel »)." >&2
exit 2
