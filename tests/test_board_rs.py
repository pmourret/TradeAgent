"""Le deuxième siège du board : la force relative entre BTC et ETH (backtest seulement)."""
from __future__ import annotations

import math

import pytest

from tradeagent.agents import MarketView
from tradeagent.backtest import BACKTEST_AGENTS, run_backtest, warmup_seconds
from tradeagent.board import RS_SWITCH_GAP_PTS, BoardAgent, RelativeStrengthSleeve, TrendSleeve
from tradeagent.models import BUY, HOLD, SELL, Candle

from helpers import default_cfg

LIMITS = {"max_order_quote": 20.0, "max_position_quote_per_symbol": 30.0, "max_total_exposure_quote": 40.0,
          "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 47.5}
BTC, ETH = "BTC/EUR", "ETH/EUR"
PRICES = {BTC: 60_000.0, ETH: 2_500.0}


def view(r30=None, regimes=None, held=None, prices=None, exit_pct=5.0, size_pct=20.0, **limits):
    """`r30` : rendement sur 30 jours par symbole (absent = inconnu) ; `held` : valeur détenue par symbole."""
    r30 = {BTC: 4.0, ETH: 10.0} if r30 is None else r30
    regimes, held, prices = regimes or {}, held or {}, {**PRICES, **(prices or {})}
    market = {}
    for symbol in (BTC, ETH):
        market[symbol] = {"models": {"trend": {"regime": regimes.get(symbol, "range"), "score": 0.0},
                                     "risk": {"exit_pct": exit_pct, "size_pct": size_pct}}}
        if symbol in r30:
            market[symbol]["change_long_pct"] = {"7d": 0.0, "30d": r30[symbol]}
    positions = {s: {"quantity": held.get(s, 0.0) / PRICES[s], "price": prices[s],
                     "value": held.get(s, 0.0) / PRICES[s] * prices[s]} for s in (BTC, ETH)}
    return MarketView(timestamp=1_760_000_000.0, quote_currency="EUR", stake=50.0, equity=50.0,
                      cash=50.0 - sum(held.values()), positions=positions, limits={**LIMITS, **limits},
                      risk_tier="normal", market=market)


def rs():
    return BoardAgent([RelativeStrengthSleeve()])


def held_rs(symbol, value=10.0, stop_price=None):
    """Un board dont le siège de force relative détient déjà `symbol` (comme après un achat servi)."""
    state = {"qty": {symbol: value / PRICES[symbol]}, "stops": {symbol: stop_price or PRICES[symbol] * 0.95},
             "notes": {}}
    return BoardAgent([RelativeStrengthSleeve(state)]), state


# -- entrer --------------------------------------------------------------------------------
def test_il_achete_le_meneur_sur_30_jours_a_la_taille_du_modele_de_risque():
    buy = rs().decide(view())
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, ETH, 10.0)       # ETH mène, 20 % de 50
    assert "rs" in buy.reasoning and "meneur sur 30 jours (+10.0 %)" in buy.reasoning
    assert rs().decide(view(r30={BTC: 12.0, ETH: 3.0})).symbol == BTC
    assert rs().decide(view(size_pct=12.0)).amount_quote == 6.0


@pytest.mark.parametrize("case", [
    {"r30": {BTC: -6.0, ETH: -1.0}},                    # le meneur baisse : personne n'est fort
    {"r30": {BTC: -6.0, ETH: 0.0}},                     # zéro n'est pas une hausse
    {"regimes": {ETH: "down"}},                          # le meneur est en tendance baissière
    {"r30": {ETH: 10.0}},                                # une seule force connue : rien à comparer
    {"buys_left_today": 0},                              # plus d'achat permis
    {"size_pct": 8.0},                                   # 4 EUR : sous l'ordre minimal
])
def test_il_n_entre_pas_sans_meneur_qui_monte(case):
    assert rs().decide(view(**case)).action == HOLD


def test_une_seule_position_a_la_fois():
    board, state = held_rs(ETH)
    assert board.decide(view(held={ETH: 10.0})).action == HOLD     # déjà sur le meneur : rien de plus
    assert list(state["qty"]) == [ETH]


