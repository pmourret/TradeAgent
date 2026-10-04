import pytest

from helpers import START, FakeClock, ScriptedAgent, ScriptedFeed, default_cfg, make_engine
from tradeagent.config import ConfigError, CostsConfig, GuardrailsConfig, RiskTiersConfig
from tradeagent.guardrails import CAUTIOUS, DEFENSIVE, NORMAL, DayState, Guardrails
from tradeagent.models import Decision, Quote
from tradeagent.portfolio import Portfolio

SYMBOLS = ("BTC/EUR", "ETH/EUR")


def portfolio(cash=100.0, btc=0.0):
    quotes = {s: Quote(s, p, START) for s, p in (("BTC/EUR", 60_000.0), ("ETH/EUR", 2_500.0))}
    return Portfolio("EUR", cash, {"BTC/EUR": btc, "ETH/EUR": 0.0}, quotes)


def guardrails(**tiers):
    return Guardrails(GuardrailsConfig(), CostsConfig(), SYMBOLS, "EUR", RiskTiersConfig(**tiers))


def day(start=100.0):
    return DayState("2025-10-09", start)


# -- paliers selon le drawdown ----------------------------------------------

@pytest.mark.parametrize("equity,expected", [
    (100.0, NORMAL), (90.0, NORMAL), (85.1, NORMAL),
    (85.0, CAUTIOUS), (80.0, CAUTIOUS), (75.1, CAUTIOUS),
    (75.0, DEFENSIVE), (60.0, DEFENSIVE),
])
def test_tier_thresholds(equity, expected):
    assert guardrails().tier_for(equity, 100.0) == expected


def test_tier_is_measured_from_the_peak_not_from_the_stake():
    assert guardrails().tier_for(equity=130.0, peak_equity=200.0) == DEFENSIVE
    assert guardrails().tier_for(equity=130.0, peak_equity=140.0) == NORMAL


def test_tier_with_no_peak_is_normal():
    assert guardrails().tier_for(10.0, 0.0) == NORMAL


# -- effet sur les ordres ----------------------------------------------------

def test_cautious_tier_halves_the_order_cap():
    gr = guardrails(cautious_size_factor=0.5)
    normal = gr.check(Decision("buy", "BTC/EUR", 50.0), portfolio(), day(), START, NORMAL)
    cautious = gr.check(Decision("buy", "BTC/EUR", 50.0), portfolio(), day(), START, CAUTIOUS)
    assert normal.order.notional == pytest.approx(20, abs=0.1)
    assert cautious.order.notional == pytest.approx(10, abs=0.1)
    assert "palier prudent" in cautious.reason


def test_cautious_tier_also_scales_the_position_cap():
    # plafond de position : 40 % -> 20 % en palier prudent ; 18 déjà investis -> il reste 2 < minimum
    gr = guardrails(cautious_size_factor=0.5)
    held = portfolio(cash=82.0, btc=18 / 60_000)
    assert gr.check(Decision("buy", "BTC/EUR", 20.0), held, day(), START, NORMAL).approved
    refused = gr.check(Decision("buy", "BTC/EUR", 20.0), held, day(), START, CAUTIOUS)
    assert not refused.approved and "max_position_pct" in refused.reason


def test_defensive_tier_blocks_buys_but_never_sells():
    gr = guardrails()
    held = portfolio(cash=60.0, btc=40 / 60_000)
    assert "défensif" in gr.check(Decision("buy", "ETH/EUR", 10.0), held, day(), START, DEFENSIVE).reason
    sale = gr.check(Decision("sell", "BTC/EUR", 40.0), held, day(), START, DEFENSIVE)
    assert sale.approved and sale.order.side == "sell"


def test_describe_limits_reflects_the_tier_for_the_agent():
    gr = guardrails(cautious_size_factor=0.5)
    p = portfolio()
    assert gr.describe_limits(p, day(), NORMAL)["max_order_quote"] == 20.0
    assert gr.describe_limits(p, day(), CAUTIOUS)["max_order_quote"] == 10.0
    assert gr.describe_limits(p, day(), DEFENSIVE)["max_order_quote"] == 0.0


