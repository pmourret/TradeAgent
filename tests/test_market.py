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
