"""Le board : des sous-agents qui tiennent des cibles, un seul portefeuille qui trade l'écart."""
from __future__ import annotations

import json
import math

import pytest

from tradeagent.agents import MarketView
from tradeagent.backtest import BACKTEST_AGENTS, run_backtest, warmup_seconds
from tradeagent.app import AGENT_KINDS, LIFE_KEYS, build_agent, reset_life
from tradeagent.board import STATE_KEY, BoardAgent, TrendSleeve, clean_state, stored_board
from tradeagent.models import BUY, HOLD, SELL, Candle

from tradeagent.storage import Storage

from helpers import default_cfg, make_engine

LIMITS = {"max_order_quote": 10.0, "max_position_quote_per_symbol": 20.0, "max_total_exposure_quote": 40.0,
          "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 47.5}
BTC, ETH = "BTC/EUR", "ETH/EUR"


def view(btc_value=0.0, btc_price=60_000.0, regime="up", exit_pct=5.0, size_pct=20.0, eth_regime="range",
         eth_value=0.0, btc_qty=None, **limits):
    models = lambda r: {"trend": {"regime": r, "score": 1.0}, "risk": {"exit_pct": exit_pct, "size_pct": size_pct}}  # noqa: E731
    btc_qty = btc_value / btc_price if btc_qty is None else btc_qty
    return MarketView(
        timestamp=1_760_000_000.0, quote_currency="EUR", stake=50.0, equity=50.0, cash=50.0 - btc_value - eth_value,
        positions={BTC: {"quantity": btc_qty, "price": btc_price, "value": btc_qty * btc_price},
                   ETH: {"quantity": eth_value / 2_500.0, "price": 2_500.0, "value": eth_value}},
        limits={**LIMITS, **limits}, risk_tier="normal",
        market={BTC: {"models": models(regime)}, ETH: {"models": models(eth_regime)}},
    )


class FixedSleeve:
    """Un sous-agent qui veut ce qu'on lui dit : pour tester le board sans la logique de tendance.

    `wanted` : ce qu'il détient déjà. `enter` : ce qu'il réclame à chaque cycle où une entrée est permise.
    """

    def __init__(self, name, enter=None, **wanted):
        self.name = name
        self.qty = dict(wanted)
        self.enter = dict(enter or {})
        self.updates = 0

    def wanted(self):
        return dict(self.qty)

    def rescale(self, symbol, factor):
        if symbol in self.qty:
            self.qty[symbol] *= factor

    def update(self, view, may_enter):
        self.updates += 1
        self.may_enter = may_enter
        if may_enter:
            self.qty.update(self.enter)

    def note(self, symbol):
        return f"raison de {self.name}"


def board():
    return BoardAgent([TrendSleeve()])


# -- le sous-agent tendance ------------------------------------------------------------
def test_une_entree_reste_revendable_jusqu_a_son_niveau_de_sortie():
    # 5.35 EUR à sortie -9.58 % vaudraient 4.84 EUR au niveau de sortie : invendables. Arrondi au minimum revendable.
    buy = board().decide(view(size_pct=10.7, exit_pct=9.58))
    need = round(5.0 / (1 - 0.0958) * 1.02 + 0.005, 2)
    assert (buy.action, buy.amount_quote) == (BUY, need) and need * (1 - 0.0958) >= 5.0
    assert board().decide(view(size_pct=10.7, exit_pct=9.58, max_order_quote=5.5)).action == HOLD   # pas la marge
    assert board().decide(view(size_pct=14.0, exit_pct=9.58)).amount_quote == 7.0           # déjà revendable : inchangé
    assert board().decide(view(size_pct=9.0, exit_pct=9.58)).action == HOLD                 # 4.50 : toujours refusé


def test_tendance_entre_a_la_taille_du_modele_de_risque():
    buy = board().decide(view())
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, BTC, 10.0)                  # 20 % de 50
    assert "trend" in buy.reasoning and "tendance à la hausse" in buy.reasoning
    assert board().decide(view(size_pct=12.0)).amount_quote == 6.0                         # la taille vient du risque
    assert board().decide(view(size_pct=8.0)).action == HOLD                               # 4 EUR : sous l'ordre minimum
    assert board().decide(view(size_pct=50.0)).amount_quote == 10.0                        # jamais plus que la marge
    assert board().decide(view(regime="range")).action == HOLD
    assert board().decide(view(regime="down")).action == HOLD
    assert board().decide(view(buys_left_today=0)).action == HOLD


