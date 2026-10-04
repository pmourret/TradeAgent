import pytest

from helpers import START
from tradeagent.budget import InferenceBudget, day_start_ts
from tradeagent.config import LLMConfig
from tradeagent.llm import LLMUsage
from tradeagent.storage import Storage


def make(storage=None, **cfg):
    storage = storage or Storage(":memory:")
    defaults = dict(price_input_per_mtok_usd=1.0, price_output_per_mtok_usd=5.0, usd_to_eur=1.0,
                    daily_budget_eur=1.0, total_budget_eur=3.0)
    return InferenceBudget(LLMConfig(**{**defaults, **cfg}), storage), storage


def test_cost_formula():
    budget, _ = make()
    # 1 M tokens d'entrée à 1 $ + 200 k tokens de sortie à 5 $ = 2 $
    assert budget.cost_eur(LLMUsage(1_000_000, 200_000)) == pytest.approx(2.0)


def test_cost_applies_the_exchange_rate():
    budget, _ = make(usd_to_eur=0.5)
    assert budget.cost_eur(LLMUsage(1_000_000, 0)) == pytest.approx(0.5)


def test_cache_tokens_are_priced_relative_to_input():
    budget, _ = make()
    cost = budget.cost_eur(LLMUsage(0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000))
    assert cost == pytest.approx(0.10 + 1.25)


def test_a_typical_call_is_cheap():
    # Prompt compact : ~1 500 tokens en entrée, ~150 en sortie, tarif Haiku 4.5.
    budget, _ = make(usd_to_eur=0.90)
    assert budget.cost_eur(LLMUsage(1_500, 150)) < 0.003


def test_record_persists_and_accumulates():
    budget, storage = make()
    budget.record(LLMUsage(1_000_000, 0), START)      # 1 €
    budget.record(LLMUsage(500_000, 0), START + 10)   # 0,5 €
    assert budget.spent_total() == pytest.approx(1.5)
    assert storage.count("llm_calls") == 2
    # un nouvel objet sur la même base retrouve les dépenses
    again, _ = make(storage=storage)
    assert again.spent_total() == pytest.approx(1.5)


def test_daily_cap_blocks_then_resets_the_next_utc_day():
    budget, _ = make(daily_budget_eur=1.0, total_budget_eur=10.0)
    assert budget.can_spend(START) == (True, "")
    budget.record(LLMUsage(1_000_000, 0), START)
    ok, why = budget.can_spend(START + 60)
    assert not ok and "du jour" in why
    assert budget.can_spend(START + 86_400) == (True, "")


def test_total_cap_blocks_for_good():
    budget, _ = make(daily_budget_eur=1.0, total_budget_eur=2.0)
    budget.record(LLMUsage(1_000_000, 0), START)
    budget.record(LLMUsage(1_000_000, 0), START + 86_400)
    ok, why = budget.can_spend(START + 3 * 86_400)
    assert not ok and "total" in why


def test_left_never_goes_negative():
    budget, _ = make(daily_budget_eur=1.0, total_budget_eur=2.0)
    budget.record(LLMUsage(5_000_000, 0), START)
    assert budget.left(START) == {"today": 0.0, "total": 0.0}


def test_day_start_is_midnight_utc():
    midnight = day_start_ts(START)
    assert midnight % 86_400 == 0
    assert 0 <= START - midnight < 86_400


def test_config_rejects_daily_above_total():
    with pytest.raises(Exception, match="daily_budget_eur"):
        LLMConfig(daily_budget_eur=5.0, total_budget_eur=1.0)
