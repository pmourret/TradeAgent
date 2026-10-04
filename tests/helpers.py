"""Outils de test : horloge, flux de prix et agent scriptés."""
from __future__ import annotations

from tradeagent.app import build_engine
from tradeagent.config import Config, GuardrailsConfig, KillSwitchConfig, config_from_dict
from tradeagent.models import TIMEFRAME_SECONDS, Candle, Decision, Quote
from tradeagent.storage import Storage

START = 1_760_000_000.0  # 2025-10-09 08:53 UTC
PRICES = {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}


class FakeClock:
    def __init__(self, t: float = START) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ScriptedFeed:
    def __init__(self, clock: FakeClock, prices: dict[str, float] | None = None) -> None:
        self.clock = clock
        self.prices = dict(prices or PRICES)
        self.fail = False
        self.fail_candles = False
        self.candles: dict[str, list[Candle]] = {}

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def get_quote(self, symbol: str) -> Quote:
        from tradeagent.feeds import FeedError

        if self.fail:
            raise FeedError("flux indisponible (test)")
        return Quote(symbol, self.prices[symbol], self.clock())

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 48) -> list[Candle]:
        from tradeagent.feeds import FeedError

        if self.fail_candles:
            raise FeedError("bougies indisponibles (test)")
        if symbol in self.candles:
            return self.candles[symbol]
        step = TIMEFRAME_SECONDS[timeframe]
        price = self.prices[symbol]
        end = self.clock()
        return [Candle(end - (limit - 1 - i) * step, price, price, price, price) for i in range(limit)]


class ScriptedAgent:
    """Renvoie les décisions dans l'ordre ; une exception dans la liste est levée."""

    name = "scripted"

    def __init__(self, items: list | None = None) -> None:
        self.items = list(items or [])
        self.calls = 0

    def decide(self, view):
        self.calls += 1
        item = self.items.pop(0) if self.items else Decision("hold")
        if isinstance(item, BaseException):
            raise item
        return item


def default_cfg(**overrides) -> Config:
    return config_from_dict(overrides)


def permissive_cfg(**killswitch) -> Config:
    """Garde-fous grands ouverts : pour tester le kill switch sans être freiné avant."""
    return Config(
        guardrails=GuardrailsConfig(max_order_pct=100, max_position_pct=100, max_total_exposure_pct=100,
                                    max_daily_loss_pct=100, max_buys_per_day=1000),
        killswitch=KillSwitchConfig(**killswitch),
    )


def make_engine(cfg: Config, agent: ScriptedAgent, *, clock=None, feed=None, storage=None):
    clock = clock or FakeClock()
    feed = feed or ScriptedFeed(clock)
    storage = storage or Storage(":memory:")
    engine = build_engine(cfg, agent, feed, storage, clock)
    return engine, feed, clock, storage
