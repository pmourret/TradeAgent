"""Rejeu de bougies : pas de vue sur le futur, historique paginé et mis en cache. Aucun accès réseau."""
from __future__ import annotations

import json

import pytest

from tradeagent.feeds import FeedError
from tradeagent.models import Candle
from tradeagent.replay import (MAX_GAP_CANDLES, ReplayPriceFeed, SimClock, check_coverage, fetch_history, load_history,
                               synthetic_history)

T0 = 1_760_000_400.0  # un multiple de 3600
HOUR = 3_600.0


def candles(closes: list[float], start: float = T0) -> list[Candle]:
    return [Candle(start + i * HOUR, c - 0.5, c + 1, c - 1, c) for i, c in enumerate(closes)]


def feed_at(t: float, closes: list[float] = (100.0, 110.0, 120.0)):
    clock = SimClock(t)
    return ReplayPriceFeed({"BTC/EUR": candles(list(closes))}, "1h", clock), clock


class FakeOhlcv:
    """Faux client ccxt : sert des bougies horaires à partir de `since`, par pages de `limit`."""

    def __init__(self, first: float, count: int, page_cap: int | None = None) -> None:
        self.rows = [[(first + i * HOUR) * 1000, 100 + i, 101 + i, 99 + i, 100 + i, 1.0] for i in range(count)]
        self.calls: list[tuple] = []
        self.page_cap = page_cap

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        size = min(limit, self.page_cap) if self.page_cap else limit
        return [r for r in self.rows if r[0] >= since][:size]


# -- pas de vue sur le futur ---------------------------------------------------
def test_le_prix_est_la_cloture_de_la_derniere_bougie_terminee():
    feed, clock = feed_at(T0 + HOUR)            # la première bougie vient de se terminer
    quote = feed.get_quote("BTC/EUR")
    assert quote.price == 100.0
    assert quote.timestamp == clock()           # prix « courant » : il passe le contrôle de fraîcheur des garde-fous
    clock.set(T0 + 2 * HOUR - 1)                # la deuxième bougie est encore en cours : on ne la voit pas
    assert feed.get_quote("BTC/EUR").price == 100.0
    clock.set(T0 + 2 * HOUR)
    assert feed.get_quote("BTC/EUR").price == 110.0


def test_une_bougie_en_cours_n_est_jamais_montree():
    feed, clock = feed_at(T0 + 2.5 * HOUR)
    seen = feed.get_candles("BTC/EUR", "1h", 48)
    assert [c.close for c in seen] == [100.0, 110.0]
    assert all(c.timestamp + HOUR <= clock() for c in seen)
    clock.set(T0 + 10 * HOUR)
    assert [c.close for c in feed.get_candles("BTC/EUR", "1h", 2)] == [110.0, 120.0]      # `limit` = les plus récentes


def test_avant_la_premiere_bougie_terminee_il_n_y_a_pas_de_prix():
    feed, _ = feed_at(T0 + HOUR - 1)
    with pytest.raises(FeedError, match="aucune bougie terminée"):
        feed.get_quote("BTC/EUR")
    with pytest.raises(FeedError, match="aucune bougie terminée"):
        feed.get_candles("BTC/EUR", "1h", 48)


def test_un_trou_dans_l_historique_refuse_de_coter():
    feed, clock = feed_at(T0 + 3 * HOUR + MAX_GAP_CANDLES * HOUR)       # pile à la limite : encore accepté
    assert feed.get_quote("BTC/EUR").price == 120.0
    clock.set(clock() + 1)
    with pytest.raises(FeedError, match="trou dans l'historique"):
        feed.get_quote("BTC/EUR")


def test_symbole_timeframe_ou_historique_invalides():
    feed, _ = feed_at(T0 + 2 * HOUR)
    with pytest.raises(FeedError, match="absent de l'historique"):
        feed.get_quote("ETH/EUR")
    with pytest.raises(FeedError, match="en 1h, pas en 5m"):
        feed.get_candles("BTC/EUR", "5m", 48)
    with pytest.raises(FeedError, match="timeframe inconnu"):
        ReplayPriceFeed({"BTC/EUR": candles([1.0])}, "2h", SimClock(T0))
    with pytest.raises(FeedError, match="historique vide"):
        ReplayPriceFeed({"BTC/EUR": []}, "1h", SimClock(T0))