def test_tendance_sort_quand_le_regime_se_retourne_et_pas_sans_signal():
    agent = board()
    agent.decide(view())
    assert agent.decide(view(btc_value=10.0, regime="range")).action == HOLD
    sell = agent.decide(view(btc_value=10.0, regime="down"))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, BTC, 10.0)
    assert "tendance à la baisse" in sell.reasoning


def test_tendance_sort_a_son_niveau_qui_suit_le_prix_a_la_hausse_seulement():
    agent = board()
    agent.decide(view())                                                        # achat à 60 000 : sortie à 57 000
    qty = 10.0 / 60_000.0
    assert agent.decide(view(btc_qty=qty, btc_price=57_001.0)).action == HOLD
    hit = agent.decide(view(btc_qty=qty, btc_price=57_000.0))
    assert hit.action == SELL and "niveau de sortie" in hit.reasoning

    trailing = board()
    trailing.decide(view())
    trailing.decide(view(btc_qty=qty, btc_price=70_000.0))                      # le prix monte : le niveau suit, à 66 500
    assert trailing.decide(view(btc_qty=qty, btc_price=66_501.0)).action == HOLD
    assert trailing.decide(view(btc_qty=qty, btc_price=66_500.0)).action == SELL

    sleeve = TrendSleeve()
    flat = BoardAgent([sleeve])
    flat.decide(view())
    flat.decide(view(btc_qty=qty, btc_price=58_000.0))                          # le niveau ne redescend pas
    assert sleeve._stops[BTC] == 57_000.0
    flat.decide(view(btc_value=0.0, regime="range"))                            # position disparue : niveau oublié
    assert BTC not in sleeve._stops and BTC not in sleeve.wanted()


def test_deux_entrees_le_meme_cycle_un_seul_achat_et_la_seconde_est_rejugee_avec_les_marges_du_moment():
    sleeve = TrendSleeve()
    agent = BoardAgent([sleeve])
    first = agent.decide(view(eth_regime="up"))
    assert (first.action, first.symbol) == (BUY, BTC) and set(sleeve.wanted()) == {BTC, ETH}
    second = agent.decide(view(btc_value=10.0, eth_regime="up", max_total_exposure_quote=16.0))
    assert (second.action, second.symbol, second.amount_quote) == (BUY, ETH, 6.0)          # 16 - 10 : la marge a changé
    assert sleeve.wanted()[ETH] == pytest.approx(6.0 / 2_500.0)
    assert agent.decide(view(btc_value=10.0, eth_value=6.0, eth_regime="up")).action == HOLD   # détenu : pas racheté


def test_une_entree_impossible_ne_bloque_pas_les_autres_symboles():
    agent = BoardAgent([TrendSleeve()])
    # 4 EUR de BTC que personne ne réclame : l'entrée BTC voudrait 6 EUR, il n'en manque que 2, aucun ordre possible.
    for _ in range(2):
        buy = agent.decide(view(btc_value=4.0, size_pct=12.0, eth_regime="up"))
        assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, ETH, 6.0)


def test_une_sortie_sous_l_ordre_minimal_laisse_la_poussiere_vendue_quand_elle_repasse_le_minimum():
    sleeve = TrendSleeve()
    agent = BoardAgent([sleeve])
    agent.decide(view(size_pct=11.0))                                           # 5.50 EUR à 60 000, sortie à 57 000
    qty = 5.5 / 60_000.0
    assert agent.decide(view(btc_qty=qty, btc_price=54_000.0, regime="range")).action == HOLD   # 4.95 EUR : invendable
    assert sleeve.wanted() == {} and sleeve._stops == {}                        # le niveau est touché : il n'en veut plus
    sell = agent.decide(view(btc_qty=qty, btc_price=55_000.0, regime="range"))  # 5.04 EUR : vendable, personne n'en veut
    assert (sell.action, sell.symbol) == (SELL, BTC)


