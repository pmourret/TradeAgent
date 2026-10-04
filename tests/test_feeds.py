import pytest

from tradeagent.feeds import CcxtPriceFeed, FeedError, SyntheticPriceFeed


class FakeCcxt:
    def __init__(self, ticker=None, error=None):
        self.ticker, self.error = ticker, error

    def fetch_ticker(self, symbol):
        if self.error:
            raise self.error
        return self.ticker


def test_ccxt_feed_returns_a_quote():
    feed = CcxtPriceFeed(client=FakeCcxt({"last": 60123.5, "timestamp": 1_760_000_000_000}))
    q = feed.get_quote("BTC/EUR")
    assert (q.symbol, q.price, q.timestamp) == ("BTC/EUR", 60123.5, 1_760_000_000.0)


def test_ccxt_feed_falls_back_to_close():
    assert CcxtPriceFeed(client=FakeCcxt({"close": 10.0, "timestamp": 1})).get_quote("X/EUR").price == 10.0


@pytest.mark.parametrize("ticker", [{}, {"last": None}, {"last": 0}, {"last": -5}, {"last": float("nan")},
                                    {"last": float("inf")}, {"last": "60000"}, {"last": True}])
def test_ccxt_feed_rejects_bad_prices(ticker):
    with pytest.raises(FeedError):
        CcxtPriceFeed(client=FakeCcxt(ticker)).get_quote("BTC/EUR")


def test_ccxt_feed_wraps_network_errors():
    with pytest.raises(FeedError, match="a échoué"):
        CcxtPriceFeed(client=FakeCcxt(error=ConnectionError("network down"))).get_quote("BTC/EUR")


def test_unknown_exchange_id():
    with pytest.raises(FeedError, match="exchange ccxt inconnu"):
        CcxtPriceFeed("not_an_exchange")


# -- paire absente de l'exchange -------------------------------------------------

class BadSymbol(Exception):
    """Même nom que l'exception de ccxt : le flux la reconnaît par son nom, sans importer ccxt."""


class FakeMarkets:
    def __init__(self, symbols=None, error=None):
        self.error = error or BadSymbol("bitvavo does not have market symbol DOGE/EUR")
        if symbols is not None:
            self.symbols = symbols

    def fetch_ticker(self, symbol):
        raise self.error

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        raise self.error


@pytest.mark.parametrize("call", [lambda f: f.get_quote("DOGE/EUR"), lambda f: f.get_candles("DOGE/EUR")])
def test_missing_pair_names_the_pair_the_exchange_and_what_exists_instead(call):
    feed = CcxtPriceFeed("bitvavo", client=FakeMarkets(["ETH/EUR", "BTC/USDT", "BTC/EUR", "ETH/BTC"]))
    with pytest.raises(FeedError) as err:
        call(feed)
    text = str(err.value)
    assert "DOGE/EUR" in text and "bitvavo" in text
    assert "BTC/EUR, ETH/EUR" in text                        # même devise de cotation, triées
    assert "BTC/USDT" not in text and "ETH/BTC" not in text
    assert "a échoué" not in text                            # ce n'est pas une panne : inutile de réessayer


def test_missing_pair_lists_at_most_ten_suggestions():
    symbols = [f"C{i:02d}/EUR" for i in range(25)]
    with pytest.raises(FeedError) as err:
        CcxtPriceFeed("bitvavo", client=FakeMarkets(symbols)).get_quote("DOGE/EUR")
    assert [s for s in symbols if s in str(err.value)] == symbols[:10]


@pytest.mark.parametrize("symbols", [None, [], ["BTC/USDT"]])
def test_missing_pair_without_any_alternative_says_so(symbols):
    with pytest.raises(FeedError, match="Aucune paire en EUR") as err:
        CcxtPriceFeed("bitvavo", client=FakeMarkets(symbols)).get_quote("DOGE/EUR")
    assert "DOGE/EUR" in str(err.value) and "bitvavo" in str(err.value)