def test_les_bougies_sont_triees_meme_si_l_historique_ne_l_est_pas():
    shuffled = list(reversed(candles([100.0, 110.0, 120.0])))
    feed = ReplayPriceFeed({"BTC/EUR": shuffled}, "1h", SimClock(T0 + 3 * HOUR))
    assert [c.close for c in feed.get_candles("BTC/EUR", "1h", 48)] == [100.0, 110.0, 120.0]


# -- historique synthétique ----------------------------------------------------
def test_l_historique_synthetique_est_reproductible_et_borne():
    a = synthetic_history(["BTC/EUR", "ETH/EUR"], "1h", T0, T0 + 24 * HOUR, seed=3)
    b = synthetic_history(["BTC/EUR", "ETH/EUR"], "1h", T0, T0 + 24 * HOUR, seed=3)
    c = synthetic_history(["BTC/EUR", "ETH/EUR"], "1h", T0, T0 + 24 * HOUR, seed=4)
    assert a == b and a != c
    assert len(a["BTC/EUR"]) == 24
    assert a["BTC/EUR"][0].timestamp == T0 and a["BTC/EUR"][-1].timestamp == T0 + 23 * HOUR
    assert [x.close for x in a["BTC/EUR"]] != [x.close / 24 for x in a["ETH/EUR"]]      # deux marches distinctes
    for candle in a["BTC/EUR"]:
        assert candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high


# -- téléchargement paginé -----------------------------------------------------
def test_l_historique_est_telecharge_page_par_page_sans_doublon_ni_debordement():
    client = FakeOhlcv(T0 - 5 * HOUR, 1300)
    got = fetch_history(client, "BTC/EUR", "1h", T0, T0 + 1200 * HOUR)
    assert len(got) == 1200
    assert got[0].timestamp == T0 and got[-1].timestamp == T0 + 1199 * HOUR
    assert [c.timestamp for c in got] == sorted({c.timestamp for c in got})
    assert len(client.calls) == 3                                   # 500 + 500 + 200
    assert client.calls[0][2] == int(T0 * 1000) and client.calls[1][2] == int((T0 + 500 * HOUR) * 1000)


def test_un_exchange_qui_rend_moins_que_demande_est_quand_meme_parcouru_en_entier():
    client = FakeOhlcv(T0, 300, page_cap=100)
    assert len(fetch_history(client, "BTC/EUR", "1h", T0, T0 + 300 * HOUR)) == 300
    assert len(client.calls) == 3


def test_un_exchange_qui_ne_rend_rien_ou_qui_echoue_donne_une_erreur_claire():
    with pytest.raises(FeedError, match="aucune bougie"):
        fetch_history(FakeOhlcv(T0, 0), "BTC/EUR", "1h", T0, T0 + 10 * HOUR)

    class Broken:
        def fetch_ohlcv(self, *args, **kwargs):
            raise RuntimeError("réseau coupé")

    with pytest.raises(FeedError, match="réseau coupé"):
        fetch_history(Broken(), "BTC/EUR", "1h", T0, T0 + 10 * HOUR)

    class Stuck:        # rend toujours les mêmes vieilles bougies : ne doit pas boucler
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            return [[(T0 - HOUR) * 1000, 1, 1, 1, 1, 1]]

    with pytest.raises(FeedError, match="aucune bougie"):
        fetch_history(Stuck(), "BTC/EUR", "1h", T0, T0 + 10 * HOUR)


# -- cache ---------------------------------------------------------------------
def test_le_cache_evite_de_retelecharger_et_ne_complete_que_ce_qui_manque(tmp_path):
    client = FakeOhlcv(T0, 100)
    first = load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 50 * HOUR, tmp_path)
    assert len(first["BTC/EUR"]) == 50 and len(client.calls) == 1
    assert (tmp_path / "bitvavo-BTC-EUR-1h.json").exists()

    again = load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0 + 10 * HOUR, T0 + 40 * HOUR, tmp_path)
    assert len(client.calls) == 1                                   # tout est dans le cache
    assert [c.timestamp for c in again["BTC/EUR"]] == [T0 + i * HOUR for i in range(10, 40)]

    longer = load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 80 * HOUR, tmp_path)
    assert len(longer["BTC/EUR"]) == 80 and len(client.calls) == 2
    assert client.calls[1][2] == int((T0 + 50 * HOUR) * 1000)       # seulement la suite
    load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 80 * HOUR, tmp_path)
    assert len(client.calls) == 2

    load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 80 * HOUR, tmp_path, refresh=True)
    assert len(client.calls) == 3


