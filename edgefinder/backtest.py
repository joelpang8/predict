"""Backtesting: the model against history, and closing line value (CLV) for the
app's own signals and paper bets.

Model backtest (`model_report`): walk-forward Elo replay; log loss, Brier and
calibration on test seasons, plus the de-vigged closing moneyline on the same
games where the history source has one (NFL via nflverse; the NBA and MLB
sources carry no odds).

CLV (`clv_report`): for every side the app flagged (net edge >= the minimum
at some refresh) and every paper bet, compare the price we'd have paid with
the sportsbook consensus from our **last odds pull before kickoff** (the
closing line we recorded). Positive CLV = the market closed above our price,
the usual sign of a real edge. It needs our own stored odds pulls, so it
fills in as the app runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from edgefinder.config import Settings
from edgefinder.devig import BookLine, consensus, devig_two_way
from edgefinder.matching.teams import team_code
from edgefinder.models import evaluate
from edgefinder.models.service import PARAMS
from edgefinder.store.db import Store, iso, parse_ts


def model_report(store: Store, sport: str, first: int, last: int, train: tuple[int, int] | None = None):
    games = store.history(sport)
    params = PARAMS[sport]
    if train:
        params, _ = evaluate.tune(games, *train, base=params)
    return params, evaluate.evaluate(games, params, first, last)


def closing_fair(store: Store, s: Settings, sport: str, team: str, opp: str, side: str, kickoff: datetime) -> tuple[float, datetime] | None:
    """Consensus fair value for a side from the last odds pull before kickoff."""
    with store.conn() as c:
        r = c.execute(
            "SELECT MAX(fetched_at) FROM odds_snapshots WHERE sport = ? AND commence_time = ? AND fetched_at < ?",
            (sport, iso(kickoff), iso(kickoff)),
        ).fetchone()
        if not r or not r[0]:
            return None
        rows = c.execute(
            "SELECT book, book_update, outcome, price, home_team, away_team FROM odds_snapshots "
            "WHERE sport = ? AND commence_time = ? AND fetched_at = ?",
            (sport, iso(kickoff), r[0]),
        ).fetchall()
    books: dict[str, dict] = {}
    for b, upd, outcome, price, home, away in rows:
        if {team_code(sport, home), team_code(sport, away)} != {team, opp}:
            continue
        books.setdefault(b, {"u": upd, "p": {}})["p"][team_code(sport, outcome)] = price
    lines = []
    for b, d in books.items():
        if set(d["p"]) == {team, opp}:
            try:
                p_team, _ = devig_two_way(d["p"][team], d["p"][opp])
            except ValueError:
                continue
            lines.append(BookLine(b, p_team, parse_ts(d["u"])))
    cons = consensus(lines, s.book_weights)
    if cons is None or len(cons.books) < s.min_books:
        return None
    t = s.nfl_tie_prob if sport == "nfl" else 0.0
    fair_team = cons.prob_a * (1 - t) + 0.5 * t
    fair = fair_team if side == "YES" else 1 - fair_team
    return fair, parse_ts(r[0])


@dataclass
class ClvRow:
    kind: str  # "signal" | "paper"
    market_ticker: str
    side: str
    label: str
    entry_time: datetime
    entry_price: float
    entry_fair: float
    closing_fair: float
    closing_time: datetime

    @property
    def clv_pts(self) -> float:
        """Closing fair minus the price paid, in points. Positive = beat the close."""
        return (self.closing_fair - self.entry_price) * 100

    @property
    def line_move_pts(self) -> float:
        return (self.closing_fair - self.entry_fair) * 100


@dataclass
class ClvSummary:
    rows: list[ClvRow]
    pending: int  # flagged/bet but no closing line yet (game not started, or no pull before kickoff)

    def stats(self, kind: str) -> dict:
        rs = [r for r in self.rows if r.kind == kind]
        if not rs:
            return {"n": 0}
        clv = [r.clv_pts for r in rs]
        return {
            "n": len(rs),
            "avg_clv_pts": sum(clv) / len(clv),
            "beat_close_pct": sum(c > 0 for c in clv) / len(clv),
            "avg_line_move_pts": sum(r.line_move_pts for r in rs) / len(rs),
        }


def clv_report(store: Store, s: Settings, now: datetime, sport: str | None = None) -> ClvSummary:
    rows, pending = [], 0
    # Signals: the first refresh at which each side cleared the minimum net edge.
    q = (
        "SELECT e.sport, e.market_ticker, e.side, e.team, e.opponent, e.label, e.kickoff, e.ask, e.fair, e.odds_fetched "
        "FROM edge_rows e WHERE e.net_edge * 100 >= ?" + (" AND e.sport = ?" if sport else "") + " ORDER BY e.odds_fetched"
    )
    args = (s.min_net_edge_pts, sport) if sport else (s.min_net_edge_pts,)
    with store.conn() as c:
        flagged = c.execute(q, args).fetchall()
    first: dict[tuple[str, str], tuple] = {}
    for r in flagged:
        first.setdefault((r[1], r[2]), r)
    for sp, ticker, side, team, opp, label, kickoff, ask, fair, t in first.values():
        k = parse_ts(kickoff)
        close = closing_fair(store, s, sp, team, opp, side, k) if k <= now else None
        if close is None or close[1] <= parse_ts(t):
            pending += 1
            continue
        rows.append(ClvRow("signal", ticker, side, label, parse_ts(t), float(Decimal(ask)), fair, close[0], close[1]))

    for b in store.paper_bets():
        if sport and b["sport"] != sport:
            continue
        team_side = b["label"].split(" ")[0]  # label starts with the team code
        opp = _opponent(store, b)
        k = parse_ts(b["kickoff"])
        close = closing_fair(store, s, b["sport"], team_side, opp, b["side"], k) if (opp and k <= now) else None
        if close is None:
            pending += 1
            continue
        rows.append(ClvRow("paper", b["market_ticker"], b["side"], b["label"], parse_ts(b["created_at"]),
                           float(Decimal(b["price"])), b["fair_at_entry"], close[0], close[1]))
    return ClvSummary(rows, pending)


def _opponent(store: Store, bet: dict) -> str | None:
    with store.conn() as c:
        r = c.execute(
            "SELECT team, opponent FROM edge_rows WHERE market_ticker = ? AND side = ? LIMIT 1",
            (bet["market_ticker"], bet["side"]),
        ).fetchone()
    return r[1] if r else None
