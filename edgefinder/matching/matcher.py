"""Match Kalshi game markets to odds-feed events. Conservative by design.

A wrong match creates a fake edge, so every check must pass or the market is
skipped and logged with a reason. Rules:

1. A Kalshi game is one event with exactly two markets, one per team. Both
   team codes must be known, and the ticker's team block must be exactly
   those two codes.
2. The game date in the ticker (e.g. 26NOV02) must equal the "originally
   scheduled for <date>" date in the market rules. Both are US/Eastern dates.
3. Odds events are candidates only if both team names map to the same two
   codes and the kickoff (`commence_time`), converted to US/Eastern, falls on
   that date.
4. Kalshi has no kickoff field; its `expected_expiration_time` was kickoff +
   3h on every NFL game checked. Each candidate's kickoff must fall within a
   configurable window before it. This is also what separates doubleheaders.
5. Exactly one candidate must survive, or the game is ambiguous and skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from edgefinder.matching.teams import is_code, team_code
from edgefinder.sources.kalshi_models import KalshiMarket
from edgefinder.sources.odds_api import OddsEvent

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_TICKER_RE = re.compile(r"^(?P<series>[A-Z0-9]+)-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<teams>[A-Z]+)$")
_RULES_DATE_RE = re.compile(r"originally scheduled for ([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


@dataclass(frozen=True)
class KalshiGame:
    sport: str
    event_ticker: str
    game_date: date  # US/Eastern
    markets: dict[str, KalshiMarket]  # team code -> market whose YES = that team wins
    expected_expiration: datetime | None


@dataclass(frozen=True)
class Match:
    game: KalshiGame
    event: OddsEvent


@dataclass(frozen=True)
class Unmatched:
    key: str  # event ticker or odds event id
    source: str  # "kalshi" | "odds"
    reason: str


@dataclass
class MatchReport:
    matches: list[Match] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    def skip(self, key: str, source: str, reason: str) -> None:
        self.unmatched.append(Unmatched(key, source, reason))
        log.info("unmatched %s %s: %s", source, key, reason)


def _parse_ticker_date(s: str) -> date | None:
    try:
        return date(2000 + int(s[:2]), _MONTHS[s[2:5]], int(s[5:7]))
    except (KeyError, ValueError):
        return None


def _parse_rules_date(rules: str) -> date | None:
    m = _RULES_DATE_RE.search(rules or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(1).upper()], int(m.group(2)))
    except (KeyError, ValueError):
        return None


def build_games(sport: str, markets: list[KalshiMarket], report: MatchReport) -> list[KalshiGame]:
    by_event: dict[str, list[KalshiMarket]] = {}
    for m in markets:
        by_event.setdefault(m.event_ticker, []).append(m)

    games = []
    for ev, ms in sorted(by_event.items()):
        tm = _TICKER_RE.match(ev)
        if not tm:
            report.skip(ev, "kalshi", "event ticker format not recognised")
            continue
        if len(ms) != 2:
            report.skip(ev, "kalshi", f"expected 2 markets, found {len(ms)}")
            continue
        codes = {}
        for m in ms:
            code = m.ticker.rsplit("-", 1)[-1]
            if not is_code(sport, code):
                report.skip(ev, "kalshi", f"unknown team code {code!r}")
                break
            if team_code(sport, m.yes_sub_title) != code:
                report.skip(ev, "kalshi", f"market {m.ticker} says {m.yes_sub_title!r}, not {code}")
                break
            codes[code] = m
        else:
            if len(codes) != 2:
                report.skip(ev, "kalshi", "both markets are for the same team")
                continue
            a, b = codes
            if tm.group("teams") not in (a + b, b + a):
                report.skip(ev, "kalshi", f"ticker teams {tm.group('teams')!r} != markets {a},{b}")
                continue
            tdate = _parse_ticker_date(tm.group("date"))
            rdates = {_parse_rules_date(m.rules_primary) for m in ms}
            if tdate is None or rdates != {tdate}:
                report.skip(ev, "kalshi", f"game date unclear: ticker {tdate}, rules {sorted(map(str, rdates))}")
                continue
            exps = {m.expected_expiration_time for m in ms}
            games.append(
                KalshiGame(sport, ev, tdate, codes, exps.pop() if len(exps) == 1 else None)
            )
    return games


def match_games(
    sport: str,
    games: list[KalshiGame],
    events: list[OddsEvent],
    report: MatchReport,
    kickoff_window: tuple[timedelta, timedelta] = (timedelta(hours=2), timedelta(hours=5)),
) -> MatchReport:
    """kickoff_window: allowed (min, max) of expected_expiration - commence_time."""
    indexed: list[tuple[frozenset[str], OddsEvent]] = []
    for e in events:
        h, a = team_code(sport, e.home_team), team_code(sport, e.away_team)
        if not h or not a:
            report.skip(e.id, "odds", f"unknown team name(s): {e.home_team!r} / {e.away_team!r}")
            continue
        indexed.append((frozenset({h, a}), e))

    picked: list[tuple[KalshiGame, OddsEvent]] = []
    for g in games:
        pair = frozenset(g.markets)
        cands = [e for p, e in indexed if p == pair and e.commence_time.astimezone(ET).date() == g.game_date]
        if g.expected_expiration is None:
            report.skip(g.event_ticker, "kalshi", "markets disagree on expected expiration; cannot place kickoff")
            continue
        lo, hi = kickoff_window
        timed = [e for e in cands if lo <= g.expected_expiration - e.commence_time <= hi]
        if not cands:
            report.skip(g.event_ticker, "kalshi", f"no odds event for {'/'.join(sorted(pair))} on {g.game_date}")
        elif len(timed) == 0:
            report.skip(
                g.event_ticker,
                "kalshi",
                f"odds kickoff(s) {[e.commence_time.isoformat() for e in cands]} not consistent with "
                f"Kalshi expected expiration {g.expected_expiration.isoformat()}",
            )
        elif len(timed) > 1:
            report.skip(g.event_ticker, "kalshi", f"ambiguous: {len(timed)} odds events fit (doubleheader?)")
        else:
            picked.append((g, timed[0]))

    claims: dict[str, int] = {}
    for _, e in picked:
        claims[e.id] = claims.get(e.id, 0) + 1
    for g, e in picked:
        if claims[e.id] > 1:
            report.skip(g.event_ticker, "kalshi", f"odds event {e.id} fits {claims[e.id]} Kalshi games")
        else:
            report.matches.append(Match(g, e))
    return report