def test_un_cache_abime_ou_trop_court_est_retelecharge(tmp_path):
    client = FakeOhlcv(T0 - 20 * HOUR, 200)
    path = tmp_path / "bitvavo-BTC-EUR-1h.json"
    path.write_text("pas du json", encoding="utf-8")
    assert len(load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 10 * HOUR, tmp_path)["BTC/EUR"]) == 10
    assert json.loads(path.read_text(encoding="utf-8"))["start"] == T0

    earlier = load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0 - 10 * HOUR, T0 + 10 * HOUR, tmp_path)
    assert len(earlier["BTC/EUR"]) == 20                            # le cache commençait trop tard : tout est repris
    assert json.loads(path.read_text(encoding="utf-8"))["start"] == T0 - 10 * HOUR


# -- historique incomplet ------------------------------------------------------
def test_une_page_vide_n_arrete_pas_le_telechargement():
    class Sparse(FakeOhlcv):        # comme Bitvavo : chaque page est bornée à une fenêtre de temps
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            self.calls.append((symbol, timeframe, since, limit))
            window_end = since + limit * HOUR * 1000
            return [r for r in self.rows if since <= r[0] < window_end]

    client = Sparse(T0 + 600 * HOUR, 100)                           # rien dans la première fenêtre de 500 bougies
    got = fetch_history(client, "BTC/EUR", "1h", T0, T0 + 700 * HOUR)
    assert len(got) == 100 and got[0].timestamp == T0 + 600 * HOUR
    assert len(client.calls) == 2


def test_la_couverture_tolere_quelques_bougies_absentes_mais_pas_un_trou():
    full = candles([100.0] * 48)
    check_coverage("BTC/EUR", full, "1h", T0, T0 + 48 * HOUR)
    holed = full[:10] + full[10 + MAX_GAP_CANDLES:]                 # pile la tolérance
    check_coverage("BTC/EUR", holed, "1h", T0, T0 + 48 * HOUR)
    for bad, where in ((full[:10] + full[11 + MAX_GAP_CANDLES:], "milieu"), (full[MAX_GAP_CANDLES + 1:], "début"),
                       (full[:-(MAX_GAP_CANDLES + 1)], "fin"), ([], "vide")):
        with pytest.raises(FeedError, match="historique incomplet pour BTC/EUR"):
            check_coverage("BTC/EUR", bad, "1h", T0, T0 + 48 * HOUR)


def test_un_telechargement_tronque_est_refuse_et_n_est_pas_mis_en_cache(tmp_path):
    client = FakeOhlcv(T0, 30)                                      # l'exchange n'a que 30 h sur les 100 demandées
    with pytest.raises(FeedError, match="historique incomplet"):
        load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 100 * HOUR, tmp_path)
    assert not (tmp_path / "bitvavo-BTC-EUR-1h.json").exists()      # sinon il serait resservi comme complet

    load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 30 * HOUR, tmp_path)
    with pytest.raises(FeedError, match="aucune bougie"):           # le complément vide n'allonge pas le cache
        load_history(client, "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 100 * HOUR, tmp_path)
    with pytest.raises(FeedError, match="historique incomplet"):    # ni un complément qui s'arrête trop tôt
        load_history(FakeOhlcv(T0, 60), "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 100 * HOUR, tmp_path)
    assert json.loads((tmp_path / "bitvavo-BTC-EUR-1h.json").read_text(encoding="utf-8"))["end"] == T0 + 30 * HOUR


def test_un_cache_troue_est_refuse_a_la_lecture(tmp_path):
    rows = [[(T0 + i * HOUR) * 1000, 1, 1, 1, 1, 1] for i in range(100) if not 20 <= i < 40]
    (tmp_path / "bitvavo-BTC-EUR-1h.json").write_text(
        json.dumps({"start": T0, "end": T0 + 100 * HOUR, "rows": rows}), encoding="utf-8")
    with pytest.raises(FeedError, match="historique incomplet"):
        load_history(FakeOhlcv(T0, 0), "bitvavo", ["BTC/EUR"], "1h", T0, T0 + 100 * HOUR, tmp_path)
    ok = load_history(FakeOhlcv(T0, 0), "bitvavo", ["BTC/EUR"], "1h", T0 + 50 * HOUR, T0 + 100 * HOUR, tmp_path)
    assert len(ok["BTC/EUR"]) == 50                                 # la partie saine du cache reste utilisable
