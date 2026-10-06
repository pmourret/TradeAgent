"""La référence « marché » : buyhold acheté au début de la vie, noté par le moteur, valorisé par l'interface."""
import pytest

from helpers import START, FakeClock, ScriptedAgent, ScriptedFeed, default_cfg, make_engine
from tradeagent import reference
from tradeagent.app import LIFE_KEYS, reset_life
from tradeagent.dashboard import build_snapshot
from tradeagent.models import Candle, Decision
from tradeagent.storage import Storage
from tradeagent.strategies import BuyAndHoldAgent

HOURS_3 = 3 * 3600


def engine_with_old_life(age: float, cfg=None):
    """Un moteur dont la vie a commencé `age` secondes avant l'horloge (bot mis à jour en cours de vie)."""
    cfg = cfg or default_cfg()
    storage = Storage(":memory:")
    storage.set("life", {"stake": cfg.stake, "started": START - age})
    return make_engine(cfg, ScriptedAgent([]), storage=storage)


def test_the_allocation_is_the_one_buyhold_reaches_under_the_guardrails():
    cfg = default_cfg()     # position 40 %, exposition 80 %, deux symboles
    assert reference.weights(cfg) == {"BTC/EUR": 0.4, "ETH/EUR": 0.4}
    wide = default_cfg(guardrails={"max_position_pct": 30, "max_total_exposure_pct": 90})
    assert reference.weights(wide) == {"BTC/EUR": 0.3, "ETH/EUR": 0.3}     # plafonnée par la position
    record = reference.build(cfg, 100.0, {"BTC/EUR": 50_000.0, "ETH/EUR": 2_000.0}, START, START)
    assert record["quantities"] == {"BTC/EUR": pytest.approx(40 / 50_000), "ETH/EUR": pytest.approx(40 / 2_000)}
    entry = (1 + cfg.costs.slippage_bps / 10_000) * (1 + cfg.costs.fee_rate)
    assert record["cash"] == pytest.approx(100 - 80 * entry)               # l'entrée est payée
    assert record["weights_pct"] == {"BTC/EUR": pytest.approx(40), "ETH/EUR": pytest.approx(40)}


def test_the_engine_notes_it_once_per_life_and_reset_forgets_it():
    engine, feed, clock, storage = make_engine(default_cfg(), ScriptedAgent([]))
    engine.run_cycle()
    first = storage.get(reference.KEY)
    assert first["started"] == START and first["prices"] == {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}
    feed.set("BTC/EUR", 70_000.0)
    clock.advance(900)
    engine.run_cycle()
    assert storage.get(reference.KEY) == first                              # jamais réécrite dans la même vie
    assert reference.KEY in LIFE_KEYS
    reset_life(storage, clock())
    assert storage.get(reference.KEY) is None


def test_a_life_older_than_the_reference_finds_its_starting_prices_in_the_candles():
    engine, feed, clock, storage = engine_with_old_life(HOURS_3)
    started = START - HOURS_3
    seen = []
    for symbol, price in (("BTC/EUR", 55_000.0), ("ETH/EUR", 2_200.0)):
        feed.candles[symbol] = [Candle(started - 120, 1.0, 1.0, 1.0, 1.0),
                                Candle(started - 30, price, price, price, price),   # contient le début de la vie
                                Candle(START - 60, 9.0, 9.0, 9.0, 9.0)]
    original = feed.get_candles
    feed.get_candles = lambda s, tf="1h", limit=48: seen.append((tf, limit)) or original(s, tf, limit)
    engine.run_cycle()
    record = storage.get(reference.KEY)
    assert record["started"] == started and record["life_started"] == started
    assert record["prices"] == {"BTC/EUR": 55_000.0, "ETH/EUR": 2_200.0}
    assert ("1m", 182) in seen                            # la granularité la plus fine qui remonte jusque-là


def test_the_granularity_widens_with_the_age_of_the_life():
    asked = []

    def candles(symbol, timeframe, limit):
        asked.append((timeframe, limit))
        return [Candle(0.0, 3.0, 3.0, 3.0, 3.0)]

    assert reference.backfill_prices(candles, ["BTC/EUR"], START - 10 * 86_400, START) is None   # pas de bougie : rien
    assert asked == [("1h", 242), ("1d", 12)]           # bougie absente : on tente plus large
    asked.clear()
    assert reference.backfill_prices(candles, ["BTC/EUR"], START - 5000 * 86_400, START) is None  # trop vieux partout
    assert asked == []


