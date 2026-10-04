import pytest

from helpers import FakeClock, ScriptedFeed, default_cfg
from tradeagent.exchange import ExchangeError
from tradeagent.paper import PaperExchange
from tradeagent.storage import Storage


def make(stake=100.0, fee=0.001, slip=5):
    cfg = default_cfg(stake=stake, costs={"fee_rate": fee, "slippage_bps": slip})
    clock = FakeClock()
    feed = ScriptedFeed(clock)
    storage = Storage(":memory:")
    return PaperExchange(cfg, feed, storage, clock), feed, storage, cfg, clock


def test_initial_balances():
    ex, *_ = make(stake=100)
    assert ex.get_balances() == {"EUR": 100.0, "BTC": 0.0, "ETH": 0.0}


def test_buy_moves_balances_and_charges_costs():
    ex, *_ = make()
    fill = ex.market_order("BTC/EUR", "buy", 0.0002)
    assert fill.price == pytest.approx(60_000 * 1.0005)  # glissement défavorable
    cost = 0.0002 * fill.price
    assert fill.fee == pytest.approx(cost * 0.001)
    b = ex.get_balances()
    assert b["BTC"] == pytest.approx(0.0002)
    assert b["EUR"] == pytest.approx(100 - cost - fill.fee)


def test_the_fee_charged_is_the_one_from_the_config():
    ex, *_ = make(fee=0.0025, slip=0)
    fill = ex.market_order("BTC/EUR", "buy", 0.0002)
    assert fill.fee == pytest.approx(0.0002 * 60_000 * 0.0025)
    assert ex.get_balances()["EUR"] == pytest.approx(100 - 12 - 0.03)


def test_round_trip_at_constant_price_loses_exactly_the_costs():
    ex, *_ = make()
    ex.market_order("BTC/EUR", "buy", 0.0003)
    ex.market_order("BTC/EUR", "sell", 0.0003)
    b = ex.get_balances()
    assert b["BTC"] == 0.0
    assert b["EUR"] < 100  # on ne gagne jamais en allant-retour au même prix
    assert 100 - b["EUR"] == pytest.approx(18 * (0.0005 * 2 + 0.001 * 2), rel=0.05)


def test_insufficient_funds_raises_and_changes_nothing():
    ex, *_ = make(stake=100)
    with pytest.raises(ExchangeError, match="fonds insuffisants"):
        ex.market_order("BTC/EUR", "buy", 0.01)  # ~600 EUR
    assert ex.get_balances() == {"EUR": 100.0, "BTC": 0.0, "ETH": 0.0}


def test_selling_more_than_held_raises():
    ex, *_ = make()
    ex.market_order("BTC/EUR", "buy", 0.0001)
    with pytest.raises(ExchangeError, match="insuffisante"):
        ex.market_order("BTC/EUR", "sell", 0.001)


@pytest.mark.parametrize("quantity", [0, -1, float("nan"), float("inf")])
def test_invalid_quantity_raises(quantity):
    ex, *_ = make()
    with pytest.raises(ExchangeError):
        ex.market_order("BTC/EUR", "buy", quantity)


def test_unknown_symbol_and_side_raise():
    ex, *_ = make()
    with pytest.raises(ExchangeError):
        ex.market_order("DOGE/EUR", "buy", 1)
    with pytest.raises(ExchangeError):
        ex.market_order("BTC/EUR", "short", 1)


def test_balances_survive_a_restart():
    ex, feed, storage, cfg, clock = make()
    ex.market_order("BTC/EUR", "buy", 0.0002)
    again = PaperExchange(cfg, feed, storage, clock)
    assert again.get_balances() == ex.get_balances()
    assert again.get_balances()["BTC"] == pytest.approx(0.0002)


def test_get_balances_returns_a_copy():
    ex, *_ = make()
    ex.get_balances()["EUR"] = 1e9
    assert ex.get_balances()["EUR"] == 100.0
