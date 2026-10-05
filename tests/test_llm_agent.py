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
    assert data["api_safety_caps_left"]["today"] <= 1.0 and "your_api_budget_left_eur" not in data
    assert "hold" in SYSTEM_PROMPT and "not money you own" in SYSTEM_PROMPT


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


# -- prompt versionné, loyer visible, réveil choisi par l'agent, palier « économie » ---------------------------

def test_le_prompt_donne_les_vrais_frais_le_loyer_et_les_bornes_de_reveil():
    from tradeagent.llm_agent import PROMPT_VERSION, PROMPT_VERSION_KEY

    agent, client, clock, storage, _ = make([reply(HOLD)])
    storage.set("life", {"stake": 50.0, "started": START - 100})
    storage.record_llm_call(START - 90_000, "m", 0, 0, 0, 0, 0.9)                 # vie précédente : hors loyer
    storage.record_llm_call(START - 50, "m", 0, 0, 0, 0, 0.13)
    agent.decide(view())
    system, user, _ = client.calls[0]
    data = json.loads(user.split("\n", 1)[1])
    # Ceux de la config, pas un texte figé ; l'aller-retour est donné tout calculé (0,15 % par sens, 0,30 % les deux).
    assert data["costs"] == {"fee_pct": 0.1, "slippage_pct": 0.05, "one_way_pct": 0.15, "round_trip_pct": 0.3}
    assert data["api_cost_per_call"] == 0.13                                    # un seul appel dans cette vie
    assert data["api_cost_so_far"] == 0.13 and data["net_equity"] == 49.87 and data["equity"] == 50.0
    assert data["call_interval_minutes"] == {"min": 60, "max": 1440, "default": 60, "default_wake_move_pct": None,
                                             "wake_move_pct_while_holding": 3.0}
    assert "0.1%" not in system and "round_trip_pct" in system and "net_equity" in system
    # L'inaction n'est pas présentée comme une réussite, et les plafonds d'API ne sont pas « son » argent.
    assert "usually the right one" not in system and "counts as failure" in system
    assert "api_safety_caps_left" in system and "api_cost_per_call" in system
    assert "next_check_minutes" in system and "wake_if_move_pct" in system
    assert storage.get(PROMPT_VERSION_KEY) == PROMPT_VERSION == 9


def test_les_cles_de_reveil_sont_lues_avec_prudence():
    from tradeagent.llm_agent import parse_wake

    assert parse_wake('{"action": "hold", "next_check_minutes": 360, "wake_if_move_pct": 3}') == (360.0, 3.0)
    assert parse_wake('```json\n{"action": "hold", "next_check_minutes": 90.5}\n```') == (90.5, None)
    for bad in ('{"next_check_minutes": "360"}', '{"next_check_minutes": -5, "wake_if_move_pct": 0}',
                '{"next_check_minutes": true}', '{"next_check_minutes": NaN}', '[360]', "pas du json", '{"action": "hold"}',
                '{"next_check_minutes": 1' + "0" * 400 + '}', '{"wake_if_move_pct": 1e999}'):
        assert parse_wake(bad) == (None, None), bad


def sleepy(minutes=None, move=None):
    extra = {}
    if minutes is not None:
        extra["next_check_minutes"] = minutes
    if move is not None:
        extra["wake_if_move_pct"] = move
    return reply(json.dumps({"action": "hold", "reasoning": "calme", **extra}))


def priced(now, btc=60_000.0, tier="normal", **over):
    from dataclasses import replace

    positions = {"BTC/EUR": {"quantity": 0.0, "price": btc, "value": 0.0},
                 "ETH/EUR": {"quantity": 0.0, "price": 2_500.0, "value": 0.0}}
    return replace(view(now), **{"positions": positions, "risk_tier": tier, **over})


HELD = {"BTC/EUR": {"quantity": 0.001, "price": 60_000.0, "value": 60.0},
        "ETH/EUR": {"quantity": 0.0, "price": 2_500.0, "value": 0.0}}
DUST = {"BTC/EUR": {"quantity": 0.00001, "price": 60_000.0, "value": 0.6},
        "ETH/EUR": {"quantity": 0.0, "price": 2_500.0, "value": 0.0}}
LIMITS = {"max_order_quote": 10.0, "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 47.5}


def test_l_agent_peut_demander_a_dormir_et_n_est_pas_appele_avant():
    agent, client, clock, *_ = make([sleepy(minutes=360), reply(HOLD), reply(HOLD)])
    agent.decide(priced(clock()))
    clock.advance(3_600)                                                        # la cadence de la config est passée
    asleep = agent.decide(priced(clock()))
    assert asleep.skipped and "sommeil choisi par l'agent : encore 300 min" in asleep.reasoning
    clock.advance(5 * 3_600 - 1)
    assert agent.decide(priced(clock())).skipped
    clock.advance(1)
    assert not agent.decide(priced(clock())).skipped and len(client.calls) == 2
    clock.advance(3_600)                                                        # la seconde réponse ne demandait rien :
    assert not agent.decide(priced(clock())).skipped and len(client.calls) == 3      # retour à la cadence par défaut


def test_le_sommeil_demande_est_borne_par_le_code():
    from tradeagent.llm_agent import WAKE_KEY

    agent, _, clock, storage, _ = make([sleepy(minutes=1), sleepy(minutes=1_000_000)])
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY)["at"] == clock() + 3_600                       # jamais plus tôt que la cadence
    clock.advance(3_600)
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY)["at"] == clock() + 86_400                      # jamais plus tard que le maximum


