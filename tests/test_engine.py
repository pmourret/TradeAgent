import pytest

from helpers import FakeClock, ScriptedAgent, ScriptedFeed, default_cfg, make_engine, permissive_cfg
from tradeagent.app import build_engine, reset_life
from tradeagent.config import ConfigError
from tradeagent.models import Decision
from tradeagent.storage import Storage


def buy(amount, symbol="BTC/EUR"):
    return Decision("buy", symbol, amount, "test")


def sell(amount, symbol="BTC/EUR"):
    return Decision("sell", symbol, amount, "test")


# -- cas simples -------------------------------------------------------------

def test_hold_does_nothing_but_records_equity():
    engine, _, _, storage = make_engine(default_cfg(), ScriptedAgent([Decision("hold")]))
    result = engine.run_cycle()
    assert result.status == "hold"
    assert storage.count("fills") == 0
    assert storage.count("equity") == 1
    assert storage.last_equity()["equity"] == pytest.approx(100.0)


def test_approved_buy_reaches_the_exchange():
    engine, _, _, storage = make_engine(default_cfg(), ScriptedAgent([buy(15)]))
    result = engine.run_cycle()
    assert result.status == "filled"
    assert result.fill.side == "buy"
    assert storage.get("paper_balances")["BTC"] > 0
    assert storage.count("fills") == 1
    assert storage.recent_decisions(1)[0]["approved"] == 1


def test_unknown_symbol_never_reaches_the_exchange():
    engine, _, _, storage = make_engine(default_cfg(), ScriptedAgent([buy(10, "DOGE/EUR")]))
    result = engine.run_cycle()
    assert result.status == "rejected"
    assert storage.count("fills") == 0
    assert storage.get("paper_balances")["EUR"] == 100.0
    assert storage.recent_decisions(1)[0]["approved"] == 0


def test_greedy_buy_is_clamped_by_the_engine():
    engine, _, _, _ = make_engine(default_cfg(), ScriptedAgent([buy(1000)]))
    result = engine.run_cycle()
    assert result.status == "filled"
    assert result.fill.notional == pytest.approx(20, abs=0.5)  # max_order_pct = 20 % de 100


def test_buy_then_sell_round_trip_is_journaled():
    agent = ScriptedAgent([buy(20), sell(1000)])
    engine, _, _, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    result = engine.run_cycle()
    assert result.status == "filled" and result.fill.side == "sell"
    balances = storage.get("paper_balances")
    assert balances["BTC"] == pytest.approx(0.0, abs=1e-12)
    assert balances["EUR"] < 100  # frais et glissement payés
    assert storage.count("fills") == 2


def test_agent_view_exposes_limits_but_no_secrets():
    seen = []

    class Spy(ScriptedAgent):
        def decide(self, view):
            seen.append(view)
            return Decision("hold")

    engine, *_ = make_engine(default_cfg(), Spy())
    engine.run_cycle()
    view = seen[0].to_dict()
    assert view["equity"] == 100.0 and view["limits"]["max_order_quote"] == 20.0
    assert set(view) == {"timestamp", "quote_currency", "stake", "equity", "cash", "positions",
                         "recent_fills", "limits", "risk_tier", "candle_timeframe", "market"}
    assert view["risk_tier"] == "normal"
    assert set(view["market"]) == {"BTC/EUR", "ETH/EUR"}


# -- mort --------------------------------------------------------------------

def test_total_loss_kills_liquidates_and_stops_calling_the_agent():
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=100)
    agent = ScriptedAgent([buy(99)])
    engine, feed, _, storage = make_engine(cfg, agent)
    engine.run_cycle()
    assert storage.get("paper_balances")["BTC"] > 0

    feed.set("BTC/EUR", 29_000.0)  # le BTC perd plus de la moitié : equity ~ 49
    result = engine.run_cycle()
    assert result.status == "dead" and "perte totale" in result.note
    balances = storage.get("paper_balances")
    assert balances["BTC"] == pytest.approx(0.0, abs=1e-12)  # tout est vendu
    assert balances["EUR"] > 0
    assert storage.recent_fills(1)[0]["source"] == "killswitch"

    calls = agent.calls
    assert engine.run_cycle().status == "stopped"
    assert agent.calls == calls  # un bot mort n'est plus consulté


def test_drawdown_kills():
    cfg = permissive_cfg(max_total_loss_pct=90, max_drawdown_pct=30)
    engine, feed, _, _ = make_engine(cfg, ScriptedAgent([buy(99)]))
    engine.run_cycle()
    feed.set("BTC/EUR", 40_000.0)  # -33 %
    assert engine.run_cycle().status == "dead"


def test_no_liquidation_when_disabled():
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=100, liquidate_on_death=False)
    engine, feed, _, storage = make_engine(cfg, ScriptedAgent([buy(99)]))
    engine.run_cycle()
    feed.set("BTC/EUR", 29_000.0)
    assert engine.run_cycle().status == "dead"
    assert storage.get("paper_balances")["BTC"] > 0


