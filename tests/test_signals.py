"""Modèles mathématiques : tendance, volatilité, risque. Fonctions pures, sans réseau."""
from __future__ import annotations

import math

import pytest

from tradeagent.agents import MarketView
from tradeagent.market import summarize_candles
from tradeagent.models import BUY, HOLD, SELL, Candle
from tradeagent.signals import compute_models, risk_sizing, trend_regime, volatility_forecast
from tradeagent.strategies import QuantAgent

HOUR = 3_600


def hourly(closes, start=1_760_000_400.0):
    return [Candle(start + i * HOUR, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]


def ramp(first, last, n):
    return [first + (last - first) * i / (n - 1) for i in range(n)]


# -- régime de tendance ----------------------------------------------------------
def test_une_hausse_reguliere_est_un_regime_haussier_et_inversement():
    assert trend_regime(hourly(ramp(100, 200, 744)), "1h") == {"regime": "up", "score": 1.0}
    assert trend_regime(hourly(ramp(200, 100, 744)), "1h") == {"regime": "down", "score": -1.0}
    assert trend_regime(hourly([100.0] * 744), "1h") == {"regime": "range", "score": 0.0}


def test_un_rebond_dans_une_baisse_ne_fait_pas_un_regime_haussier():
    closes = ramp(200, 100, 743) + [103.0]                                      # +3 % sur la dernière heure
    assert trend_regime(hourly(closes), "1h")["regime"] == "down"


def test_un_retournement_se_lit_dans_le_score():
    # Longue hausse puis chute nette sur 5 jours : le prix passe sous ses moyennes, la moyenne de 7 j reste au-dessus de celle de 30 j.
    closes = ramp(100, 200, 624) + ramp(200, 175, 120)
    signal = trend_regime(hourly(closes), "1h")
    assert signal == {"regime": "range", "score": 0.0}                         # deux votes baissiers, deux haussiers
    deeper = trend_regime(hourly(ramp(100, 200, 624) + ramp(200, 150, 120)), "1h")
    assert deeper == {"regime": "down", "score": -0.5}                         # le prix passe aussi sous sa moyenne de 30 j


def test_avec_moins_de_30_jours_seuls_deux_votes_comptent_et_sans_8_jours_rien():
    week = trend_regime(hourly(ramp(100, 120, 200)), "1h")                      # 8 jours et quelques : deux votes
    assert week == {"regime": "up", "score": 1.0}
    assert trend_regime(hourly(ramp(100, 120, 191)), "1h") is None              # il faut 7 jours + 24 h
    assert trend_regime([], "1h") is None


def test_un_ecart_minuscule_est_un_vote_neutre():
    closes = [100.0] * 743 + [100.05]                                           # 0,05 % : sous le seuil de 0,1 %
    assert trend_regime(hourly(closes), "1h") == {"regime": "range", "score": 0.0}


def test_un_seul_vote_sur_quatre_ne_suffit_pas_a_declarer_une_tendance():
    # 23 jours à 100,1, 7 jours à 100, puis une dernière bougie à 100,15 : seul le prix contre sa moyenne de 7 jours vote.
    up = [100.1] * (23 * 24) + [100.0] * (7 * 24 + 23) + [100.15]
    assert trend_regime(hourly(up), "1h") == {"regime": "range", "score": 0.25}
    down = [99.9] * (23 * 24) + [100.0] * (7 * 24 + 23) + [99.85]
    assert trend_regime(hourly(down), "1h") == {"regime": "range", "score": -0.25}


# -- prévision de volatilité -------------------------------------------------------
def test_la_volatilite_prevue_vaut_l_ecart_type_horaire_fois_racine_de_24():
    closes = [100.0]
    for i in range(400):
        closes.append(closes[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))     # +/- 1 % (log) chaque heure
    forecast = volatility_forecast(hourly(closes), "1h")
    assert forecast["move_24h_pct"] == pytest.approx(1.0 * math.sqrt(24), abs=0.01)
    assert forecast["level"] == "normal" and forecast["ratio"] == pytest.approx(1.0, abs=0.01)


def test_un_marche_qui_s_agite_est_signale_et_un_marche_qui_se_calme_aussi():
    def series(first, second):
        closes = [100.0]
        for i in range(600):
            size = first if i < 500 else second
            closes.append(closes[-1] * math.exp(size if i % 2 == 0 else -size))
        return hourly(closes)

    assert volatility_forecast(series(0.002, 0.02), "1h")["level"] == "high"
    assert volatility_forecast(series(0.02, 0.002), "1h")["level"] == "low"
    flat = volatility_forecast(hourly([100.0] * 100), "1h")
    assert flat == {"move_24h_pct": 0.0, "level": "normal", "ratio": 1.0}       # pas de division par zéro
    assert volatility_forecast(hourly([100.0] * 48), "1h") is None              # il faut deux jours et une bougie


# -- risque et taille ----------------------------------------------------------------
def test_la_sortie_est_a_deux_mouvements_types_et_la_taille_risque_un_pour_cent():
    assert risk_sizing({"move_24h_pct": 2.5}) == {"exit_pct": 5.0, "size_pct": 20.0}      # 1 % / 5 % = 20 % de l'equity
    assert risk_sizing({"move_24h_pct": 0.2}) == {"exit_pct": 2.0, "size_pct": 50.0}      # plancher : jamais sous 2 %
    assert risk_sizing({"move_24h_pct": 20.0}) == {"exit_pct": 15.0, "size_pct": 6.7}     # plafond : jamais au-delà de 15 %
    assert risk_sizing(None) is None


def test_les_modeles_arrivent_dans_le_resume_de_marche_quand_l_historique_suffit():
    full = summarize_candles(hourly(ramp(100, 200, 744)), "1h")
    assert set(full["models"]) == {"trend", "vol", "risk"} and full["models"]["trend"]["regime"] == "up"
    assert compute_models(hourly([100.0] * 10), "1h") == {}
    assert "models" not in summarize_candles(hourly([100.0] * 10), "1h")
    three_days = compute_models(hourly(ramp(100, 110, 72)), "1h")
    assert set(three_days) == {"vol", "risk"}                                   # pas assez pour la tendance


# -- le témoin quant -------------------------------------------------------------------
LIMITS = {"max_order_quote": 10.0, "max_position_quote_per_symbol": 20.0, "max_total_exposure_quote": 40.0,
          "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 47.5}


def view(btc_value=0.0, btc_price=60_000.0, regime="up", exit_pct=5.0, size_pct=20.0, eth_regime="range", **limits):
    cash = 50.0 - btc_value
    models = lambda r: {"trend": {"regime": r, "score": 1.0}, "risk": {"exit_pct": exit_pct, "size_pct": size_pct}}  # noqa: E731
    return MarketView(
        timestamp=1_760_000_000.0, quote_currency="EUR", stake=50.0, equity=50.0, cash=cash,
        positions={"BTC/EUR": {"quantity": btc_value / btc_price, "price": btc_price, "value": btc_value},
                   "ETH/EUR": {"quantity": 0.0, "price": 2_500.0, "value": 0.0}},
        limits={**LIMITS, **limits}, risk_tier="normal",
        market={"BTC/EUR": {"models": models(regime)}, "ETH/EUR": {"models": models(eth_regime)}},
    )


def test_quant_achete_une_tendance_haussiere_a_la_taille_du_modele_de_risque():
    buy = QuantAgent().decide(view())
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, "BTC/EUR", 10.0)           # 20 % de 50
    small = QuantAgent().decide(view(size_pct=12.0))
    assert small.amount_quote == 6.0                                            # la taille vient du risque, pas du plafond
    assert QuantAgent().decide(view(size_pct=8.0)).action == HOLD              # 4 EUR : sous l'ordre minimum
    capped = QuantAgent().decide(view(size_pct=50.0))
    assert capped.amount_quote == 10.0                                          # jamais plus que la marge des garde-fous
    assert QuantAgent().decide(view(regime="range")).action == HOLD
    assert QuantAgent().decide(view(regime="down")).action == HOLD
    assert QuantAgent().decide(view(buys_left_today=0)).action == HOLD


def test_quant_sort_quand_la_tendance_se_retourne():
    sell = QuantAgent().decide(view(btc_value=10.0, regime="down"))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, "BTC/EUR", 10.0)
    assert QuantAgent().decide(view(btc_value=10.0, regime="range")).action == HOLD       # pas de signal, pas de vente


