"""Superviseur : le LLM ne donne qu'une directive bornée, le code trade, et sortir reste toujours possible."""
from __future__ import annotations

import json

import pytest

from helpers import START, FakeClock, default_cfg
from tradeagent import app, cli
from tradeagent.agents import MarketView
from tradeagent.backtest import BACKTEST_AGENTS, DEFAULT_AGENTS, PAID_AGENTS, run_backtest, warmup_seconds
from tradeagent.budget import InferenceBudget
from tradeagent.config import ConfigError
from tradeagent.context import ContextData, LiveContext
from tradeagent.llm import LLMError, LLMReply, LLMUsage
from tradeagent.llm_cache import SpendCapReached
from tradeagent.models import BUY, HOLD, SELL
from tradeagent.replay import synthetic_history
from tradeagent.storage import Storage
from tradeagent.strategies import QuantAgent
from tradeagent.supervisor import (DIRECTIVE_KEY, DIRECTIVE_SCHEMA, ESTIMATE_REPLY, STANCES, STATE_KEY, SYSTEM_PROMPT,
                                   InvalidDirective, SupervisedAgent, parse_directive)

HOUR, DAY = 3_600, 86_400
SYMBOLS = ("BTC/EUR", "ETH/EUR")
LIMITS = {"max_order_quote": 40.0, "max_position_quote_per_symbol": 60.0, "max_total_exposure_quote": 80.0,
          "min_order_quote": 5.0, "buys_left_today": 10, "buys_blocked_below_equity": 90.0}


class StubClient:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, max_output_tokens):
        self.calls.append((system, user, max_output_tokens))
        item = self.replies.pop(0) if self.replies else directive("normal")
        if isinstance(item, Exception):
            raise item
        return item


def directive(stance, symbols=None, reasoning="because"):
    text = json.dumps({"stance": stance, "symbols": symbols, "reasoning": reasoning})
    return LLMReply(text, LLMUsage(1200, 60), "stub")


def view(now=START, btc=0.0, eth=0.0, btc_price=60_000.0, regime="up", eth_regime="range", tier="normal",
         exit_pct=5.0, size_pct=10.0, market=True, **limits):
    models = lambda r: {"trend": {"regime": r, "score": 1.0}, "risk": {"exit_pct": exit_pct, "size_pct": size_pct}}  # noqa: E731
    return MarketView(
        timestamp=now, quote_currency="EUR", stake=100.0, equity=100.0, cash=100.0 - btc - eth,
        positions={"BTC/EUR": {"quantity": btc / btc_price, "price": btc_price, "value": btc},
                   "ETH/EUR": {"quantity": eth / 2_500.0, "price": 2_500.0, "value": eth}},
        limits={**LIMITS, **limits}, risk_tier=tier, candle_timeframe="1h",
        market={"BTC/EUR": {"last": btc_price, "models": models(regime), "closes": [1.0, 2.0]},
                "ETH/EUR": {"last": 2_500.0, "models": models(eth_regime)}} if market else {},
    )


def make(replies=(), storage=None, context=None, **llm):
    cfg = default_cfg(stake=100.0, llm={"call_every_seconds": 3600, "daily_budget_eur": 1.0, "total_budget_eur": 5.0, **llm})
    clock = FakeClock()
    storage = storage or Storage(":memory:")
    client = StubClient(replies)
    agent = SupervisedAgent(cfg, client, InferenceBudget(cfg.llm, storage, clock), storage, clock, context)
    return agent, client, clock, storage


def step(agent, clock, seconds=300, **kwargs):
    clock.advance(seconds)
    return agent.decide(view(now=clock(), **kwargs))


