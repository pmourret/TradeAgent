import pytest

from tradeagent.models import Decision, InvalidDecision


def test_valid_buy_from_json():
    d = Decision.from_json('{"action": "buy", "symbol": "BTC/EUR", "amount_quote": 15, "reasoning": "x"}')
    assert (d.action, d.symbol, d.amount_quote) == ("buy", "BTC/EUR", 15.0)


def test_fenced_json_is_accepted():
    d = Decision.from_json('```json\n{"action": "hold"}\n```')
    assert d.action == "hold"


def test_action_is_normalised():
    assert Decision.from_dict({"action": " BUY ", "symbol": "BTC/EUR", "amount_quote": 5}).action == "buy"


def test_hold_ignores_symbol_and_amount():
    d = Decision.from_dict({"action": "hold", "symbol": "BTC/EUR", "amount_quote": 5})
    assert d.symbol is None and d.amount_quote is None


@pytest.mark.parametrize("text", [
    "not json at all",
    "[1, 2]",
    '"buy"',
    '{"symbol": "BTC/EUR", "amount_quote": 5}',                              # pas d'action
    '{"action": "short", "symbol": "BTC/EUR", "amount_quote": 5}',           # action inconnue
    '{"action": "buy", "amount_quote": 5}',                                  # pas de symbole
    '{"action": "buy", "symbol": "", "amount_quote": 5}',
    '{"action": "buy", "symbol": "BTC/EUR"}',                                # pas de montant
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": 0}',
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": -5}',
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": "5"}',           # chaîne, pas nombre
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": true}',          # bool != nombre
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": NaN}',
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": Infinity}',
    '{"action": "buy", "symbol": "BTC/EUR", "amount_quote": 1e999}',         # déborde en inf
    '{"action": "hold", "reasoning": 42}',
])
def test_invalid_decisions_are_rejected(text):
    with pytest.raises(InvalidDecision):
        Decision.from_json(text)


def test_reasoning_is_truncated():
    d = Decision("hold", reasoning="x" * 5000)
    assert len(d.reasoning) == 500


def test_skip_is_a_hold_flagged_by_the_agent_code_only():
    d = Decision.skip("cadence")
    assert d.action == "hold" and d.skipped and d.reasoning == "cadence"


def test_a_model_cannot_flag_its_own_output_as_skipped():
    # `skipped` désactive le journal et le compteur d'erreurs : il ne doit jamais venir d'un LLM.
    d = Decision.from_json('{"action": "buy", "symbol": "BTC/EUR", "amount_quote": 5, "skipped": true}')
    assert d.action == "buy" and d.skipped is False