def test_quant_sort_a_son_niveau_qui_suit_le_prix_a_la_hausse_seulement():
    agent = QuantAgent()
    agent.decide(view())                                                        # achat à 60 000 : sortie à 57 000
    assert agent.decide(view(btc_value=10.0, btc_price=57_001.0)).action == HOLD
    hit = agent.decide(view(btc_value=10.0, btc_price=57_000.0))
    assert hit.action == SELL and "niveau de sortie" in hit.reasoning

    trailing = QuantAgent()
    trailing.decide(view())
    trailing.decide(view(btc_value=10.0, btc_price=70_000.0))                   # le prix monte : le niveau suit, à 66 500
    assert trailing.decide(view(btc_value=10.0, btc_price=66_501.0)).action == HOLD
    assert trailing.decide(view(btc_value=10.0, btc_price=66_500.0)).action == SELL
    # Le niveau ne redescend pas quand le prix baisse, et il est oublié une fois la position fermée.
    flat = QuantAgent()
    flat.decide(view())
    flat.decide(view(btc_value=10.0, btc_price=58_000.0))
    assert flat._stops["BTC/EUR"] == 57_000.0
    flat.decide(view(btc_value=0.0, regime="range"))
    assert "BTC/EUR" not in flat._stops


def test_quant_ne_fait_rien_sans_modeles_et_ne_rachete_pas_ce_qu_il_detient():
    bare = MarketView(timestamp=0.0, quote_currency="EUR", stake=50.0, equity=50.0, cash=50.0,
                      positions={"BTC/EUR": {"quantity": 0.0, "price": 60_000.0, "value": 0.0}},
                      limits=LIMITS, market={})
    assert QuantAgent().decide(bare).action == HOLD
    held = QuantAgent().decide(view(btc_value=10.0, eth_regime="up"))
    assert (held.action, held.symbol) == (BUY, "ETH/EUR")                       # le BTC est déjà détenu : il passe à l'ETH
    assert QuantAgent().decide(view(btc_value=10.0)).action == HOLD