# -- la directive : bornée, validée ---------------------------------------------------------------------------------
def test_une_directive_est_une_posture_connue_et_des_symboles_connus():
    assert parse_directive('{"stance": "offensive", "symbols": null, "reasoning": "ok"}', SYMBOLS) == {
        "stance": "offensive", "symbols": None, "reasoning": "ok"}
    fenced = parse_directive('```json\n{"stance": "defensive", "symbols": ["ETH/EUR", "DOGE/EUR"], "reasoning": 3}\n```', SYMBOLS)
    assert fenced == {"stance": "defensive", "symbols": ["ETH/EUR"], "reasoning": ""}        # symbole inconnu écarté
    assert parse_directive('{"stance": "normal", "symbols": ["ETH/EUR", "BTC/EUR"]}', SYMBOLS)["symbols"] is None
    assert len(parse_directive(json.dumps({"stance": "pause", "symbols": None, "reasoning": "x" * 900}), SYMBOLS)["reasoning"]) == 300
    assert list(STANCES.items()) == [("offensive", 2.0), ("normal", 1.0), ("defensive", 0.5), ("pause", 0.0)]
    assert DIRECTIVE_SCHEMA["properties"]["stance"]["enum"] == list(STANCES) and DIRECTIVE_SCHEMA["additionalProperties"] is False
    assert parse_directive(ESTIMATE_REPLY, SYMBOLS)["stance"] == "normal"


@pytest.mark.parametrize("text", [
    "je pense qu'il faut être prudent", "[1, 2]", '{"stance": "all-in", "symbols": null}', '{"symbols": null}',
    '{"stance": 2, "symbols": null}', '{"stance": "normal", "symbols": []}', '{"stance": "normal", "symbols": ["DOGE/EUR"]}',
    '{"stance": "normal", "symbols": "BTC/EUR"}', '{"stance": "normal", "symbols": 3}', "",
])
def test_tout_le_reste_est_refuse(text):
    with pytest.raises(InvalidDirective):
        parse_directive(text, SYMBOLS)


def test_le_prompt_systeme_dit_ce_que_fait_chaque_posture_et_se_mefie_des_donnees():
    for kept in ("You place no orders", '"offensive": position sizes x2', '"defensive": position sizes x0.5',
                 '"pause": sell everything', "pays fees", "counts as failure", "fear_greed", "funding", "untrusted"):
        assert kept in SYSTEM_PROMPT, kept
    assert len(SYSTEM_PROMPT) < 3_400                                           # il se paie à chaque appel


# -- le cœur trade, la directive règle la taille ---------------------------------------------------------------------
def test_en_posture_normale_le_superviseur_trade_comme_le_temoin():
    agent, client, clock, storage = make()
    buy = agent.decide(view())
    assert (buy.action, buy.symbol, buy.amount_quote) == (BUY, "BTC/EUR", 10.0)           # 10 % de 100
    assert len(client.calls) == 1 and storage.get(DIRECTIVE_KEY)["stance"] == "normal"
    assert buy.reasoning == QuantAgent().decide(view()).reasoning


@pytest.mark.parametrize("stance,amount", [("offensive", 20.0), ("normal", 10.0), ("defensive", 5.0)])
def test_la_posture_multiplie_la_taille_des_achats(stance, amount):
    agent, *_ = make([directive(stance)])
    buy = agent.decide(view())
    assert (buy.action, buy.amount_quote) == (BUY, amount)


def test_une_taille_doublee_reste_sous_la_marge_des_garde_fous():
    agent, *_ = make([directive("offensive")])
    assert agent.decide(view(size_pct=30.0)).amount_quote == 40.0               # 60 demandés, 40 par ordre au plus
    small, *_ = make([directive("defensive")])
    assert small.decide(view(size_pct=8.0)).action == HOLD                     # 4 EUR : sous l'ordre minimum


def test_en_pause_tout_est_vendu_et_rien_n_est_achete():
    agent, client, clock, _ = make([directive("pause")])
    sell = agent.decide(view(btc=10.0, eth=8.0))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, "BTC/EUR", 10.0) and "pause" in sell.reasoning
    sell = step(agent, clock, eth=8.0)
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, "ETH/EUR", 8.0)
    idle = step(agent, clock, eth_regime="up")
    assert idle.action == HOLD and not idle.skipped and len(client.calls) == 1