def test_un_mouvement_de_prix_reveille_l_agent_mais_jamais_avant_la_cadence():
    agent, client, clock, *_ = make([sleepy(minutes=600, move=3), reply(HOLD)])
    agent.decide(priced(clock(), btc=60_000.0))
    clock.advance(1_800)
    assert agent.decide(priced(clock(), btc=70_000.0)).skipped                  # gros mouvement, mais cadence non écoulée
    clock.advance(1_800)
    assert agent.decide(priced(clock(), btc=61_799.0)).skipped                  # +2,998 % : pas assez
    assert agent.decide(priced(clock(), btc=58_201.0)).skipped                  # -2,998 %
    woken = agent.decide(priced(clock(), btc=58_200.0))                         # -3 % pile : réveil
    assert not woken.skipped and len(client.calls) == 2


def test_le_seuil_de_mouvement_est_borne():
    from tradeagent.llm_agent import WAKE_KEY

    agent, _, clock, storage, _ = make([sleepy(move=0.01), sleepy(move=900)])
    agent.decide(priced(clock()))
    wake = storage.get(WAKE_KEY)
    assert wake["move_pct"] == 0.5 and wake["at"] == clock() + 3_600 and wake["prices"]["BTC/EUR"] == 60_000.0
    clock.advance(3_600)
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY)["move_pct"] == 50.0


def test_un_changement_de_palier_reveille_l_agent():
    agent, client, clock, *_ = make([sleepy(minutes=600), reply(HOLD)])
    agent.decide(priced(clock()))
    clock.advance(7_200)                                                        # cadence du palier prudent : 2 h
    assert agent.decide(priced(clock())).skipped                                # même palier : il dort
    assert not agent.decide(priced(clock(), tier="cautious")).skipped
    assert len(client.calls) == 2


def test_une_reponse_invalide_ne_regle_aucun_reveil():
    from tradeagent.llm_agent import WAKE_KEY

    agent, _, clock, storage, _ = make([reply('{"action": "danse", "next_check_minutes": 600}')])
    with pytest.raises(InvalidDecision):
        agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY) is None


def test_palier_economie_l_intervalle_minimal_s_allonge_quand_l_equity_nette_recule():
    agent, client, clock, *_ = make([reply(HOLD)] * 4)
    assert (agent.min_interval("normal"), agent.min_interval("cautious"), agent.min_interval("defensive")) == (3_600, 7_200, 14_400)
    agent.decide(priced(clock(), tier="cautious"))
    clock.advance(7_199)
    assert agent.decide(priced(clock(), tier="cautious")).skipped
    clock.advance(1)
    assert not agent.decide(priced(clock(), tier="cautious")).skipped
    clock.advance(14_399)
    assert agent.decide(priced(clock(), tier="defensive", positions=HELD)).skipped
    clock.advance(1)
    assert not agent.decide(priced(clock(), tier="defensive", positions=HELD)).skipped
    assert len(client.calls) == 3

    capped, *_ = make([], call_every_seconds=3_600, max_call_interval_seconds=10_000, economy_call_factor=5)
    assert capped.min_interval("defensive") == 10_000                           # jamais au-delà du maximum
    flat, *_ = make([], economy_call_factor=1)
    assert flat.min_interval("defensive") == 3_600


def test_aucun_appel_paye_quand_aucun_ordre_n_est_possible():
    stuck = [
        priced(START, tier="defensive"),                                        # achats bloqués, rien à vendre
        priced(START, limits={**LIMITS, "buys_left_today": 0}),                 # plafond d'achats du jour atteint
        priced(START, limits=LIMITS, equity=47.49, cash=47.49),                 # perte du jour atteinte
        priced(START, limits=LIMITS, cash=4.99),                                # pas de quoi passer l'ordre minimum
        priced(START, tier="defensive", positions=DUST),                        # poussière invendable
    ]
    for case in stuck:
        agent, client, *_ = make([reply(HOLD)])
        decision = agent.decide(case)
        assert decision.skipped and "rien à faire" in decision.reasoning and client.calls == []
    for case in (priced(START, tier="defensive", positions=HELD), priced(START, limits=LIMITS),
                 priced(START, limits=LIMITS, equity=47.5, cash=47.5),       # pile le plancher : achat permis, comme les garde-fous
                 priced(START, limits=LIMITS, cash=5.0),
                 priced(START, limits={**LIMITS, "buys_left_today": 0}, positions=HELD)):
        agent, client, *_ = make([reply(HOLD)])
        assert not agent.decide(case).skipped and len(client.calls) == 1        # il peut vendre, ou acheter


def test_les_nouvelles_cles_de_cadence_sont_validees():
    from tradeagent.config import ConfigError

    for bad in ({"max_call_interval_seconds": 1_800}, {"max_call_interval_seconds": 8 * 86_400},
                {"economy_call_factor": 0.5}, {"economy_call_factor": 11}, {"economy_call_factor": "2"},
                {"max_call_interval_seconds": float("nan")}):
        with pytest.raises(ConfigError):
            default_cfg(llm=bad)
    cfg = default_cfg(llm={"max_call_interval_seconds": 3_600, "economy_call_factor": 1})
    assert cfg.llm.max_call_interval_seconds == 3_600.0 and cfg.llm.economy_call_factor == 1.0
    assert default_cfg().llm.max_call_interval_seconds == 86_400.0 and default_cfg().llm.economy_call_factor == 2.0


def test_le_seuil_de_mouvement_est_atteint_a_l_egalite():
    agent, client, clock, *_ = make([sleepy(minutes=600, move=50), reply(HOLD)])
    agent.decide(priced(clock(), btc=60_000.0))
    clock.advance(3_600)
    assert agent.decide(priced(clock(), btc=30_001.0)).skipped
    assert not agent.decide(priced(clock(), btc=30_000.0)).skipped              # -50 % pile