def test_une_position_sous_le_minimum_reste_reclamee_tant_que_rien_ne_dit_de_sortir():
    # Une position de 5.50 EUR à sortie -15 % ne s'ouvre plus (elle serait arrondie à 6.00, revendable jusqu'à sa
    # sortie) ; elle existe encore si elle date d'avant cette règle, ou après un saut de prix.
    qty = 5.5 / 60_000.0
    sleeve = TrendSleeve({"qty": {BTC: qty}, "stops": {BTC: 51_000.0}})
    agent = BoardAgent([sleeve])
    assert agent.decide(view(btc_qty=qty, btc_price=54_000.0, exit_pct=15.0)).action == HOLD   # 4.95 EUR, au-dessus du niveau
    assert sleeve.wanted() == {BTC: qty} and sleeve._stops[BTC] == 51_000.0
    back = agent.decide(view(btc_qty=qty, btc_price=55_000.0, exit_pct=15.0, regime="down"))
    assert (back.action, back.amount_quote) == (SELL, 5.04)


def test_une_vente_due_passe_avant_toute_entree_meme_aux_cycles_suivants():
    sleeve = TrendSleeve({"qty": {BTC: 10.0 / 60_000.0, ETH: 0.004}, "stops": {BTC: 57_000.0, ETH: 2_600.0}})
    agent = BoardAgent([sleeve])
    first = agent.decide(view(btc_price=56_000.0, btc_qty=10.0 / 60_000.0, eth_value=10.0, eth_regime="up"))
    assert (first.action, first.symbol) == (SELL, BTC) and sleeve.wanted() == {}           # les deux niveaux sont touchés
    second = agent.decide(view(eth_value=10.0, eth_regime="up"))                 # l'ETH est encore là : on le vend,
    assert (second.action, second.symbol, second.amount_quote) == (SELL, ETH, 10.0)        # on ne le reprend pas
    assert sleeve.wanted() == {}
    third = agent.decide(view(eth_regime="up"))                                 # tout est sorti : on peut rentrer
    assert third.action == BUY


def test_les_sous_agents_savent_quand_une_vente_est_due():
    waiting = FixedSleeve("a")
    BoardAgent([waiting]).decide(view(btc_value=10.0))
    assert waiting.may_enter is False
    free = FixedSleeve("a")
    BoardAgent([free]).decide(view(btc_value=4.0))                              # de la poussière : rien à vendre
    assert free.may_enter is True


def test_tendance_ne_fait_rien_sans_modeles_et_oublie_un_symbole_disparu():
    bare = MarketView(timestamp=0.0, quote_currency="EUR", stake=50.0, equity=50.0, cash=50.0,
                      positions={BTC: {"quantity": 0.0, "price": 60_000.0, "value": 0.0}}, limits=LIMITS, market={})
    assert board().decide(bare).action == HOLD
    sleeve = TrendSleeve({"qty": {"SOL/EUR": 1.0}, "stops": {"SOL/EUR": 100.0}})
    sleeve.update(bare, True)
    assert sleeve.wanted() == {}


def test_tendance_garde_son_etat_dans_le_dictionnaire_fourni():
    state: dict = {}
    BoardAgent([TrendSleeve(state)]).decide(view())
    assert state["qty"][BTC] == pytest.approx(10.0 / 60_000.0) and state["stops"][BTC] == 57_000.0
    again = BoardAgent([TrendSleeve(state)])                                    # un autre objet, le même état
    assert again.decide(view(btc_value=10.0, btc_price=56_000.0)).action == SELL