def test_un_symbole_ecarte_est_vendu_et_n_est_plus_achete():
    agent, client, clock, storage = make([directive("normal", ["ETH/EUR"])])
    sell = agent.decide(view(btc=10.0, eth_regime="up"))
    assert (sell.action, sell.symbol, sell.amount_quote) == (SELL, "BTC/EUR", 10.0) and "écarté" in sell.reasoning
    buy = step(agent, clock, eth_regime="up")
    assert (buy.action, buy.symbol) == (BUY, "ETH/EUR")                         # le BTC, à la hausse lui aussi, est ignoré
    assert storage.get(DIRECTIVE_KEY)["symbols"] == ["ETH/EUR"]
    changes = [e["message"] for e in storage.recent_events(9) if "posture" in e["message"]]
    assert changes == ["superviseur : posture normal -> normal, symboles : ETH/EUR"]      # journalisé, sans texte du LLM


def test_un_changement_de_posture_retaille_la_position_une_seule_fois_quand_l_ordre_passe():
    agent, client, clock, storage = make([directive("normal"), directive("defensive"), directive("offensive")])
    assert agent.decide(view()).amount_quote == 10.0
    assert step(agent, clock, btc=10.0).action == HOLD                          # déjà à sa taille
    trim = step(agent, clock, DAY, btc=10.0)                                    # le lendemain : défensif
    assert (trim.action, trim.symbol, trim.amount_quote) == (SELL, "BTC/EUR", 5.0) and "on allège" in trim.reasoning
    assert step(agent, clock, btc=5.0).action == HOLD                           # pas de nouvelle vente au cycle suivant
    assert step(agent, clock, btc=5.0).action == HOLD
    grow = step(agent, clock, DAY, btc=5.0)                                     # puis offensif : de x0,5 à x2
    assert (grow.action, grow.symbol, grow.amount_quote) == (BUY, "BTC/EUR", 15.0) and "on renforce" in grow.reasoning
    assert step(agent, clock, btc=20.0).action == HOLD
    state = storage.get(STATE_KEY)
    assert state["units"] == {"BTC/EUR": pytest.approx(10.0 / 60_000.0)} and state["pending"] == {}


def test_un_allegement_qui_n_a_pas_eu_lieu_est_retente():
    agent, client, clock, _ = make([directive("normal"), directive("defensive")])
    agent.decide(view())
    step(agent, clock, btc=10.0)
    assert step(agent, clock, DAY, btc=10.0).amount_quote == 5.0
    again = step(agent, clock, btc=10.0)                                        # l'ordre a raté : la position n'a pas bougé
    assert (again.action, again.amount_quote) == (SELL, 5.0)
    assert step(agent, clock, btc=5.0).action == HOLD


def test_un_achat_reduit_par_les_garde_fous_n_est_pas_complete_a_chaque_cycle():
    agent, client, clock, storage = make([directive("offensive")])
    assert agent.decide(view()).amount_quote == 20.0
    assert step(agent, clock, btc=12.0).action == HOLD                          # 12 exécutés sur 20 : c'est sa taille, comme le témoin
    assert storage.get(STATE_KEY)["units"] == {"BTC/EUR": pytest.approx(6.0 / 60_000.0)}


def test_un_renforcement_reste_dans_la_marge_des_garde_fous():
    agent, client, clock, _ = make([directive("normal"), directive("offensive")])
    agent.decide(view())
    step(agent, clock, btc=10.0)
    grow = step(agent, clock, DAY, btc=10.0, max_order_quote=6.0)
    assert (grow.action, grow.amount_quote) == (BUY, 6.0)                       # 10 manquent, 6 par ordre au plus
    assert step(agent, clock, btc=10.0, buys_left_today=0).action == HOLD       # achats bloqués : pas de renforcement


def test_une_position_rouverte_repart_d_une_taille_de_base_neuve():
    agent, client, clock, storage = make([directive("normal"), directive("offensive")])
    agent.decide(view())
    step(agent, clock, btc=10.0)
    assert step(agent, clock, btc=10.0, regime="down").action == SELL
    step(agent, clock, regime="range")                                          # position fermée : tout est oublié
    assert storage.get(STATE_KEY) == {"stops": {}, "units": {}, "pending": {}}
    assert step(agent, clock, DAY).amount_quote == 20.0                         # rachetée en posture offensive
    assert step(agent, clock, btc=20.0).action == HOLD                          # 20 est sa taille : ni renfort ni allègement