def test_une_reponse_sans_cle_de_reveil_efface_le_sommeil_precedent():
    from tradeagent.llm_agent import WAKE_KEY

    agent, client, clock, storage, _ = make([sleepy(minutes=600, move=3), reply(HOLD), reply(HOLD)])
    agent.decide(priced(clock(), btc=60_000.0))
    clock.advance(3_600)
    assert not agent.decide(priced(clock(), btc=66_000.0)).skipped              # réveillé par le mouvement
    assert storage.get(WAKE_KEY) is None                                        # la nouvelle réponse ne demande rien
    clock.advance(3_600)
    assert not agent.decide(priced(clock(), btc=60_000.0)).skipped              # l'ancien sommeil de 10 h ne tient plus
    assert len(client.calls) == 3


def test_le_tableau_de_bord_annonce_le_reveil_choisi_par_l_agent(tmp_path):
    from helpers import ScriptedAgent
    from tradeagent.dashboard import build_snapshot
    from tradeagent.llm_agent import LAST_CALL_KEY, PROMPT_VERSION_KEY, WAKE_KEY

    path = tmp_path / "agent.db"
    cfg = default_cfg(database=str(path))
    clock = FakeClock()
    storage = Storage(str(path))
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent(), clock=clock, storage=storage)
    engine.run_cycle()
    api = build_snapshot(cfg, now=clock())["api"]
    assert api["next_call"] is None and api["wake_if_move_pct"] is None and api["prompt_version"] is None

    storage.set(LAST_CALL_KEY, clock())
    storage.set(PROMPT_VERSION_KEY, 2)
    api = build_snapshot(cfg, now=clock())["api"]
    assert api["next_call"] == clock() + cfg.llm.call_every_seconds and api["prompt_version"] == 2      # valeur écrite ci-dessus

    storage.set(WAKE_KEY, {"at": clock() + 6 * 3_600, "tier": "normal", "move_pct": 3.0, "prices": {}})
    api = build_snapshot(cfg, now=clock())["api"]
    assert api["next_call"] == clock() + 6 * 3_600 and api["wake_if_move_pct"] == 3.0
    late = build_snapshot(cfg, now=clock() + 7 * 3_600)["api"]                  # réveil dépassé : plus d'alerte de mouvement
    assert late["wake_if_move_pct"] is None

    storage.set(WAKE_KEY, {"at": clock() + 60, "tier": "normal", "move_pct": None, "prices": {}})
    assert build_snapshot(cfg, now=clock())["api"]["next_call"] == clock() + cfg.llm.call_every_seconds   # jamais avant la cadence


def test_un_entier_demesure_dans_la_reponse_ne_gache_pas_une_decision_payee():
    huge = '{"action": "hold", "reasoning": "x", "next_check_minutes": 1' + "0" * 400 + "}"
    agent, client, clock, storage, _ = make([reply(huge)])
    decision = agent.decide(priced(clock()))
    assert decision.action == "hold" and not decision.skipped                   # pas d'exception, pas d'erreur de cycle


def test_l_arret_faute_d_ordre_possible_est_signale_une_fois_puis_leve():
    from tradeagent.llm_agent import IDLE_KEY

    agent, client, clock, storage, _ = make([reply(HOLD)])
    for _ in range(3):
        assert agent.decide(priced(clock(), tier="defensive")).skipped
        clock.advance(900)
    assert storage.get(IDLE_KEY) == {"since": START, "cause": "defensive"}
    events = [e["message"] for e in storage.recent_events(10)]
    assert sum("agent à l'arrêt (defensive) : aucun ordre possible" in m for m in events) == 1   # une fois, pas à chaque cycle
    assert not agent.decide(priced(clock())).skipped                            # un ordre redevient possible
    assert storage.get(IDLE_KEY) is None
    assert any("agent de nouveau appelé" in e["message"] for e in storage.recent_events(10))


def test_reset_efface_le_sommeil_et_l_arret_mais_pas_la_cadence():
    from tradeagent.app import LIFE_KEYS, reset_life
    from tradeagent.llm_agent import IDLE_KEY, LAST_CALL_KEY, WAKE_KEY

    assert WAKE_KEY in LIFE_KEYS and IDLE_KEY in LIFE_KEYS and LAST_CALL_KEY not in LIFE_KEYS
    agent, client, clock, storage, _ = make([sleepy(minutes=1_440), reply(HOLD)])
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY) is not None
    clock.advance(1_800)
    reset_life(storage, clock())
    assert storage.get(WAKE_KEY) is None
    assert agent.decide(priced(clock())).skipped                                # la cadence payante survit au reset
    clock.advance(1_800)
    assert not agent.decide(priced(clock())).skipped and len(client.calls) == 2      # mais pas le sommeil de 24 h


def test_le_tableau_de_bord_suit_le_palier_economie_et_signale_l_arret(tmp_path):
    from helpers import ScriptedAgent
    from tradeagent.dashboard import build_snapshot
    from tradeagent.llm_agent import IDLE_KEY, LAST_CALL_KEY

    path = tmp_path / "agent.db"
    cfg = default_cfg(database=str(path))
    clock = FakeClock()
    storage = Storage(str(path))
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent(), clock=clock, storage=storage)
    engine.run_cycle()
    storage.set(LAST_CALL_KEY, clock())
    for tier, factor in (("normal", 1), ("cautious", 2), ("defensive", 4)):
        storage.set("risk_tier", tier)
        api = build_snapshot(cfg, now=clock())["api"]
        assert api["next_call"] == clock() + factor * cfg.llm.call_every_seconds, tier
        assert api["min_interval_seconds"] == factor * cfg.llm.call_every_seconds
    assert api["idle_since"] is None and build_snapshot(cfg, now=clock())["advice"] is None
    storage.set(IDLE_KEY, {"since": clock(), "cause": "defensive"})
    snap = build_snapshot(cfg, now=clock())
    assert snap["api"]["idle_since"] == clock() and snap["advice"]["decision"] is True


# -- réveil obligatoire sur une position, causes d'arrêt, conseil à l'utilisateur ------------------------------

