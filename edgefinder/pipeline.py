"""One pull-and-compute pass for a sport. The CLI uses it now; the refresh job will in phase 3."""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from edgefinder.config import Settings
from edgefinder.edge import EdgeRow, FeeParams, Skip, compute_edges
from edgefinder.fees import QUADRATIC_FEE_TYPES
from edgefinder.matching.matcher import MatchReport, build_games, match_games
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.sources.odds_api import OddsApiClient

log = logging.getLogger(__name__)
UNMATCHED_LOG = pathlib.Path("data/unmatched.jsonl")


@dataclass
class RunResult:
    sport: str
    rows: list[EdgeRow] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    report: MatchReport = field(default_factory=MatchReport)
    kalshi_fetched: datetime | None = None
    odds_fetched: datetime | None = None
    odds_events: int = 0
    kalshi_games: int = 0
    credits: list[dict] = field(default_factory=list)


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


def run(sport: str, settings: Settings, kalshi: KalshiClient, odds: OddsApiClient) -> RunResult:
    res = RunResult(sport)
    series = settings.kalshi_series.get(sport) or ()
    if not series:
        raise ValueError(f"set KALSHI_SERIES_{sport.upper()} in .env")

    markets, fees = [], {}
    for st in series:
        markets.extend(kalshi.iter_markets(series_ticker=st, status="open"))
        fees.update(fee_params_by_event(kalshi, st))
    books = kalshi.get_orderbooks([m.ticker for m in markets])
    res.kalshi_fetched = datetime.now(timezone.utc)

    events, res.odds_fetched = odds.get_moneylines(sport)
    res.odds_events = len(events)
    res.credits = list(odds.usage.calls)

    games = build_games(sport, markets, res.report)
    res.kalshi_games = len(games)
    match_games(sport, games, events, res.report)

    now = datetime.now(timezone.utc)
    for m in res.report.matches:
        fp = fees.get(m.game.event_ticker)
        if fp is None or fp.fee_type not in QUADRATIC_FEE_TYPES:
            res.skips.append(Skip(m.game.event_ticker, f"fee schedule unknown for this event ({fp})"))
            continue
        out = compute_edges(m, fp, books, res.kalshi_fetched, res.odds_fetched, now, settings)
        if isinstance(out, Skip):
            res.skips.append(out)
        else:
            res.rows.extend(out)
    res.rows.sort(key=lambda r: r.net_edge, reverse=True)
    _log_unmatched(res)
    return res


def _log_unmatched(res: RunResult) -> None:
    if not res.report.unmatched and not res.skips:
        return
    UNMATCHED_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with UNMATCHED_LOG.open("a") as f:
        for u in res.report.unmatched:
            f.write(json.dumps({"ts": ts, "sport": res.sport, "source": u.source, "key": u.key, "reason": u.reason}) + "\n")
        for s in res.skips:
            f.write(json.dumps({"ts": ts, "sport": res.sport, "source": "edge", "key": s.key, "reason": s.reason}) + "\n")