def test_on_ne_renforce_pas_une_position_dont_la_tendance_ne_tient_plus():
    agent, client, clock, _ = make([directive("normal"), directive("offensive")])
    agent.decide(view())
    assert step(agent, clock, DAY, btc=10.0, regime="range").action == HOLD
    grow = step(agent, clock, btc=10.0, regime="up")                            # la tendance revient : on complète
    assert (grow.action, grow.amount_quote) == (BUY, 10.0)


def test_une_part_trop_petite_n_est_pas_vendue():
    agent, client, clock, storage = make([directive("normal"), directive("defensive"), directive("normal"),
                                          directive("offensive")])
    agent.decide(view(size_pct=8.0))                                            # position de 8
    assert step(agent, clock, DAY, btc=8.0).action == HOLD                      # 4 à vendre : sous l'ordre minimum
    assert step(agent, clock, btc=8.0).action == HOLD
    # Rien n'a été vendu, donc rien n'est racheté au retour : la taille suit ce qui est réellement détenu.
    assert step(agent, clock, DAY, btc=8.0).action == HOLD
    grow = step(agent, clock, DAY, btc=8.0)
    assert (grow.action, grow.amount_quote) == (BUY, 8.0)                       # x2 de la taille de base, pas x4


# -- sortir reste toujours possible ----------------------------------------------------------------------------------
def test_un_appel_rate_ne_coute_pas_le_cycle_le_coeur_sort_quand_meme():
    agent, client, clock, storage = make([directive("normal"), LLMError("503 de l'API")])
    agent.decide(view())                                                        # achat à 60 000 : sortie à 57 000
    hit = step(agent, clock, DAY, btc=10.0, btc_price=56_000.0)
    assert (hit.action, hit.symbol, hit.amount_quote) == (SELL, "BTC/EUR", 10.0) and "niveau de sortie" in hit.reasoning
    assert len(client.calls) == 2
    event = storage.recent_events(1)[0]
    assert event["level"] == "error" and "appel raté" in event["message"] and "posture en cours" in event["message"]
    assert storage.count("llm_calls") == 1                                      # l'appel raté n'est pas compté comme payé


def test_une_reponse_invalide_est_payee_journalisee_et_ne_change_rien():
    agent, client, clock, storage = make([directive("offensive"), LLMReply("soyons prudents", LLMUsage(1000, 50), "stub")])
    agent.decide(view())
    held = step(agent, clock, DAY, btc=20.0)
    assert held.action == HOLD and storage.get(DIRECTIVE_KEY)["stance"] == "offensive"     # la directive en cours tient
    assert storage.count("llm_calls") == 2
    assert "réponse invalide" in storage.recent_events(1)[0]["message"]
    for _ in range(11):
        step(agent, clock, 2 * HOUR, btc=20.0)
    assert len(client.calls) == 2                                               # payée : pas de nouvel essai avant le lendemain
    step(agent, clock, 2 * HOUR, btc=20.0)
    assert len(client.calls) == 3


def test_le_texte_du_llm_est_borne_dans_le_journal():
    agent, client, clock, storage = make([LLMReply(json.dumps({"stance": "x" * 900, "symbols": None}), LLMUsage(10, 10), "stub"),
                                          LLMReply(json.dumps({"stance": "normal", "symbols": ["y" * 900]}), LLMUsage(10, 10), "stub")])
    agent.decide(view())
    step(agent, clock, DAY)
    assert all(len(e["message"]) < 300 for e in storage.recent_events(5))


def test_un_plafond_de_depense_atteint_ne_bloque_pas_le_coeur():
    agent, client, clock, _ = make([SpendCapReached("plafond de dépense réelle atteint")])
    assert agent.decide(view(btc=10.0, regime="down")).action == SELL


def test_sans_donnees_de_marche_le_superviseur_n_est_pas_appele_mais_le_niveau_de_sortie_joue():
    agent, client, clock, _ = make([directive("normal")])
    agent.decide(view())
    clock.advance(DAY)
    hit = agent.decide(view(now=clock(), btc=10.0, btc_price=56_000.0, market=False))
    assert hit.action == SELL and len(client.calls) == 1


def test_meme_un_bug_du_superviseur_n_empeche_pas_de_sortir():
    agent, client, clock, storage = make([RuntimeError("bug")])
    assert agent.decide(view(btc=10.0, regime="down")).action == SELL
    event = storage.recent_events(1)[0]
    assert event["level"] == "error" and "erreur inattendue (RuntimeError: bug)" in event["message"]