def test_reveil_obligatoire_sur_mouvement_de_prix_quand_l_agent_detient_une_position():
    from tradeagent.llm_agent import WAKE_KEY

    # Il demande 24 h de sommeil sans réveil sur prix, en détenant du BTC : le code impose le seuil de la config.
    agent, client, clock, storage, _ = make([sleepy(minutes=1_440), reply(HOLD)])
    agent.decide(priced(clock(), positions=HELD))
    assert storage.get(WAKE_KEY)["move_pct"] == 3.0
    clock.advance(3_600)
    moved = {**HELD, "BTC/EUR": {**HELD["BTC/EUR"], "price": 58_200.0}}         # -3 %
    calm = {**HELD, "BTC/EUR": {**HELD["BTC/EUR"], "price": 59_000.0}}
    assert agent.decide(priced(clock(), positions=calm)).skipped
    assert not agent.decide(priced(clock(), positions=moved)).skipped and len(client.calls) == 2

    for asked, kept in ((10, 3.0), (3, 3.0), (1.5, 1.5)):                       # plus serré oui, plus large non
        agent, _, clock, storage, _ = make([sleepy(minutes=600, move=asked)])
        agent.decide(priced(clock(), positions=HELD))
        assert storage.get(WAKE_KEY)["move_pct"] == kept, asked

    # Sans position, rien d'imposé : en cash, dormir ne laisse rien sans surveillance.
    agent, _, clock, storage, _ = make([sleepy(minutes=600), sleepy(minutes=600, move=10)])
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY)["move_pct"] is None
    clock.advance(36_000)
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY)["move_pct"] == 10.0

    # Même une poussière compte comme une position : on ne dort pas dessus sans réveil.
    agent, _, clock, storage, _ = make([sleepy(minutes=600)])
    agent.decide(priced(clock(), positions=DUST))
    assert storage.get(WAKE_KEY)["move_pct"] == 3.0

    tight, _, clock, storage, _ = make([sleepy(minutes=600)], position_wake_move_pct=1.0)
    tight.decide(priced(clock(), positions=HELD))
    assert storage.get(WAKE_KEY)["move_pct"] == 1.0


def test_le_seuil_de_reveil_impose_est_valide_dans_la_config():
    from tradeagent.config import ConfigError

    for bad in (0.4, 51, "3", float("inf")):
        with pytest.raises(ConfigError):
            default_cfg(llm={"position_wake_move_pct": bad})
    assert default_cfg().llm.position_wake_move_pct == 3.0


def test_les_causes_d_arret_distinguent_le_temporaire_du_sans_issue():
    cause = LLMAgent.idle_cause
    assert cause(priced(START, limits=LIMITS)) is None
    assert cause(priced(START, tier="defensive", positions=HELD)) is None       # il peut vendre
    assert cause(priced(START, tier="defensive")) == "defensive"
    assert cause(priced(START, tier="defensive", limits={**LIMITS, "buys_left_today": 0}, cash=1.0)) == "defensive"
    assert cause(priced(START, limits=LIMITS, cash=4.99)) == "cash"
    assert cause(priced(START, limits={**LIMITS, "buys_left_today": 0}, cash=4.99)) == "cash"   # sans issue avant temporaire
    assert cause(priced(START, limits={**LIMITS, "buys_left_today": 0})) == "daily"
    assert cause(priced(START, limits=LIMITS, equity=47.49, cash=47.49)) == "daily"


def test_la_cause_d_arret_est_mise_a_jour_sans_perdre_la_date():
    from tradeagent.llm_agent import IDLE_KEY

    agent, _, clock, storage, _ = make([])
    agent.decide(priced(clock(), limits={**LIMITS, "buys_left_today": 0}))
    assert storage.get(IDLE_KEY) == {"since": START, "cause": "daily"}
    clock.advance(900)
    agent.decide(priced(clock(), tier="defensive"))
    assert storage.get(IDLE_KEY) == {"since": START, "cause": "defensive"}       # la cause change, pas le début
    assert sum("agent à l'arrêt" in e["message"] for e in storage.recent_events(10)) == 2


def test_le_conseil_dresse_le_bilan_et_laisse_l_utilisateur_decider():
    from tradeagent.advice import idle_advice

    lost = idle_advice("defensive", stake=50.0, equity=50.4, rent=12.9, currency="EUR")
    assert lost["decision"] is True and lost["title"] == "Agent à l'arrêt : à toi de décider"
    assert "+0.40 EUR en trading, 12.90 EUR d'API consommés, soit -12.50 EUR net" in lost["text"]
    assert "mettre fin à cette vie (tradeagent reset)" in lost["text"] and "n'a pas couvert son loyer" in lost["text"]
    assert lost["net"] == pytest.approx(-12.5) and lost["trading"] == pytest.approx(0.4) and lost["rent"] == 12.9

    won = idle_advice("cash", stake=50.0, equity=58.0, rent=3.0, currency="EUR")
    assert won["decision"] is True and "soit +5.00 EUR net" in won["text"]
    assert "a couvert son loyer" in won["text"] and "plus assez de cash" in won["text"]
    assert "n'a pas couvert" not in won["text"]
    even = idle_advice("defensive", stake=50.0, equity=53.0, rent=3.0, currency="EUR")
    assert "a couvert son loyer" in even["text"]                                # pile à l'équilibre : loyer couvert

    wait = idle_advice("daily", stake=50.0, equity=49.0, rent=0.5, currency="EUR")
    assert wait["decision"] is False and "reprennent demain (UTC)" in wait["text"] and "reset" not in wait["text"]


