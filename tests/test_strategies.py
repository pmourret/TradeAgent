"""Stratégies de comparaison : elles ne demandent que ce que les limites visibles permettent."""
from __future__ import annotations

from tradeagent.agents import MarketView
from tradeagent.models import BUY, HOLD, SELL
from tradeagent.strategies import STRATEGIES, BuyAndHoldAgent, buy_room

LIMITS = {
    "max_order_quote": 10.0, "max_position_quote_per_symbol": 20.0, "max_total_exposure_quote": 40.0,
    "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 47.5,
}


def view(btc: float = 0.0, eth: float = 0.0, cash: float | None = None, ts: float = 1_760_000_000.0,
         tier: str = "normal", market: dict | None = None, **limits) -> MarketView:
    cash = 50.0 - btc - eth if cash is None else cash
    return MarketView(
        timestamp=ts, quote_currency="EUR", stake=50.0, equity=cash + btc + eth, cash=cash,
        positions={"BTC/EUR": {"quantity": btc / 60_000, "price": 60_000.0, "value": btc},
                   "ETH/EUR": {"quantity": eth / 2_500, "price": 2_500.0, "value": eth}},
        limits={**LIMITS, **limits}, risk_tier=tier, market=market or {},
    )


def change(btc: float | None = None, eth: float | None = None) -> dict:
    out = {}
    if btc is not None:
        out["BTC/EUR"] = {"change_pct": {"24h": btc}}
    if eth is not None:
        out["ETH/EUR"] = {"change_pct": {"24h": eth}}
    return out


# -- marge d'achat -------------------------------------------------------------
def test_la_marge_d_achat_est_la_plus_petite_des_limites():
    assert buy_room(view(), "BTC/EUR") == 10.0                              # taille max d'un ordre
    assert buy_room(view(btc=14.0), "BTC/EUR") == 6.0                       # plafond par symbole
    capped = view(btc=20.0, eth=4.0, max_total_exposure_quote=30.0)
    assert buy_room(capped, "ETH/EUR") == 6.0                               # plafond d'exposition totale (30 - 24)
    poor = view(cash=8.0, buys_blocked_below_equity=0.0)
    assert buy_room(poor, "BTC/EUR") == 7.92                                # cash, avec la marge pour les frais
    assert buy_room(view(btc=16.0), "BTC/EUR") == 0.0                       # reste 4 : sous l'ordre minimum
    assert buy_room(view(btc=15.0), "BTC/EUR") == 5.0                       # pile le minimum : accepté


def test_aucune_marge_quand_les_achats_sont_bloques():
    assert buy_room(view(tier="defensive"), "BTC/EUR") == 0.0
    assert buy_room(view(tier="cautious"), "BTC/EUR") == 10.0               # les limites visibles sont déjà réduites
    assert buy_room(view(buys_left_today=0), "BTC/EUR") == 0.0
    assert buy_room(view(cash=47.5), "BTC/EUR") == 0.0                      # perte du jour atteinte (equity <= seuil)
    assert buy_room(view(cash=47.51), "BTC/EUR") == 10.0


# -- achat-conservation --------------------------------------------------------
def test_buyhold_remplit_a_parts_egales_puis_ne_touche_plus_a_rien():
    agent = BuyAndHoldAgent()
    first = agent.decide(view())
    assert (first.action, first.symbol, first.amount_quote) == (BUY, "BTC/EUR", 10.0)
    second = agent.decide(view(btc=10.0))
    assert (second.action, second.symbol) == (BUY, "ETH/EUR")               # le moins servi d'abord
    assert agent.decide(view(btc=20.0, eth=20.0)).action == HOLD            # plafonds atteints : allocation finie
    later = agent.decide(view(btc=5.0, eth=5.0, cash=40.0))                 # même avec de la marge retrouvée
    assert later.action == HOLD and "conserve" in later.reasoning


def test_buyhold_attend_sans_renoncer_tant_qu_il_n_a_rien_achete():
    agent = BuyAndHoldAgent()
    assert agent.decide(view(buys_left_today=0)).action == HOLD             # bloqué aujourd'hui, pas fini
    assert agent.decide(view()).action == BUY


def test_le_registre_des_strategies():
    assert set(STRATEGIES) == {"buyhold", "quant"}
    assert all(cls().name == name for name, cls in STRATEGIES.items())