@pytest.mark.parametrize("key,value", [
    ("sup_directive", {"stance": "pause", "set_at": "hier"}), ("sup_directive", {"stance": "pause"}),
    ("sup_directive", {"stance": ["pause"], "set_at": START}), ("sup_directive", "pause"),
    ("sup_directive", {"stance": "normal", "set_at": START, "symbols": [["BTC/EUR"]]}),
    ("sup_directive", {"stance": "normal", "set_at": START, "symbols": ["DOGE/EUR"]}),
    ("sup_directive", {"stance": "normal", "set_at": START, "symbols": []}),
    ("sup_wake", {"at": "demain"}), ("sup_wake", {"at": START + DAY, "tier": "normal", "prices": 3}),
    ("sup_wake", {"at": START + DAY, "tier": "normal", "prices": {"BTC/EUR": "cher"}}), ("sup_wake", [1]),
    ("sup_state", {"stops": "abîmé", "units": None}), ("sup_state", {"stops": {"BTC/EUR": "bas"}, "units": {"BTC/EUR": []}}),
    ("sup_state", 3),
])
def test_un_etat_abime_en_base_ne_bloque_jamais_les_sorties(key, value):
    agent, client, clock, storage = make()
    storage.set("llm_last_call", START - 600)                                   # et aucun appel n'est dû à ce cycle
    storage.set(key, value)
    for _ in range(3):
        sell = step(agent, clock, btc=10.0, regime="down")
        assert (sell.action, sell.amount_quote) == (SELL, 10.0)
    assert step(agent, clock, HOUR).action == BUY                               # et le superviseur repart : posture normale
    if key == "sup_directive":
        assert sum("illisible" in e["message"] for e in storage.recent_events(9)) == 1


# -- cadence, budget, vieillissement ---------------------------------------------------------------------------------
def test_l_horodatage_est_ecrit_avant_l_appel():
    agent, client, clock, storage = make()
    seen = []
    client.complete = lambda *args: seen.append(storage.get("llm_last_call")) or directive("normal")
    agent.decide(view())
    assert seen == [START]


def test_le_superviseur_est_rappele_une_fois_par_jour_ou_sur_un_mouvement_de_prix():
    agent, client, clock, _ = make()
    agent.decide(view())
    for _ in range(12):
        step(agent, clock, 2 * HOUR - 300, btc=10.0)
    assert len(client.calls) == 1                                               # presque 24 h sans évènement : aucun appel
    step(agent, clock, 3_600, btc=10.0)
    assert len(client.calls) == 2                                               # le délai est passé
    step(agent, clock, 2 * HOUR, btc=10.0, btc_price=61_700.0)                  # +2,8 % : sous le seuil de 3 %
    assert len(client.calls) == 2
    step(agent, clock, 300, btc=10.0, btc_price=61_800.0)                       # +3 %
    assert len(client.calls) == 3
    step(agent, clock, 600, btc=10.0, btc_price=58_000.0)                       # -6 %, mais moins d'une heure après
    assert len(client.calls) == 3
    step(agent, clock, 3_000, btc=10.0, btc_price=58_000.0)
    assert len(client.calls) == 4


def test_un_changement_de_palier_rappelle_le_superviseur_et_le_palier_economie_allonge_l_intervalle():
    agent, client, clock, _ = make()
    agent.decide(view())
    step(agent, clock, HOUR, tier="cautious")                                   # palier changé, mais intervalle x2 en prudent
    assert len(client.calls) == 1
    step(agent, clock, HOUR, tier="cautious")
    assert len(client.calls) == 2
    assert agent.min_interval("defensive") == 4 * HOUR and agent.min_interval("normal") == HOUR
    capped, *_ = make(max_call_interval_seconds=2 * HOUR + 60)
    assert capped.min_interval("defensive") == 2 * HOUR + 60                    # jamais au-delà de l'intervalle maximal


def test_apres_un_echec_pas_de_rafale_d_appels():
    agent, client, clock, _ = make([LLMError("panne")] * 5)
    agent.decide(view())
    for _ in range(11):
        step(agent, clock, 300)
    assert len(client.calls) == 1                                               # 55 minutes : pas de nouvel essai
    step(agent, clock, 300)
    assert len(client.calls) == 2


