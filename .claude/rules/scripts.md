---
paths:
  - "scripts/**"
---

# Scripts de lancement

- Toujours par paires : `.sh` POSIX (`#!/usr/bin/env sh`, exécutable, fins de ligne LF) **et** `.bat` (ASCII, CRLF). `.gitattributes` fixe les fins de ligne.
- Fines enveloppes : elles n'appellent que des sous-commandes existantes de `tradeagent` ; `tests/test_scripts.py` le vérifie.
- `live.*` refuse, volontairement.
- Sous Windows, seul `paper.bat` a été lancé pour de vrai ; les `.sh` ne sont testés que sous Linux.
