"""Refresh job, cooldown/caching, credit budget, stale handling, poller and paper ledger.

Kalshi and The Odds API are served by in-process fakes (httpx.MockTransport)
built from the same shapes as the recorded/official responses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import httpx
import pytest

from edgefinder.config import Settings
from edgefinder.jobs import poller
from edgefinder.jobs.refresh import RefreshService
from edgefinder.paper import ledger
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.sources.odds_api import OddsApiClient
from edgefinder.store.db import Store, iso
from tests.test_kalshi import market as raw_market

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


class Fakes:
    """Fake Kalshi + odds servers for one NE @ CHI game two days out."""

    def __init__(self):
        self.kick = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
        d = self.kick.astimezone(ET)
        self.event = f"KXNFLGAME-{d:%y}{d.strftime('%b').upper()}{d:%d}NECHI"
        self.calls = {"kalshi": 0, "odds": 0}
        self.kalshi_down = self.odds_down = False
        self.settled = {}  # ticker -> market overrides for GET /markets/{ticker}

    def market(self, code, title, yes, no):
        d = self.kick.astimezone(ET)
        return raw_market(
            f"{self.event}-{code}",
            event_ticker=self.event,
            yes_sub_title=title,
            yes_ask_dollars=yes,
            no_ask_dollars=no,
            expected_expiration_time=iso(self.kick + timedelta(hours=3)),
            rules_primary=f"If {title} wins the New England vs Chicago Pro Football game originally scheduled for "
            f"{d:%b} {d.day}, {d.year}, then the market resolves to Yes.",
        )

    def kalshi(self, req: httpx.Request) -> httpx.Response:
        self.calls["kalshi"] += 1
        if self.kalshi_down:
            return httpx.Response(503)
        path = req.url.path.split("/trade-api/v2")[-1]
        markets = [self.market("CHI", "Chicago", "0.50", "0.52"), self.market("NE", "New England", "0.48", "0.52")]
        if path == "/series/KXNFLGAME":
            return httpx.Response(200, json={"series": {"ticker": "KXNFLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}})
        if path == "/events":
            return httpx.Response(200, json={"events": [{"event_ticker": self.event}], "cursor": ""})
        if path == "/markets":
            return httpx.Response(200, json={"markets": markets, "cursor": ""})
        if path == "/markets/orderbooks":
            obs = [{"ticker": t, "orderbook_fp": {"yes_dollars": [["0.47", "100"]], "no_dollars": [["0.50", "500"]]}} for t in req.url.params.get_list("tickers")]
            return httpx.Response(200, json={"orderbooks": obs})
        if path.startswith("/markets/"):
            t = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"market": {**raw_market(t), **self.settled.get(t, {})}})
        return httpx.Response(404)

    def odds(self, req: httpx.Request) -> httpx.Response:
        self.calls["odds"] += 1
        if self.odds_down:
            return httpx.Response(500, text="boom")
        books = [
            {"key": k, "last_update": iso(datetime.now(UTC)), "markets": [{"key": "h2h", "outcomes": [
                {"name": "Chicago Bears", "price": -150}, {"name": "New England Patriots", "price": 130}]}]}
            for k in ("pinnacle", "draftkings", "fanduel")
        ]
        ev = {"id": "ev1", "sport_key": "americanfootball_nfl", "commence_time": iso(self.kick),
              "home_team": "Chicago Bears", "away_team": "New England Patriots", "bookmakers": books}
        return httpx.Response(200, json=[ev], headers={"x-requests-last": "1", "x-requests-used": "1", "x-requests-remaining": "499"})


@pytest.fixture
def env(tmp_path):
    f = Fakes()
    s = Settings(
        kalshi_base_url="https://kalshi.test/trade-api/v2",
        kalshi_max_rps=1000,
        kalshi_series={"nfl": ("KXNFLGAME",), "nba": (), "mlb": ()},
        odds_api_key="k",
        odds_bookmakers=("pinnacle", "draftkings", "fanduel"),
        db_path=str(tmp_path / "t.sqlite"),
    )
    store = Store(s.db_path)
    offset = {"t": timedelta(0)}

    def make(settings=s):
        return RefreshService(
            settings,
            store,
            kalshi_factory=lambda: KalshiClient(settings, transport=httpx.MockTransport(f.kalshi), sleep=lambda _: None),
            odds_factory=lambda: OddsApiClient("k", bookmakers=settings.odds_bookmakers, transport=httpx.MockTransport(f.odds)),
            clock=lambda: datetime.now(UTC) + offset["t"],
        )

    return f, s, store, make, offset


def age_everything(store: Store, by: timedelta) -> None:
    """Shift stored timestamps into the past, as if time had passed."""
    with store.conn() as c:
        for table, cols in {
            "refresh_runs": ("started_at", "finished_at", "kalshi_fetched_at", "odds_fetched_at", "odds_source_at"),
            "kalshi_snapshots": ("fetched_at",),
            "odds_snapshots": ("fetched_at",),
            "edge_rows": ("kalshi_fetched", "odds_fetched"),
        }.items():
            for col in cols:
                for rowid, v in c.execute(f"SELECT rowid, {col} FROM {table}").fetchall():
                    if v:
                        c.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (iso(datetime.fromisoformat(v) - by), rowid))


# -- refresh / cooldown / caching ------------------------------------------------------


def test_refresh_computes_stores_and_stamps_every_row(env):
    f, s, store, make, _ = env
    out = make().refresh("nfl")
    assert out.status == "ok" and out.credits_cost == 1
    v = out.view
    assert len(v.rows) == 4 and not v.stale
    assert all(r["kalshi_fetched"] and r["odds_fetched"] for r in v.rows)
    assert v.credits_spent_today == 1 and v.credits_remaining == 499
    with store.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 6


def test_cooldown_returns_cache_without_any_api_call(env):
    f, s, store, make, _ = env
    svc = make()
    svc.refresh("nfl")
    before = dict(f.calls)
    for _ in range(5):
        out = svc.refresh("nfl")
        assert out.status == "cooldown"
        assert 0 < out.view.cooldown_remaining_s <= 20
        assert len(out.view.rows) == 4
    assert f.calls == before
    assert store.credits_spent_today(datetime.now(UTC)) == 1


def test_refresh_allowed_again_after_cooldown(env):
    f, s, store, make, offset = env
    make().refresh("nfl")
    offset["t"] = timedelta(seconds=21)
    out = make().refresh("nfl")
    assert out.status == "ok" and f.calls["odds"] == 2


def test_daily_budget_stops_odds_but_uses_cached_odds_within_skew(env):
    f, s, store, make, offset = env
    tight = replace(s, odds_daily_credit_budget=1)
    make(tight).refresh("nfl")
    offset["t"] = timedelta(seconds=21)
    out = make(tight).refresh("nfl")
    assert f.calls["odds"] == 1
    assert out.status == "ok" and len(out.view.rows) == 4
    assert any("budget reached" in w for w in out.view.warnings)


def test_odds_failure_keeps_last_good_rows_with_warning(env):
    f, s, store, make, offset = env
    make().refresh("nfl")
    age_everything(store, timedelta(minutes=10))
    f.odds_down = True
    out = make().refresh("nfl")
    v = out.view
    assert out.status == "partial"
    assert any("Odds pull failed" in w for w in v.warnings)
    assert any("edges not recomputed" in w for w in v.warnings)
    assert any("Showing results computed at" in w for w in v.warnings)
    assert len(v.rows) == 4 and v.stale  # old rows stay visible, marked stale
    assert any(w.startswith("STALE") for w in v.warnings)


def test_kalshi_failure_does_not_compute_from_mismatched_data(env):
    f, s, store, make, offset = env
    make().refresh("nfl")
    age_everything(store, timedelta(minutes=10))
    f.kalshi_down = True
    out = make().refresh("nfl")
    assert out.status == "partial"
    assert any("Kalshi pull failed" in w for w in out.view.warnings)
    assert store.last_run("nfl")["edges_computed"] == 0
    assert out.view.stale


def test_both_sources_down_is_failed(env):
    f, s, store, make, _ = env
    f.kalshi_down = f.odds_down = True
    out = make().refresh("nfl")
    assert out.status == "failed" and not out.view.rows


def test_latest_odds_is_newest_pull_even_if_empty(env):
    f, s, store, make, offset = env
    make().refresh("nfl")
    f.odds = lambda req: httpx.Response(200, json=[], headers={"x-requests-last": "0", "x-requests-remaining": "499"})
    offset["t"] = timedelta(seconds=21)
    make().refresh("nfl")
    assert store.latest_odds("nfl").events == []


def test_missing_key_never_calls_odds(env):
    f, s, store, make, _ = env
    out = make(replace(s, odds_api_key="")).refresh("nfl")
    assert f.calls["odds"] == 0
    assert any("ODDS_API_KEY" in w for w in out.view.warnings)


# -- poller -----------------------------------------------------------------------------


def test_poller_refreshes_when_due_and_not_otherwise(env):
    f, s, store, make, offset = env
    svc = make()
    assert poller.tick(svc) == [("nfl", "ok")]
    assert poller.tick(svc) == []  # just ran
    offset["t"] = timedelta(seconds=s.poll_interval_s + 1)
    assert poller.tick(svc) == [("nfl", "ok")]


def test_poller_uses_fast_interval_near_kickoff(env):
    f, s, store, make, offset = env
    svc = make()
    svc.refresh("nfl")
    assert poller.interval_for(svc, "nfl") == s.poll_interval_s
    offset["t"] = f.kick - datetime.now(UTC) - timedelta(hours=1)
    assert poller.interval_for(svc, "nfl") == s.poll_interval_near_kickoff_s


def test_poller_does_not_pull_odds_between_odds_intervals(env):
    f, s, store, make, offset = env
    make().refresh("nfl")
    offset["t"] = timedelta(seconds=s.poll_interval_s + 1)
    make().refresh("nfl", trigger="poller")
    assert f.calls["odds"] == 1  # Kalshi refreshed, odds left alone


def test_poller_keeps_manual_reserve(env):
    f, s, store, make, offset = env
    tight = replace(s, odds_daily_credit_budget=6, poller_manual_reserve=5, poller_odds_interval_s=0)
    make(tight).refresh("nfl", trigger="poller")  # spends 1 of the 1 the poller may use
    offset["t"] = timedelta(seconds=400)
    make(tight).refresh("nfl", trigger="poller")
    assert f.calls["odds"] == 1
    offset["t"] = timedelta(seconds=800)
    make(tight).refresh("nfl", trigger="manual")
    assert f.calls["odds"] == 2


# -- paper trading ----------------------------------------------------------------------


def chi_ticker(f):
    return f"{f.event}-CHI"


def test_log_paper_bet_uses_latest_ask_and_size(env):
    f, s, store, make, _ = env
    make().refresh("nfl")
    bet_id = ledger.log_bet(store, s, "nfl", chi_ticker(f), "YES", datetime.now(UTC))
    b = store.paper_bets()[0]
    assert b["id"] == bet_id and b["price"] == "0.50" and D(b["contracts"]) > 0
    assert b["label"] == "CHI to win" and b["status"] == "open"


def test_log_paper_bet_explicit_contracts_recomputes_fee(env):
    f, s, store, make, _ = env
    make().refresh("nfl")
    ledger.log_bet(store, s, "nfl", chi_ticker(f), "NO", datetime.now(UTC), contracts=D(100))
    b = store.paper_bets()[0]
    # NO ask is $0.52: 0.07 * 100 * 0.52 * 0.48 = 1.7472 -> rounded up to $1.75 for 100 contracts
    assert b["price"] == "0.52"
    assert D(b["fee_per_contract"]) == D("1.75") / 100


def test_paper_bet_refusals(env):
    f, s, store, make, _ = env
    with pytest.raises(ledger.PaperError, match="refresh first"):
        ledger.log_bet(store, s, "nfl", chi_ticker(f), "YES", datetime.now(UTC))
    make().refresh("nfl")
    with pytest.raises(ledger.PaperError, match="not in the latest"):
        ledger.log_bet(store, s, "nfl", "KXNFLGAME-NOPE-X", "YES", datetime.now(UTC))
    with pytest.raises(ledger.PaperError, match="size is 0"):
        ledger.log_bet(store, s, "nfl", chi_ticker(f), "NO", datetime.now(UTC))  # negative edge side
    with pytest.raises(ledger.PaperError, match="stale"):
        ledger.log_bet(store, s, "nfl", chi_ticker(f), "YES", datetime.now(UTC) + timedelta(hours=1))


@pytest.mark.parametrize(
    "market,side,payout",
    [
        ({"status": "finalized", "result": "yes"}, "YES", D(1)),
        ({"status": "finalized", "result": "no"}, "YES", D(0)),
        ({"status": "finalized", "result": "no"}, "NO", D(1)),
        ({"status": "finalized", "result": "scalar", "settlement_value_dollars": "0.5000"}, "NO", D("0.5")),
    ],
)
def test_settlement_payouts(env, market, side, payout):
    f, s, store, make, _ = env
    make().refresh("nfl")
    ledger.log_bet(store, s, "nfl", chi_ticker(f), side, datetime.now(UTC), contracts=D(10))
    f.settled[chi_ticker(f)] = market
    with KalshiClient(s, transport=httpx.MockTransport(f.kalshi)) as k:
        done = ledger.settle(store, k, datetime.now(UTC))
    b = store.paper_bets()[0]
    assert len(done) == 1 and b["status"] == "settled"
    assert D(b["payout_per_contract"]) == payout
    assert D(b["pnl"]) == 10 * (payout - D(b["price"]) - D(b["fee_per_contract"]))


@pytest.mark.parametrize("status", ["active", "closed", "determined", "disputed"])
def test_not_settled_until_finalized(env, status):
    f, s, store, make, _ = env
    make().refresh("nfl")
    ledger.log_bet(store, s, "nfl", chi_ticker(f), "YES", datetime.now(UTC))
    f.settled[chi_ticker(f)] = {"status": status, "result": "yes"}
    with KalshiClient(s, transport=httpx.MockTransport(f.kalshi)) as k:
        assert ledger.settle(store, k, datetime.now(UTC)) == []
    assert store.paper_bets()[0]["status"] == "open"


def test_stats_and_clv(env):
    f, s, store, make, _ = env
    make().refresh("nfl")
    ledger.log_bet(store, s, "nfl", chi_ticker(f), "YES", datetime.now(UTC), contracts=D(10))
    f.settled[chi_ticker(f)] = {"status": "finalized", "result": "yes"}
    with KalshiClient(s, transport=httpx.MockTransport(f.kalshi)) as k:
        ledger.settle(store, k, datetime.now(UTC))
    st = ledger.stats(store.paper_bets())
    assert (st.bets, st.settled, st.wins) == (1, 1, 1) and st.hit_rate == 1.0
    assert st.pnl == D(10) * (1 - D("0.50") - D(store.paper_bets()[0]["fee_per_contract"]))
    # Closing ask = last stored snapshot before kickoff = 0.50, same as entry -> CLV 0
    assert st.clv_count == 1 and st.avg_clv_cents == pytest.approx(0.0)