def test_status_et_tableau_de_bord_donnent_le_conseil(tmp_path, monkeypatch, capsys):
    from helpers import ScriptedAgent
    from tradeagent import cli
    from tradeagent.dashboard import build_snapshot
    from tradeagent.llm_agent import IDLE_KEY

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\n", encoding="utf-8")
    cfg = default_cfg(database="data/agent.db")
    clock = FakeClock()
    storage = Storage("data/agent.db")
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent(), clock=clock, storage=storage)
    engine.run_cycle()
    storage.record_llm_call(clock(), "m", 0, 0, 0, 0, 26.0)
    storage.set(IDLE_KEY, {"since": clock(), "cause": "defensive"})
    snap = build_snapshot(cfg, now=clock())
    assert snap["advice"]["decision"] is True and snap["advice"]["net"] == pytest.approx(-26.0)
    assert "26.00 EUR d'API consommés" in snap["advice"]["text"]
    storage.close()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "AGENT À L'ARRÊT : À TOI DE DÉCIDER" in out and "mettre fin à cette vie (tradeagent reset)" in out


# -- appels sur évènement décidés par le code, format de réponse imposé par l'API ------------------------------

EVENTS = {"quiet_call_interval_seconds": 43_200, "wake_move_pct": 2.0}


def test_sans_demande_de_l_agent_le_code_impose_le_delai_de_calme_et_le_seuil_de_mouvement():
    from tradeagent.llm_agent import WAKE_KEY

    agent, client, clock, storage, _ = make([reply(HOLD)] * 3, **EVENTS)
    agent.decide(priced(clock(), btc=60_000.0))
    wake = storage.get(WAKE_KEY)
    assert wake["at"] == clock() + 43_200 and wake["move_pct"] == 2.0           # il n'a rien demandé : défauts du code
    clock.advance(3_600)
    assert agent.decide(priced(clock(), btc=60_000.0)).skipped                  # plus d'appel toutes les heures
    clock.advance(3_600)
    assert agent.decide(priced(clock(), btc=61_199.0)).skipped                  # +1,998 % : pas un évènement
    assert not agent.decide(priced(clock(), btc=61_200.0)).skipped              # +2 % : appelé
    clock.advance(43_199)
    assert agent.decide(priced(clock(), btc=61_200.0)).skipped
    clock.advance(1)
    assert not agent.decide(priced(clock(), btc=61_200.0)).skipped              # 12 h de calme : appelé
    assert len(client.calls) == 3


def test_l_agent_peut_changer_les_defauts_dans_les_bornes():
    from tradeagent.llm_agent import WAKE_KEY

    agent, _, clock, storage, _ = make([sleepy(minutes=120), sleepy(move=5), sleepy(minutes=1, move=0.1)], **EVENTS)
    agent.decide(priced(clock()))
    assert storage.get(WAKE_KEY) == {"at": clock() + 7_200, "tier": "normal", "move_pct": 2.0,
                                     "prices": {"BTC/EUR": 60_000.0, "ETH/EUR": 2_500.0}, "regimes": {}}
    clock.advance(7_200)
    agent.decide(priced(clock()))
    wake = storage.get(WAKE_KEY)
    assert wake["at"] == clock() + 43_200 and wake["move_pct"] == 5.0           # délai par défaut, seuil choisi
    clock.advance(43_200)
    agent.decide(priced(clock()))
    wake = storage.get(WAKE_KEY)
    assert wake["at"] == clock() + 3_600 and wake["move_pct"] == 0.5            # bornes : cadence minimale, seuil plancher


def test_les_defauts_respectent_le_palier_economie_et_la_position():
    from tradeagent.llm_agent import WAKE_KEY

    agent, _, clock, storage, _ = make([reply(HOLD)], quiet_call_interval_seconds=3_600, wake_move_pct=5.0)
    agent.decide(priced(clock(), tier="cautious", positions=HELD))
    wake = storage.get(WAKE_KEY)
    assert wake["at"] == clock() + 7_200                                        # jamais sous l'intervalle minimal du palier
    assert wake["move_pct"] == 3.0                                              # position détenue : seuil imposé, plus serré

    agent, client, clock, storage, _ = make([reply(HOLD)], **EVENTS)
    agent.decide(priced(clock()))
    data_user = client.calls[0][1]
    data = json.loads(data_user.split("\n", 1)[1])
    assert data["call_interval_minutes"] == {"min": 60, "max": 1440, "default": 720, "default_wake_move_pct": 2.0,
                                             "wake_move_pct_while_holding": 3.0}


def test_les_cles_d_appels_sur_evenement_sont_validees():
    from tradeagent.config import ConfigError, load_config

    for bad in ({"quiet_call_interval_seconds": 1_800}, {"quiet_call_interval_seconds": 90_000},
                {"wake_move_pct": 0.4}, {"wake_move_pct": 51}, {"wake_move_pct": "2"},
                {"quiet_call_interval_seconds": float("nan")}):
        with pytest.raises(ConfigError):
            default_cfg(llm=bad)
    assert default_cfg().llm.quiet_call_interval_seconds is None and default_cfg().llm.wake_move_pct is None
    cfg = default_cfg(llm=EVENTS)
    assert cfg.llm.quiet_call_interval_seconds == 43_200.0 and cfg.llm.wake_move_pct == 2.0


def test_le_schema_de_reponse_couvre_la_decision_et_le_reveil():
    from tradeagent.llm_agent import DECISION_SCHEMA
    from tradeagent.models import Decision

    assert DECISION_SCHEMA["additionalProperties"] is False
    assert set(DECISION_SCHEMA["required"]) == set(DECISION_SCHEMA["properties"]) == {
        "action", "symbol", "amount_quote", "reasoning", "exit_below", "next_check_minutes", "wake_if_move_pct"}
    assert DECISION_SCHEMA["properties"]["action"]["enum"] == ["buy", "sell", "hold"]
    # Une réponse conforme au schéma, avec ses null, reste lisible par le code existant.
    text = '{"action":"hold","symbol":null,"amount_quote":null,"reasoning":"calme","next_check_minutes":null,"wake_if_move_pct":null}'
    assert Decision.from_json(text).action == "hold"
    buy = '{"action":"buy","symbol":"BTC/EUR","amount_quote":8,"reasoning":"x","next_check_minutes":240,"wake_if_move_pct":null}'
    assert Decision.from_json(buy).amount_quote == 8.0
    with pytest.raises(InvalidDecision):
        Decision.from_json('{"action":"buy","symbol":null,"amount_quote":null,"reasoning":"x","next_check_minutes":null,"wake_if_move_pct":null}')


