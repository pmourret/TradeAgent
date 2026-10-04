"""Backtest : le vrai moteur sur des bougies rejouées. Aucun accès réseau, aucune base sur disque."""
from __future__ import annotations

import pytest

from helpers import default_cfg, permissive_cfg
from tradeagent import cli
from tradeagent.backtest import (BACKTEST_AGENTS, DEFAULT_AGENTS, compare, format_table, market_return_pct,
                                 max_drawdown_pct, run_backtest, warmup_seconds)
from tradeagent.config import ConfigError
from tradeagent.models import Candle
from tradeagent.replay import synthetic_history

T0 = 1_760_000_400.0
HOUR = 3_600.0
DAY = 86_400.0


def flat_then(prices: dict[str, list[float]], cfg, lead: float = 60_000.0) -> dict[str, list[Candle]]:
    """Historique : prix constant pendant l'échauffement, puis une clôture par heure à partir de T0."""
    warm = int(warmup_seconds(cfg) // HOUR)
    history = {}
    for symbol, closes in prices.items():
        base = closes[0]
        series = [base] * warm + closes
        history[symbol] = [Candle(T0 - (warm - i) * HOUR, p, p, p, p) for i, p in enumerate(series)]
    return history


def test_le_drawdown_maximal():
    assert max_drawdown_pct([]) == 0.0
    assert max_drawdown_pct([50, 55, 44, 60, 57]) == pytest.approx(20.0)        # de 55 à 44
    assert max_drawdown_pct([50, 51, 52]) == 0.0
    assert max_drawdown_pct([50, 40, 45, 30]) == pytest.approx(40.0)            # depuis le plus haut, pas le dernier pic


def test_l_echauffement_couvre_les_bougies_que_l_agent_demande():
    assert warmup_seconds(default_cfg()) == 49 * HOUR


def test_hold_ne_fait_rien_et_finit_a_la_mise():
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 3 * DAY, seed=1)
    result = run_backtest(cfg, "hold", history, T0, T0 + 3 * DAY)
    assert (result.status, result.orders, result.fees, result.api_cost) == ("alive", 0, 0.0, 0.0)
    assert result.final_equity == cfg.stake and result.net_result == 0.0 and result.max_drawdown_pct == 0.0
    assert result.cycles == 3 * 96                                              # un cycle toutes les 15 min


def test_buyhold_passe_par_les_garde_fous_et_suit_le_marche():
    cfg = default_cfg(stake=50.0)
    up = [60_000.0] * 24 + [66_000.0] * 48                                      # +10 % après un jour
    history = flat_then({"BTC/EUR": up, "ETH/EUR": [2_500.0] * 72}, cfg)
    result = run_backtest(cfg, "buyhold", history, T0, T0 + 72 * HOUR)
    assert result.orders == 4                                                   # 2 x 20 % par symbole (max_order_pct)
    # 40 % de la mise sur BTC (plafond par symbole) qui prend 10 % = +2 EUR, moins frais et glissement des 4 achats.
    costs = 40 * (cfg.costs.fee_rate + cfg.costs.slippage_bps / 10_000)
    assert result.final_equity == pytest.approx(50 + 2.0 - costs, abs=0.02)
    assert result.final_equity < 52.0
    assert result.fees == pytest.approx(40 * cfg.costs.fee_rate, rel=0.02)
    assert result.net_result == pytest.approx(result.final_equity - 50)
    assert result.return_pct == pytest.approx(result.net_result / 50 * 100)


def test_un_agent_ne_voit_jamais_le_prix_de_la_bougie_en_cours():
    cfg = default_cfg(stake=50.0)
    # Le prix double à la clôture de la bougie qui s'ouvre à T0 + 1 h : visible seulement à T0 + 2 h.
    closes = [60_000.0, 60_000.0, 120_000.0, 120_000.0]
    history = flat_then({"BTC/EUR": closes, "ETH/EUR": [2_500.0] * 4}, cfg)
    before = run_backtest(cfg, "buyhold", history, T0, T0 + 2 * HOUR)           # dernier cycle à T0 + 1 h 45
    assert before.final_equity < 50.0                                           # rien gagné : seulement des frais
    after = run_backtest(cfg, "buyhold", history, T0, T0 + 3 * HOUR)
    assert after.final_equity > 60.0


def test_le_kill_switch_tue_en_backtest_comme_en_vrai():
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=90)
    crash = [60_000.0] * 6 + [20_000.0] * 20
    history = flat_then({"BTC/EUR": crash, "ETH/EUR": [2_500.0] * 6 + [800.0] * 20}, cfg)
    result = run_backtest(cfg, "buyhold", history, T0, T0 + 26 * HOUR)
    assert result.status == "dead"
    assert result.cycles < 26 * 4                                               # arrêté dès la mort
    assert result.final_equity < 50.0 and result.max_drawdown_pct > 50.0           # mise de 100 dans cette config
    assert result.orders == 2                                                   # un achat, puis la liquidation par le moteur
    assert "perte totale" in result.reason
    assert result.net_result == pytest.approx(result.final_equity - 100.0)


