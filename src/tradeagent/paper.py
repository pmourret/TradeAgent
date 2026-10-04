"""Exchange simulé : prix réels (ou synthétiques), exécution locale avec frais et glissement.

Le solde est persisté : relancer le programme ne remet pas la mise à zéro.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from .config import Config
from .exchange import ExchangeError
from .feeds import PriceFeed
from .models import BUY, SELL, Candle, Fill, Quote
from .portfolio import base_currency
from .storage import Storage

_EPS = 1e-9
BALANCES_KEY = "paper_balances"


class PaperExchange:
    def __init__(self, cfg: Config, feed: PriceFeed, storage: Storage,
                 clock: Callable[[], float] = time.time) -> None:
        self._feed = feed
        self._storage = storage
        self._clock = clock
        self._quote_currency = cfg.quote_currency
        self._symbols = set(cfg.symbols)
        self._fee_rate = cfg.costs.fee_rate
        self._slippage = cfg.costs.slippage_bps / 10_000

        balances = storage.get(BALANCES_KEY)
        if balances is None:
            balances = {cfg.quote_currency: cfg.stake}
            for symbol in cfg.symbols:
                balances[base_currency(symbol)] = 0.0
            storage.set(BALANCES_KEY, balances)
        self._balances: dict[str, float] = balances

    def get_quote(self, symbol: str) -> Quote:
        return self._feed.get_quote(symbol)

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]:
        return self._feed.get_candles(symbol, timeframe, limit)

    def get_balances(self) -> dict[str, float]:
        return dict(self._balances)

    def market_order(self, symbol: str, side: str, quantity: float) -> Fill:
        if symbol not in self._symbols:
            raise ExchangeError(f"symbole non géré : {symbol}")
        if side not in (BUY, SELL):
            raise ExchangeError(f"sens inconnu : {side!r}")
        if not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
            raise ExchangeError(f"quantité invalide : {quantity!r}")

        quote = self._feed.get_quote(symbol)
        base, quote_ccy = base_currency(symbol), self._quote_currency
        balances = dict(self._balances)  # on ne modifie l'état qu'une fois l'ordre validé

        if side == BUY:
            price = quote.price * (1 + self._slippage)
            cost = quantity * price
            fee = cost * self._fee_rate
            if cost + fee > balances[quote_ccy] + _EPS:
                raise ExchangeError(
                    f"fonds insuffisants : besoin de {cost + fee:.4f} {quote_ccy}, "
                    f"disponible {balances[quote_ccy]:.4f}"
                )
            balances[quote_ccy] = max(balances[quote_ccy] - cost - fee, 0.0)
            balances[base] = balances.get(base, 0.0) + quantity
        else:
            held = balances.get(base, 0.0)
            if quantity > held + _EPS:
                raise ExchangeError(f"quantité insuffisante : vente de {quantity} {base}, détenu {held}")
            quantity = min(quantity, held)
            price = quote.price * (1 - self._slippage)
            proceeds = quantity * price
            fee = proceeds * self._fee_rate
            balances[base] = max(held - quantity, 0.0)
            balances[quote_ccy] += proceeds - fee

        self._balances = balances
        self._storage.set(BALANCES_KEY, balances)
        return Fill(symbol, side, quantity, price, fee, self._clock())
