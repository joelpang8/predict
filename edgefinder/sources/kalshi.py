"""Read-only Kalshi REST client.

READ-ONLY BY CONSTRUCTION: the only HTTP verb this module can send is GET, and
it never signs requests. Every endpoint used here is unauthenticated in
Kalshi's official OpenAPI spec (empty auth settings in kalshi_python_sync
3.33.0). There is no order, portfolio or account code, and none should be
added in v1. A test enforces this.

Endpoints and pagination (verified against the official spec, see
docs/api-verification.md):
  GET /series                        no cursor; filters: category, tags
  GET /events                        cursor; limit default 200, max 200
  GET /markets                       cursor; limit default 100, max 1000
  GET /markets/{ticker}/orderbook    depth 0 = all levels
  GET /markets/orderbooks            up to 100 tickers, repeated `tickers=`
  GET /markets/trades                cursor; limit default 100, max 1000
  GET /markets/candlesticks          <=100 tickers, <=10,000 candles per call;
                                     period_interval 1, 60 or 1440 minutes
An empty cursor in a response means no more pages.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from edgefinder.config import Settings
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook

log = logging.getLogger(__name__)

MARKETS_PAGE_MAX = 1000
EVENTS_PAGE_MAX = 200
ORDERBOOKS_BATCH_MAX = 100
CANDLE_TICKERS_MAX = 100
CANDLES_PER_CALL_MAX = 10_000
RETRY_STATUSES = {429, 500, 502, 503, 504}


class KalshiError(RuntimeError):
    pass


class _Throttle:
    """Simple minimum-interval limiter (self-imposed; see config)."""

    def __init__(self, max_rps: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class KalshiClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.settings = settings
        self._sleep = sleep
        self._throttle = _Throttle(settings.kalshi_max_rps, sleep=sleep)
        self._http = httpx.Client(
            base_url=settings.kalshi_base_url,
            timeout=settings.kalshi_timeout_s,
            headers={"Accept": "application/json", "User-Agent": "kalshi-edge-finder/0.1 (read-only)"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: Any = None) -> dict:
        attempt = 0
        while True:
            self._throttle.wait()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt >= self.settings.kalshi_max_retries:
                    raise KalshiError(f"GET {path} failed: {e}") from e
                self._backoff(attempt, None)
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.settings.kalshi_max_retries:
                log.warning("Kalshi %s on GET %s, retrying", resp.status_code, path)
                self._backoff(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise KalshiError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 2.0 * (2**attempt)
        if retry_after:
            try:
                delay = max(float(retry_after), 0.0)
            except ValueError:
                pass
        self._sleep(min(delay, 60.0))

    def _paginate(self, path: str, key: str, params: dict, page_size: int) -> Iterator[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params["limit"] = page_size
        cursor = None
        seen: set[str] = set()
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get(path, params)
            yield from data.get(key) or []
            cursor = data.get("cursor") or ""
            if not cursor:
                return
            if cursor in seen:  # defensive: never loop forever on a repeated cursor
                raise KalshiError(f"Repeated pagination cursor on {path}")
            seen.add(cursor)

    # -- endpoints ---------------------------------------------------------

    def list_series(self, category: str | None = None, tags: str | None = None) -> list[dict]:
        params = {k: v for k, v in {"category": category, "tags": tags}.items() if v}
        return self._get("/series", params).get("series") or []

    def get_series(self, series_ticker: str) -> dict:
        return self._get(f"/series/{series_ticker}").get("series") or {}

    def iter_events(self, series_ticker: str | None = None, status: str | None = None) -> Iterator[dict]:
        return self._paginate(
            "/events", "events", {"series_ticker": series_ticker, "status": status}, EVENTS_PAGE_MAX
        )

    def iter_markets(
        self, series_ticker: str | None = None, event_ticker: str | None = None, status: str | None = "open"
    ) -> Iterator[KalshiMarket]:
        params = {"series_ticker": series_ticker, "event_ticker": event_ticker, "status": status}
        for m in self._paginate("/markets", "markets", params, MARKETS_PAGE_MAX):
            yield KalshiMarket.from_api(m)

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market") or {}

    def get_orderbook(self, ticker: str, depth: int = 0) -> Orderbook:
        data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return Orderbook.from_api(ticker, data.get("orderbook_fp"))

    def get_orderbooks(self, tickers: list[str]) -> dict[str, Orderbook]:
        out: dict[str, Orderbook] = {}
        for i in range(0, len(tickers), ORDERBOOKS_BATCH_MAX):
            batch = tickers[i : i + ORDERBOOKS_BATCH_MAX]
            data = self._get("/markets/orderbooks", [("tickers", t) for t in batch])
            for ob in data.get("orderbooks") or []:
                out[ob["ticker"]] = Orderbook.from_api(ob["ticker"], ob.get("orderbook_fp"))
        return out

    def get_candlesticks(self, tickers: list[str], start_ts: int, end_ts: int, period: int = 60) -> dict[str, list[dict]]:
        """Batch candlesticks, chunked so each call stays under 100 tickers and 10,000 candles."""
        per_market = max(1, (end_ts - start_ts) // (period * 60) + 2)
        chunk = max(1, min(CANDLE_TICKERS_MAX, CANDLES_PER_CALL_MAX // per_market))
        out: dict[str, list[dict]] = {}
        for i in range(0, len(tickers), chunk):
            data = self._get(
                "/markets/candlesticks",
                {"market_tickers": ",".join(tickers[i : i + chunk]), "start_ts": start_ts, "end_ts": end_ts, "period_interval": period},
            )
            for m in data.get("markets") or []:
                out[m["market_ticker"]] = m.get("candlesticks") or []
        return out

    def get_trades(self, ticker: str, min_ts: int, max_pages: int = 5) -> list[dict]:
        trades = []
        for i, t in enumerate(self._paginate("/markets/trades", "trades", {"ticker": ticker, "min_ts": min_ts}, MARKETS_PAGE_MAX)):
            trades.append(t)
            if i + 1 >= MARKETS_PAGE_MAX * max_pages:
                break
        return trades


# Words that identify a sport in a Kalshi series title/tags. Used only to
# *suggest* series when none are configured; the user confirms them in .env.
SPORT_KEYWORDS = {
    "nfl": ("nfl", "pro football"),
    "nba": ("nba", "pro basketball"),
    "mlb": ("mlb", "pro baseball"),
}


def series_matches_sport(series: dict, sport: str) -> bool:
    hay = " ".join([series.get("title") or "", " ".join(series.get("tags") or [])]).lower()
    # Word boundaries so "nba" does not match "wnba".
    return any(re.search(rf"\b{re.escape(w)}\b", hay) for w in SPORT_KEYWORDS[sport])


def discover_series(client: KalshiClient, sport: str) -> list[dict]:
    # The category filter is exact and case-sensitive per the spec; the
    # "Sports" label itself is unverified, so fall back to all series.
    series = client.list_series(category="Sports") or client.list_series()
    return [s for s in series if series_matches_sport(s, sport)]
