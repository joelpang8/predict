"""Kalshi client and parsing tests.

Payloads below are hand-built to the shape of Kalshi's official OpenAPI spec
(kalshi_python_sync 3.33.0 models). They are NOT recorded live responses;
record real ones once the API host is reachable and add them here.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import httpx
import pytest

from edgefinder.config import Settings
from edgefinder.sources.kalshi import (
    KalshiClient,
    KalshiError,
    _Throttle,
    discover_series,
    series_matches_sport,
)
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook

BASE = "https://example.test/trade-api/v2"


def market(ticker="KXTEST-26OCT12NYGPHI-NYG", **over):
    m = {
        "ticker": ticker,
        "event_ticker": "KXTEST-26OCT12NYGPHI",
        "market_type": "binary",
        "title": "New York G at Philadelphia Winner?",
        "yes_sub_title": "New York G",
        "no_sub_title": "New York G",
        "created_time": "2026-10-05T12:00:00Z",
        "updated_time": "2026-10-05T12:00:00Z",
        "open_time": "2026-10-05T12:00:00Z",
        "close_time": "2026-10-13T03:00:00Z",
        "expected_expiration_time": "2026-10-13T03:00:00Z",
        "latest_expiration_time": "2026-10-27T00:00:00Z",
        "settlement_timer_seconds": 300,
        "status": "active",
        "yes_bid_dollars": "0.4100",
        "yes_bid_size_fp": "120.00",
        "yes_ask_dollars": "0.4300",
        "yes_ask_size_fp": "80.00",
        "no_bid_dollars": "0.5700",
        "no_ask_dollars": "0.5900",
        "last_price_dollars": "0.4200",
        "volume_fp": "15000.00",
        "volume_24h_fp": "2500.00",
        "result": "",
        "can_close_early": True,
        "open_interest_fp": "9000.00",
        "rules_primary": "If New York G wins ...",
        "rules_secondary": "",
        "price_level_structure": "linear_cent",
        "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
    }
    m.update(over)
    return m


def settings(**over) -> Settings:
    kw = dict(kalshi_base_url=BASE, kalshi_max_rps=1000, kalshi_max_retries=2)
    kw.update(over)
    return Settings(**kw)


def client_with(handler, **over) -> tuple[KalshiClient, list[float]]:
    sleeps: list[float] = []
    c = KalshiClient(settings(**over), transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return c, sleeps


# -- parsing -------------------------------------------------------------------


def test_market_parses_dollar_strings_to_decimal():
    m = KalshiMarket.from_api(market())
    assert m.yes_ask == Decimal("0.43") and m.no_ask == Decimal("0.59")
    assert m.yes_spread == Decimal("0.02")
    assert m.volume_24h == Decimal("2500")
    assert m.close_time.tzinfo is not None


def test_subpenny_prices_survive():
    m = KalshiMarket.from_api(market(yes_ask_dollars="0.432500"))
    assert m.yes_ask == Decimal("0.4325")


@pytest.mark.parametrize("bad", ["0.0000", "1.0000", "", None, "nonsense"])
def test_missing_or_boundary_ask_is_no_quote(bad):
    m = KalshiMarket.from_api(market(yes_ask_dollars=bad))
    assert m.yes_ask is None
    assert m.yes_spread is None


def test_orderbook_implied_asks_and_sorting():
    # Deliberately unsorted: we must not trust response order.
    book = Orderbook.from_api(
        "T",
        {
            "yes_dollars": [["0.39", "50.00"], ["0.41", "120.00"], ["0.40", "10.00"]],
            "no_dollars": [["0.55", "30.00"], ["0.57", "80.00"], ["0.56", "0.00"]],
        },
    )
    assert book.best_yes_bid() == (Decimal("0.41"), Decimal("120"))
    # YES ask = 1 - best NO bid, same size.
    assert book.yes_ask() == (Decimal("0.43"), Decimal("80"))
    assert book.no_ask() == (Decimal("0.59"), Decimal("120"))
    # Zero-size levels are dropped.
    assert all(size > 0 for _, size in book.no_bids)


def test_empty_orderbook():
    book = Orderbook.from_api("T", None)
    assert book.yes_ask() is None and book.no_ask() is None


# -- pagination / transport ---------------------------------------------------------


def test_markets_pagination_follows_cursor_until_empty():
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.url.params))
        cur = req.url.params.get("cursor")
        if cur is None:
            return httpx.Response(200, json={"markets": [market("A")], "cursor": "c1"})
        if cur == "c1":
            return httpx.Response(200, json={"markets": [market("B")], "cursor": ""})
        raise AssertionError(cur)

    c, _ = client_with(handler)
    got = [m.ticker for m in c.iter_markets(series_ticker="KXTEST")]
    assert got == ["A", "B"]
    assert calls[0]["limit"] == "1000" and calls[0]["series_ticker"] == "KXTEST"
    assert calls[0]["status"] == "open"


def test_repeated_cursor_raises():
    c, _ = client_with(lambda req: httpx.Response(200, json={"markets": [], "cursor": "same"}))
    with pytest.raises(KalshiError):
        list(c.iter_markets())


def test_429_retries_with_retry_after_then_succeeds():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"series": []})

    c, sleeps = client_with(handler)
    assert c.list_series() == []
    assert 3.0 in sleeps


def test_gives_up_after_max_retries():
    c, sleeps = client_with(lambda req: httpx.Response(503), kalshi_max_retries=2)
    with pytest.raises(KalshiError):
        c.list_series()
    assert len([s for s in sleeps if s >= 1]) == 2


def test_orderbooks_batches_of_100_with_repeated_tickers_param():
    batches = []

    def handler(req: httpx.Request):
        assert req.url.path.endswith("/markets/orderbooks")
        ts = req.url.params.get_list("tickers")
        batches.append(ts)
        return httpx.Response(
            200,
            json={"orderbooks": [{"ticker": t, "orderbook_fp": {"yes_dollars": [], "no_dollars": []}} for t in ts]},
        )

    c, _ = client_with(handler)
    out = c.get_orderbooks([f"T{i}" for i in range(250)])
    assert [len(b) for b in batches] == [100, 100, 50]
    assert len(out) == 250


def test_throttle_spaces_requests():
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    th = _Throttle(4, clock=lambda: t["now"], sleep=sleep)
    for _ in range(3):
        th.wait()
    assert sleeps == [0.25, 0.25]


# -- discovery --------------------------------------------------------------------


def test_series_keyword_match_uses_word_boundaries():
    assert series_matches_sport({"title": "Pro Basketball Game (NBA)"}, "nba")
    assert not series_matches_sport({"title": "WNBA Game"}, "nba")
    assert series_matches_sport({"title": "Pro Football Game", "tags": ["Football"]}, "nfl")
    assert not series_matches_sport({"title": "College Football Game"}, "nfl")


def test_discover_falls_back_to_all_series_when_category_empty():
    def handler(req):
        if req.url.params.get("category") == "Sports":
            return httpx.Response(200, json={"series": []})
        return httpx.Response(200, json={"series": [{"ticker": "X", "title": "NFL Game"}, {"ticker": "Y", "title": "CPI"}]})

    c, _ = client_with(handler)
    assert [s["ticker"] for s in discover_series(c, "nfl")] == ["X"]


# -- read-only guarantee ------------------------------------------------------------


def test_client_only_ever_sends_get():
    methods = []

    def handler(req):
        methods.append(req.method)
        return httpx.Response(200, json={"series": [], "markets": [], "cursor": "", "orderbooks": []})

    c, _ = client_with(handler)
    c.list_series()
    list(c.iter_markets())
    c.get_orderbooks(["A"])
    assert set(methods) == {"GET"}


def test_source_has_no_order_or_write_calls():
    """No module may call a write verb or touch trading/portfolio endpoints."""
    root = pathlib.Path(__file__).resolve().parents[1] / "edgefinder"
    banned_attrs = {"post", "put", "patch", "delete"}
    banned_paths = ("/portfolio", "/orders", "/order_groups", "/communications")
    for path in root.rglob("*.py"):
        src = path.read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
                raise AssertionError(f"{path}: .{node.attr} is not allowed (read-only app)")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not node.value.startswith(banned_paths), f"{path}: {node.value}"


# -- recorded live responses (tests/fixtures, recorded 2026-10-20) ------------------

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_recorded_markets_parse():
    import json

    data = json.loads((FIXTURES / "kalshi_markets_kxnflgame.json").read_text())
    markets = [KalshiMarket.from_api(m) for m in data["markets"]]
    assert markets and all(m.event_ticker.startswith("KXNFLGAME-") for m in markets)
    assert all(m.yes_ask is None or Decimal(0) < m.yes_ask < 1 for m in markets)


def test_recorded_orderbook_implied_asks_match_market_quote():
    import json

    m = KalshiMarket.from_api(json.loads((FIXTURES / "kalshi_markets_kxnflgame.json").read_text())["markets"][0])
    book = Orderbook.from_api(m.ticker, json.loads((FIXTURES / "kalshi_orderbook.json").read_text())["orderbook_fp"])
    assert book.yes_ask()[0] == m.yes_ask
    assert book.no_ask()[0] == m.no_ask