def test_un_historique_qui_s_arrete_trop_tot_arrete_le_bot_sans_faux_resultat():
    cfg = default_cfg(stake=50.0)
    history = flat_then({"BTC/EUR": [60_000.0] * 10, "ETH/EUR": [2_500.0] * 10}, cfg)
    result = run_backtest(cfg, "buyhold", history, T0, T0 + 5 * DAY)            # bien au-delà des données
    assert result.status == "halted"                                            # erreurs de flux comptées par le kill switch
    assert "trou dans l'historique" in result.reason
    table = format_table([result], cfg, 0.0)
    assert "! buyhold : halted après" in table and "non comparable" in table    # l'arrêt et sa cause sont affichés
    assert result.final_equity == pytest.approx(50.0, abs=0.2)


def test_llm_fake_compte_ses_couts_d_api_dans_le_resultat_net():
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 2 * DAY, seed=2)
    result = run_backtest(cfg, "llm-fake", history, T0, T0 + 2 * DAY, seed=2)
    assert result.api_cost > 0
    assert result.net_result == pytest.approx(result.final_equity - cfg.stake - result.api_cost)
    assert result.api_cost <= 2 * cfg.llm.daily_budget_eur + 0.01               # le budget du jour s'applique en temps simulé


def test_le_vrai_llm_est_refuse_et_un_agent_inconnu_aussi():
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + DAY, seed=1)
    with pytest.raises(ConfigError, match="argent réel"):
        run_backtest(cfg, "llm", history, T0, T0 + DAY)
    with pytest.raises(ConfigError, match="inconnu"):
        run_backtest(cfg, "martingale", history, T0, T0 + DAY)
    with pytest.raises(ConfigError, match="vide"):
        run_backtest(cfg, "hold", history, T0, T0)
    assert "llm" not in DEFAULT_AGENTS and set(DEFAULT_AGENTS) <= set(BACKTEST_AGENTS)      # jamais payant par défaut


def test_le_meme_backtest_donne_deux_fois_le_meme_resultat():
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 2 * DAY, seed=5)
    first = compare(cfg, ["chaos", "momentum", "dca"], history, T0, T0 + 2 * DAY, seed=7)
    second = compare(cfg, ["chaos", "momentum", "dca"], history, T0, T0 + 2 * DAY, seed=7)
    assert first == second
    assert [r.agent for r in first] == ["chaos", "momentum", "dca"]
    assert first[0].orders > 0


def test_le_repere_du_marche_est_un_achat_a_parts_egales_sans_garde_fou():
    cfg = default_cfg(stake=50.0)
    history = flat_then({"BTC/EUR": [60_000.0] * 2 + [66_000.0] * 10, "ETH/EUR": [2_500.0] * 12}, cfg)
    cost = (1 + cfg.costs.slippage_bps / 10_000) * (1 + cfg.costs.fee_rate)
    expected = ((1.10 / cost + 1.0 / cost) / 2 - 1) * 100                       # BTC +10 %, ETH à plat, frais d'achat
    assert market_return_pct(cfg, history, T0, T0 + 12 * HOUR) == pytest.approx(expected)
    with pytest.raises(ConfigError, match="historique insuffisant"):
        market_return_pct(cfg, {"BTC/EUR": [], "ETH/EUR": []}, T0, T0 + HOUR)


def test_le_tableau_montre_chaque_agent_et_le_repere():
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + DAY, seed=1)
    table = format_table(compare(cfg, ["hold", "dca"], history, T0, T0 + DAY), cfg, 1.234)
    lines = table.splitlines()
    assert lines[1].startswith("hold") and lines[2].startswith("dca")
    assert len(lines) == 4 and "!" not in table                                 # aucun arrêt à signaler
    assert "+1.23 %" in lines[-1] and "hors garde-fous" in lines[-1]