# -- le board : cibles contre portefeuille ---------------------------------------------
def test_un_achat_qui_n_est_pas_parti_est_rendu_et_l_entree_est_rejugee():
    sleeve = TrendSleeve()
    agent = BoardAgent([sleeve])
    agent.decide(view())                                                        # achat demandé à 60 000...
    retry = agent.decide(view(btc_price=50_000.0, size_pct=12.0))               # ...jamais exécuté : on repart de zéro
    assert (retry.action, retry.amount_quote) == (BUY, 6.0)
    assert sleeve.wanted()[BTC] == pytest.approx(6.0 / 50_000.0) and sleeve._stops[BTC] == 47_500.0
    gone = agent.decide(view(regime="range"))                                   # le signal a disparu : plus rien à vouloir
    assert gone.action == HOLD and sleeve.wanted() == {}


def test_un_achat_reduit_est_accepte_tel_quel_sans_complement():
    sleeve = TrendSleeve()
    agent = BoardAgent([sleeve])
    agent.decide(view())                                                        # 10 EUR demandés
    assert agent.decide(view(btc_value=6.0)).action == HOLD                     # 6 EUR obtenus : c'est la position
    assert sleeve.wanted()[BTC] == pytest.approx(6.0 / 60_000.0)
    assert agent.decide(view(btc_value=6.0)).action == HOLD


def test_le_board_additionne_les_cibles_et_vend_ce_qui_depasse():
    qty = 10.0 / 60_000.0
    both = BoardAgent([FixedSleeve("a", **{BTC: qty}), FixedSleeve("b", **{BTC: qty})])
    assert both.decide(view(btc_value=20.0)).action == HOLD                     # 10 + 10 : le compte y est
    leaving = FixedSleeve("b")
    half = BoardAgent([FixedSleeve("a", **{BTC: qty}), leaving])
    sell = half.decide(view(btc_value=20.0))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, BTC, 10.0)   # l'un sort, l'autre reste
    assert sell.reasoning == "board/b: raison de b"
    small = BoardAgent([FixedSleeve("a", **{BTC: qty})])
    assert small.decide(view(btc_value=14.0)).action == HOLD                    # 4 EUR de trop : pas un ordre


def test_un_reste_invendable_part_avec_la_sortie():
    stay = FixedSleeve("a", **{BTC: 3.0 / 60_000.0})
    sell = BoardAgent([stay, FixedSleeve("b")]).decide(view(btc_value=13.0))    # sortir 10 laisserait 3 EUR
    assert (sell.action, sell.amount_quote) == (SELL, 13.0)


def test_personne_ne_veut_la_position_elle_est_vendue_en_entier_et_la_poussiere_est_laissee():
    agent = BoardAgent([FixedSleeve("a")])
    sell = agent.decide(view(btc_value=10.0, eth_value=7.0))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, BTC, 10.0)   # un ordre par cycle, dans l'ordre
    assert agent.decide(view(eth_value=7.0)).symbol == ETH
    assert agent.decide(view(btc_value=4.0)).action == HOLD                     # sous l'ordre minimum


def test_les_ventes_passent_avant_les_achats_et_ne_dependent_pas_des_marges():
    qty = 10.0 / 2_500.0
    eager = FixedSleeve("a", enter={ETH: qty})
    sell = BoardAgent([eager]).decide(view(btc_value=10.0))
    assert (sell.action, sell.symbol) == (SELL, BTC) and eager.qty == {}        # vente due : il n'a pas pu entrer
    free = BoardAgent([FixedSleeve("a", enter={ETH: qty})])
    buy = free.decide(view())
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, ETH, 10.0)
    blocked = BoardAgent([FixedSleeve("a", enter={ETH: qty})])
    assert blocked.decide(view(buys_left_today=0)).action == HOLD               # achat bloqué : rien demandé
    assert BoardAgent([FixedSleeve("a")]).decide(view(btc_value=10.0, buys_left_today=0)).action == SELL


