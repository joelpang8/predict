from decimal import Decimal

from edgefinder.cli import cents, market_rows
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook
from tests.test_kalshi import market


def test_cents_formatting():
    assert cents(Decimal("0.43")) == "43"
    assert cents(Decimal("0.4325")) == "43.2"
    assert cents(None) == "-"


def test_market_rows_shows_both_sides_and_depth():
    m = KalshiMarket.from_api(market())
    book = Orderbook.from_api(m.ticker, {"yes_dollars": [["0.41", "120.00"]], "no_dollars": [["0.57", "80.00"]]})
    row = market_rows([m], {m.ticker: book}).iloc[0]
    assert row["YES ask¢"] == "43" and row["NO ask¢"] == "59"
    assert row["YES ask qty"] == "80" and row["NO ask qty"] == "120"