def test_budget_epuise_pas_d_appel_et_le_coeur_continue():
    agent, client, clock, storage = make(daily_budget_eur=0.001, total_budget_eur=0.001)
    agent.decide(view())                                                        # ce premier appel épuise le budget
    buy = step(agent, clock, 2 * DAY, eth_regime="up", btc=10.0)
    assert len(client.calls) == 1 and (buy.action, buy.symbol) == (BUY, "ETH/EUR")


def test_une_directive_non_renouvelee_finit_par_tomber():
    agent, client, clock, storage = make([directive("offensive")], daily_budget_eur=0.001, total_budget_eur=0.001)
    assert agent.decide(view()).amount_quote == 20.0
    assert step(agent, clock, 2 * DAY - 300, btc=20.0).action == HOLD           # encore valable
    back = step(agent, clock, 600, btc=20.0)                                    # 48 h passées sans renouvellement
    assert (back.action, back.amount_quote) == (SELL, 10.0)                     # retour à la taille normale
    assert storage.get(DIRECTIVE_KEY) is None
    events = [e["message"] for e in storage.recent_events(5)]
    assert sum("périmée" in m for m in events) == 1
    step(agent, clock, 300, btc=10.0)
    assert sum("périmée" in e["message"] for e in storage.recent_events(9)) == 1      # signalé une fois


def test_une_pause_non_renouvelee_ne_dure_pas_toujours():
    agent, client, clock, _ = make([directive("pause")], daily_budget_eur=0.001, total_budget_eur=0.001)
    assert agent.decide(view()).action == HOLD
    assert step(agent, clock, 2 * DAY + 300).action == BUY


# -- ce que le superviseur voit --------------------------------------------------------------------------------------
class Source:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.asked = value, error, []

    def context(self, now):
        self.asked.append(now)
        if self.error:
            raise self.error
        return self.value


def sent(client, index=-1):
    return json.loads(client.calls[index][1].split("\n", 1)[1])


def test_le_prompt_donne_le_climat_les_flux_et_la_directive_en_cours():
    source = Source({"fear_greed": {"value": 20, "label": "extreme_fear"}})
    agent, client, clock, storage = make([directive("defensive", ["BTC/EUR"])], context=source)
    agent.decide(view(btc=10.0))
    data = sent(client)
    assert data["context"] == {"fear_greed": {"value": 20, "label": "extreme_fear"}} and source.asked == [START]
    assert data["directive"] is None and data["invested_pct"] == 10 and data["risk_tier"] == "normal"
    assert data["positions"] == {"BTC/EUR": {"value": 10.0}}                    # l'ETH, non détenu, n'y est pas
    assert data["market"]["BTC/EUR"] == {"last": 60_000.0, "models": {"trend": {"regime": "up", "score": 1.0},
                                                                     "risk": {"exit_pct": 5.0, "size_pct": 10.0}}}
    assert data["costs"] == {"round_trip_pct": pytest.approx(0.6, abs=0.5)}
    assert client.calls[0][0] == SYSTEM_PROMPT and client.calls[0][2] == 400
    step(agent, clock, DAY, btc=5.0)
    again = sent(client)
    assert again["directive"] == {"stance": "defensive", "symbols": ["BTC/EUR"], "age_hours": 24}
    assert again["api_cost_so_far"] > 0 and again["api_cost_per_call"] > 0
    assert again["positions"]["BTC/EUR"]["exit_distance_pct"] == 5.0
    for secret in ("limits", "max_order", "api_key", "killswitch"):
        assert secret not in client.calls[-1][1]                                # ni garde-fous, ni clé


def test_un_flux_en_panne_n_empeche_pas_la_directive():
    agent, client, *_ = make([directive("offensive")], context=Source(error=RuntimeError("panne")))
    assert agent.decide(view()).amount_quote == 20.0
    assert sent(client)["context"] == {}
    bare, client, *_ = make()
    bare.decide(view())
    assert sent(client)["context"] == {}


