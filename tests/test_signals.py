from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from edgefinder.signals.trending import Candle, SignalConfig, Trade, book_imbalance, compute, price_at, trend_score
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.sources.kalshi_models import Orderbook
from tests.test_kalshi import settings as ksettings

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CFG = SignalConfig()


def candle(hours_ago, ask=None, bid=None, vol=100):
    return Candle(NOW - timedelta(hours=hours_ago), None if ask is None else D(ask), None if bid is None else D(bid), D(vol))


def series(asks, vol=100):
    """asks[i] = YES ask at i hours ago (end of that candle)."""
    return [candle(h, a, str(D(a) - D("0.01")), vol) for h, a in enumerate(asks)]


# -- candles / price history --------------------------------------------------------------


def test_candle_from_api_shape():
    c = Candle.from_api({"end_period_ts": 1792479600, "volume_fp": "6586.22", "yes_ask": {"close_dollars": "0.4100"},
                         "yes_bid": {"close_dollars": "0.4000"}, "price": {"close_dollars": None}})
    assert c.side_ask("YES") == D("0.41") and c.side_ask("NO") == D("0.60") and c.volume == D("6586.22")


def test_side_ask_ignores_empty_book_values():
    assert candle(0, "0", "0").side_ask("YES") is None
    assert candle(0, "1.00", "0").side_ask("NO") is None


def test_price_at_uses_last_candle_at_or_before_t():
    cs = series(["0.60", "0.58", "0.55"])  # now, 1h ago, 2h ago
    assert price_at(cs, "YES", NOW - timedelta(minutes=90)) == D("0.55")
    assert price_at(cs, "YES", NOW - timedelta(hours=1)) == D("0.58")
    assert price_at(cs, "YES", NOW - timedelta(hours=5)) is None


def test_price_at_prefers_own_snapshot():
    cs = series(["0.60", "0.58"])
    lookup = lambda side, t, tol: D("0.57")  # noqa: E731
    assert price_at(cs, "YES", NOW - timedelta(hours=1), lookup) == D("0.57")
    assert price_at(cs, "YES", NOW - timedelta(hours=1), lambda *a: None) == D("0.58")


def test_moves_in_points_for_both_sides():
    cs = series(["0.60"] + ["0.55"] * 30)
    s = compute("YES", D("0.60"), 0.0, cs, None, None, NOW, CFG)
    assert s.move_1h == pytest.approx(5) and s.move_24h == pytest.approx(5)
    # NO ask = 1 - YES bid: YES bid went 0.54 -> 0.59, so NO got 5 pts cheaper.
    s_no = compute("NO", D("0.41"), 0.0, cs, None, None, NOW, CFG)
    assert s_no.move_1h == pytest.approx(-5)


# -- trend score -------------------------------------------------------------------------------


def test_trend_score_spike():
    cs = [candle(h, "0.5", "0.49", 1000 if h < 3 else 100) for h in range(48)]
    # last 3h: 3000; normal 3h window: 300
    assert trend_score(cs, NOW, CFG) == pytest.approx(10.0, rel=0.05)


def test_trend_score_needs_baseline():
    assert trend_score([candle(h, "0.5", "0.49") for h in range(10)], NOW, CFG) is None
    assert trend_score([candle(h, "0.5", "0.49", 0) for h in range(48)], NOW, CFG) is None


# -- order book imbalance ---------------------------------------------------------------------


def test_imbalance_within_window_and_side_sign():
    book = Orderbook.from_api("T", {
        "yes_dollars": [["0.55", "300"], ["0.53", "100"], ["0.40", "9999"]],  # 0.40 is outside the 5c window
        "no_dollars": [["0.43", "100"]],
    })
    assert book_imbalance(book, "YES", D("0.05")) == pytest.approx((400 - 100) / 500)
    assert book_imbalance(book, "NO", D("0.05")) == pytest.approx(-(400 - 100) / 500)
    assert book_imbalance(Orderbook.from_api("T", {"yes_dollars": [], "no_dollars": []}), "YES", D("0.05")) is None


# -- trades ------------------------------------------------------------------------------------