def test_too_short_a_history_starts_the_reference_now_and_says_so():
    engine, feed, clock, storage = engine_with_old_life(HOURS_3)
    for symbol in ("BTC/EUR", "ETH/EUR"):    # l'historique ne remonte qu'à une heure
        feed.candles[symbol] = [Candle(START - 3600, 1.0, 1.0, 1.0, 1.0), Candle(START - 60, 1.0, 1.0, 1.0, 1.0)]
    engine.run_cycle()
    record = storage.get(reference.KEY)
    assert record["started"] == START and record["life_started"] == START - HOURS_3
    value = reference.valuation(record, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, 100.0, default_cfg())
    assert value["late"] is True


def test_a_feed_failure_is_retried_and_never_counts_as_a_cycle_error():
    engine, feed, clock, storage = engine_with_old_life(HOURS_3)
    feed.fail_candles = True
    result = engine.run_cycle()
    assert result.status == "hold" and storage.get(reference.KEY) is None
    assert not [e for e in storage.recent_events(20) if e["level"] in ("error", "critical")]
    feed.fail_candles = False
    clock.advance(900)
    engine.run_cycle()
    assert storage.get(reference.KEY)["started"] == START - HOURS_3


def test_a_feed_that_keeps_failing_ends_with_a_reference_starting_now():
    engine, feed, clock, storage = engine_with_old_life(HOURS_3)
    feed.fail_candles = True
    for _ in range(reference.BACKFILL_ATTEMPTS - 1):
        engine.run_cycle()
        clock.advance(900)
        assert storage.get(reference.KEY) is None
    engine.run_cycle()
    record = storage.get(reference.KEY)
    assert record["started"] == clock() and record["life_started"] == START - HOURS_3


def test_a_missing_fine_candle_falls_back_to_a_wider_one():
    started = START - HOURS_3
    hour_open = started - (started % 3600)

    def candles(symbol, timeframe, limit):
        if timeframe == "1m":
            return [Candle(started - 600, 1.0, 1.0, 1.0, 1.0)]                 # trou : pas la minute cherchée
        return [Candle(hour_open, 7.0, 7.0, 7.0, 7.0)]

    assert reference.backfill_prices(candles, ["BTC/EUR"], started, START) == {"BTC/EUR": 7.0}


def test_a_bug_abandons_the_reference_for_this_life_without_stopping_the_cycle(monkeypatch):
    engine, feed, clock, storage = make_engine(default_cfg(), ScriptedAgent([Decision("buy", "BTC/EUR", 10.0, "t")]))

    def broken(*args, **kwargs):
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(reference, "build", broken)
    assert engine.run_cycle().status == "filled"
    assert storage.get(reference.KEY) == {"abandoned": True}
    assert not [e for e in storage.recent_events(20) if e["level"] in ("error", "critical")]
    assert reference.valuation(storage.get(reference.KEY), {"BTC/EUR": 1.0, "ETH/EUR": 1.0}, 100.0, default_cfg()) is None


@pytest.mark.parametrize("stored", [
    None, "texte", [], {"cash": 20.0},
    {"cash": 20.0, "started": START, "life_started": START, "quantities": {"BTC/EUR": 1e-3}, "weights_pct": {}},
    {"cash": float("nan"), "started": START, "life_started": START,
     "quantities": {"BTC/EUR": 1e-3, "ETH/EUR": 1e-2}, "weights_pct": {"BTC/EUR": 40, "ETH/EUR": 40}},
    {"cash": 20.0, "started": START, "life_started": START,
     "quantities": {"BTC/EUR": -1, "ETH/EUR": 1e-2}, "weights_pct": {"BTC/EUR": 40, "ETH/EUR": 40}},
    {"cash": 20.0, "started": True, "life_started": START,
     "quantities": {"BTC/EUR": 1e-3, "ETH/EUR": 1e-2}, "weights_pct": {"BTC/EUR": 40, "ETH/EUR": 40}},
])
def test_a_doubtful_reference_is_never_shown(stored):
    assert reference.valuation(stored, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, 100.0, default_cfg()) is None


def test_a_missing_price_hides_the_reference():
    record = reference.build(default_cfg(), 100.0, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, START, START)
    assert reference.valuation(record, {"BTC/EUR": 60_000.0}, 100.0, default_cfg()) is None
    assert reference.valuation(record, {"BTC/EUR": 60_000.0, "ETH/EUR": 0}, 100.0, default_cfg()) is None


