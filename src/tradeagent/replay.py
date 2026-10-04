"""Rejouer le passé : horloge simulée, flux de prix sur bougies historiques, historique en cache.

Sert au backtest (`backtest.py`). Règle centrale : à l'instant simulé `t`, le flux ne montre que les bougies
déjà TERMINÉES à `t`. Montrer une bougie en cours reviendrait à donner à l'agent un prix du futur
(biais d'anticipation), et tous les résultats seraient faux.
"""
from __future__ import annotations

import bisect
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .feeds import FeedError, _candle_from_row
from .models import TIMEFRAME_SECONDS, Candle, Quote
from .portfolio import base_currency

MAX_GAP_CANDLES = 6     # au-delà, on refuse de coter : l'historique a un trou, le prix serait périmé
PAGE = 500              # bougies demandées par appel à l'exchange
MAX_PAGES = 400


class SimClock:
    """Horloge du backtest : n'avance que quand on le lui dit."""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        self.t = float(t)


class ReplayPriceFeed:
    """Flux de prix sur un historique de bougies. Le prix à `t` est la clôture de la dernière bougie terminée."""

    def __init__(self, history: dict[str, list[Candle]], timeframe: str, clock: Callable[[], float]) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise FeedError(f"timeframe inconnu : {timeframe!r}")
        self._timeframe = timeframe
        self._step = TIMEFRAME_SECONDS[timeframe]
        self._clock = clock
        self._candles: dict[str, list[Candle]] = {}
        self._opens: dict[str, list[float]] = {}
        for symbol, candles in history.items():
            ordered = sorted(candles, key=lambda c: c.timestamp)
            if not ordered:
                raise FeedError(f"historique vide pour {symbol}")
            self._candles[symbol] = ordered
            self._opens[symbol] = [c.timestamp for c in ordered]

    def _completed(self, symbol: str) -> list[Candle]:
        """Les bougies terminées à l'instant simulé (ouverture + durée <= maintenant), de la plus ancienne à la plus récente."""
        if symbol not in self._candles:
            raise FeedError(f"symbole absent de l'historique : {symbol}")
        count = bisect.bisect_right(self._opens[symbol], self._clock() - self._step)
        return self._candles[symbol][:count]

    def get_quote(self, symbol: str) -> Quote:
        now = self._clock()
        done = self._completed(symbol)
        if not done:
            raise FeedError(f"aucune bougie terminée pour {symbol} à cette date : l'historique commence plus tard")
        last = done[-1]
        age = now - (last.timestamp + self._step)
        if age > MAX_GAP_CANDLES * self._step:
            raise FeedError(f"trou dans l'historique de {symbol} : dernière bougie terminée il y a {age / 3600:.1f} h")
        # Le prix est celui de la dernière clôture, vu à l'instant simulé : c'est le prix « courant » du backtest.
        return Quote(symbol, last.close, now)

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]:
        if timeframe != self._timeframe:
            raise FeedError(f"l'historique est en {self._timeframe}, pas en {timeframe}")
        done = self._completed(symbol)
        if not done:
            raise FeedError(f"aucune bougie terminée pour {symbol} à cette date")
        return done[-limit:]


def synthetic_history(symbols: tuple[str, ...] | list[str], timeframe: str, start: float, end: float,
                      seed: int | None = None, volatility: float = 0.006) -> dict[str, list[Candle]]:
    """Marche aléatoire reproductible sur [start, end) : pour essayer le backtest sans réseau."""
    step = TIMEFRAME_SECONDS[timeframe]
    defaults = {"BTC": 60_000.0, "ETH": 2_500.0}
    history: dict[str, list[Candle]] = {}
    for index, symbol in enumerate(symbols):
        rng = random.Random(None if seed is None else seed * 1_000 + index)
        price = defaults.get(base_currency(symbol), 100.0)
        candles = []
        t = float(start)
        while t < end:
            close = max(price * (1.0 + rng.gauss(0.0, volatility)), 0.01)
            wick = abs(rng.gauss(0.0, volatility / 2))
            candles.append(Candle(t, price, max(price, close) * (1 + wick), min(price, close) * (1 - wick), close, 0.0))
            price = close
            t += step
        history[symbol] = candles
    return history


def public_client(exchange_id: str) -> Any:
    """Client ccxt sans clé : données publiques, lecture seule."""
    import ccxt

    try:
        return getattr(ccxt, exchange_id)({"enableRateLimit": True})
    except AttributeError as exc:
        raise FeedError(f"exchange ccxt inconnu : {exchange_id!r}") from exc