def test_ce_qui_manque_est_rendu_a_chaque_sous_agent_en_proportion():
    a, b = FixedSleeve("a", **{BTC: 3.0}), FixedSleeve("b", **{BTC: 1.0})
    BoardAgent([a, b]).decide(view(btc_qty=2.0, btc_price=10.0))                # 4 voulus, 2 détenus
    assert (a.qty[BTC], b.qty[BTC]) == (1.5, 0.5)
    assert (a.updates, b.updates) == (1, 1)
    lost = FixedSleeve("a", **{BTC: 1.0})
    BoardAgent([lost]).decide(view(btc_qty=0.4, btc_price=10.0))                # 4 EUR : sous l'ordre minimum, perdu
    assert lost.qty[BTC] == 0.0
    over = FixedSleeve("a", **{BTC: 1.0})
    BoardAgent([over]).decide(view(btc_qty=1.2, btc_price=10.0))                # plus que voulu : la cible ne gonfle pas
    assert over.qty[BTC] == 1.0


def test_une_cible_negative_ne_compte_pas_et_un_board_vide_est_refuse():
    agent = BoardAgent([FixedSleeve("a", **{BTC: -5.0}), FixedSleeve("b", **{BTC: 10.0 / 60_000.0})])
    assert agent.decide(view(btc_value=10.0)).action == HOLD
    with pytest.raises(ValueError):
        BoardAgent([])


# -- bornes exactes et cas tordus (issus du test par mutation et de la relecture) --------
def cheap(qty, **limits):
    """Un BTC à 10 EUR : des montants exacts en flottant, pour tomber pile sur les seuils."""
    return view(btc_qty=qty, btc_price=10.0, regime="range", **limits)


def test_les_seuils_sont_inclus_pile_sur_l_ordre_minimal():
    sell = BoardAgent([FixedSleeve("a")]).decide(cheap(0.5))                    # 5.00 EUR, personne n'en veut
    assert (sell.action, sell.amount_quote) == (SELL, 5.0)
    part = BoardAgent([FixedSleeve("a", **{BTC: 1.0})]).decide(cheap(1.5))      # 5.00 EUR de trop
    assert (part.action, part.amount_quote) == (SELL, 5.0)
    buy = BoardAgent([FixedSleeve("a", enter={BTC: 0.5})]).decide(cheap(0.0))   # il manque 5.00 EUR
    assert (buy.action, buy.amount_quote) == (BUY, 5.0)
    half = FixedSleeve("a", **{BTC: 1.0})
    BoardAgent([half]).decide(cheap(0.5))                                       # 5.00 EUR détenus sur 10 voulus
    assert half.qty[BTC] == 0.5                                                 # c'est une position : la cible suit


def test_le_seuil_se_juge_sur_la_valeur_exacte_pas_sur_la_valeur_arrondie_de_la_vue():
    almost = view(btc_qty=4.996 / 60_000.0)                                     # la vue affiche 5.00, les garde-fous refuseraient
    almost.positions[BTC]["value"] = 5.0
    watcher = FixedSleeve("a")
    assert BoardAgent([watcher]).decide(almost).action == HOLD
    assert watcher.may_enter is True                                            # pas une vente due : les entrées restent permises


def test_une_cible_servie_aux_frais_pres_reste_reclamee_meme_sous_l_ordre_minimal():
    sleeve = TrendSleeve({"qty": {BTC: 5.5 / 60_000.0}, "stops": {BTC: 51_000.0}})   # d'avant la règle du minimum
    agent = BoardAgent([sleeve])                                                # revendable : 5.50 EUR à -15 %
    got = 0.000091                                                              # 5.46 EUR obtenus (frais, arrondi)
    assert agent.decide(view(btc_qty=got, btc_price=54_000.0, exit_pct=15.0)).action == HOLD    # 4.91 EUR
    assert sleeve.wanted() == {BTC: pytest.approx(got)} and sleeve._stops[BTC] == 51_000.0
    assert agent.decide(view(btc_qty=got, btc_price=52_000.0, exit_pct=15.0)).action == HOLD
    assert sleeve._stops[BTC] == 51_000.0                                       # le niveau ne descend pas avec le prix
    back = agent.decide(view(btc_qty=got, btc_price=56_000.0, exit_pct=15.0, regime="range"))
    assert back.action == HOLD and BTC in sleeve.wanted()                       # revenue au-dessus : gardée, pas vendue


