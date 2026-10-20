"""Data helpers for the Streamlit dashboard (kept out of the UI file so they are testable)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from edgefinder.devig import devig_two_way
from edgefinder.matching.teams import team_code
from edgefinder.store.db import Store, parse_ts

DISCLAIMER = (
    "Analysis tool only, not financial advice. No edge is guaranteed. Sportsbook odds and models can be wrong, "
    "and markets can move before you act. This app never places orders."
)


def book_comparison(store: Store, row: dict) -> list[dict]:
    """Each book's no-vig probability for this row's side, from the odds pull the row was computed from."""
    lines = store.book_lines(row["sport"], parse_ts(row["odds_fetched"]), parse_ts(row["kickoff"]))
    pair = {row["team"], row["opponent"]}
    books: dict[str, dict] = {}
    for ln in lines:
        if {team_code(row["sport"], ln["home_team"]), team_code(row["sport"], ln["away_team"])} != pair:
            continue  # another game with the same kickoff
        code = team_code(row["sport"], ln["outcome"])
        books.setdefault(ln["book"], {"update": ln["book_update"], "prices": {}})["prices"][code] = ln["price"]
    out = []
    for book, b in sorted(books.items()):
        if set(b["prices"]) != pair:
            continue
        p_team, _ = devig_two_way(b["prices"][row["team"]], b["prices"][row["opponent"]])
        p_side = p_team if row["side"] == "YES" else 1 - p_team
        out.append(
            {
                "book": book,
                "odds (team)": f"{b['prices'][row['team']]:+.0f}",
                "odds (opponent)": f"{b['prices'][row['opponent']]:+.0f}",
                "no-vig % for this side": round(p_side * 100, 1),
                "vs Kalshi ask (pts)": round((p_side - float(row["ask"])) * 100, 1),
                "book updated": b["update"],
            }
        )
    return out


def history_frame(store: Store, row: dict, candles: list[dict] | None, hours: int = 72) -> list[dict]:
    """Long-form rows (time, series, value %) for the price chart: Kalshi ask (candles + our
    snapshots), sportsbook fair and Elo per refresh."""
    from edgefinder.signals.trending import Candle

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for c in candles or []:
        cd = Candle.from_api(c)
        a = cd.side_ask(row["side"])
        if a is not None and cd.end >= since:
            out.append({"time": cd.end, "series": "Kalshi ask", "value": float(a) * 100, "source": "Kalshi hourly candle"})
    for t, a in store.ask_history(row["market_ticker"], row["side"], since):
        out.append({"time": t, "series": "Kalshi ask", "value": float(a) * 100, "source": "our snapshot"})
    for t, fair, model in store.fair_history(row["market_ticker"], row["side"], since):
        out.append({"time": t, "series": "Sportsbook fair", "value": fair * 100, "source": "de-vigged consensus"})
        if model is not None:
            out.append({"time": t, "series": "Elo model", "value": model * 100, "source": "Elo"})
    return sorted(out, key=lambda r: (r["series"], r["time"]))


def age_text(ts: str | datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return "never"
    t = parse_ts(ts) if isinstance(ts, str) else ts
    secs = ((now or datetime.now(timezone.utc)) - t).total_seconds()
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    return f"{secs / 3600:.1f} h ago"


def money(x: Decimal | float | None) -> str:
    return "-" if x is None else f"${x:,.2f}"