def test_le_client_anthropic_envoie_le_schema_a_l_api_quand_on_lui_en_donne_un():
    from types import SimpleNamespace

    from tradeagent.llm import AnthropicClient
    from tradeagent.llm_agent import DECISION_SCHEMA

    seen = []

    class Messages:
        def create(self, **kwargs):
            seen.append(kwargs)
            usage = SimpleNamespace(input_tokens=10, output_tokens=5)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=HOLD)], usage=usage)

    fake = SimpleNamespace(messages=Messages())
    AnthropicClient("m", client=fake, output_schema=DECISION_SCHEMA).complete("sys", "user", 400)
    assert seen[0]["output_config"] == {"format": {"type": "json_schema", "schema": DECISION_SCHEMA}}
    AnthropicClient("m", client=fake).complete("sys", "user", 400)
    assert "output_config" not in seen[1]                                       # sans schéma : requête inchangée


def test_le_bot_et_le_backtest_demandent_le_format_impose(monkeypatch):
    from tradeagent import app, cli
    from tradeagent.llm_agent import DECISION_SCHEMA

    built = []
    monkeypatch.setattr(app, "AnthropicClient", lambda model, **kwargs: built.append(kwargs) or object())
    app.build_agent("llm", default_cfg(), Storage(":memory:"))
    assert built == [{"output_schema": DECISION_SCHEMA}]
    source = open(cli.__file__, encoding="utf-8").read()
    assert "output_schema=schema" in source and "else (DECISION_SCHEMA," in source     # le backtest : schéma de l'agent rejoué


# -- positions détaillées : prix de revient, gain ou perte latente, âge ------------------------------------------

def test_le_prix_de_revient_suit_les_achats_et_les_ventes():
    from tradeagent.llm_agent import position_stats

    def fill(ts, side, qty, price, fee=0.0, symbol="BTC/EUR"):
        return {"ts": ts, "symbol": symbol, "side": side, "quantity": qty, "price": price, "fee": fee}

    assert position_stats([]) == {}
    one = position_stats([fill(10, "buy", 2.0, 100.0, fee=1.0)])
    assert one == {"BTC/EUR": {"entry_price": 100.5, "opened": 10}}             # les frais d'achat entrent dans le prix de revient
    two = position_stats([fill(10, "buy", 1.0, 100.0), fill(20, "buy", 1.0, 200.0)])
    assert two["BTC/EUR"] == {"entry_price": 150.0, "opened": 10}               # moyenne pondérée, date du premier achat
    partial = position_stats([fill(10, "buy", 2.0, 100.0), fill(20, "sell", 1.0, 500.0)])
    assert partial["BTC/EUR"]["entry_price"] == 100.0                           # une vente ne change pas le prix de revient
    closed = position_stats([fill(10, "buy", 2.0, 100.0), fill(20, "sell", 2.0, 90.0)])
    assert closed == {}
    reopened = position_stats([fill(10, "buy", 2.0, 100.0), fill(20, "sell", 2.0, 90.0), fill(30, "buy", 1.0, 80.0)])
    assert reopened == {"BTC/EUR": {"entry_price": 80.0, "opened": 30}}         # position rouverte : on repart de zéro
    both = position_stats([fill(10, "buy", 1.0, 100.0), fill(11, "buy", 4.0, 25.0, symbol="ETH/EUR")])
    assert both["ETH/EUR"]["entry_price"] == 25.0 and both["BTC/EUR"]["entry_price"] == 100.0


def test_le_prompt_montre_le_gain_latent_et_l_age_de_chaque_position():
    from tradeagent.models import Fill

    agent, client, clock, storage, _ = make([reply(HOLD)])
    storage.set("life", {"stake": 50.0, "started": START - 10_000})
    storage.record_fill(Fill("BTC/EUR", "buy", 0.5, 50_000.0, 0.0, START - 20_000), source="llm")   # vie précédente
    storage.record_fill(Fill("BTC/EUR", "buy", 0.001, 66_000.0, 0.0, START - 7_200), source="llm")
    agent.decide(priced(clock(), btc=60_000.0, positions=HELD))
    data = json.loads(client.calls[0][1].split("\n", 1)[1])
    assert data["positions"]["BTC/EUR"] == {"qty": 0.001, "value": 60.0, "entry_price": 66000.0,
                                            "pnl_pct": -9.09, "held_hours": 2}
    assert data["positions"]["ETH/EUR"] == {"qty": 0.0, "value": 0.0}           # pas de position : rien de plus
    system = client.calls[0][0]
    assert "trend_vs_sma_pct" in system and "pnl_pct" in system and "not on getting back to your entry price" in system


# -- plan de sortie fixé à l'achat, prompt allégé ---------------------------------------------------------------

def decision_reply(action, symbol=None, amount=None, exit_below=None, minutes=None, move=None, why="raison"):
    return reply(json.dumps({"action": action, "symbol": symbol, "amount_quote": amount, "reasoning": why,
                             "exit_below": exit_below, "next_check_minutes": minutes, "wake_if_move_pct": move}))


def with_market(view_, atr=1.0):
    from dataclasses import replace

    market = {s: {**m, "atr_pct": atr} for s, m in view_.market.items()}
    return replace(view_, market=market)