# -- configuration -----------------------------------------------------------

def test_tier_order_is_enforced():
    with pytest.raises(ConfigError, match="cautious_drawdown_pct"):
        RiskTiersConfig(cautious_drawdown_pct=30, defensive_drawdown_pct=20)


def test_defensive_tier_must_come_before_death():
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        default_cfg(risk_tiers={"defensive_drawdown_pct": 45}, killswitch={"max_drawdown_pct": 40})


@pytest.mark.parametrize("factor", [0, -0.5, 1.5])
def test_size_factor_bounds(factor):
    with pytest.raises(ConfigError):
        RiskTiersConfig(cautious_size_factor=factor)


# -- avec le moteur ----------------------------------------------------------

def test_engine_walks_down_the_tiers_before_the_kill_switch_fires():
    buy_btc = Decision("buy", "BTC/EUR", 20.0, "x")
    agent = ScriptedAgent([buy_btc, buy_btc])
    engine, feed, clock, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    engine.run_cycle()
    assert storage.get("risk_tier", "normal") == "normal"

    # Le krach a lieu « hier » : sinon la perte journalière (5 %) bloquerait les achats avant le palier.
    feed.set("BTC/EUR", 30_000.0)  # ~40 EUR investis perdent la moitié : drawdown ~20 %
    clock.advance(86_400)
    agent.items = [Decision("buy", "ETH/EUR", 100.0, "revenge trade")]
    cautious = engine.run_cycle()
    assert storage.get("risk_tier") == "cautious"
    assert cautious.status == "filled" and "palier prudent" in cautious.note
    assert cautious.fill.notional < 10.5  # 20 % de ~80 EUR, divisé par deux

    feed.set("BTC/EUR", 10_000.0)  # drawdown > 25 % mais < 40 % : défensif, pas encore la mort
    clock.advance(86_400)
    agent.items = [Decision("buy", "ETH/EUR", 10.0, "encore"), Decision("sell", "BTC/EUR", 100.0, "on sort")]
    blocked = engine.run_cycle()
    assert storage.get("risk_tier") == "defensive"
    assert blocked.status == "rejected" and "défensif" in blocked.note
    sold = engine.run_cycle()
    assert sold.status == "filled" and sold.fill.side == "sell"

    events = [e["message"] for e in storage.recent_events(20)]
    assert any("normal -> cautious" in m for m in events)
    assert any("cautious -> defensive" in m for m in events)


def test_agent_sees_its_tier_and_scaled_limits():
    seen = []

    class Spy(ScriptedAgent):
        def decide(self, view):
            seen.append(view)
            return Decision("hold")

    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 20.0), Decision("buy", "BTC/EUR", 20.0)])
    engine, feed, clock, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    engine.run_cycle()
    feed.set("BTC/EUR", 30_000.0)
    engine._agent = Spy()
    engine.run_cycle()
    assert seen[0].risk_tier == "cautious"
    assert seen[0].limits["max_order_quote"] < 10.5


def test_tier_goes_back_to_normal_when_equity_recovers():
    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 20.0), Decision("buy", "BTC/EUR", 20.0)])
    engine, feed, _, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    engine.run_cycle()
    feed.set("BTC/EUR", 30_000.0)
    engine.run_cycle()
    assert storage.get("risk_tier") == "cautious"
    feed.set("BTC/EUR", 60_000.0)
    engine.run_cycle()
    assert storage.get("risk_tier") == "normal"


def test_within_one_day_the_daily_loss_limit_bites_before_the_cautious_tier():
    """Les deux garde-fous se complètent : le plafond journalier (5 %) agit sur les chutes brutales,
    les paliers (15 % / 25 % depuis le plus-haut) sur les pertes qui s'étalent sur plusieurs jours."""
    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 20.0), Decision("buy", "BTC/EUR", 20.0)])
    engine, feed, _, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    engine.run_cycle()
    feed.set("BTC/EUR", 30_000.0)
    agent.items = [Decision("buy", "ETH/EUR", 10.0)]
    result = engine.run_cycle()
    assert storage.get("risk_tier") == "cautious"
    assert result.status == "rejected" and "perte journalière" in result.note
