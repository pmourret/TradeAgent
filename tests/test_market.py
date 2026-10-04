import pytest

from tradeagent.market import summarize_candles
from tradeagent.models import Candle


def hourly(closes, start=1_760_000_000.0, spread=0.0):
    return [Candle(start + i * 3600, c, c * (1 + spread), c * (1 - spread), c) for i, c in enumerate(closes)]


def test_empty_history():
    assert summarize_candles([], "1h") == {}


def test_changes_are_measured_against_the_right_candle():
    closes = [100.0] * 30 + [110.0]  # +10 % sur la dernière heure, mais plat avant
    s = summarize_candles(hourly(closes), "1h")
    assert s["last"] == 110.0
    assert s["change_pct"]["1h"] == pytest.approx(10.0)
    assert s["change_pct"]["6h"] == pytest.approx(10.0)
    assert s["change_pct"]["24h"] == pytest.approx(10.0)


def test_24h_change_uses_the_candle_24_hours_back():
    closes = [50.0] + [100.0] * 24  # 25 bougies : -24 est la première
    s = summarize_candles(hourly(closes), "1h")
    assert s["change_pct"]["24h"] == pytest.approx(100.0)
    assert s["change_pct"]["1h"] == pytest.approx(0.0)


def test_short_history_omits_horizons_it_cannot_compute():
    s = summarize_candles(hourly([100.0, 101.0, 102.0]), "1h")
    assert set(s["change_pct"]) == {"1h"}


def test_high_low_cover_only_the_last_24_hours():
    closes = [1000.0] + [100.0] * 30  # un vieux pic hors fenêtre
    s = summarize_candles(hourly(closes, spread=0.01), "1h")
    assert s["high_24h"] == pytest.approx(101.0)
    assert s["low_24h"] == pytest.approx(99.0)


def test_closes_are_capped_and_rounded_to_five_significant_digits():
    closes = [60_123.456 + i for i in range(48)]
    s = summarize_candles(hourly(closes), "1h")
    assert len(s["closes"]) == 24
    assert s["closes"][-1] == 60170.0
    assert s["last"] == 60170.0


def test_volatility_is_zero_for_flat_prices_and_positive_otherwise():
    flat = summarize_candles(hourly([100.0] * 30), "1h")
    assert flat["volatility_pct_per_candle"] == 0.0
    wobbly = summarize_candles(hourly([100.0, 102.0] * 15), "1h")
    assert wobbly["volatility_pct_per_candle"] > 1.0


def test_daily_timeframe_skips_sub_day_horizons():
    candles = [Candle(i * 86_400, 100 + i, 100 + i, 100 + i, 100 + i) for i in range(30)]
    s = summarize_candles(candles, "1d")
    assert set(s["change_pct"]) == {"24h"}


def test_summary_is_json_serialisable_and_small():
    import json

    s = summarize_candles(hourly([60_000 + i * 7.5 for i in range(48)], spread=0.002), "1h")
    assert len(json.dumps(s, separators=(",", ":"))) < 500


# -- indicateurs calculés par le code --------------------------------------------------------------------------

def test_rsi_extremes_et_valeur_connue():
    from tradeagent.market import rsi

    assert rsi([float(i) for i in range(1, 30)]) == 100.0                       # que des hausses
    assert rsi([float(i) for i in range(30, 1, -1)]) == 0.0                     # que des baisses
    assert rsi([100.0] * 20) == 50.0                                            # plat : ni l'un ni l'autre
    assert rsi([100.0] * 14) is None                                            # il faut 15 clôtures
    # 14 variations : 7 hausses de 2, 7 baisses de 1. Gain moyen 1, perte moyenne 0,5 : RSI = 100 - 100/3.
    closes = [100.0]
    for i in range(14):
        closes.append(closes[-1] + (2.0 if i % 2 == 0 else -1.0))
    assert rsi(closes) == pytest.approx(100 - 100 / 3)


def test_atr_est_l_amplitude_moyenne_en_pourcentage_du_prix():
    from tradeagent.market import atr_pct

    flat = [Candle(i * 3600, 100.0, 101.0, 99.0, 100.0) for i in range(20)]     # 2 d'amplitude sur un prix de 100
    assert atr_pct(flat) == pytest.approx(2.0)
    assert atr_pct(flat[:14]) is None
    gap = flat[:-1] + [Candle(20 * 3600, 110.0, 111.0, 109.0, 110.0)]           # un saut : la vraie amplitude compte l'écart
    assert atr_pct(gap) == pytest.approx((13 * 2 + 11) / 14 / 110 * 100)


def test_tendance_et_fourchette_sur_30_jours():
    # 30 jours de hausse régulière de 100 à 200, puis une chute de 10 % sur les dernières 24 h.
    up = [100.0 + i * 100.0 / 695 for i in range(696)]
    closes = up + [200.0 - (i + 1) * 20.0 / 24 for i in range(24)]
    s = summarize_candles(hourly(closes), "1h")
    assert s["last"] == 180.0
    assert s["change_pct"]["24h"] == pytest.approx(-10.0)
    assert s["change_long_pct"]["7d"] > 0 and "30d" not in s["change_long_pct"]      # 720 bougies : pas 30 j + 1
    trend = s["trend_vs_sma_pct"]
    assert trend["24h"] < 0 < trend["30d"]                                      # sous sa moyenne du jour, au-dessus de celle du mois
    assert set(trend) == {"24h", "7d", "30d"}
    month = s["range"]["30d"]
    assert month["high"] == 200.0 and month["low"] == 100.0
    assert month["pos_pct"] == 80 and month["from_high_pct"] == pytest.approx(-10.0)
    assert s["range"]["7d"]["low"] > 150 and s["range"]["7d"]["high"] == 200.0          # la semaine, pas le mois
    assert s["rsi"]["1h"] < 30 and 30 < s["rsi"]["1d"] <= 100                   # survendu sur l'heure, pas sur le jour
    assert len(s["daily_closes"]) == 10 and s["daily_closes"][-1] == 180.0
    assert s["atr_pct"] > 0


def test_un_rebond_d_une_heure_dans_une_tendance_baissiere_se_voit():
    down = [200.0 - i * 100.0 / 718 for i in range(719)]
    closes = down + [down[-1] * 1.03]                                           # +3 % sur la dernière heure
    s = summarize_candles(hourly(closes), "1h")
    assert s["change_pct"]["1h"] == pytest.approx(3.0, abs=0.01)
    assert s["trend_vs_sma_pct"]["7d"] < 0 and s["trend_vs_sma_pct"]["30d"] < -20      # mais loin sous ses moyennes
    assert s["range"]["30d"]["pos_pct"] <= 5                                    # et au plus bas du mois


def test_les_indicateurs_n_apparaissent_que_si_l_historique_suffit():
    short = summarize_candles(hourly([100.0 + i for i in range(10)]), "1h")
    for absent in ("change_long_pct", "trend_vs_sma_pct", "rsi", "atr_pct", "range", "daily_closes"):
        assert absent not in short
    two_days = summarize_candles(hourly([100.0 + i for i in range(48)]), "1h")
    assert set(two_days["trend_vs_sma_pct"]) == {"24h"} and "range" not in two_days and "1d" not in two_days["rsi"]
    flat = summarize_candles(hourly([100.0] * 200), "1h")
    assert flat["range"]["7d"]["pos_pct"] == 50                                 # fourchette nulle : pas de division par zéro


def test_le_resume_complet_reste_compact():
    import json

    s = summarize_candles(hourly([60_000 + i * 7.5 for i in range(720)], spread=0.002), "1h")
    assert len(json.dumps(s, separators=(",", ":"))) < 900
