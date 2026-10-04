"""Le loyer sort de la mise : le kill switch, le plus-haut et les paliers jugent l'equity nette du coût d'API."""
from __future__ import annotations

import pytest

from helpers import START, FakeClock, ScriptedAgent, default_cfg, make_engine, permissive_cfg
from tradeagent.app import reset_life
from tradeagent.backtest import run_backtest, warmup_seconds
from tradeagent.dashboard import build_snapshot
from tradeagent.models import Decision
from tradeagent.replay import synthetic_history
from tradeagent.storage import Storage


def pay(storage: Storage, clock: FakeClock, cost: float) -> None:
    """Un appel au LLM facturé `cost` à l'instant courant."""
    storage.record_llm_call(clock(), "m", 0, 0, 0, 0, cost)


class Watcher(ScriptedAgent):
    """Agent qui ne fait rien, mais garde la dernière vue reçue."""

    def decide(self, view):
        self.view = view
        return super().decide(view)


def test_un_bot_qui_ne_trade_pas_meurt_de_son_loyer():
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=90)            # mise 100, mort à 50 % de perte
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent())
    assert engine.run_cycle().status == "hold"
    pay(storage, clock, 49.99)
    clock.advance(900)
    assert engine.run_cycle().status == "hold"                                  # 50.01 net : encore vivant
    pay(storage, clock, 0.01)
    clock.advance(900)
    result = engine.run_cycle()
    assert result.status == "dead"
    assert "perte totale : equity nette du loyer 50.00 <= 50.00" in result.note
    assert storage.get("killswitch")["status"] == "dead"
    assert storage.get("paper_balances")["EUR"] == 100.0                        # pas un centime perdu en trading
    clock.advance(900)
    assert engine.run_cycle().status == "stopped"                               # mort définitive


def test_sans_loyer_rien_ne_change():
    engine, _, clock, storage = make_engine(default_cfg(), ScriptedAgent())
    for _ in range(3):
        assert engine.run_cycle().status == "hold"
        clock.advance(900)
    assert storage.get("peak_equity") == 100.0 and storage.get("risk_tier", "normal") == "normal"


def test_le_loyer_d_une_vie_passee_ne_compte_pas():
    clock = FakeClock()
    storage = Storage(":memory:")
    storage.record_llm_call(START - 10, "m", 0, 0, 0, 0, 80.0)                  # payé avant le début de cette vie
    engine, _, clock, storage = make_engine(default_cfg(), ScriptedAgent(), clock=clock, storage=storage)
    assert engine.run_cycle().status == "hold"
    assert storage.get("peak_equity") == 100.0

    pay(storage, clock, 60.0)
    clock.advance(900)
    assert engine.run_cycle().status == "dead"
    clock.advance(900)
    reset_life(storage, clock())                                                # nouvelle vie : le compteur repart
    fresh, _, clock, storage = make_engine(default_cfg(), ScriptedAgent(), clock=clock, storage=storage)
    assert fresh.run_cycle().status == "hold"
    assert storage.llm_spend_since(0.0) == 140.0                                # le suivi des coûts, lui, est gardé


def test_le_loyer_fait_descendre_de_palier_et_reduit_les_limites():
    agent = Watcher()
    engine, _, clock, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    assert agent.view.risk_tier == "normal" and agent.view.limits["max_order_quote"] == 20.0
    pay(storage, clock, 15.0)                                                   # 15 % sous le plus-haut net de 100
    clock.advance(900)
    engine.run_cycle()
    assert agent.view.risk_tier == "cautious"
    assert agent.view.limits["max_order_quote"] == 10.0                         # 20 % de l'equity, facteur 0,5
    assert agent.view.equity == 100.0                                           # l'agent voit toujours son equity réelle
    assert any("normal -> cautious (drawdown 15.0 % depuis 100.00)" in e["message"] for e in storage.recent_events(5))
    pay(storage, clock, 10.0)                                                   # 25 % : défensif, achats bloqués
    clock.advance(900)
    engine.run_cycle()
    assert agent.view.risk_tier == "defensive" and agent.view.limits["max_order_quote"] == 0.0


