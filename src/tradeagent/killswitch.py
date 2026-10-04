"""Kill switch : indépendant de l'agent, état persistant.

Trois états :
- alive  : le bot trade.
- halted : arrêt opérationnel (trop d'erreurs d'affilée). Pas de liquidation : on ne touche
           à rien tant qu'un humain n'a pas regardé. `resume` le débloque.
- dead   : la mise est perdue au-delà des seuils. Définitif : seul `reset` (nouvelle vie) repart.
"""
from __future__ import annotations

import time
from typing import Callable

from .config import KillSwitchConfig
from .storage import Storage

ALIVE = "alive"
HALTED = "halted"
DEAD = "dead"
KEY = "killswitch"


class KillSwitchError(RuntimeError):
    pass


class KillSwitch:
    def __init__(self, cfg: KillSwitchConfig, storage: Storage, stake: float,
                 clock: Callable[[], float] = time.time) -> None:
        self._cfg = cfg
        self._storage = storage
        self._stake = stake
        self._clock = clock
        self._consecutive_errors = 0

    # -- état ------------------------------------------------------------
    def _state(self) -> dict:
        return self._storage.get(KEY) or {"status": ALIVE, "reason": "", "ts": None}

    @property
    def status(self) -> str:
        return self._state()["status"]

    @property
    def reason(self) -> str:
        return self._state()["reason"]

    @property
    def active(self) -> bool:
        """Vrai si le bot ne doit plus trader (halted ou dead)."""
        return self.status != ALIVE

    # -- seuils financiers -----------------------------------------------
    def check_financial(self, equity: float, peak_equity: float) -> str | None:
        """Retourne la raison de la mort si un seuil est franchi, sinon None."""
        loss_floor = self._stake * (1 - self._cfg.max_total_loss_pct / 100)
        if equity <= loss_floor:
            return (
                f"perte totale : equity {equity:.2f} <= {loss_floor:.2f} "
                f"({self._cfg.max_total_loss_pct:g} % de la mise {self._stake:.2f} perdus)"
            )
        dd_floor = peak_equity * (1 - self._cfg.max_drawdown_pct / 100)
        if peak_equity > 0 and equity <= dd_floor:
            return (
                f"drawdown max : equity {equity:.2f} <= {dd_floor:.2f} "
                f"(-{self._cfg.max_drawdown_pct:g} % depuis le plus haut {peak_equity:.2f})"
            )
        return None

    # -- transitions -----------------------------------------------------
    def _set(self, status: str, reason: str) -> None:
        self._storage.set(KEY, {"status": status, "reason": reason, "ts": self._clock()})

    def die(self, reason: str) -> None:
        if self.status == DEAD:
            return  # la première cause de mort est conservée
        self._set(DEAD, reason)

    def halt(self, reason: str) -> None:
        if self.status == ALIVE:  # on n'écrase ni un arrêt existant ni une mort
            self._set(HALTED, reason)

    def resume(self) -> None:
        status = self.status
        if status == DEAD:
            raise KillSwitchError("un bot mort ne se relance pas : utilise `reset` pour une nouvelle vie")
        if status == HALTED:
            self._storage.delete(KEY)
        self._consecutive_errors = 0

    # -- erreurs opérationnelles -----------------------------------------
    def note_error(self, reason: str) -> bool:
        """Compte une erreur. Retourne True si ce cumul vient de déclencher l'arrêt."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._cfg.max_consecutive_errors:
            self.halt(f"{self._consecutive_errors} erreurs d'affilée, dernière : {reason}")
            return True
        return False

    def note_success(self) -> None:
        self._consecutive_errors = 0
