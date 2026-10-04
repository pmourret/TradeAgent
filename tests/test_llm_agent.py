import json

import pytest

from helpers import START, FakeClock, ScriptedFeed, default_cfg, make_engine
from tradeagent.agents import MarketView
from tradeagent.budget import InferenceBudget
from tradeagent.llm import LLMError, LLMReply, LLMUsage
from tradeagent.llm_agent import SYSTEM_PROMPT, LLMAgent
from tradeagent.market import summarize_candles
from tradeagent.models import Candle, InvalidDecision
from tradeagent.storage import Storage


class StubClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, max_output_tokens):
        self.calls.append((system, user, max_output_tokens))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(text, tokens_in=1500, tokens_out=150):
    return LLMReply(text, LLMUsage(tokens_in, tokens_out), "stub")


HOLD = '{"action": "hold", "reasoning": "rien de clair"}'
BUY = '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": 8, "reasoning": "momentum"}'


def candles(price=60_000.0, n=48):
    return [Candle(START + i * 3600, price, price * 1.001, price * 0.999, price + i) for i in range(n)]


def view(now=START, with_market=True):
    market = {s: summarize_candles(candles(p), "1h") for s, p in (("BTC/EUR", 60_000.0), ("ETH/EUR", 2_500.0))}
    return MarketView(
        timestamp=now, quote_currency="EUR", stake=50.0, equity=50.0, cash=50.0,
        positions={s: {"quantity": 0.0, "price": 1.0, "value": 0.0} for s in ("BTC/EUR", "ETH/EUR")},
        recent_fills=[{"ts": START, "symbol": "BTC/EUR", "side": "buy", "quantity": 0.0001, "price": 59_900.5, "fee": 0.01}],
        limits={"max_order_quote": 10.0, "min_order_quote": 5.0, "buys_left_today": 10},
        risk_tier="normal", candle_timeframe="1h", market=market if with_market else {},
    )


def make(replies, storage=None, **llm):
    cfg = default_cfg(llm={"call_every_seconds": 3600, "daily_budget_eur": 1.0, "total_budget_eur": 5.0, **llm})
    clock = FakeClock()
    storage = storage or Storage(":memory:")
    client = StubClient(replies)
    budget = InferenceBudget(cfg.llm, storage, clock)
    return LLMAgent(cfg, client, budget, storage, clock), client, clock, storage, budget


def test_no_market_data_means_no_call():
    agent, client, *_ = make([reply(HOLD)])
    decision = agent.decide(view(with_market=False))
    assert decision.skipped and not client.calls


def test_first_call_returns_the_decision_and_records_the_cost():
    agent, client, _, storage, budget = make([reply(BUY)])
    decision = agent.decide(view())
    assert (decision.action, decision.symbol, decision.amount_quote) == ("buy", "BTC/EUR", 8.0)
    assert not decision.skipped
    assert len(client.calls) == 1
    assert storage.count("llm_calls") == 1
    assert budget.spent_total() > 0


def test_cadence_blocks_calls_until_the_period_has_elapsed():
    agent, client, clock, *_ = make([reply(HOLD), reply(HOLD)])
    agent.decide(view())
    clock.advance(1800)
    second = agent.decide(view(clock()))
    assert second.skipped and "cadence" in second.reasoning and len(client.calls) == 1
    clock.advance(1800)
    third = agent.decide(view(clock()))
    assert not third.skipped and len(client.calls) == 2


def test_cadence_survives_a_restart():
    agent, client, clock, storage, _ = make([reply(HOLD)])
    agent.decide(view())
    reborn, client2, *_ = make([reply(HOLD)], storage=storage)
    assert reborn.decide(view()).skipped and not client2.calls


def test_exhausted_daily_budget_means_no_call():
    agent, client, clock, *_ = make([reply(HOLD), reply(HOLD)], daily_budget_eur=0.001)
    agent.decide(view())  # ce premier appel dépasse le plafond du jour
    clock.advance(7200)
    decision = agent.decide(view(clock()))
    assert decision.skipped and "budget" in decision.reasoning and len(client.calls) == 1


def test_budget_resets_the_next_day_but_total_does_not():
    agent, client, clock, *_ = make([reply(HOLD), reply(HOLD)], daily_budget_eur=0.001, total_budget_eur=5.0)
    agent.decide(view())
    clock.advance(86_400)
    assert not agent.decide(view(clock())).skipped  # nouveau jour : le budget du jour est reparti

    agent2, _, clock2, *_ = make([reply(HOLD, tokens_in=4_000_000)], daily_budget_eur=0.5, total_budget_eur=1.0)
    agent2.decide(view())
    clock2.advance(86_400 * 5)
    blocked = agent2.decide(view(clock2()))
    assert blocked.skipped and "total" in blocked.reasoning