def trade(hours_ago, count, yes_price, side):
    return Trade(NOW - timedelta(hours=hours_ago), D(count), D(yes_price), side)


def test_trade_notional_uses_the_side_bought():
    assert trade(0, 1000, "0.60", "yes").notional == D("600")
    assert trade(0, 1000, "0.60", "no").notional == D("400")
    t = Trade.from_api({"created_time": "2026-10-20T07:06:05.482271Z", "count_fp": "311.81", "yes_price_dollars": "0.6000",
                        "taker_outcome_side": "yes", "taker_side": "yes"})
    assert t.notional == D("311.81") * D("0.6") and t.taker_side == "yes"


def test_large_trades_last_24h_and_direction():
    trades = [trade(1, 10000, "0.60", "yes"), trade(2, 10000, "0.60", "no"), trade(30, 50000, "0.6", "yes"), trade(1, 10, "0.6", "yes")]
    s = compute("YES", D("0.60"), 0.0, [], None, trades, NOW, CFG)
    assert s.large_trades == 2 and s.large_trades_with == 1
    assert s.large_trade_max_usd == D("6000")
    assert any("2 large trade(s), 1 bought this side" in f for f in s.flags)


def test_trades_not_fetched_is_none_not_zero():
    assert compute("YES", D("0.6"), 0.0, [], None, None, NOW, CFG).large_trades is None


# -- flags ------------------------------------------------------------------------------------


def test_check_for_news_when_market_falls_against_our_underpriced_side():
    cs = series(["0.50", "0.57"] + ["0.57"] * 10)  # our side fell 7 pts in the last hour
    s = compute("YES", D("0.50"), gross_edge=0.06, candles=cs, book=None, trades=None, now=NOW, cfg=CFG)
    assert any(f.startswith("check for news") for f in s.flags)


def test_no_news_flag_without_positive_edge_or_small_move():
    cs = series(["0.50", "0.57"] + ["0.57"] * 10)
    assert not compute("YES", D("0.50"), -0.01, cs, None, None, NOW, CFG).flags
    small = series(["0.55", "0.57"] + ["0.57"] * 10)
    assert not any("news" in f for f in compute("YES", D("0.55"), 0.06, small, None, None, NOW, CFG).flags)


def test_confirming_needs_move_and_book_support():
    cs = series(["0.60", "0.55"] + ["0.55"] * 10)
    book_for = Orderbook.from_api("T", {"yes_dollars": [["0.59", "900"]], "no_dollars": [["0.40", "100"]]})
    book_against = Orderbook.from_api("T", {"yes_dollars": [["0.59", "100"]], "no_dollars": [["0.40", "900"]]})
    assert any("confirming" in f for f in compute("YES", D("0.60"), 0.03, cs, book_for, None, NOW, CFG).flags)
    assert not any("confirming" in f for f in compute("YES", D("0.60"), 0.03, cs, book_against, None, NOW, CFG).flags)


# -- client batching -------------------------------------------------------------------------


def test_candlesticks_chunked_under_documented_limits():
    calls = []

    def handler(req):
        ts = req.url.params["market_tickers"].split(",")
        calls.append(ts)
        assert req.url.params["period_interval"] == "60"
        return httpx.Response(200, json={"markets": [{"market_ticker": t, "candlesticks": []} for t in ts]})

    c = KalshiClient(ksettings(), transport=httpx.MockTransport(handler), sleep=lambda _: None)
    out = c.get_candlesticks([f"T{i}" for i in range(250)], 0, 72 * 3600, 60)
    assert len(out) == 250
    assert all(len(b) <= 100 and len(b) * 74 <= 10_000 for b in calls)


def test_trades_paginate_with_min_ts():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"trades": [{"trade_id": "a"}], "cursor": "c1"})
        return httpx.Response(200, json={"trades": [{"trade_id": "b"}], "cursor": ""})

    c = KalshiClient(ksettings(), transport=httpx.MockTransport(handler), sleep=lambda _: None)
    assert [t["trade_id"] for t in c.get_trades("T", 123)] == ["a", "b"]
    assert seen[0]["ticker"] == "T" and seen[0]["min_ts"] == "123"
