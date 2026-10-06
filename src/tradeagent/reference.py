"""La référence « marché » d'une vie : ce qu'aurait donné acheter au début de la vie et tout garder (`buyhold`).

Elle n'achète rien. Une fois par vie, le moteur note un portefeuille virtuel (clé `market_reference`, dans
`LIFE_KEYS` : un `reset` l'efface) : les quantités qu'aurait achetées `buyhold` aux prix du début de la vie, frais
et glissement d'entrée payés, et le cash restant. L'interface le valorise ensuite aux derniers prix, en lecture
seule. Ni l'agent ni les garde-fous ne la voient.

L'allocation est celle de l'agent `buyhold` du backtest une fois son allocation finie : chaque symbole à parts
égales, dans la limite de l'exposition totale et de la position par symbole (40 % et 40 % avec la config
actuelle), le reste en cash. C'est « le marché tel qu'on a le droit de le détenir ici ».

Une vie commencée avant que la référence existe (ou un bot redémarré après une longue coupure avant son premier
cycle) retrouve ses prix de départ dans les bougies : l'ouverture de la bougie qui contient le début de la vie, à
la granularité la plus fine qui remonte jusque-là et qui la contient. Si aucune ne la contient, ou si le flux échoue
plusieurs cycles d'affilée, la référence part du cycle présent, et le dit (`started` postérieur au début de la vie).
"""
from __future__ import annotations

import math
from typing import Any, Callable

from .config import Config
from .models import TIMEFRAME_SECONDS, Candle

KEY = "market_reference"
BACKFILL_TIMEFRAMES = ("1m", "5m", "1h", "1d")   # du plus fin au plus large
MAX_CANDLES = 1000                                # ce qu'on demande au plus à l'exchange en une fois
LATE_AFTER_SECONDS = 120                          # au-delà, le début de la vie est cherché dans les bougies
BACKFILL_ATTEMPTS = 4                             # cycles d'affilée où le flux peut échouer avant d'y renoncer


def weights(cfg: Config) -> dict[str, float]:
    """La part de la mise placée sur chaque symbole (0.4 = 40 %)."""
    g = cfg.guardrails
    share = min(g.max_position_pct, g.max_total_exposure_pct / len(cfg.symbols)) / 100
    return {symbol: share for symbol in cfg.symbols}


def build(cfg: Config, stake: float, prices: dict[str, float], started: float, life_started: float) -> dict[str, Any]:
    """Le portefeuille virtuel acheté à `prices`, comme le paierait l'exchange papier (glissement, puis frais)."""
    entry = (1 + cfg.costs.slippage_bps / 10_000) * (1 + cfg.costs.fee_rate)
    shares, quantities, cash = weights(cfg), {}, stake
    for symbol, share in shares.items():
        amount = stake * share
        quantities[symbol] = amount / prices[symbol]
        cash -= amount * entry
    return {"started": started, "life_started": life_started, "prices": dict(prices),
            "weights_pct": {symbol: share * 100 for symbol, share in shares.items()},
            "quantities": quantities, "cash": max(cash, 0.0)}


def price_at(candles: list[Candle], ts: float, timeframe: str) -> float | None:
    """L'ouverture de la bougie qui contient l'instant `ts`, ou None si aucune ne le contient."""
    step = TIMEFRAME_SECONDS[timeframe]
    for candle in candles:
        if candle.timestamp <= ts < candle.timestamp + step:
            return candle.open
    return None


def backfill_prices(get_candles: Callable[[str, str, int], list[Candle]], symbols: tuple[str, ...] | list[str],
                    started: float, now: float) -> dict[str, float] | None:
    """Les prix à l'instant `started`, lus dans les bougies ; None si l'historique disponible ne remonte pas assez.
    Une erreur du flux (`ExchangeError`) remonte : l'appelant réessaiera au cycle suivant."""
    age = max(now - started, 0.0)
    for timeframe in BACKFILL_TIMEFRAMES:
        needed = math.ceil(age / TIMEFRAME_SECONDS[timeframe]) + 2
        if needed > MAX_CANDLES:
            continue
        prices = {}
        for symbol in symbols:
            price = price_at(get_candles(symbol, timeframe, needed), started, timeframe)
            if price is None:
                break               # bougie absente (aucun échange cette minute-là ?) : granularité plus large
            prices[symbol] = price
        else:
            return prices
    return None


def _number(value: Any, positive: bool = True) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def valuation(stored: Any, quotes: dict[str, Any], stake: float, cfg: Config) -> dict[str, Any] | None:
    """La valeur de la référence aux derniers prix, pour l'interface. None si elle manque, est mal formée, ou si
    un prix manque : une référence douteuse n'est jamais affichée."""
    if not isinstance(stored, dict):
        return None
    cash, started = _number(stored.get("cash"), positive=False), _number(stored.get("started"))
    life_started = _number(stored.get("life_started"), positive=False)
    quantities, shares = stored.get("quantities"), stored.get("weights_pct")
    if (cash is None or started is None or life_started is None
            or not isinstance(quantities, dict) or not isinstance(shares, dict)):
        return None
    equity, exposure, weights_pct = cash, 0.0, {}
    for symbol in cfg.symbols:
        quantity, price = _number(quantities.get(symbol), positive=False), _number(quotes.get(symbol))
        share = _number(shares.get(symbol), positive=False)
        if quantity is None or price is None or share is None:
            return None      # un symbole ajouté à la config en cours de vie : la référence ne dit plus rien de juste
        equity += quantity * price
        exposure += quantity * price
        weights_pct[symbol] = share
    return {
        "started": started,
        # Partie après le début de la vie (bot arrêté trop longtemps, historique trop court) : l'interface le dit.
        "late": started - life_started > LATE_AFTER_SECONDS,
        "weights_pct": weights_pct,
        "equity": equity,
        "net_result": equity - stake,
        "exit_costs": exposure * exit_cost_rate(cfg),
    }


def exit_cost_rate(cfg: Config) -> float:
    """Ce qu'une vente au marché laisse en route, en fraction de la valeur : glissement puis frais."""
    return 1 - (1 - cfg.costs.slippage_bps / 10_000) * (1 - cfg.costs.fee_rate)