# -- ligne de commande ---------------------------------------------------------
def test_la_commande_backtest_tourne_hors_ligne_et_n_ecrit_aucune_base(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\n", encoding="utf-8")
    monkeypatch.setattr(cli, "public_client", lambda exchange: pytest.fail("accès réseau en mode synthétique"))
    code = cli.main(["backtest", "--synthetic", "--days", "2", "--agents", "hold,dca", "--end", "2025-10-01"])
    out = capsys.readouterr().out
    assert code == 0
    assert "backtest du 2025-09-29 00:00 au 2025-10-01 00:00 UTC (2 j)" in out
    assert "hold" in out and "dca" in out and "Ce n'est pas un conseil de placement" in out
    assert not (tmp_path / "data").exists()                                     # ni base ni cache


def test_la_commande_backtest_refuse_les_mauvais_arguments(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    for argv in (["--agents", "hold,martingale"], ["--agents", ""], ["--days", "0"], ["--end", "hier"],
                 ["--end", "2999-01-01"], ["--agents", "llm"]):
        assert cli.main(["backtest", "--synthetic", *argv]) == 2, argv
    assert "erreur de configuration" in capsys.readouterr().err


def test_la_commande_backtest_telecharge_par_le_client_public_et_met_en_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\n", encoding="utf-8")
    asked = []

    class Client:
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            asked.append(symbol)
            first = since / 1000
            base = 60_000.0 if symbol.startswith("BTC") else 2_500.0
            return [[(first + i * HOUR) * 1000, base, base, base, base, 1.0] for i in range(limit)]

    monkeypatch.setattr(cli, "public_client", lambda exchange: Client())
    assert cli.main(["backtest", "--days", "1", "--agents", "hold", "--end", "2025-10-01"]) == 0
    assert set(asked) == {"BTC/EUR", "ETH/EUR"}
    assert sorted(p.name for p in (tmp_path / "data" / "history").iterdir()) == [
        "bitvavo-BTC-EUR-1h.json", "bitvavo-ETH-EUR-1h.json"]
    assert not (tmp_path / "data" / "agent.db").exists()
    calls = len(asked)
    assert cli.main(["backtest", "--days", "1", "--agents", "hold", "--end", "2025-10-01"]) == 0
    assert len(asked) == calls                                                  # second lancement : cache seulement

    class Down:
        def fetch_ohlcv(self, *args, **kwargs):
            raise RuntimeError("réseau coupé")

    monkeypatch.setattr(cli, "public_client", lambda exchange: Down())
    assert cli.main(["backtest", "--days", "1", "--agents", "hold", "--end", "2025-10-01", "--refresh"]) == 2
    assert "erreur de données" in capsys.readouterr().err


def test_la_commande_backtest_rend_le_niveau_de_journal(tmp_path, monkeypatch):
    import logging

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("stake: 50\n", encoding="utf-8")
    logger = logging.getLogger("tradeagent")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    try:
        assert cli.main(["backtest", "--synthetic", "--days", "1", "--agents", "chaos", "--end", "2025-10-01"]) == 0
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(previous)
    return
    assert cli.main(["backtest", "--synthetic", "--days", "1", "--agents", "chaos", "--end", "2025-10-01"]) == 0
    assert logger.level == logging.WARNING


def test_la_commande_rejoue_plusieurs_periodes_et_affiche_le_cumul(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\nstake: 50\n", encoding="utf-8")
    code = cli.main(["backtest", "--synthetic", "--days", "2", "--months", "3", "--agents", "hold,dca",
                     "--end", "2025-10-07"])
    out = capsys.readouterr().out
    assert code == 0
    assert "backtest du 2025-10-01 00:00 au 2025-10-07 00:00 UTC (3 périodes de 2 j)" in out
    for window in ("-- du 2025-10-01 00:00 au 2025-10-03 00:00", "-- du 2025-10-03 00:00 au 2025-10-05 00:00",
                   "-- du 2025-10-05 00:00 au 2025-10-07 00:00"):
        assert window in out
    assert out.index("2025-10-01 00:00 au 2025-10-03") < out.index("2025-10-05 00:00 au 2025-10-07")     # de la plus ancienne à la plus récente
    assert "== cumul des 3 périodes" in out and "Chaque période repart de la mise" in out
    summary = out.split("== cumul des 3 périodes")[1]
    assert "hold" in summary and "0/3" in summary                               # hold : jamais de mois positif
    for bad in ("0", "25"):
        assert cli.main(["backtest", "--synthetic", "--months", bad]) == 2


def test_le_cumul_additionne_chaque_periode():
    from tradeagent.backtest import BacktestResult, format_summary

    def result(agent, net, orders=1, status="alive"):
        return BacktestResult(agent=agent, status=status, cycles=1, final_equity=50 + net, api_cost=0.1, net_result=net,
                              return_pct=net / 50 * 100, max_drawdown_pct=1.0, orders=orders, fees=0.05)

    table = format_summary([[result("llm", 2.0), result("quant", -1.0)],
                            [result("llm", -0.5, orders=3), result("quant", 1.0, status="dead")]], default_cfg(stake=50.0))
    llm, quant = table.splitlines()[1:3]
    assert llm.split()[:6] == ["llm", "+1.50", "+3.00%", "1/2", "-1.00%", "4"]  # net, %, mois positifs, pire mois, ordres
    assert "0.100" in llm and "0.200" in llm                                    # frais et API additionnés
    assert quant.split()[:4] == ["quant", "+0.00", "+0.00%", "1/2"] and "arrêté 1 fois" in quant