# -- rester ou sortir -------------------------------------------------------------------------
def test_un_meneur_depasse_de_moins_de_l_ecart_reste_detenu():
    board, state = held_rs(ETH)
    close = RS_SWITCH_GAP_PTS - 0.1
    assert board.decide(view(r30={BTC: 10.0 + close, ETH: 10.0}, held={ETH: 10.0})).action == HOLD
    assert ETH in state["qty"]


def test_depasse_de_l_ecart_il_sort_puis_prend_le_nouveau_meneur_au_cycle_suivant():
    board, state = held_rs(ETH)
    r30 = {BTC: 10.0 + RS_SWITCH_GAP_PTS, ETH: 10.0}
    sell = board.decide(view(r30=r30, held={ETH: 10.0}))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, ETH, 10.0)
    assert "dépassé de 5.0 points" in sell.reasoning
    assert state["qty"] == {}                                       # pas d'entrée le cycle d'une sortie
    buy = board.decide(view(r30=r30))                               # la vente est passée
    assert (buy.action, buy.symbol) == (BUY, BTC)


@pytest.mark.parametrize("case, why", [
    ({"r30": {BTC: -9.0, ETH: 0.0}}, "rendement sur 30 jours à +0.0 %"),
    ({"r30": {BTC: -9.0, ETH: -2.0}}, "rendement sur 30 jours à -2.0 %"),
    ({"regimes": {ETH: "down"}}, "tendance à la baisse"),
    ({"r30": {BTC: 4.0}}, "force sur 30 jours inconnue"),
    ({"prices": {ETH: 2_300.0}}, "niveau de sortie touché"),
])
def test_il_sort_quand_le_meneur_faiblit(case, why):
    board, state = held_rs(ETH)
    sell = board.decide(view(held={ETH: 10.0}, **case))
    assert (sell.action, sell.symbol) == (SELL, ETH) and why in sell.reasoning
    assert state["qty"] == {}


def test_le_niveau_de_sortie_suit_le_prix_a_la_hausse_seulement():
    board, state = held_rs(ETH, stop_price=2_000.0)
    board.decide(view(held={ETH: 10.0}, prices={ETH: 3_000.0}))
    assert state["stops"][ETH] == pytest.approx(3_000.0 * 0.95)
    board.decide(view(held={ETH: 10.0}, prices={ETH: 2_900.0}))
    assert state["stops"][ETH] == pytest.approx(3_000.0 * 0.95)


def test_une_position_perdue_est_rendue_et_le_meneur_rejuge():
    board, state = held_rs(ETH)
    decision = board.decide(view(regimes={ETH: "down"}))            # le portefeuille ne détient rien
    assert state["notes"][ETH] == "position non obtenue ou perdue" and state["qty"] == {}
    assert decision.action == HOLD
    decision = board.decide(view())
    assert (decision.action, decision.symbol) == (BUY, ETH)         # libre, il rejuge son entrée


# -- avec le suivi de tendance ------------------------------------------------------------------
def test_les_cibles_des_deux_sieges_s_additionnent_dans_les_marges():
    board = BoardAgent([TrendSleeve(), RelativeStrengthSleeve()])
    buy = board.decide(view(regimes={ETH: "up"}))                   # les deux veulent ETH : 10 + 10
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, ETH, 20.0)
    assert "trend:" in buy.reasoning and "rs:" in buy.reasoning