def test_un_achat_garde_son_plan_de_sortie_et_sa_raison():
    from tradeagent.llm_agent import PLAN_KEY

    agent, client, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10, exit_below=57_000, why="tendance haussière sur 30 j"),
                                             reply(HOLD)])
    agent.decide(with_market(priced(clock(), btc=60_000.0)))
    assert storage.get(PLAN_KEY) == {"BTC/EUR": {"exit_below": 57000.0, "thesis": "tendance haussière sur 30 j", "set_at": START}}

    clock.advance(3_600)                                                        # l'achat a eu lieu : il détient du BTC
    agent.decide(with_market(priced(clock(), btc=59_000.0, positions={
        **HELD, "BTC/EUR": {**HELD["BTC/EUR"], "price": 59_000.0}})))
    data = json.loads(client.calls[1][1].split("\n", 1)[1])
    btc = data["positions"]["BTC/EUR"]
    assert btc["exit_below"] == 57000.0 and btc["exit_crossed"] is False and btc["thesis"] == "tendance haussière sur 30 j"
    assert "exit_below" not in data["positions"]["ETH/EUR"]


def test_le_niveau_de_sortie_est_borne_par_le_code():
    from tradeagent.llm_agent import PLAN_KEY

    cases = [
        (59_900, 59_400.0),      # dans le bruit d'une bougie (amplitude 1 %) : repoussé à une amplitude sous le prix
        (61_000, 59_400.0),      # au-dessus du prix : pareil
        (10_000, 45_000.0),      # absurde : pas plus de 25 % sous le prix
        (None, 58_200.0),        # achat sans plan : le code en pose un, 3 amplitudes (au moins 3 %) sous le prix
        (57_123.456, 57_123.0),  # cinq chiffres significatifs
    ]
    for asked, kept in cases:
        agent, _, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10, exit_below=asked)])
        agent.decide(with_market(priced(clock(), btc=60_000.0), atr=1.0))
        assert storage.get(PLAN_KEY)["BTC/EUR"]["exit_below"] == kept, asked

    wide, _, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10)])
    wide.decide(with_market(priced(clock(), btc=60_000.0), atr=2.0))            # marché agité : 3 x 2 % = 6 % sous le prix
    assert storage.get(PLAN_KEY)["BTC/EUR"]["exit_below"] == 56_400.0


def test_le_franchissement_du_niveau_reveille_l_agent():
    agent, client, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10, exit_below=57_000, minutes=1_440, move=20),
                                             reply(HOLD)], position_wake_move_pct=20.0)
    agent.decide(with_market(priced(clock(), btc=60_000.0)))
    clock.advance(3_600)
    held = lambda price: with_market(priced(clock(), btc=price, positions={   # noqa: E731
        **HELD, "BTC/EUR": {**HELD["BTC/EUR"], "price": price}}))
    assert agent.decide(held(57_001.0)).skipped                                 # -5 %, mais au-dessus de son niveau : il dort
    woken = agent.decide(held(57_000.0))                                        # niveau touché : appelé
    assert not woken.skipped and len(client.calls) == 2
    data = json.loads(client.calls[1][1].split("\n", 1)[1])
    assert data["positions"]["BTC/EUR"]["exit_crossed"] is True


def test_un_hold_peut_deplacer_le_niveau_et_une_position_fermee_oublie_son_plan():
    from tradeagent.llm_agent import PLAN_KEY

    agent, client, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10, exit_below=57_000, why="achat"),
                                             decision_reply("hold", "BTC/EUR", exit_below=61_000, why="je remonte mon niveau"),
                                             decision_reply("hold", "ETH/EUR", exit_below=2_000),
                                             reply(HOLD)])
    agent.decide(with_market(priced(clock(), btc=60_000.0)))
    held = lambda price: with_market(priced(clock(), btc=price, positions={   # noqa: E731
        **HELD, "BTC/EUR": {**HELD["BTC/EUR"], "price": price}}))
    clock.advance(3_600)
    agent.decide(held(64_000.0))
    plan = storage.get(PLAN_KEY)["BTC/EUR"]
    assert plan["exit_below"] == 61_000.0 and plan["thesis"] == "achat"         # niveau remonté, raison d'achat gardée
    clock.advance(3_600)
    agent.decide(held(64_000.0))                                                # un niveau sur un symbole non détenu : ignoré
    assert set(storage.get(PLAN_KEY)) == {"BTC/EUR"}
    clock.advance(3_600)
    agent.decide(with_market(priced(clock(), btc=64_000.0)))                    # position vendue entre-temps
    assert storage.get(PLAN_KEY) == {}


def test_reset_efface_les_plans_de_sortie():
    from tradeagent.app import LIFE_KEYS
    from tradeagent.llm_agent import PLAN_KEY

    assert PLAN_KEY in LIFE_KEYS


def test_le_prompt_n_envoie_plus_ce_que_les_indicateurs_disent_deja():
    from dataclasses import replace

    full = {"last": 1.0, "volatility_pct_per_candle": 0.5, "atr_pct": 1.2, "daily_closes": [1.0, 2.0, 3.0],
            "closes": [float(i) for i in range(24)]}
    short = {"last": 1.0, "volatility_pct_per_candle": 0.5, "closes": [float(i) for i in range(24)]}
    agent, client, clock, *_ = make([reply(HOLD)])
    agent.decide(replace(priced(clock()), market={"BTC/EUR": full, "ETH/EUR": short}))
    data = json.loads(client.calls[0][1].split("\n", 1)[1])
    assert data["market"]["BTC/EUR"]["closes"] == [18.0, 19.0, 20.0, 21.0, 22.0, 23.0]
    assert "volatility_pct_per_candle" not in data["market"]["BTC/EUR"] and data["market"]["BTC/EUR"]["atr_pct"] == 1.2
    assert data["market"]["ETH/EUR"] == short                                   # historique court : rien à retirer