def test_la_perte_du_jour_reste_celle_du_trading():
    agent = Watcher()
    engine, _, clock, storage = make_engine(default_cfg(), agent)
    engine.run_cycle()
    pay(storage, clock, 5.0)
    clock.advance(86_400)                                                       # nouveau jour, loyer déjà payé
    engine.run_cycle()
    assert storage.get("day")["start_equity"] == 100.0                          # equity réelle, pas 95
    assert agent.view.limits["buys_blocked_below_equity"] == 95.0
    assert storage.last_equity()["equity"] == 100.0                             # l'equity enregistrée reste brute


def test_le_plus_haut_est_net_du_loyer():
    cfg = permissive_cfg(max_total_loss_pct=90, max_drawdown_pct=40)
    engine, feed, clock, storage = make_engine(cfg, ScriptedAgent([Decision("buy", "BTC/EUR", 50.0, "test")]))
    engine.run_cycle()
    feed.set("BTC/EUR", 120_000.0)                                              # la position double
    pay(storage, clock, 10.0)
    clock.advance(900)
    engine.run_cycle()
    gross = storage.last_equity()["equity"]
    assert gross > 140.0
    assert storage.get("peak_equity") == pytest.approx(gross - 10.0)            # plus-haut net, pas brut
    peak = storage.get("peak_equity")
    pay(storage, clock, peak * 0.4 - 0.01)                                      # juste au-dessus du seuil de drawdown
    clock.advance(900)
    assert engine.run_cycle().status == "hold"
    pay(storage, clock, 0.01)
    clock.advance(900)
    result = engine.run_cycle()
    assert result.status == "dead" and "drawdown max : equity nette du loyer" in result.note
    assert storage.get("paper_balances")["BTC"] == 0.0                          # la mort liquide, comme toujours


def test_le_tableau_de_bord_parle_la_meme_langue_que_le_kill_switch(tmp_path):
    path = tmp_path / "agent.db"
    cfg = default_cfg(database=str(path))
    clock = FakeClock()
    storage = Storage(str(path))
    storage.record_llm_call(START - 10, "m", 0, 0, 0, 0, 30.0)                  # loyer d'une vie passée : hors sujet
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent(), clock=clock, storage=storage)
    engine.run_cycle()
    pay(storage, clock, 4.0)
    clock.advance(900)
    engine.run_cycle()
    pay(storage, clock, 6.0)
    clock.advance(900)
    engine.run_cycle()

    money = build_snapshot(cfg, now=clock())["money"]
    assert money["equity"] == 100.0 and money["net_equity"] == pytest.approx(90.0)
    assert money["net_result"] == pytest.approx(-10.0)
    assert money["peak_equity"] == pytest.approx(100.0) and money["drawdown_pct"] == pytest.approx(10.0)
    assert money["tier_lines"]["cautious"] == pytest.approx(85.0)
    series = build_snapshot(cfg, now=clock())["equity_series"]
    assert [round(v, 6) for _, v in series] == [100.0, 96.0, 90.0]              # la courbe est nette du loyer, cumulé
    assert build_snapshot(cfg, now=clock())["risk_tier"] == "normal"


def test_le_tableau_de_bord_affiche_un_plus_haut_net_meme_quand_l_equity_brute_le_depasse(tmp_path):
    path = tmp_path / "agent.db"
    cfg = default_cfg(database=str(path))
    clock = FakeClock()
    storage = Storage(str(path))
    engine, feed, clock, storage = make_engine(cfg, ScriptedAgent([Decision("buy", "BTC/EUR", 20.0, "test")]),
                                               clock=clock, storage=storage)
    engine.run_cycle()
    feed.set("BTC/EUR", 90_000.0)                                               # la position prend 50 %
    pay(storage, clock, 5.0)
    clock.advance(900)
    engine.run_cycle()
    money = build_snapshot(cfg, now=clock())["money"]
    assert money["equity"] > 105.0 and money["net_equity"] == pytest.approx(money["equity"] - 5.0)
    assert money["peak_equity"] == pytest.approx(storage.get("peak_equity")) == pytest.approx(money["net_equity"])
    assert money["drawdown_pct"] == pytest.approx(0.0)