def test_invalid_json_is_an_error_but_the_money_is_still_counted():
    agent, client, clock, storage, budget = make([reply("je pense qu'il faut acheter du BTC"), reply(HOLD)])
    with pytest.raises(InvalidDecision, match="réponse"):
        agent.decide(view())
    assert storage.count("llm_calls") == 1 and budget.spent_total() > 0
    clock.advance(60)
    assert agent.decide(view(clock())).skipped  # pas de rafale de nouvelles tentatives payantes


def test_nan_amount_from_the_model_is_rejected():
    agent, *_ = make([reply('{"action": "buy", "symbol": "BTC/EUR", "amount_quote": NaN}')])
    with pytest.raises(InvalidDecision):
        agent.decide(view())


def test_client_failure_propagates_without_cost_and_respects_cadence():
    agent, client, clock, storage, _ = make([LLMError("API down"), reply(HOLD)])
    with pytest.raises(LLMError):
        agent.decide(view())
    assert storage.count("llm_calls") == 0
    clock.advance(60)
    assert agent.decide(view(clock())).skipped and len(client.calls) == 1


def test_prompt_is_compact_complete_and_free_of_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    agent, client, *_ = make([reply(HOLD)])
    agent.decide(view())
    system, user, max_tokens = client.calls[0]

    assert "sk-super-secret" not in system + user
    assert len(user) < 4_500                      # ~1 500 tokens : le coût reste maîtrisé
    assert max_tokens == agent._cfg.llm.max_output_tokens
    data = json.loads(user.split("\n", 1)[1])
    assert data["risk_tier"] == "normal" and data["limits"]["max_order_quote"] == 10.0
    assert set(data["market"]) == {"BTC/EUR", "ETH/EUR"}
    assert data["your_api_budget_left_eur"]["today"] <= 1.0
    assert "hold" in SYSTEM_PROMPT and "valid action" in SYSTEM_PROMPT


def test_prompt_never_exposes_guardrail_internals():
    agent, client, *_ = make([reply(HOLD)])
    agent.decide(view())
    _, user, _ = client.calls[0]
    for forbidden in ("killswitch", "max_total_loss", "max_drawdown", "api_key", "database"):
        assert forbidden not in user


# -- avec le moteur ----------------------------------------------------------

def run_engine(replies, **cfg_kwargs):
    cfg = default_cfg(llm={"call_every_seconds": 60}, **cfg_kwargs)
    clock = FakeClock()
    storage = Storage(":memory:")
    client = StubClient(replies)
    agent = LLMAgent(cfg, client, InferenceBudget(cfg.llm, storage, clock), storage, clock)
    engine, *_ = make_engine(cfg, agent, clock=clock, storage=storage)
    return engine, client, clock, storage


def test_engine_with_llm_agent_trades_then_pauses():
    engine, client, clock, storage = run_engine([reply('{"action": "buy", "symbol": "BTC/EUR", '
                                                       '"amount_quote": 15, "reasoning": "test"}')])
    first = engine.run_cycle()
    assert first.status == "filled" and first.fill.notional == pytest.approx(15, abs=0.5)
    clock.advance(30)
    assert engine.run_cycle().status == "skip"
    assert storage.count("decisions") == 1      # un skip n'est pas une décision journalisée
    assert len(client.calls) == 1


def test_pauses_do_not_hide_repeated_llm_failures():
    # Sans précaution, les cycles « skip » entre deux appels remettraient le compteur d'erreurs à zéro
    # et un LLM durablement en panne ne déclencherait jamais l'arrêt.
    engine, client, clock, _ = run_engine([LLMError("down")] * 3, killswitch={"max_consecutive_errors": 3})
    statuses = []
    for _ in range(5):
        statuses.append(engine.run_cycle().status)
        clock.advance(40)
    assert statuses == ["error", "skip", "error", "skip", "halted"]


def test_missing_candles_pause_the_llm_without_stopping_the_engine():
    cfg = default_cfg(llm={"call_every_seconds": 60})
    clock = FakeClock()
    feed = ScriptedFeed(clock)
    feed.fail_candles = True
    storage = Storage(":memory:")
    client = StubClient([reply(HOLD)])
    agent = LLMAgent(cfg, client, InferenceBudget(cfg.llm, storage, clock), storage, clock)
    engine, *_ = make_engine(cfg, agent, clock=clock, feed=feed, storage=storage)
    result = engine.run_cycle()
    assert result.status == "skip" and "marché" in result.note and not client.calls
    assert any("bougies" in e["message"] for e in storage.recent_events(5))