def _history(cfg, closes, start):
    first = start - warmup_seconds(cfg)
    series = [closes[0]] * int(warmup_seconds(cfg) // 3600) + list(closes)
    return [Candle(first + i * 3600.0, c, c, c, c, 1.0) for i, c in enumerate(series)]


def test_board_rs_tourne_sur_le_vrai_moteur_et_change_de_meneur():
    assert "board-rs" in BACKTEST_AGENTS
    cfg = default_cfg(market={"candles": 744})
    start = 1_760_000_400.0
    wobble = lambda i: 1 + 0.01 * math.sin(i / 5)  # noqa: E731
    hours = 24 * 50
    # BTC mène d'abord, puis ETH le dépasse nettement ; les deux montent.
    btc = [60_000.0 * (1.0015 if i < hours // 2 else 1.0002) ** i * wobble(i) for i in range(hours)]
    eth = [2_500.0 * (1.0004 if i < hours // 2 else 1.004) ** i * wobble(i) for i in range(hours)]
    history = {"BTC/EUR": _history(cfg, btc, start), "ETH/EUR": _history(cfg, eth, start)}
    result = run_backtest(cfg, "board-rs", history, start, start + (hours - 1) * 3600.0)
    assert result.status == "alive" and result.orders >= 3


# -- le socle -----------------------------------------------------------------------------------
from tradeagent.board import CoreSleeve  # noqa: E402


def test_le_socle_achete_sa_part_de_la_mise_puis_ne_sort_jamais():
    core = CoreSleeve(30)
    board = BoardAgent([core])
    buy = board.decide(view(regimes={BTC: "down", ETH: "down"}))        # la tendance ne compte pas pour le socle
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, BTC, 7.5)  # 30 % de 50, à parts égales
    assert core.wanted() == {BTC: pytest.approx(7.5 / 60_000), ETH: pytest.approx(7.5 / 2_500)}
    nxt = board.decide(view(held={BTC: 7.5}, regimes={BTC: "down", ETH: "down"}))
    assert (nxt.action, nxt.symbol) == (BUY, ETH)                         # l'autre part, au cycle suivant
    crash = view(held={BTC: 7.5, ETH: 7.5}, prices={BTC: 30_000.0}, regimes={BTC: "down", ETH: "down"})
    assert board.decide(crash).action == HOLD                             # moitié du prix : on garde


def test_le_socle_et_la_tendance_s_additionnent_et_la_tendance_sort_seule():
    core, trend = CoreSleeve(30, {"qty": {BTC: 7.5 / 60_000, ETH: 7.5 / 2_500}}), TrendSleeve()
    board = BoardAgent([core, trend])
    held = {BTC: 7.5, ETH: 7.5}
    buy = board.decide(view(held=held, regimes={BTC: "up"}))
    assert core.wanted() and (buy.action, buy.symbol, buy.amount_quote) == (BUY, BTC, 10.0)
    sell = board.decide(view(held={BTC: 17.5, ETH: 7.5}, regimes={BTC: "down"}))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, BTC, 10.0)   # seule la part de la tendance


def test_un_socle_hors_bornes_est_refuse():
    for share in (0, -5, 120):
        with pytest.raises(ValueError):
            CoreSleeve(share)


def test_les_variantes_du_socle_tournent_sur_le_vrai_moteur():
    cfg = default_cfg(market={"candles": 744})
    start = 1_760_000_400.0
    closes = [100.0 * (1.001 ** i) * (1 + 0.01 * math.sin(i / 5)) for i in range(24 * 20)]
    history = {s: _history(cfg, closes, start) for s in cfg.symbols}
    end = start + (len(closes) - 1) * 3600.0
    results = {k: run_backtest(cfg, k, history, start, end) for k in ("board-core20", "board-core60")}
    assert all(r.status == "alive" for r in results.values())
    assert results["board-core60"].net_result > results["board-core20"].net_result > 0   # marché qui monte


def test_force_relative_et_socle_ne_sont_branches_qu_en_backtest():
    """Les brancher en `run` demande d'abord de garder leur état en base (`SLEEVE_NAMES`, `clean_state`) : sinon un
    redémarrage l'effacerait et le board vendrait ce qu'ils détiennent."""
    from tradeagent.app import AGENT_KINDS
    from tradeagent.backtest import CORE_AGENTS
    from tradeagent.board import SLEEVE_NAMES, clean_state
    assert not {"board-rs", *CORE_AGENTS} & set(AGENT_KINDS)
    assert SLEEVE_NAMES == ("trend",) and set(clean_state({"rs": {}, "core": {}})) == {"trend"}
