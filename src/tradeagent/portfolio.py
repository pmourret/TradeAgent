"""Vue du portefeuille à un instant donné."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Quote


def base_currency(symbol: str) -> str:
    return symbol.split("/")[0]


@dataclass(frozen=True)
class Portfolio:
    quote_currency: str
    cash: float
    positions: dict[str, float]  # symbole -> quantité en monnaie de base
    quotes: dict[str, Quote]     # symbole -> dernier prix connu

    def price(self, symbol: str) -> float:
        return self.quotes[symbol].price

    def position_value(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0) * self.price(symbol)

    @property
    def exposure(self) -> float:
        return sum(self.position_value(s) for s in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + self.exposure
