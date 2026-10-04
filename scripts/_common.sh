# Fichier partagé, sourcé par les autres scripts (ne se lance pas seul).
# Se place à la racine du projet (config.yaml, .env et data/ y sont relatifs) et trouve le Python du venv.
cd "$(dirname "$0")/.." || exit 1
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Environnement absent : lance d'abord  scripts/setup.sh" >&2
  exit 1
fi