def test_death_survives_a_restart_and_the_wallet_too(tmp_path):
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=100, liquidate_on_death=False)
    path = tmp_path / "agent.db"
    clock = FakeClock()
    feed = ScriptedFeed(clock)

    first = build_engine(cfg, ScriptedAgent([buy(99)]), feed, Storage(path), clock)
    first.run_cycle()
    feed.set("BTC/EUR", 29_000.0)
    assert first.run_cycle().status == "dead"

    agent = ScriptedAgent([buy(10)])
    second = build_engine(cfg, agent, feed, Storage(path), clock)  # « on relance le script »
    assert second.run_cycle().status == "stopped"
    assert agent.calls == 0


def test_reset_starts_a_new_life():
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=100)
    clock = FakeClock()
    feed = ScriptedFeed(clock)
    storage = Storage(":memory:")
    engine = build_engine(cfg, ScriptedAgent([buy(99)]), feed, storage, clock)
    engine.run_cycle()
    feed.set("BTC/EUR", 29_000.0)
    assert engine.run_cycle().status == "dead"

    reset_life(storage, clock())
    feed.set("BTC/EUR", 60_000.0)
    fresh = build_engine(cfg, ScriptedAgent(), feed, storage, clock)
    assert fresh.run_cycle().status == "hold"
    assert storage.get("paper_balances")["EUR"] == 100.0


def test_changing_the_stake_without_reset_is_refused():
    storage = Storage(":memory:")
    clock = FakeClock()
    feed = ScriptedFeed(clock)
    build_engine(default_cfg(stake=100), ScriptedAgent(), feed, storage, clock)
    with pytest.raises(ConfigError, match="mise"):
        build_engine(default_cfg(stake=1000), ScriptedAgent(), feed, storage, clock)


# -- erreurs -----------------------------------------------------------------

def test_agent_exception_is_treated_as_no_action():
    agent = ScriptedAgent([RuntimeError("API LLM down")])
    engine, _, _, storage = make_engine(default_cfg(), agent)
    result = engine.run_cycle()
    assert result.status == "error" and "API LLM down" in result.note
    assert storage.count("fills") == 0


def test_agent_returning_garbage_is_an_error_not_a_trade():
    agent = ScriptedAgent(["buy everything"])
    engine, _, _, storage = make_engine(default_cfg(), agent)
    assert engine.run_cycle().status == "error"
    assert storage.count("fills") == 0


def test_too_many_consecutive_errors_halt_without_liquidating():
    cfg = default_cfg(killswitch={"max_consecutive_errors": 3})
    agent = ScriptedAgent([buy(20)] + [RuntimeError("boom")] * 3)
    engine, _, _, storage = make_engine(cfg, agent)
    engine.run_cycle()
    btc = storage.get("paper_balances")["BTC"]
    assert btc > 0

    assert engine.run_cycle().status == "error"
    assert engine.run_cycle().status == "error"
    assert engine.run_cycle().status == "halted"
    assert engine.run_cycle().status == "stopped"
    assert storage.get("paper_balances")["BTC"] == btc  # arrêt opérationnel : on ne touche à rien


def test_feed_outage_is_an_error_and_never_a_trade():
    engine, feed, _, storage = make_engine(default_cfg(), ScriptedAgent([buy(10)]))
    feed.fail = True
    result = engine.run_cycle()
    assert result.status == "error"
    assert storage.count("fills") == 0


def test_stale_prices_block_orders():
    cfg = default_cfg()
    clock = FakeClock()

    class FrozenFeed(ScriptedFeed):
        def get_quote(self, symbol):
            q = super().get_quote(symbol)
            return type(q)(q.symbol, q.price, q.timestamp - 3600)  # prix vieux d'une heure

    engine, *_ = make_engine(cfg, ScriptedAgent([buy(10)]), clock=clock, feed=FrozenFeed(clock))
    result = engine.run_cycle()
    assert result.status == "rejected" and "périmé" in result.note


# -- compteurs du jour -------------------------------------------------------

def test_daily_buy_cap_then_new_day_resets_it():
    cfg = default_cfg(guardrails={"max_buys_per_day": 2})
    agent = ScriptedAgent([buy(5), buy(5), buy(5), buy(5)])
    engine, _, clock, _ = make_engine(cfg, agent)
    assert engine.run_cycle().status == "filled"
    assert engine.run_cycle().status == "filled"
    third = engine.run_cycle()
    assert third.status == "rejected" and "par jour" in third.note
    clock.advance(86_400)
    assert engine.run_cycle().status == "filled"


def test_daily_loss_limit_blocks_buying_after_a_drop():
    cfg = default_cfg(guardrails={"max_daily_loss_pct": 5, "max_order_pct": 40, "max_position_pct": 50})
    agent = ScriptedAgent([buy(40), buy(10), sell(1000)])
    engine, feed, _, _ = make_engine(cfg, agent)
    engine.run_cycle()
    feed.set("BTC/EUR", 30_000.0)  # -50 % sur ~40 EUR : equity ~ 80, sous le plancher de 95
    blocked = engine.run_cycle()
    assert blocked.status == "rejected" and "perte journalière" in blocked.note
    assert engine.run_cycle().status == "filled"  # mais vendre reste possible