def test_la_serie_nette_cumule_le_loyer_paye_avant_chaque_point():
    storage = Storage(":memory:")
    for ts in (0.0, 10.0, 20.0, 30.0):
        storage.record_equity(ts, 100.0, 100.0)
    storage.record_llm_call(5.0, "m", 0, 0, 0, 0, 4.0)
    storage.record_llm_call(15.0, "m", 0, 0, 0, 0, 6.0)
    storage.record_llm_call(30.0, "m", 0, 0, 0, 0, 50.0)                        # payé au cycle même : compte au suivant
    assert storage.net_equity_series() == [100.0, 96.0, 90.0, 90.0]
    assert storage.net_equity_series(since=10.0) == [100.0, 94.0, 94.0]         # seulement depuis le début de la vie
    assert storage.equity_series() == [100.0] * 4


def test_en_backtest_un_agent_trop_cher_cesse_de_payer_quand_il_ne_peut_plus_agir():
    from tradeagent.llm import LLMReply, LLMUsage

    class AlwaysHold:
        """Un LLM qui ne trade jamais et coûte 1,80 EUR par appel (1 000 tokens à 2 000 USD le million)."""

        calls = 0

        def complete(self, system, user, max_output_tokens):
            self.calls += 1
            return LLMReply('{"action": "hold", "reasoning": "rien"}', LLMUsage(input_tokens=1_000), "m")

    cfg = default_cfg(stake=50.0, llm={"price_input_per_mtok_usd": 2_000.0, "price_output_per_mtok_usd": 2_000.0,
                                       "daily_budget_eur": 1_000.0, "total_budget_eur": 1_000.0})
    start = 1_760_000_400.0
    history = synthetic_history(cfg.symbols, "1h", start - warmup_seconds(cfg), start + 3 * 86_400, seed=1, volatility=0.0001)
    client = AlwaysHold()
    result = run_backtest(cfg, "llm", history, start, start + 3 * 86_400, llm_client=client)
    # Le loyer l'a mené au palier défensif (25 % sous le plus-haut net) : en cash, achats bloqués, il ne peut plus
    # rien faire. Il n'est donc plus appelé : il ne paie plus, et n'atteint pas le seuil de mort (40 %).
    assert result.status == "alive" and result.orders == 0
    assert result.final_equity == 50.0                                          # rien perdu en trading
    assert client.calls == 7 and result.api_cost == pytest.approx(12.6)         # 7 x 1,80 : juste au-delà de 25 %
    assert result.net_result == pytest.approx(result.final_equity - 50.0 - result.api_cost)
    assert result.max_drawdown_pct == pytest.approx(result.api_cost / 50.0 * 100)   # le drawdown affiché est net


def test_status_annonce_le_vrai_seuil_de_mort(tmp_path, monkeypatch, capsys):
    from tradeagent import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\n", encoding="utf-8")
    cfg = default_cfg(database="data/agent.db")
    clock = FakeClock()
    storage = Storage("data/agent.db")
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent(), clock=clock, storage=storage)
    engine.run_cycle()
    assert cli.main(["status"]) == 0
    assert "equity nette du loyer" not in capsys.readouterr().out               # pas de loyer : rien à dire
    pay(storage, clock, 12.5)
    storage.close()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    # Mise 100, plus-haut net 100 : mort sous 60 (drawdown de 40 %), pas sous 50 (perte totale).
    assert "equity nette du loyer : 87.50 EUR (loyer 12.5000 ; mort sous 60.00)" in out