def fetch_history(client: Any, symbol: str, timeframe: str, start: float, end: float) -> list[Candle]:
    """Bougies de [start, end) via `fetch_ohlcv` (données publiques, sans clé), page par page."""
    step = TIMEFRAME_SECONDS[timeframe]
    found: dict[float, Candle] = {}
    since = float(start)
    for _ in range(MAX_PAGES):
        if since >= end:
            break
        try:
            rows = client.fetch_ohlcv(symbol, timeframe, since=int(since * 1000), limit=PAGE)
        except Exception as exc:  # ccxt lève une famille d'exceptions réseau/exchange
            raise FeedError(f"fetch_ohlcv({symbol}, {timeframe}) a échoué : {exc}") from exc
        candles = [_candle_from_row(symbol, r) for r in rows or []]
        fresh = [c for c in candles if c.timestamp >= since]
        if not fresh:
            # Page vide : l'exchange borne chaque page à une fenêtre de temps. Ce n'est pas la fin de l'historique :
            # on passe à la fenêtre suivante (un vrai trou sera refusé par `check_coverage`).
            since += PAGE * step
            continue
        for candle in fresh:
            if start <= candle.timestamp < end:
                found[candle.timestamp] = candle
        since = max(c.timestamp for c in fresh) + step     # avance toujours : pas de boucle infinie
    else:
        raise FeedError(f"historique de {symbol} trop long : plus de {MAX_PAGES} pages")
    if not found:
        raise FeedError(f"aucune bougie pour {symbol} sur la période demandée")
    return [found[ts] for ts in sorted(found)]


def check_coverage(symbol: str, candles: list[Candle], timeframe: str, start: float, end: float) -> None:
    """Refuse un historique qui ne couvre pas [start, end) : début ou fin manquants, ou trou au milieu.

    Sans ce contrôle, un téléchargement tronqué donnerait un backtest plus court que la période affichée.
    Quelques bougies absentes de suite sont tolérées (un exchange n'en publie pas quand il n'y a aucun échange).
    """
    step = TIMEFRAME_SECONDS[timeframe]
    tolerance = MAX_GAP_CANDLES * step
    opens = [c.timestamp for c in candles]
    day = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    previous = start - step
    for ts in [*opens, end]:
        if ts - previous - step > tolerance:
            raise FeedError(f"historique incomplet pour {symbol} : aucune bougie entre {day(previous + step)} et {day(ts)} UTC. "
                            "Choisis une autre période (--end, --days) ou relance avec --refresh.")
        previous = ts


def _cache_path(cache_dir: Path, exchange: str, symbol: str, timeframe: str) -> Path:
    return cache_dir / f"{exchange}-{symbol.replace('/', '-')}-{timeframe}.json"


def _read_cache(path: Path, symbol: str) -> tuple[float, float, list[Candle]] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        candles = [_candle_from_row(symbol, row) for row in raw["rows"]]
        return float(raw["start"]), float(raw["end"]), candles
    except (OSError, ValueError, KeyError, TypeError, FeedError):
        return None     # cache absent ou abîmé : on retélécharge


def _write_cache(path: Path, start: float, end: float, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [[c.timestamp * 1000, c.open, c.high, c.low, c.close, c.volume] for c in candles]
    path.write_text(json.dumps({"start": start, "end": end, "rows": rows}), encoding="utf-8")


def load_history(client: Any, exchange: str, symbols: tuple[str, ...] | list[str], timeframe: str,
                 start: float, end: float, cache_dir: str | Path, refresh: bool = False) -> dict[str, list[Candle]]:
    """Historique de [start, end) par symbole. Ne télécharge que ce qui manque au cache (un fichier par paire)."""
    history: dict[str, list[Candle]] = {}
    for symbol in symbols:
        path = _cache_path(Path(cache_dir), exchange, symbol, timeframe)
        cached = None if refresh else _read_cache(path, symbol)
        if cached and cached[0] <= start and cached[1] >= end:
            candles = cached[2]
            check_coverage(symbol, [c for c in candles if start <= c.timestamp < end], timeframe, start, end)
        elif cached and cached[0] <= start < cached[1]:
            # Le cache couvre le début : on ne télécharge que la suite.
            merged = {c.timestamp: c for c in cached[2]}
            merged.update({c.timestamp: c for c in fetch_history(client, symbol, timeframe, cached[1], end)})
            candles = [merged[ts] for ts in sorted(merged)]
            check_coverage(symbol, candles, timeframe, cached[0], end)      # on ne met en cache que du complet
            _write_cache(path, cached[0], end, candles)
        else:
            candles = fetch_history(client, symbol, timeframe, start, end)
            check_coverage(symbol, candles, timeframe, start, end)
            _write_cache(path, start, end, candles)
        history[symbol] = [c for c in candles if start <= c.timestamp < end]
    return history
