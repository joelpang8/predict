"""Pull and compute steps for one sport, kept separate so one source can fail
(or be served from cache) without the other."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from edgefinder.config import Settings
from edgefinder.edge import EdgeRow, FeeParams, Skip, compute_edges
from edgefinder.fees import QUADRATIC_FEE_TYPES
from edgefinder.matching.matcher import MatchReport, build_games, match_games
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook
from edgefinder.sources.odds_api import OddsApiClient, OddsEvent

log = logging.getLogger(__name__)


@dataclass
class KalshiPull:
    markets: list[KalshiMarket]
    books: dict[str, Orderbook]
    fees: dict[str, FeeParams]
    fetched_at: datetime


@dataclass
class OddsPull:
    events: list[OddsEvent]
    fetched_at: datetime
    credits_cost: int = 0
    credits_remaining: int | None = None


@dataclass
class ComputeResult:
    sport: str
    rows: list[EdgeRow] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    report: MatchReport = field(default_factory=MatchReport)
    kalshi_games: int = 0
    odds_events: int = 0


def fee_params_by_event(kalshi: KalshiClient, series_ticker: str) -> dict[str, FeeParams]:
    """Series fee type/multiplier, with any per-event override applied (official precedence)."""
    s = kalshi.get_series(series_ticker)
    base = FeeParams(s.get("fee_type", ""), Decimal(str(s.get("fee_multiplier", "1"))))
    out = {}
    for e in kalshi.iter_events(series_ticker=series_ticker, status="open"):
        if e.get("fee_type_override") and e.get("fee_multiplier_override") is not None:
            out[e["event_ticker"]] = FeeParams(e["fee_type_override"], Decimal(str(e["fee_multiplier_override"])))
        else:
            out[e["event_ticker"]] = base
    return out


def fetch_kalshi(sport: str, settings: Settings, kalshi: KalshiClient) -> KalshiPull:
    series = settings.kalshi_series.get(sport) or ()
    if not series:
        raise ValueError(f"set KALSHI_SERIES_{sport.upper()} in .env")
    markets, fees = [], {}
    for st in series:
        markets.extend(kalshi.iter_markets(series_ticker=st, status="open"))
        fees.update(fee_params_by_event(kalshi, st))
    books = kalshi.get_orderbooks([m.ticker for m in markets]) if markets else {}
    return KalshiPull(markets, books, fees, datetime.now(timezone.utc))


def fetch_odds(sport: str, odds: OddsApiClient) -> OddsPull:
    events, fetched = odds.get_moneylines(sport)
    return OddsPull(events, fetched, odds.usage.last_cost or 0, odds.usage.remaining)


def compute(sport: str, kp: KalshiPull, op: OddsPull, settings: Settings, now: datetime | None = None) -> ComputeResult:
    res = ComputeResult(sport, odds_events=len(op.events))
    games = build_games(sport, kp.markets, res.report)
    res.kalshi_games = len(games)
    match_games(sport, games, op.events, res.report)
    now = now or datetime.now(timezone.utc)
    for m in res.report.matches:
        fp = kp.fees.get(m.game.event_ticker)
        if fp is None or fp.fee_type not in QUADRATIC_FEE_TYPES:
            res.skips.append(Skip(m.game.event_ticker, f"fee schedule unknown for this event ({fp})"))
            continue
        out = compute_edges(m, fp, kp.books, kp.fetched_at, op.fetched_at, now, settings)
        if isinstance(out, Skip):
            res.skips.append(out)
        else:
            res.rows.extend(out)
    res.rows.sort(key=lambda r: r.net_edge, reverse=True)
    return res