def test_une_position_perdue_le_dit_dans_la_raison_de_sa_vente():
    sleeve = TrendSleeve({"qty": {BTC: 10.0 / 60_000.0}, "stops": {BTC: 30_000.0}, "notes": {BTC: "entrée"}})
    agent = BoardAgent([sleeve])
    assert agent.decide(view(btc_value=4.0, regime="range")).action == HOLD     # 4 EUR sur 10 voulus : perdue
    sell = agent.decide(view(btc_value=6.0, regime="range"))
    assert sell.action == SELL and "perdue" in sell.reasoning


def test_un_sous_agent_qui_ne_detient_pas_le_symbole_ne_casse_pas_le_partage():
    trend = TrendSleeve()
    other = FixedSleeve("b", **{BTC: 10.0 / 60_000.0})
    assert BoardAgent([trend, other]).decide(view(btc_value=8.0, regime="range")).action == HOLD
    assert trend.wanted() == {} and other.qty[BTC] == pytest.approx(8.0 / 60_000.0)
    BoardAgent([trend, other]).decide(view(regime="range"))                     # tout a disparu : seul le détenteur le note
    assert trend.note(BTC) == ""


def test_tendance_sans_modele_de_risque_prix_nul_ou_etat_incomplet_ne_leve_rien():
    no_risk = view()
    no_risk.market[BTC]["models"].pop("risk")
    assert board().decide(no_risk).action == HOLD
    assert board().decide(view(btc_qty=0.0, btc_price=0.0)).action == HOLD
    partial = TrendSleeve({"qty": {BTC: 10.0 / 60_000.0}, "stops": {}})         # une quantité sans niveau de sortie
    bare = view(btc_value=10.0, regime="range")
    bare.market[BTC]["models"].pop("risk")
    assert BoardAgent([partial]).decide(bare).action == HOLD


def test_sans_ordre_minimal_un_montant_nul_ne_cree_ni_cible_ni_ordre():
    sleeve = TrendSleeve()
    assert BoardAgent([sleeve]).decide(view(min_order_quote=0.0, max_order_quote=0.0)).action == HOLD
    assert sleeve.wanted() == {}
    exact = BoardAgent([FixedSleeve("a", **{BTC: 1.0})])
    assert exact.decide(cheap(1.0, min_order_quote=0.0)).action == HOLD         # rien ne manque : pas d'achat de 0


def test_l_ordre_des_symboles_ne_depend_pas_de_l_ordre_du_dictionnaire():
    def reversed_view(value):
        base = view(btc_value=value, eth_value=value)
        return MarketView(timestamp=base.timestamp, quote_currency="EUR", stake=50.0, equity=50.0, cash=base.cash,
                          positions={ETH: base.positions[ETH], BTC: base.positions[BTC]}, limits=base.limits,
                          market=base.market)
    assert BoardAgent([FixedSleeve("a")]).decide(reversed_view(10.0)).symbol == BTC         # vente
    wants = FixedSleeve("a", enter={BTC: 10.0 / 60_000.0, ETH: 0.004})
    assert BoardAgent([wants]).decide(reversed_view(0.0)).symbol == BTC                     # achat


# -- l'état en base (tradeagent run) -----------------------------------------------------
def test_l_etat_survit_a_un_redemarrage_avec_ses_niveaux_de_sortie():
    storage = Storage(":memory:")
    assert "board" in AGENT_KINDS and build_agent("board", default_cfg(), storage).name == "board"
    build_agent("board", default_cfg(), storage).decide(view(size_pct=12.0))    # celui de `run` écrit bien en base
    assert storage.get(STATE_KEY)["trend"]["qty"][BTC] == pytest.approx(6.0 / 60_000.0)
    storage.delete(STATE_KEY)
    stored_board(storage).decide(view())                                        # entrée à 60 000, sortie à 57 000
    saved = storage.get(STATE_KEY)
    assert saved["trend"]["qty"][BTC] == pytest.approx(10.0 / 60_000.0) and saved["trend"]["stops"][BTC] == 57_000.0
    restarted = stored_board(storage)                                           # un autre processus, la même base
    assert restarted.decide(view(btc_value=10.0)).action == HOLD
    late = stored_board(storage)                                                # le prix est passé sous le niveau pendant l'arrêt
    sell = late.decide(view(btc_qty=10.0 / 60_000.0, btc_price=56_000.0))
    assert sell.action == SELL and "niveau de sortie" in sell.reasoning
    assert storage.get(STATE_KEY)["trend"]["qty"] == {}


