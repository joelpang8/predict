"""SQLite store (plain sqlite3). One short-lived connection per operation, WAL
mode, so the Streamlit app and the background poller can share the file."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from edgefinder.edge import EdgeRow
from edgefinder.pipeline import KalshiPull, OddsPull
from edgefinder.sources.odds_api import BookOdds, OddsEvent

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")
ET = ZoneInfo("America/New_York")


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _s(x) -> str | None:
    return None if x is None else str(x)


class Store:
    def __init__(self, path: str | pathlib.Path = "data/edgefinder.sqlite"):
        self.path = str(path)
        if self.path != ":memory:":
            pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA.read_text())
            self._migrate(c)

    @staticmethod
    def _migrate(c) -> None:
        """Additive column migrations for databases created by earlier phases."""
        cols = {r[1] for r in c.execute("PRAGMA table_info(edge_rows)")}
        if "model_fair" not in cols:
            c.execute("ALTER TABLE edge_rows ADD COLUMN model_fair REAL")
            c.execute("ALTER TABLE edge_rows ADD COLUMN model_note TEXT")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            yield c
            c.commit()
        finally:
            c.close()

    # -- runs ---------------------------------------------------------------------

    def start_run(self, sport: str, trigger: str, started_at: datetime) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO refresh_runs (sport, trigger, started_at, status) VALUES (?, ?, ?, 'running')",
                (sport, trigger, iso(started_at)),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        fields["finished_at"] = iso(datetime.now(timezone.utc))
        if "warnings" in fields:
            fields["warnings"] = json.dumps(fields["warnings"])
        for k in ("kalshi_fetched_at", "odds_fetched_at", "odds_source_at"):
            if isinstance(fields.get(k), datetime):
                fields[k] = iso(fields[k])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE refresh_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def last_run(self, sport: str, edges_only: bool = False) -> dict | None:
        q = "SELECT * FROM refresh_runs WHERE sport = ?" + (" AND edges_computed = 1" if edges_only else "")
        with self.conn() as c:
            r = c.execute(q + " ORDER BY id DESC LIMIT 1", (sport,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"] or "[]")
        return d

    def credits_spent_today(self, now: datetime) -> int:
        """Odds credits spent since midnight US/Eastern, across all sports."""
        start = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(credits_cost), 0) FROM refresh_runs WHERE started_at >= ?", (iso(start),)
            ).fetchone()
        return int(r[0])

    # -- snapshots -------------------------------------------------------------------

    def save_kalshi(self, run_id: int, sport: str, kp: KalshiPull) -> None:
        rows = []
        for m in kp.markets:
            b = kp.books.get(m.ticker)
            ya = b.yes_ask() if b else None
            na = b.no_ask() if b else None
            rows.append(
                (
                    run_id, iso(kp.fetched_at), sport, m.event_ticker, m.ticker, m.status,
                    _s(m.yes_bid), _s(m.yes_ask), _s(m.no_bid), _s(m.no_ask),
                    _s(ya[1]) if ya else None, _s(na[1]) if na else None,
                    _s(m.last_price), _s(m.volume), _s(m.volume_24h), _s(m.open_interest),
                    iso(m.expected_expiration_time),
                )
            )
        with self.conn() as c:
            c.executemany(f"INSERT INTO kalshi_snapshots VALUES ({','.join('?' * 17)})", rows)

    def save_odds(self, run_id: int, sport: str, op: OddsPull) -> None:
        rows = [
            (
                run_id, iso(op.fetched_at), sport, e.id, e.sport_key, iso(e.commence_time), e.home_team,
                e.away_team, b.book, iso(b.last_update), name, price,
            )
            for e in op.events
            for b in e.books
            for name, price in b.prices.items()
        ]
        with self.conn() as c:
            c.executemany(f"INSERT INTO odds_snapshots VALUES ({','.join('?' * 12)})", rows)

    def latest_odds(self, sport: str) -> OddsPull | None:
        """Most recent odds pull for a sport, rebuilt into events (the odds cache).
        Always the newest pull, even if it returned no events: never an older one."""
        with self.conn() as c:
            r = c.execute(
                "SELECT odds_fetched_at FROM refresh_runs WHERE sport = ? AND odds_fetched_at IS NOT NULL "
                "ORDER BY odds_fetched_at DESC LIMIT 1",
                (sport,),
            ).fetchone()
            if not r:
                return None
            fetched = r[0]
            rows = c.execute("SELECT * FROM odds_snapshots WHERE sport = ? AND fetched_at = ?", (sport, fetched)).fetchall()
        events: dict[str, dict] = {}
        for x in rows:
            e = events.setdefault(x["event_id"], {"row": x, "books": {}})
            bk = e["books"].setdefault(x["book"], {"update": x["book_update"], "prices": {}})
            bk["prices"][x["outcome"]] = x["price"]
        out = [
            OddsEvent(
                eid, e["row"]["sport_key"], parse_ts(e["row"]["commence_time"]), e["row"]["home_team"], e["row"]["away_team"],
                tuple(BookOdds(k, parse_ts(v["update"]), v["prices"]) for k, v in sorted(e["books"].items())),
            )
            for eid, e in events.items()
        ]
        return OddsPull(out, parse_ts(fetched))

    def save_results(self, run_id: int, rows: list[EdgeRow], unmatched: list[tuple[str, str, str]]) -> None:
        data = [
            (
                run_id, r.sport, r.event_ticker, r.market_ticker, r.team, r.opponent, r.side, r.label, iso(r.kickoff),
                str(r.ask), _s(r.ask_depth), r.fair, r.gross_edge, str(r.fee_per_contract), r.fee.fee_type,
                str(r.fee.multiplier), r.net_edge, r.ev_per_dollar, str(r.contracts), str(r.stake), ",".join(r.books),
                r.book_spread, iso(r.kalshi_fetched), iso(r.odds_fetched), r.model_fair, r.model_note,
            )
            for r in rows
        ]
        with self.conn() as c:
            c.executemany(f"INSERT INTO edge_rows VALUES ({','.join('?' * 26)})", data)
            c.executemany("INSERT INTO unmatched VALUES (?, ?, ?, ?)", [(run_id, *u) for u in unmatched])

    def edge_rows(self, run_id: int) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM edge_rows WHERE run_id = ? ORDER BY net_edge DESC", (run_id,))]

    def unmatched(self, run_id: int) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT source, key, reason FROM unmatched WHERE run_id = ?", (run_id,))]

    def closing_ask(self, ticker: str, side: str, before: datetime) -> Decimal | None:
        col = "yes_ask" if side == "YES" else "no_ask"
        with self.conn() as c:
            r = c.execute(
                f"SELECT {col} FROM kalshi_snapshots WHERE ticker = ? AND fetched_at < ? AND {col} IS NOT NULL "
                "ORDER BY fetched_at DESC LIMIT 1",
                (ticker, iso(before)),
            ).fetchone()
        return Decimal(r[0]) if r else None

    def next_kickoff(self, sport: str, now: datetime) -> datetime | None:
        """Earliest upcoming kickoff in the most recent odds pull (drives poll frequency)."""
        with self.conn() as c:
            r = c.execute(
                "SELECT MIN(commence_time) FROM odds_snapshots WHERE sport = ? AND commence_time > ? AND fetched_at = "
                "(SELECT MAX(fetched_at) FROM odds_snapshots WHERE sport = ?)",
                (sport, iso(now), sport),
            ).fetchone()
        return parse_ts(r[0]) if r and r[0] else None

    # -- history ---------------------------------------------------------------------

    HISTORY_COLS = (
        "sport", "game_id", "season", "game_type", "week", "gameday", "kickoff_utc", "home", "away", "home_score",
        "away_score", "neutral", "home_rest", "away_rest", "home_ml", "away_ml", "spread_line", "home_starter",
        "away_starter", "loaded_at",
    )

    def upsert_history(self, games: list[dict], loaded_at: datetime) -> int:
        rows = [tuple({**g, "loaded_at": iso(loaded_at)}.get(c) for c in self.HISTORY_COLS) for g in games]
        cols = ", ".join(self.HISTORY_COLS)
        with self.conn() as c:
            c.executemany(f"INSERT OR REPLACE INTO history_games ({cols}) VALUES ({','.join('?' * len(self.HISTORY_COLS))})", rows)
        return len(rows)

    def history(self, sport: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM history_games WHERE sport = ? ORDER BY kickoff_utc, game_id", (sport,))]

    def history_loaded_at(self, sport: str) -> datetime | None:
        with self.conn() as c:
            r = c.execute("SELECT MAX(loaded_at) FROM history_games WHERE sport = ?", (sport,)).fetchone()
        return parse_ts(r[0]) if r and r[0] else None

    # -- paper bets ---------------------------------------------------------------------

    def add_paper_bet(self, **f) -> int:
        cols = ", ".join(f)
        with self.conn() as c:
            return c.execute(f"INSERT INTO paper_bets ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values())).lastrowid

    def paper_bets(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM paper_bets", ()
        if status:
            q, args = q + " WHERE status = ?", (status,)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]

    def update_paper_bet(self, bet_id: int, **f) -> None:
        cols = ", ".join(f"{k} = ?" for k in f)
        with self.conn() as c:
            c.execute(f"UPDATE paper_bets SET {cols} WHERE id = ?", (*f.values(), bet_id))