def test_an_ordinary_error_is_not_mistaken_for_a_missing_pair():
    feed = CcxtPriceFeed("bitvavo", client=FakeMarkets(["BTC/EUR"], error=ConnectionError("network down")))
    with pytest.raises(FeedError) as err:
        feed.get_quote("BTC/EUR")
    assert str(err.value) == "fetch_ticker(BTC/EUR) a échoué : network down"
    with pytest.raises(FeedError) as err:
        feed.get_candles("BTC/EUR")
    assert str(err.value) == "fetch_ohlcv(BTC/EUR, 1h) a échoué : network down"


def test_the_real_ccxt_exception_has_the_name_the_feed_looks_for():
    import ccxt

    assert ccxt.BadSymbol.__name__ == "BadSymbol"


def test_synthetic_feed_is_reproducible_and_positive():
    a = SyntheticPriceFeed(["BTC/EUR"], seed=7, clock=lambda: 1.0)
    b = SyntheticPriceFeed(["BTC/EUR"], seed=7, clock=lambda: 1.0)
    prices = [a.get_quote("BTC/EUR").price for _ in range(200)]
    assert prices == [b.get_quote("BTC/EUR").price for _ in range(200)]
    assert all(p > 0 for p in prices)


def test_synthetic_feed_unknown_symbol():
    with pytest.raises(FeedError):
        SyntheticPriceFeed(["BTC/EUR"]).get_quote("ETH/EUR")


# -- bougies -----------------------------------------------------------------

class FakeOhlcv:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows, error

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        if self.error:
            raise self.error
        return self.rows


def test_ccxt_candles_are_parsed_and_sorted():
    rows = [[1_760_007_200_000, 3, 4, 2, 3.5, 10], [1_760_000_000_000, 1, 2, 0.5, 1.5, 7]]
    candles = CcxtPriceFeed(client=FakeOhlcv(rows)).get_candles("BTC/EUR", "1h", 2)
    assert [c.timestamp for c in candles] == [1_760_000_000.0, 1_760_007_200.0]
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close) == (1, 2, 0.5, 1.5)


def test_ccxt_candle_with_missing_volume_is_kept():
    candles = CcxtPriceFeed(client=FakeOhlcv([[1_760_000_000_000, 1, 2, 0.5, 1.5, None]])).get_candles("X/EUR")
    assert candles[0].volume == 0.0


@pytest.mark.parametrize("rows", [
    [],
    None,
    [[1, 2, 3]],                                        # trop court
    [["x", 1, 2, 0.5, 1.5, 1]],                         # horodatage invalide
    [[1_760_000_000_000, 1, 2, 0.5, float("nan"), 1]],  # NaN
    [[1_760_000_000_000, 1, 2, 0.5, -1, 1]],            # prix négatif
    [[1_760_000_000_000, 0, 2, 0.5, 1, 1]],             # prix nul
])
def test_ccxt_bad_candles_are_rejected(rows):
    with pytest.raises(FeedError):
        CcxtPriceFeed(client=FakeOhlcv(rows)).get_candles("BTC/EUR")


def test_ccxt_candle_network_error_is_wrapped():
    with pytest.raises(FeedError, match="fetch_ohlcv"):
        CcxtPriceFeed(client=FakeOhlcv(error=ConnectionError("down"))).get_candles("BTC/EUR")


def test_synthetic_candles_end_at_the_current_price_without_moving_it():
    feed = SyntheticPriceFeed(["BTC/EUR"], seed=1, clock=lambda: 1_760_000_000.0)
    price = feed.get_quote("BTC/EUR").price
    candles = feed.get_candles("BTC/EUR", "1h", 48)
    assert len(candles) == 48
    assert candles[-1].close == price
    assert candles[-1].timestamp == 1_760_000_000.0
    steps = {round(b.timestamp - a.timestamp) for a, b in zip(candles, candles[1:])}
    assert steps == {3600}
    assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) and c.low > 0 for c in candles)
    assert feed._prices["BTC/EUR"] == price


def test_synthetic_candles_unknown_symbol_or_timeframe():
    feed = SyntheticPriceFeed(["BTC/EUR"])
    with pytest.raises(FeedError):
        feed.get_candles("ETH/EUR")
    with pytest.raises(FeedError):
        feed.get_candles("BTC/EUR", "3h")
