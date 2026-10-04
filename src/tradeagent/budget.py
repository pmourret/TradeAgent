"""Budget d'inférence : le « loyer » de l'agent.

Chaque appel au LLM coûte de l'argent réel. Les coûts sont journalisés (table `llm_calls`)
et plafonnés par jour (UTC) et au total. Budget épuisé : l'agent ne peut plus décider,
mais le moteur et le kill switch continuent de tourner.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from .config import LLMConfig
from .llm import LLMUsage
from .storage import Storage

CACHE_READ_FACTOR = 0.10   # lecture du cache de prompt : 10 % du prix d'entrée
CACHE_WRITE_FACTOR = 1.25  # écriture dans le cache (5 min) : 125 % du prix d'entrée


def day_start_ts(now: float) -> float:
    """Minuit UTC du jour de `now`."""
    d = datetime.fromtimestamp(now, tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class InferenceBudget:
    def __init__(self, cfg: LLMConfig, storage: Storage, clock: Callable[[], float] = time.time) -> None:
        self._cfg = cfg
        self._storage = storage
        self._clock = clock

    def cost_eur(self, usage: LLMUsage) -> float:
        c = self._cfg
        usd = (
            usage.input_tokens * c.price_input_per_mtok_usd
            + usage.output_tokens * c.price_output_per_mtok_usd
            + usage.cache_read_tokens * c.price_input_per_mtok_usd * CACHE_READ_FACTOR
            + usage.cache_write_tokens * c.price_input_per_mtok_usd * CACHE_WRITE_FACTOR
        ) / 1_000_000
        return usd * c.usd_to_eur

    def spent_today(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return self._storage.llm_spend_since(day_start_ts(now))

    def spent_total(self) -> float:
        return self._storage.llm_spend_since(0.0)

    def left(self, now: float | None = None) -> dict[str, float]:
        return {
            "today": round(max(0.0, self._cfg.daily_budget_eur - self.spent_today(now)), 4),
            "total": round(max(0.0, self._cfg.total_budget_eur - self.spent_total()), 4),
        }

    def can_spend(self, now: float | None = None) -> tuple[bool, str]:
        total = self.spent_total()
        if total >= self._cfg.total_budget_eur:
            return False, f"budget total épuisé ({total:.2f}/{self._cfg.total_budget_eur:.2f} €)"
        today = self.spent_today(now)
        if today >= self._cfg.daily_budget_eur:
            return False, f"budget du jour épuisé ({today:.2f}/{self._cfg.daily_budget_eur:.2f} €)"
        return True, ""

    def record(self, usage: LLMUsage, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        cost = self.cost_eur(usage)
        self._storage.record_llm_call(
            now, self._cfg.model, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, usage.cache_write_tokens, cost,
        )
        return cost