def test_the_reference_tracks_a_real_buyhold_bot(tmp_path):
    """Le portefeuille virtuel vaut ce que vaut un vrai `buyhold` passé par le moteur, les garde-fous et l'exchange
    papier (aux arrondis de quantité près)."""
    cfg = default_cfg(database=str(tmp_path / "bh.db"))
    clock = FakeClock()
    engine, feed, clock, storage = make_engine(cfg, BuyAndHoldAgent(), clock=clock, storage=Storage(cfg.database))
    for _ in range(5):                       # ordres de 20 % au plus : quatre achats pour atteindre 40 % et 40 %
        engine.run_cycle()
        clock.advance(900)
    feed.set("BTC/EUR", 66_000.0)
    feed.set("ETH/EUR", 2_300.0)
    engine.run_cycle()
    snap = build_snapshot(cfg, clock())
    market = snap["market_reference"]
    assert market["late"] is False and market["started"] == START
    assert market["equity"] == pytest.approx(snap["money"]["equity"], abs=0.02)
    assert market["net_result"] == pytest.approx(snap["money"]["net_result"], abs=0.02)


def test_the_snapshot_says_what_selling_everything_would_leave(tmp_path):
    cfg = default_cfg(database=str(tmp_path / "a.db"))
    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 30.0, "t")])
    engine, feed, clock, storage = make_engine(cfg, agent, storage=Storage(cfg.database))
    engine.run_cycle()
    clock.advance(900)
    engine.run_cycle()
    storage.record_llm_call(clock(), "m", 1000, 100, 0, 0, 0.01)        # le loyer sort aussi du résultat
    snap = build_snapshot(cfg, clock())
    m = snap["money"]
    assert m["api_spent_life"] == pytest.approx(0.01)
    held = sum(p["value"] for p in snap["positions"])
    rate = 1 - (1 - cfg.costs.slippage_bps / 10_000) * (1 - cfg.costs.fee_rate)
    assert m["exit_costs"] == pytest.approx(held * rate) and m["exit_costs"] > 0
    assert m["liquidation_result"] == pytest.approx(m["net_result"] - m["exit_costs"])
    # Ce que dit l'instantané est ce que l'exchange papier rendrait vraiment en vendant tout.
    btc = storage.get("paper_balances")["BTC"]
    fill = engine._exchange.market_order("BTC/EUR", "sell", btc)
    assert storage.get("paper_balances")["EUR"] - cfg.stake - 0.01 == pytest.approx(m["liquidation_result"], abs=1e-6)
    assert fill.quantity == btc


def test_no_price_means_no_liquidation_figure(tmp_path):
    cfg = default_cfg(database=str(tmp_path / "b.db"))
    engine, feed, clock, storage = make_engine(cfg, ScriptedAgent([]), storage=Storage(cfg.database))
    engine.run_cycle()
    storage.set("last_quotes", {"BTC/EUR": 60_000.0})
    snap = build_snapshot(cfg, clock())
    assert snap["money"]["exit_costs"] is None and snap["money"]["liquidation_result"] is None
    assert snap["market_reference"] is None


# -- bornes (survivants du test par mutation) ------------------------------------------------
def test_a_candle_contains_its_opening_instant_but_not_the_next_one():
    candles = [Candle(1000.0, 5.0, 5.0, 5.0, 5.0), Candle(1060.0, 6.0, 6.0, 6.0, 6.0)]
    assert reference.price_at(candles, 1000.0, "1m") == 5.0
    assert reference.price_at(candles, 1059.9, "1m") == 5.0
    assert reference.price_at(candles, 1060.0, "1m") == 6.0
    assert reference.price_at(candles, 1120.0, "1m") is None


def test_exactly_max_candles_is_still_asked():
    asked = []
    started = START - (reference.MAX_CANDLES - 2) * 60

    def candles(symbol, timeframe, limit):
        asked.append((timeframe, limit))
        return [Candle(started, 4.0, 4.0, 4.0, 4.0)]

    assert reference.backfill_prices(candles, ["BTC/EUR"], started, START) == {"BTC/EUR": 4.0}
    assert asked == [("1m", reference.MAX_CANDLES)]


def test_a_life_exactly_at_the_late_limit_is_not_searched_in_the_candles():
    engine, feed, clock, storage = engine_with_old_life(reference.LATE_AFTER_SECONDS)
    for symbol in ("BTC/EUR", "ETH/EUR"):
        feed.candles[symbol] = [Candle(START - 3600, 1.0, 1.0, 1.0, 1.0)]    # le chercher donnerait 1.0
    engine.run_cycle()
    record = storage.get(reference.KEY)
    assert record["started"] == START and record["prices"]["BTC/EUR"] == 60_000.0
    value = reference.valuation(record, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, 100.0, default_cfg())
    assert value["late"] is False                        # 120 s pile : pas encore « en retard »
    record["started"] += 1
    assert reference.valuation(record, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, 100.0, default_cfg())["late"] is True


def test_no_cash_left_and_a_life_started_at_zero_are_valid():
    record = reference.build(default_cfg(), 100.0, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, START, 0.0)
    record["cash"] = 0.0
    value = reference.valuation(record, {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, 100.0, default_cfg())
    assert value is not None and value["equity"] == pytest.approx(80.0)
