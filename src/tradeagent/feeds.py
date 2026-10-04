"""Sources de prix : réelle (ccxt, données publiques) ou synthétique (démo hors ligne)."""
from __future__ import annotations

import math
import random
import time
from typing import Any, Callable, Protocol

from .exchange import ExchangeError
from .models import TIMEFRAME_SECONDS, Candle, Quote
from .portfolio import base_currency


class FeedError(ExchangeError):
    """Impossible d'obtenir un prix fiable."""


class PriceFeed(Protocol):
    def get_quote(self, symbol: str) -> Quote: ...

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]: ...


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _candle_from_row(symbol: str, row: Any) -> Candle:
    try:
        ts, o, h, l, c, v = list(row)[:6]
    except (TypeError, ValueError) as exc:
        raise FeedError(f"bougie mal formée pour {symbol} : {row!r}") from exc
    if not _is_number(ts) or ts <= 0 or not all(_is_number(x) and x > 0 for x in (o, h, l, c)):
        raise FeedError(f"bougie invalide pour {symbol} : {row!r}")
    volume = float(v) if _is_number(v) and v >= 0 else 0.0
    return Candle(float(ts) / 1000.0, float(o), float(h), float(l), float(c), volume)


class CcxtPriceFeed:
    """Prix publics d'un exchange via ccxt. Aucune clé API, lecture seule."""

    def __init__(self, exchange_id: str = "bitvavo", client: Any = None) -> None:
        if client is None:
            import ccxt

            try:
                client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
            except AttributeError as exc:
                raise FeedError(f"exchange ccxt inconnu : {exchange_id!r}") from exc
        self._client = client
        self._exchange_id = exchange_id

    def _bad_symbol(self, symbol: str) -> FeedError:
        """La paire n'existe pas sur cet exchange : on dit laquelle, où, et ce qui existe à la place."""
        quote = symbol.split("/")[-1]
        known = getattr(self._client, "symbols", None) or []   # rempli par ccxt lors de l'appel qui vient d'échouer
        same_quote = sorted(s for s in known if isinstance(s, str) and s.endswith("/" + quote))[:10]
        hint = (f"Paires disponibles en {quote} : {', '.join(same_quote)}." if same_quote
                else f"Aucune paire en {quote} connue sur cet exchange.")
        return FeedError(f"la paire {symbol} n'existe pas sur l'exchange {self._exchange_id}. {hint} "
                         "Corrige `symbols` ou `exchange` dans config.yaml.")

    def get_quote(self, symbol: str) -> Quote:
        try:
            ticker = self._client.fetch_ticker(symbol)
        except Exception as exc:  # ccxt lève une famille d'exceptions réseau/exchange
            if type(exc).__name__ == "BadSymbol":   # comparaison par nom : pas d'import de ccxt avec un faux client
                raise self._bad_symbol(symbol) from exc
            raise FeedError(f"fetch_ticker({symbol}) a échoué : {exc}") from exc
        price = ticker.get("last") or ticker.get("close")
        if not _is_number(price) or price <= 0:
            raise FeedError(f"prix invalide pour {symbol} : {price!r}")
        ts_ms = ticker.get("timestamp")
        # Sans horodatage on ne peut pas juger la fraîcheur : on prend l'heure de réception.
        timestamp = ts_ms / 1000 if _is_number(ts_ms) and ts_ms else time.time()
        return Quote(symbol, float(price), float(timestamp))

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]:
        try:
            rows = self._client.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as exc:
            if type(exc).__name__ == "BadSymbol":
                raise self._bad_symbol(symbol) from exc
            raise FeedError(f"fetch_ohlcv({symbol}, {timeframe}) a échoué : {exc}") from exc
        candles = sorted((_candle_from_row(symbol, r) for r in rows or []), key=lambda c: c.timestamp)
        if not candles:
            raise FeedError(f"aucune bougie pour {symbol}")
        return candles


class SyntheticPriceFeed:
    """Marche aléatoire reproductible, pour tester sans réseau.

    Chaque appel à `get_quote` fait avancer le prix d'un pas. `get_candles` fabrique un
    historique plausible qui se termine au prix courant, sans le faire bouger.
    """

    DEFAULT_START = {"BTC": 60_000.0, "ETH": 2_500.0}

    def __init__(self, symbols: tuple[str, ...] | list[str], seed: int | None = None,
                 volatility: float = 0.004, clock: Callable[[], float] = time.time) -> None:
        self._rng = random.Random(seed)
        self._vol = volatility
        self._clock = clock
        self._prices = {s: self.DEFAULT_START.get(base_currency(s), 100.0) for s in symbols}

    def get_quote(self, symbol: str) -> Quote:
        if symbol not in self._prices:
            raise FeedError(f"symbole inconnu du flux synthétique : {symbol}")
        step = self._rng.gauss(0.0, self._vol)
        self._prices[symbol] = max(self._prices[symbol] * (1.0 + step), 0.01)
        return Quote(symbol, self._prices[symbol], self._clock())

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]:
        if symbol not in self._prices:
            raise FeedError(f"symbole inconnu du flux synthétique : {symbol}")
        if timeframe not in TIMEFRAME_SECONDS:
            raise FeedError(f"timeframe inconnu : {timeframe!r}")
        step_seconds = TIMEFRAME_SECONDS[timeframe]
        end = self._clock()
        closes = [self._prices[symbol]]
        for _ in range(limit - 1):
            closes.append(max(closes[-1] / (1.0 + self._rng.gauss(0.0, self._vol)), 0.01))
        closes.reverse()  # du plus ancien au plus récent ; la dernière clôture = prix courant
        candles = []
        for i, close in enumerate(closes):
            open_ = closes[i - 1] if i else close
            wick = abs(self._rng.gauss(0.0, self._vol / 2))
            candles.append(Candle(end - (limit - 1 - i) * step_seconds, open_,
                                  max(open_, close) * (1 + wick), min(open_, close) * (1 - wick), close, 0.0))
        return candles