def test_l_etat_est_ecrit_a_la_decision_et_un_achat_jamais_execute_est_rendu_apres_redemarrage():
    storage = Storage(":memory:")
    assert stored_board(storage).decide(view()).action == BUY                   # décidé, écrit... et le bot s'arrête là
    assert BTC in storage.get(STATE_KEY)["trend"]["qty"]
    again = stored_board(storage).decide(view(regime="range"))                  # rien n'a été acheté, plus de signal
    assert again.action == HOLD and storage.get(STATE_KEY)["trend"]["qty"] == {}


def test_l_etat_n_est_reecrit_que_s_il_change():
    writes = []

    class Spy:
        def get(self, key):
            return None

        def set(self, key, value):
            writes.append((key, json.loads(json.dumps(value))))

    agent = stored_board(Spy())
    agent.decide(view(regime="range"))
    assert writes == []                                                         # rien à retenir : rien d'écrit
    agent.decide(view())
    assert [key for key, _ in writes] == [STATE_KEY]
    agent.decide(view(btc_value=10.0))                                          # la cible suit ce qui a été obtenu : pareil ici
    agent.decide(view(btc_value=10.0))
    assert len(writes) == 1


def test_une_ecriture_ratee_est_retentee_au_cycle_suivant():
    class Flaky:
        def __init__(self):
            self.fail, self.stored = True, None

        def get(self, key):
            return None

        def set(self, key, value):
            if self.fail:
                raise RuntimeError("database is locked")
            self.stored = json.loads(json.dumps(value))

    storage = Flaky()
    agent = stored_board(storage)
    with pytest.raises(RuntimeError):
        agent.decide(view())                                                    # l'entrée est décidée, l'écriture échoue
    storage.fail = False
    agent.decide(view(btc_value=10.0))                                          # rien ne change ce cycle-ci...
    assert storage.stored["trend"]["stops"][BTC] == 57_000.0                    # ...mais la base est enfin à jour


def test_une_erreur_en_cours_de_decision_n_empeche_pas_de_garder_ce_qui_a_change():
    class Broken(TrendSleeve):
        def update(self, view, may_enter):
            super().update(view, may_enter)
            raise RuntimeError("bug")

    saved = []
    state = clean_state(None)
    agent = BoardAgent([Broken(state["trend"])], state, lambda s: saved.append(json.loads(json.dumps(s))))
    with pytest.raises(RuntimeError):
        agent.decide(view())
    assert saved and BTC in saved[-1]["trend"]["qty"]


def test_un_board_avec_un_etat_mais_sans_base_decide_sans_rien_ecrire():
    state = clean_state(None)
    agent = BoardAgent([TrendSleeve(state["trend"])], state)
    assert agent.decide(view()).action == BUY and BTC in state["trend"]["qty"]


def test_reset_efface_l_etat_du_board():
    storage = Storage(":memory:")
    stored_board(storage).decide(view())
    assert STATE_KEY in LIFE_KEYS
    reset_life(storage, 0.0)
    assert storage.get(STATE_KEY) is None and stored_board(storage).decide(view(regime="range")).action == HOLD


@pytest.mark.parametrize("damaged", [None, "texte", 3, [], {"trend": "x"}, {"trend": {"qty": [1]}}, {"autre": {}},
                                     {"trend": {"notes": [1], "stops": "x"}}])
def test_un_etat_abime_est_ecarte_sans_erreur(damaged):
    assert clean_state(damaged) == {"trend": {"qty": {}, "stops": {}, "notes": {}}}


