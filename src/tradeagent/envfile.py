"""Lecture minimale d'un fichier .env (sans dépendance). Ne remplace jamais une variable déjà définie."""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> list[str]:
    """Charge KEY=VALUE dans os.environ. Retourne les noms chargés (jamais les valeurs)."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
