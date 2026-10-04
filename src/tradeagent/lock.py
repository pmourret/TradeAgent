"""Un seul `run` par base de données.

Deux bots sur la même base se marcheraient dessus (deux cycles par cycle, soldes écrasés, kill switch
contourné). Les scripts de lancement rendent le double démarrage facile (double-clic, deux terminaux) :
on le refuse donc explicitement, avec un verrou du système d'exploitation qui disparaît tout seul si le
processus meurt (pas de fichier « périmé » à nettoyer à la main).
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import ConfigError

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

WINDOWS_LOCK_OFFSET = 1 << 20


class InstanceLock:
    """Le verrou vit aussi longtemps que cet objet : garde une référence dessus (ou utilise `with`)."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(str(database) + ".lock")
        self._file = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                # Windows verrouille des octets, et un octet verrouillé est illisible par les autres processus :
                # on verrouille loin après le contenu pour que le PID écrit plus bas reste lisible.
                handle.seek(WINDOWS_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise ConfigError(
                f"un autre `tradeagent run` utilise déjà cette base ({self.path.with_suffix('')}). "
                "Deux bots sur la même base se marcheraient dessus : arrête l'autre d'abord "
                "(ou utilise un autre profil)."
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._file = handle
        return self

    def release(self) -> None:
        if self._file is not None:
            try:
                self._file.close()   # fermer le fichier libère le verrou
            finally:
                self._file = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