# -- l'état vit en base ----------------------------------------------------------------------------------------------
def test_les_niveaux_de_sortie_et_la_directive_survivent_a_un_redemarrage():
    agent, client, clock, storage = make([directive("offensive")])
    agent.decide(view())
    assert storage.get(STATE_KEY) == {"stops": {"BTC/EUR": 57_000.0}, "units": {}, "pending": {"BTC/EUR": 2.0}}
    clock.advance(HOUR + 60)
    cfg = default_cfg(stake=100.0)
    reborn = SupervisedAgent(cfg, StubClient([LLMError("panne")]), InferenceBudget(cfg.llm, storage, clock), storage, clock)
    assert reborn.decide(view(now=clock(), btc=20.0, btc_price=57_500.0)).action == HOLD
    clock.advance(300)
    hit = reborn.decide(view(now=clock(), btc=20.0, btc_price=57_000.0))
    assert hit.action == SELL and "niveau de sortie" in hit.reasoning
    assert storage.get(STATE_KEY)["units"] == {"BTC/EUR": pytest.approx(10.0 / 57_500.0)}      # 20 détenus à x2


def test_un_reset_efface_la_directive_et_l_etat_mais_pas_la_cadence():
    agent, client, clock, storage = make([directive("pause")])
    agent.decide(view(btc=10.0))
    assert {"sup_directive", "sup_wake", "sup_state"} <= set(app.LIFE_KEYS) and "llm_last_call" not in app.LIFE_KEYS
    app.reset_life(storage, clock())
    assert storage.get(DIRECTIVE_KEY) is None and storage.get("sup_wake") is None and storage.get(STATE_KEY) is None
    assert storage.get("llm_last_call") == START


# -- branchements ----------------------------------------------------------------------------------------------------
def test_le_bot_construit_le_superviseur_avec_son_format_et_ses_flux(monkeypatch):
    built = []
    monkeypatch.setattr(app, "AnthropicClient", lambda model, **kwargs: built.append(kwargs) or object())
    agent = app.build_agent("supervisor", default_cfg(), Storage(":memory:"))
    assert built == [{"output_schema": DIRECTIVE_SCHEMA}]
    assert isinstance(agent, SupervisedAgent) and isinstance(agent._context, LiveContext)
    assert agent._context._bases == ("BTC", "ETH") and agent.name == "supervisor"
    assert "supervisor" in app.AGENT_KINDS and app.PAID_KINDS == ("llm", "supervisor")


def test_le_superviseur_est_un_agent_payant_du_backtest_jamais_par_defaut():
    cfg = default_cfg(stake=50.0)
    T0 = 1_759_276_800.0
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + DAY, seed=1)
    with pytest.raises(ConfigError, match="argent réel"):
        run_backtest(cfg, "supervisor", history, T0, T0 + DAY)
    assert "supervisor" in BACKTEST_AGENTS and PAID_AGENTS == ("llm", "supervisor")
    assert not set(PAID_AGENTS) & set(DEFAULT_AGENTS)


def test_en_backtest_un_superviseur_toujours_normal_fait_les_memes_ordres_que_le_temoin():
    cfg = default_cfg(stake=50.0, market={"candles": 744}, guardrails={"max_order_pct": 40, "max_position_pct": 60})
    T0 = 1_759_276_800.0
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 20 * DAY, seed=4, volatility=0.012)
    client = StubClient()
    source = Source({"fear_greed": {"value": 50, "label": "neutral"}})
    supervised = run_backtest(cfg, "supervisor", history, T0, T0 + 20 * DAY, llm_client=client, context=source)
    quant = run_backtest(cfg, "quant", history, T0, T0 + 20 * DAY)
    assert quant.orders > 0 and supervised.orders == quant.orders
    assert supervised.final_equity == pytest.approx(quant.final_equity)
    assert supervised.api_cost > 0 and supervised.net_result == pytest.approx(quant.net_result - supervised.api_cost)
    assert 20 <= len(client.calls) <= 20 * 24 and len(source.asked) == len(client.calls)
    assert supervised.status == "alive"


