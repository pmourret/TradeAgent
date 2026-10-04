import pytest

from helpers import START
from tradeagent.config import CostsConfig, GuardrailsConfig
from tradeagent.guardrails import DayState, Guardrails, floor_to
from tradeagent.models import Decision, Quote
from tradeagent.portfolio import Portfolio

SYMBOLS = ("BTC/EUR", "ETH/EUR")
NOW = START


def portfolio(cash=100.0, btc=0.0, eth=0.0, btc_price=60_000.0, eth_price=2_500.0, ts=NOW):
    return Portfolio(
        "EUR", cash,
        {"BTC/EUR": btc, "ETH/EUR": eth},
        {"BTC/EUR": Quote("BTC/EUR", btc_price, ts), "ETH/EUR": Quote("ETH/EUR", eth_price, ts)},
    )


def guardrails(**kw):
    return Guardrails(GuardrailsConfig(**kw), CostsConfig(), SYMBOLS, "EUR")


def day(start=100.0, buys=0):
    return DayState("2025-10-09", start, buys, buys)


def buy(symbol="BTC/EUR", amount=10.0):
    return Decision("buy", symbol, amount)


def sell(symbol="BTC/EUR", amount=10.0):
    return Decision("sell", symbol, amount)


# -- règles communes ---------------------------------------------------------

def test_hold_is_never_an_order():
    v = guardrails().check(Decision("hold"), portfolio(), day(), NOW)
    assert not v.approved and v.order is None


def test_unknown_symbol_is_rejected():
    v = guardrails().check(buy("DOGE/EUR"), portfolio(), day(), NOW)
    assert not v.approved and "liste autorisée" in v.reason


def test_stale_price_is_rejected():
    v = guardrails(max_price_age_s=60).check(buy(), portfolio(ts=NOW - 300), day(), NOW)
    assert not v.approved and "périmé" in v.reason


def test_zero_equity_is_rejected():
    v = guardrails().check(buy(), portfolio(cash=0.0), day(), NOW)
    assert not v.approved


# -- achats ------------------------------------------------------------------

def test_small_buy_is_approved_as_is():
    v = guardrails().check(buy(amount=10), portfolio(), day(), NOW)
    assert v.approved and v.reason == "approuvé"
    assert v.order.side == "buy"
    assert v.order.notional == pytest.approx(10, abs=0.1)


def test_greedy_buy_is_clamped_by_max_order_pct():
    v = guardrails(max_order_pct=20).check(buy(amount=1000), portfolio(), day(), NOW)
    assert v.approved and "max_order_pct" in v.reason
    assert v.order.notional == pytest.approx(20, abs=0.1)


def test_buy_below_minimum_after_limits_is_rejected():
    # position déjà à 38 sur un plafond de 40 : il reste 2 < minimum 5
    v = guardrails(max_position_pct=40).check(buy(amount=20), portfolio(cash=62, btc=38 / 60_000), day(), NOW)
    assert not v.approved and "max_position_pct" in v.reason


def test_position_already_over_cap_cannot_be_increased():
    v = guardrails(max_position_pct=40).check(buy(), portfolio(cash=30, btc=70 / 60_000), day(), NOW)
    assert not v.approved


def test_total_exposure_cap():
    # ETH 40 + BTC 38 = 78 d'exposition sur un plafond de 80 : il reste 2
    v = guardrails(max_total_exposure_pct=80).check(
        buy("BTC/EUR", 20), portfolio(cash=22, btc=38 / 60_000, eth=40 / 2_500), day(start=100), NOW)
    assert not v.approved


def test_buy_leaves_room_for_fees_and_slippage():
    gr = guardrails(max_order_pct=100, max_position_pct=100, max_total_exposure_pct=100)
    v = gr.check(buy(amount=10), portfolio(cash=10.0), day(start=10), NOW)
    assert v.approved and "available_cash" in v.reason
    costs = CostsConfig()
    worst_case = v.order.notional * (1 + costs.slippage_bps / 10_000) * (1 + costs.fee_rate)
    assert worst_case <= 10.0 + 1e-9


def test_daily_loss_limit_blocks_buys_but_not_sells():
    gr = guardrails(max_daily_loss_pct=5)
    hurt = portfolio(cash=60, btc=34 / 60_000)  # equity 94 < 95
    assert not gr.check(buy(), hurt, day(start=100), NOW).approved
    assert gr.check(sell(amount=34), hurt, day(start=100), NOW).approved


def test_daily_buy_cap_blocks_buys_but_not_sells():
    gr = guardrails(max_buys_per_day=3)
    p = portfolio(cash=80, btc=20 / 60_000)
    assert not gr.check(buy(), p, day(buys=3), NOW).approved
    assert gr.check(buy(), p, day(buys=2), NOW).approved
    assert gr.check(sell(amount=20), p, day(buys=3), NOW).approved


# -- ventes ------------------------------------------------------------------

def test_sell_without_position_is_rejected():
    v = guardrails().check(sell(), portfolio(), day(), NOW)
    assert not v.approved and "rien à vendre" in v.reason


def test_sell_cannot_exceed_holdings():
    v = guardrails().check(sell(amount=10_000), portfolio(cash=60, btc=0.0007), day(), NOW)
    assert v.approved
    assert v.order.quantity == pytest.approx(0.0007)  # pas de vente à découvert


def test_partial_sell():
    v = guardrails().check(sell(amount=15), portfolio(cash=50, btc=50 / 60_000), day(), NOW)
    assert v.approved
    assert v.order.notional == pytest.approx(15, abs=0.01)
    assert v.order.quantity < 50 / 60_000


def test_sell_that_would_leave_dust_exits_fully():
    held = 10 / 60_000
    v = guardrails(min_order_quote=5).check(sell(amount=7), portfolio(cash=90, btc=held), day(), NOW)
    assert v.approved and v.order.quantity == held and "poussière" in v.reason


def test_dust_position_cannot_be_sold():
    v = guardrails(min_order_quote=5).check(sell(amount=2), portfolio(cash=96, btc=3 / 60_000), day(), NOW)
    assert not v.approved


def test_tiny_partial_sell_is_rejected():
    v = guardrails(min_order_quote=5).check(sell(amount=2), portfolio(cash=0.01, btc=100 / 60_000), day(), NOW)
    assert not v.approved and "trop petite" in v.reason


# -- arrondis ----------------------------------------------------------------

@pytest.mark.parametrize("value,decimals,expected", [
    (0.0000839, 6, 0.000083),
    (0.000083, 6, 0.000083),
    (1.999999999, 2, 1.99),
    (5.0, 0, 5.0),
    (1e-7, 6, 0.0),
])
def test_floor_to(value, decimals, expected):
    assert floor_to(value, decimals) == expected
