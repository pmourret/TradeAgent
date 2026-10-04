from pathlib import Path

import pytest

from tradeagent.config import ConfigError, config_from_dict, load_config

SHIPPED = Path(__file__).parent.parent / "config.yaml"


def test_shipped_config_loads():
    cfg = load_config(SHIPPED)
    assert cfg.mode == "paper"
    assert cfg.symbols == ("BTC/EUR", "ETH/EUR")
    assert cfg.guardrails.max_order_pct == 20


def test_shipped_config_targets_the_exchange_and_its_taker_fee():
    cfg = load_config(SHIPPED)
    assert cfg.exchange == "bitvavo"
    assert 0 < cfg.costs.fee_rate < 0.01


def test_default_exchange_matches_the_shipped_one_and_any_ccxt_id_is_still_accepted():
    assert config_from_dict({}).exchange == load_config(SHIPPED).exchange
    assert config_from_dict({"exchange": "kraken"}).exchange == "kraken"


def test_empty_config_uses_defaults():
    assert config_from_dict({}).stake == 100.0


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ConfigError, match="inconnue"):
        config_from_dict({"stak": 100})


def test_unknown_guardrail_key_is_rejected():
    # Une faute de frappe ne doit pas désactiver un garde-fou en silence.
    with pytest.raises(ConfigError, match="inconnue"):
        config_from_dict({"guardrails": {"max_order_percent": 50}})


def test_live_mode_is_refused():
    with pytest.raises(ConfigError, match="paper"):
        config_from_dict({"mode": "live"})


def test_symbol_must_match_quote_currency():
    with pytest.raises(ConfigError, match="coté en"):
        config_from_dict({"symbols": ["BTC/USDT"], "quote_currency": "EUR"})


def test_malformed_symbol_is_rejected():
    with pytest.raises(ConfigError, match="BASE/QUOTE"):
        config_from_dict({"symbols": ["BTCEUR"]})


@pytest.mark.parametrize("section,key,value", [
    ("guardrails", "max_order_pct", 0),
    ("guardrails", "max_order_pct", 150),
    ("guardrails", "max_buys_per_day", -1),
    ("guardrails", "max_buys_per_day", 2.5),
    ("guardrails", "min_order_quote", True),
    ("killswitch", "max_total_loss_pct", 0),
    ("killswitch", "max_consecutive_errors", 0),
    ("killswitch", "liquidate_on_death", "yes"),
    ("costs", "fee_rate", -0.1),
])
def test_bad_values_are_rejected(section, key, value):
    with pytest.raises(ConfigError):
        config_from_dict({section: {key: value}})


def test_non_positive_stake_is_rejected():
    with pytest.raises(ConfigError):
        config_from_dict({"stake": 0})


def test_config_where_no_order_could_pass_is_rejected():
    with pytest.raises(ConfigError, match="aucun ordre"):
        config_from_dict({"stake": 10, "guardrails": {"max_order_pct": 20, "min_order_quote": 5}})


def test_missing_file():
    with pytest.raises(ConfigError, match="introuvable"):
        load_config("/nonexistent/config.yaml")


# -- sections ajoutées : marché, LLM, paliers de risque ------------------------

def test_shipped_config_has_sane_llm_defaults():
    cfg = load_config(SHIPPED)
    assert cfg.llm.model == "claude-haiku-4-5-20251001"
    assert cfg.llm.call_every_seconds >= 60
    assert 0 < cfg.llm.daily_budget_eur <= cfg.llm.total_budget_eur
    assert cfg.risk_tiers.defensive_drawdown_pct < cfg.killswitch.max_drawdown_pct
    assert cfg.market.timeframe == "1h" and cfg.market.candles >= 24


@pytest.mark.parametrize("section,key,value", [
    ("market", "timeframe", "3h"),
    ("market", "candles", 5),
    ("market", "candles", 5000),
    ("llm", "provider", "openai"),
    ("llm", "model", ""),
    ("llm", "max_output_tokens", 10),
    ("llm", "max_output_tokens", 100_000),
    ("llm", "call_every_seconds", 5),
    ("llm", "usd_to_eur", 0),
    ("llm", "daily_budget_eur", 0),
    ("llm", "total_budget_eur", -1),
    ("llm", "price_input_per_mtok_usd", -1),
    ("risk_tiers", "cautious_size_factor", 0),
])
def test_new_section_bad_values_are_rejected(section, key, value):
    with pytest.raises(ConfigError):
        config_from_dict({section: {key: value}})


def test_unknown_llm_key_is_rejected():
    with pytest.raises(ConfigError, match="inconnue"):
        config_from_dict({"llm": {"daily_budget": 1}})