def test_seuls_les_nombres_finis_positifs_et_les_textes_sont_gardes():
    nan, inf = float("nan"), float("inf")
    cleaned = clean_state({"trend": {
        "qty": {BTC: 0.5, ETH: nan, "A": -1, "B": 0, "C": True, "D": "2", "E": inf, 7: 1.0, "G": 10 ** 400},
        "stops": {BTC: 57_000, ETH: 2_000.0, "Z": 5.0, "F": nan},
        "notes": {BTC: "x" * 500, ETH: 3, 7: "y"},
        "inconnu": 1,
    }})["trend"]
    assert cleaned["qty"] == {BTC: 0.5}
    assert cleaned["stops"] == {BTC: 57_000.0}                                  # un niveau sans position ne sert à rien
    assert cleaned["notes"] == {BTC: "x" * 200}


def test_une_position_dont_l_etat_est_perdu_est_vendue():
    storage = Storage(":memory:")
    storage.set(STATE_KEY, {"trend": {"qty": {BTC: "abîmé"}, "stops": {BTC: 57_000.0}}})
    sell = stored_board(storage).decide(view(btc_value=10.0, regime="range"))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, BTC, 10.0)


def test_sur_le_vrai_moteur_l_etat_suit_ce_qui_a_ete_execute_et_passe_le_redemarrage():
    cfg = default_cfg()
    storage = Storage(":memory:")
    engine, feed, clock, storage = make_engine(cfg, stored_board(storage), storage=storage)
    up = {"trend": {"regime": "up", "score": 1.0}, "risk": {"exit_pct": 5.0, "size_pct": 10.0}}
    engine._market_summary = lambda now: {s: {"models": up} for s in cfg.symbols}
    engine.run_cycle()
    clock.advance(900)
    engine.run_cycle()
    held = storage.get("paper_balances")["BTC"]
    assert held > 0 and storage.get(STATE_KEY)["trend"]["qty"][BTC] == pytest.approx(held)     # la cible = l'exécuté
    engine2, _, _, _ = make_engine(cfg, stored_board(storage), clock=clock, feed=feed, storage=storage)
    engine2._market_summary = lambda now: {s: {"models": {"trend": {"regime": "down"}, "risk": up["risk"]}} for s in cfg.symbols}
    before = storage.count("fills")
    engine2.run_cycle()
    assert storage.count("fills") == before + 1 and storage.get("paper_balances").get("BTC", 0.0) == 0.0


# -- sur le vrai moteur ------------------------------------------------------------------
def _history(cfg, closes, start):
    step = 3600.0
    first = start - warmup_seconds(cfg)
    n_warm = int(warmup_seconds(cfg) // step)
    series = [closes[0]] * n_warm + list(closes)
    return [Candle(timestamp=first + i * step, open=c, high=c, low=c, close=c, volume=1.0) for i, c in enumerate(series)]


def test_le_board_a_un_seul_sous_agent_fait_comme_le_temoin_quant_sur_le_vrai_moteur():
    assert "board" in BACKTEST_AGENTS
    cfg = default_cfg(market={"candles": 744})                                  # 31 jours : de quoi calculer la tendance
    start = 1_760_000_400.0
    wobble = lambda i: 1 + 0.01 * math.sin(i / 5)  # noqa: E731
    up = [100.0 * 1.002 ** i * wobble(i) for i in range(24 * 12)]
    down = [up[-1] * 0.996 ** i * wobble(i) for i in range(1, 24 * 8)]
    closes = up + down + [down[-1] * 1.003 ** i * wobble(i) for i in range(1, 24 * 10)]
    history = {symbol: _history(cfg, closes, start) for symbol in cfg.symbols}
    end = start + (len(closes) - 1) * 3600.0
    quant = run_backtest(cfg, "quant", history, start, end)
    board_result = run_backtest(cfg, "board", history, start, end)
    assert quant.orders >= 4                                                    # sinon le test ne compare rien
    assert (board_result.orders, board_result.status) == (quant.orders, quant.status)
    assert board_result.final_equity == pytest.approx(quant.final_equity, abs=0.01)