def test_en_backtest_une_posture_offensive_passe_par_les_garde_fous():
    cfg = default_cfg(stake=50.0, market={"candles": 744})                      # garde-fous par défaut
    T0 = 1_759_276_800.0
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 20 * DAY, seed=4, volatility=0.012)
    client = StubClient([directive("offensive")] * 500)
    result = run_backtest(cfg, "supervisor", history, T0, T0 + 20 * DAY, llm_client=client)
    assert result.status == "alive" and result.orders > 0


# -- ligne de commande -----------------------------------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\nstake: 50\n", encoding="utf-8")
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: pytest.fail("vrai client Anthropic construit dans un test"))
    monkeypatch.setattr(cli, "load_context_history", lambda *a, **k: pytest.fail("flux lus sur le réseau dans un test"))
    return tmp_path


ARGS = ["backtest", "--synthetic", "--days", "2", "--agents", "quant,supervisor", "--end", "2025-10-01"]


def test_la_commande_rejoue_le_superviseur_sous_plafond_et_relit_le_cache(workdir, monkeypatch, capsys):
    assert cli.main(ARGS) == 2
    assert "--max-api-eur" in capsys.readouterr().err
    assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 2                      # pas de clé : refus avant toute question
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    schemas, client = [], StubClient()
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: schemas.append(kwargs) or client)
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0
    out = capsys.readouterr().out
    assert schemas == [{"output_schema": DIRECTIVE_SCHEMA}]
    assert "supervisor" in out and "quant" in out and f"{len(client.calls)} appels payés" in out
    assert 2 <= len(client.calls) <= 48 and "refusés" not in out
    paid = len(client.calls)
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0             # même backtest : tout est en cache
    assert len(client.calls) == paid and "0 appels payés" in capsys.readouterr().out


def test_un_appel_refuse_par_un_plafond_est_compte(tmp_path):
    from tradeagent.llm_cache import CachingLLMClient, ReplyCache

    client = CachingLLMClient(lambda: pytest.fail("appel payant"), ReplyCache(tmp_path / "cache.jsonl"), "m",
                              lambda usage: 1.0, run_cap_eur=0.5, total_cap_eur=10.0)
    for _ in range(2):
        with pytest.raises(SpendCapReached):
            client.complete("s", "u", 100)
    assert client.refused_calls == 2
    total = CachingLLMClient(lambda: pytest.fail("appel payant"), ReplyCache(tmp_path / "cache.jsonl"), "m",
                             lambda usage: 1.0, run_cap_eur=5.0, total_cap_eur=0.5)
    with pytest.raises(SpendCapReached):
        total.complete("s", "u", 100)
    assert total.refused_calls == 1


def test_la_commande_refuse_deux_agents_payants_a_la_fois(workdir, capsys):
    assert cli.main(["backtest", "--synthetic", "--agents", "llm,supervisor", "--max-api-eur", "0.5"]) == 2
    assert "un seul agent payant par backtest" in capsys.readouterr().err
    assert not (workdir / "data").exists()


def test_la_commande_lit_les_flux_du_superviseur_et_les_cite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    (tmp_path / "config.yaml").write_text("database: data/agent.db\nstake: 50\n", encoding="utf-8")
    asked = []

    def flows(cache_dir, bases, start, end, refresh=False):
        asked.append((str(cache_dir).replace("\\", "/"), list(bases), end - start, refresh))
        return ContextData([(end - 20 * DAY + i * DAY, 20.0 + i) for i in range(21)]), ["indice Fear & Greed (source : alternative.me)"]

    client = StubClient()
    monkeypatch.setattr(cli, "load_context_history", flows)
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: client)
    history = lambda cfg_client, exchange, symbols, timeframe, start, end, cache_dir, refresh=False: synthetic_history(  # noqa: E731
        symbols, timeframe, start, end, seed=3)
    monkeypatch.setattr(cli, "load_history", history)
    monkeypatch.setattr(cli, "public_client", lambda exchange: object())
    assert cli.main(["backtest", "--days", "2", "--agents", "supervisor", "--end", "2025-10-01", "--max-api-eur", "0.5", "--yes"]) == 0
    out = capsys.readouterr().out
    assert asked == [("data/history", ["BTC", "ETH"], 2 * DAY, False)]
    assert "flux du superviseur : indice Fear & Greed (source : alternative.me)" in out
    assert "fear_greed" in sent(client)["context"]                              # le flux rejoué arrive dans le prompt
