"""Arrêt propre demandé par le processus parent, via l'entrée standard.

Sous Windows, un processus parent ne peut pas envoyer Ctrl+C à un enfant sans console : il ne lui reste que
l'arrêt sec, qui coupe le bot au milieu d'un cycle. Avec `tradeagent run --stop-on-stdin`, le parent
(l'application de bureau) garde l'entrée standard du bot ouverte et y écrit `stop` pour demander l'arrêt.

- Le bot finit son cycle en cours, puis s'arrête comme après un Ctrl+C : positions conservées, état affiché.
- Si l'entrée standard se ferme (le parent a planté ou a été tué), c'est aussi un arrêt : un bot ne continue
  jamais à tourner sans personne pour le surveiller.
- Ce n'est pas un canal de commande : une seule chose est comprise, « arrête-toi ». Tout autre texte est ignoré.
"""
from __future__ import annotations

import sys
import threading
from typing import IO

STOP_WORD = "stop"


class StdinStop:
    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = sys.stdin if stream is None else stream
        self._event = threading.Event()
        self.reason = ""

    def start(self) -> "StdinStop":
        threading.Thread(target=self._watch, name="stdin-stop", daemon=True).start()
        return self

    def _watch(self) -> None:
        reason = "entrée standard fermée"
        try:
            for line in self._stream:
                if line.strip().lower() == STOP_WORD:
                    reason = "arrêt demandé"
                    break
        except (OSError, ValueError):   # flux fermé ou invalide : même conclusion, on s'arrête
            pass
        self.reason = reason
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        """Remplace `time.sleep` entre deux cycles : rend la main dès que l'arrêt est demandé."""
        self._event.wait(seconds)
