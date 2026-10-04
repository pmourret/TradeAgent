#!/usr/bin/env sh
# Installation (à faire une fois) : environnement Python isolé (.venv), dépendances, fichier .env,
# puis vérification par les tests (hors ligne, quelques secondes).
set -eu
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.10 ou plus introuvable. Installe-le (https://www.python.org/downloads/) puis relance." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 minimum requis (trouvé : $("$PYTHON" --version 2>&1))." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Création de l'environnement .venv…"
  "$PYTHON" -m venv .venv
fi
echo "Installation des dépendances…"
.venv/bin/python -m pip install --quiet --upgrade pip || true
.venv/bin/python -m pip install --quiet -e ".[dev]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Fichier .env créé (ta clé Anthropic n'y est nécessaire que pour le profil llm)."
fi

echo "Vérification (tests hors ligne)…"
.venv/bin/python -m pytest -q

echo
echo "Prêt. Pour essayer tout de suite, sans clé ni réseau :   scripts/start.sh demo"