def test_le_plan_par_defaut_reste_a_3_pour_cent_au_moins_et_une_vente_ne_pose_pas_de_plan():
    from tradeagent.llm_agent import PLAN_KEY

    calm, _, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10)])
    calm.decide(with_market(priced(clock(), btc=60_000.0), atr=0.5))            # marché calme : 3 x 0,5 % serait trop serré
    assert storage.get(PLAN_KEY)["BTC/EUR"]["exit_below"] == 58_200.0

    agent, _, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10, exit_below=57_000),
                                        decision_reply("sell", "BTC/EUR", 5, exit_below=50_000)])
    agent.decide(with_market(priced(clock(), btc=60_000.0)))
    clock.advance(3_600)
    agent.decide(with_market(priced(clock(), btc=60_000.0, positions=HELD)))
    assert storage.get(PLAN_KEY)["BTC/EUR"]["exit_below"] == 57_000.0           # une vente partielle ne déplace rien


def test_une_vente_sans_montant_vend_toute_la_position():
    agent, *_ = make([decision_reply("sell", "BTC/EUR", None)])
    sold = agent.decide(priced(START, positions=HELD))
    assert (sold.action, sold.symbol, sold.amount_quote) == ("sell", "BTC/EUR", 60.0)

    for bad in (decision_reply("sell", "ETH/EUR", None),          # rien à vendre sur ce symbole
                decision_reply("sell", None, None),
                decision_reply("buy", "BTC/EUR", None)):          # un achat sans montant reste une erreur
        agent, *_ = make([bad])
        with pytest.raises(InvalidDecision):
            agent.decide(priced(START, positions=HELD))


# -- les modèles alimentent l'agent ---------------------------------------------------------------------------------

def with_models(view_, btc="up", eth="range", exit_pct=None):
    from dataclasses import replace

    def models(regime):
        out = {"trend": {"regime": regime, "score": 1.0}}
        if exit_pct:
            out["risk"] = {"exit_pct": exit_pct, "size_pct": 20.0}
        return out

    market = {"BTC/EUR": {**view_.market["BTC/EUR"], "models": models(btc)},
              "ETH/EUR": {**view_.market["ETH/EUR"], "models": models(eth)}}
    return replace(view_, market=market)


def test_un_changement_de_regime_reveille_l_agent():
    from tradeagent.llm_agent import WAKE_KEY

    agent, client, clock, storage, _ = make([sleepy(minutes=1_440), reply(HOLD)])
    agent.decide(with_models(priced(clock()), btc="up"))
    assert storage.get(WAKE_KEY)["regimes"] == {"BTC/EUR": "up", "ETH/EUR": "range"}
    clock.advance(3_600)
    assert agent.decide(with_models(priced(clock()), btc="up")).skipped         # rien n'a changé : il dort
    assert agent.decide(with_models(priced(clock()), btc="range")).skipped      # retour à « range » : rien à décider
    assert agent.decide(with_models(priced(clock()), btc="down")).skipped       # baisse d'un symbole non détenu : non plus
    assert not agent.decide(with_models(priced(clock()), eth="up")).skipped     # un symbole passe à la hausse : occasion
    assert len(client.calls) == 2


def test_un_symbole_detenu_qui_passe_a_la_baisse_reveille_l_agent():
    agent, client, clock, *_ = make([sleepy(minutes=1_440, move=3), reply(HOLD)])
    agent.decide(with_models(priced(clock(), positions=HELD), btc="up"))
    clock.advance(3_600)
    assert agent.decide(with_models(priced(clock(), positions=HELD), btc="range")).skipped
    assert not agent.decide(with_models(priced(clock(), positions=HELD), btc="down")).skipped
    assert len(client.calls) == 2


def test_sans_plan_donne_la_sortie_par_defaut_est_celle_du_modele_de_risque():
    from tradeagent.llm_agent import PLAN_KEY

    agent, _, clock, storage, _ = make([decision_reply("buy", "BTC/EUR", 10)])
    agent.decide(with_models(with_market(priced(clock(), btc=60_000.0)), exit_pct=5.0))
    assert storage.get(PLAN_KEY)["BTC/EUR"]["exit_below"] == 57_000.0           # 5 % sous le prix, pas 3 amplitudes


def test_le_prompt_explique_les_modeles_et_les_transmet():
    agent, client, clock, *_ = make([reply(HOLD)])
    agent.decide(with_models(priced(clock()), exit_pct=5.0))
    system, user, _ = client.calls[0]
    data = json.loads(user.split("\n", 1)[1])
    assert data["market"]["BTC/EUR"]["models"] == {"trend": {"regime": "up", "score": 1.0},
                                                   "risk": {"exit_pct": 5.0, "size_pct": 20.0}}
    assert '"models"' in system and "size_pct" in system and "turns \"up\"" in system


def test_avec_les_modeles_le_prompt_n_envoie_plus_ce_qu_ils_resument():
    from dataclasses import replace

    full = {"last": 1.0, "change_pct": {"1h": 0.1}, "high_24h": 2.0, "low_24h": 0.5, "atr_pct": 1.2,
            "range": {"7d": {"high": 3.0, "low": 0.4, "pos_pct": 23, "from_high_pct": -66.67}},
            "daily_closes": [1.0, 2.0, 3.0], "closes": [float(i) for i in range(24)],
            "models": {"trend": {"regime": "up", "score": 1.0}}}
    agent, client, clock, *_ = make([reply(HOLD)])
    agent.decide(replace(priced(clock()), market={"BTC/EUR": full, "ETH/EUR": {"last": 1.0}}))
    system, user, _ = client.calls[0]
    btc = json.loads(user.split("\n", 1)[1])["market"]["BTC/EUR"]
    assert btc == {"last": 1.0, "change_pct": {"1h": 0.1}, "range": {"7d": {"pos_pct": 23, "from_high_pct": -66.67}},
                   "models": {"trend": {"regime": "up", "score": 1.0}}}
    assert len(system) < 4_300                                                  # le prompt système reste court : il se paie à chaque appel
    for kept in ("counts as failure", "round_trip_pct", "net_equity", "exit_below", "size_pct", "untrusted"):
        assert kept in system, kept
