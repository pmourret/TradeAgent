"""Interface commune des exchanges (réel ou simulé)."""
from __future__ import annotations

from typing import Protocol

from .models import Candle, Fill, Quote


class ExchangeError(Exception):
    """Échec côté exchange ou flux de prix (réseau, fonds insuffisants, etc.)."""


class Exchange(Protocol):
    def get_quote(self, symbol: str) -> Quote: ...

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]: ...

    def get_balances(self) -> dict[str, float]: ...

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill: ...
