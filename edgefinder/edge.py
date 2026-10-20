"""Fair probability, edge, EV and size for matched Kalshi game markets.

For each team market we price both sides:
  YES (team wins)      ask = yes_ask,  fair = fair_yes
  NO  (team doesn't)   ask = no_ask,   fair = 1 - fair_yes
Implied probability is always the ask of the side being bought.

  gross edge = fair - ask
  net edge   = fair - ask - taker fee per contract   (= EV per contract, $)
  EV / $     = net edge / (ask + fee per contract)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from edgefinder.config import Settings
from edgefinder.devig import BookLine, Consensus, consensus, devig_two_way
from edgefinder.fees import taker_fee_per_contract
from edgefinder.matching.matcher import Match
from edgefinder.matching.teams import team_code
from edgefinder.sizing import suggested_contracts
from edgefinder.sources.kalshi_models import Orderbook


@dataclass(frozen=True)
class FeeParams:
    fee_type: str
    multiplier: Decimal


@dataclass(frozen=True)
class EdgeRow:
    sport: str
    event_ticker: str
    market_ticker: str
    team: str
    opponent: str
    side: str  # "YES" | "NO"
    kickoff: datetime
    ask: Decimal
    ask_depth: Decimal | None
    fair: float
    gross_edge: float
    fee_per_contract: Decimal
    net_edge: float
    ev_per_dollar: float
    contracts: Decimal
    stake: Decimal
    books: tuple[str, ...]
    book_spread: float
    kalshi_fetched: datetime
    odds_fetched: datetime

    @property
    def label(self) -> str:
        return f"{self.team} to win" if self.side == "YES" else f"{self.team} not to win ({self.opponent} or tie)"


@dataclass(frozen=True)
class Skip:
    key: str
    reason: str


def fair_yes_by_team(match: Match, settings: Settings) -> tuple[dict[str, float], Consensus] | Skip:
    """No-vig consensus probability that each team wins, as Kalshi settles it."""
    sport = match.game.sport
    a, b = sorted(match.game.markets)
    lines = []
    for bk in match.event.books:
        by_code = {team_code(sport, name): price for name, price in bk.prices.items()}
        if set(by_code) != {a, b}:
            continue  # book is missing a side, has a draw line, or a name we don't know
        try:
            pa, _ = devig_two_way(by_code[a], by_code[b])
        except ValueError:
            continue
        lines.append(BookLine(bk.book, pa, bk.last_update))
    cons = consensus(lines, settings.book_weights)
    if cons is None or len(cons.books) < settings.min_books:
        n = 0 if cons is None else len(cons.books)
        return Skip(match.game.event_ticker, f"only {n} usable books (min {settings.min_books})")
    p_a = cons.prob_a
    # Moneylines refund ties; Kalshi pays $0.50 on a tie (NFL rules_secondary).
    t = settings.nfl_tie_prob if sport == "nfl" else 0.0
    fair_a = p_a * (1 - t) + 0.5 * t
    fair_b = (1 - p_a) * (1 - t) + 0.5 * t
    return {a: fair_a, b: fair_b}, cons


def freshness_problem(kalshi_fetched: datetime, odds_fetched: datetime, now: datetime, s: Settings) -> str | None:
    if abs((kalshi_fetched - odds_fetched).total_seconds()) > s.max_skew_s:
        return f"Kalshi and odds fetched {abs((kalshi_fetched - odds_fetched).total_seconds()):.0f}s apart (max {s.max_skew_s}s)"
    oldest = min(kalshi_fetched, odds_fetched)
    if (now - oldest).total_seconds() > s.max_data_age_s:
        return f"data is {(now - oldest).total_seconds():.0f}s old (max {s.max_data_age_s}s)"
    return None


def _size_and_fee(fair: float, ask: Decimal, depth: Decimal | None, fees: FeeParams, s: Settings) -> tuple[Decimal, Decimal, Decimal]:
    """(fee per contract, contracts, stake). Fee depends on size and size on fee, so iterate."""
    kw = dict(multiplier=fees.multiplier, fee_type=fees.fee_type, precision=s.fee_precision)
    fee_pc = taker_fee_per_contract(ask, Decimal(1), **kw)  # worst case: a single contract
    n = Decimal(0)
    for _ in range(3):
        n, _stake = suggested_contracts(fair, ask + fee_pc, s.bankroll, s.kelly_multiplier, s.max_bet_pct)
        if depth is not None:
            n = min(n, depth.to_integral_value(rounding=ROUND_FLOOR))
        if n <= 0:
            break
        new_fee = taker_fee_per_contract(ask, n, **kw)
        if new_fee == fee_pc:
            break
        fee_pc = new_fee
    return fee_pc, n, n * (ask + fee_pc)


def compute_edges(
    match: Match,
    fees: FeeParams,
    books: dict[str, Orderbook],
    kalshi_fetched: datetime,
    odds_fetched: datetime,
    now: datetime,
    s: Settings,
) -> list[EdgeRow] | Skip:
    if match.event.commence_time <= now:
        return Skip(match.game.event_ticker, "game has started")
    stale = freshness_problem(kalshi_fetched, odds_fetched, now, s)
    if stale:
        return Skip(match.game.event_ticker, stale)
    fy = fair_yes_by_team(match, s)
    if isinstance(fy, Skip):
        return fy
    fair_yes, cons = fy

    rows = []
    codes = list(match.game.markets)
    for team, m in match.game.markets.items():
        opp = next(c for c in codes if c != team)
        book = books.get(m.ticker)
        for side, ask, depth, fair in (
            ("YES", m.yes_ask, book.yes_ask()[1] if book and book.yes_ask() else None, fair_yes[team]),
            ("NO", m.no_ask, book.no_ask()[1] if book and book.no_ask() else None, 1 - fair_yes[team]),
        ):
            if ask is None:
                continue
            fee_pc, n, stake = _size_and_fee(fair, ask, depth, fees, s)
            net = fair - float(ask) - float(fee_pc)
            rows.append(
                EdgeRow(
                    sport=match.game.sport,
                    event_ticker=match.game.event_ticker,
                    market_ticker=m.ticker,
                    team=team,
                    opponent=opp,
                    side=side,
                    kickoff=match.event.commence_time,
                    ask=ask,
                    ask_depth=depth,
                    fair=fair,
                    gross_edge=fair - float(ask),
                    fee_per_contract=fee_pc,
                    net_edge=net,
                    ev_per_dollar=net / float(ask + fee_pc),
                    contracts=n,
                    stake=stake,
                    books=cons.books,
                    book_spread=cons.spread,
                    kalshi_fetched=kalshi_fetched,
                    odds_fetched=odds_fetched,
                )
            )
    return rows
