"""The Refresh job: pull Kalshi + odds, recompute edges, store everything.

- Cooldown (default 20 s) per sport: a refresh inside it does nothing and
  returns the cached view with the seconds remaining. Spamming costs nothing.
- Odds credits: a daily budget (US/Eastern day). Over budget, or if the odds
  pull fails, the last stored odds pull is used, but only if it is within
  `max_skew_s` of the Kalshi pull; otherwise edges are not recomputed and the
  last good results are shown with a warning.
- A failed source never blanks the screen: the last good results stay, marked
  stale, with a visible warning.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from edgefinder.config import Settings
from edgefinder.models.service import ModelService
from edgefinder.pipeline import OddsPull, compute, fetch_kalshi, fetch_odds
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.sources.odds_api import OddsApiClient, expected_cost
from edgefinder.store.db import Store, parse_ts

log = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@dataclass
class View:
    """What the UI/CLI shows: the last computed edges plus their freshness."""

    sport: str
    rows: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    kalshi_fetched: datetime | None = None
    odds_fetched: datetime | None = None
    computed_at: datetime | None = None
    stale: bool = True
    warnings: list[str] = field(default_factory=list)
    cooldown_remaining_s: int = 0
    credits_spent_today: int = 0
    credits_budget: int = 0
    credits_remaining: int | None = None


@dataclass
class RefreshOutcome:
    status: str  # ok | partial | failed | cooldown | busy
    view: View
    credits_cost: int = 0


class RefreshService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        kalshi_factory: Callable[[], KalshiClient] | None = None,
        odds_factory: Callable[[], OddsApiClient] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.s, self.store, self.clock = settings, store, clock
        self.kalshi_factory = kalshi_factory or (lambda: KalshiClient(settings))
        self.odds_factory = odds_factory or (
            lambda: OddsApiClient(
                settings.odds_api_key, bookmakers=settings.odds_bookmakers, min_remaining=settings.odds_min_remaining
            )
        )

    # -- public ------------------------------------------------------------------------

    def cooldown_remaining(self, sport: str) -> int:
        last = self.store.last_run(sport)
        if not last:
            return 0
        elapsed = (self.clock() - parse_ts(last["started_at"])).total_seconds()
        return max(0, int(self.s.refresh_cooldown_s - elapsed + 0.999))

    def refresh(self, sport: str, trigger: str = "manual") -> RefreshOutcome:
        lock = _lock(f"{self.store.path}:{sport}")
        if not lock.acquire(blocking=False):
            return RefreshOutcome("busy", self.view(sport, ["A refresh is already running."]))
        try:
            if self.cooldown_remaining(sport) > 0:
                return RefreshOutcome("cooldown", self.view(sport))
            return self._run(sport, trigger)
        finally:
            lock.release()

    def view(self, sport: str, extra_warnings: list[str] | None = None) -> View:
        now = self.clock()
        v = View(
            sport,
            cooldown_remaining_s=self.cooldown_remaining(sport),
            credits_spent_today=self.store.credits_spent_today(now),
            credits_budget=self.s.odds_daily_credit_budget,
        )
        latest = self.store.last_run(sport)
        good = self.store.last_run(sport, edges_only=True)
        if latest:
            v.warnings.extend(latest["warnings"])
            v.credits_remaining = latest["credits_remaining"]
        if good:
            v.rows = self.store.edge_rows(good["id"])
            v.unmatched = self.store.unmatched(good["id"])
            v.kalshi_fetched = parse_ts(good["kalshi_fetched_at"])
            v.odds_fetched = parse_ts(good["odds_source_at"])
            v.computed_at = parse_ts(good["finished_at"])
            oldest = min(v.kalshi_fetched, v.odds_fetched)
            age = (now - oldest).total_seconds()
            v.stale = age > self.s.max_data_age_s
            if latest and latest["id"] != good["id"]:
                v.warnings.append(f"Showing results computed at {v.computed_at:%H:%M:%S} UTC (latest refresh could not recompute).")
            if v.stale:
                v.warnings.append(f"STALE: data is {age / 60:.0f} min old (max {self.s.max_data_age_s // 60} min). Refresh before acting.")
        elif not latest:
            v.warnings.append("No data yet. Press Refresh.")
        v.warnings.extend(extra_warnings or [])
        return v

    # -- internals --------------------------------------------------------------------

    def _odds_decision(self, sport: str, trigger: str) -> tuple[bool, str | None]:
        if not self.s.odds_api_key:
            return False, "ODDS_API_KEY is not set; odds not refreshed."
        now = self.clock()
        spent = self.store.credits_spent_today(now)
        cost = expected_cost(self.s.odds_bookmakers, ("us",))
        limit = self.s.odds_daily_credit_budget
        if trigger == "poller":
            cached = self.store.latest_odds(sport)
            if cached and (now - cached.fetched_at).total_seconds() < self.s.poller_odds_interval_s:
                return False, None  # routine: the poller keeps Kalshi warm between odds pulls
            limit -= self.s.poller_manual_reserve
        if spent + cost > limit:
            return False, f"Daily odds credit budget reached ({spent}/{self.s.odds_daily_credit_budget}); odds not refreshed."
        return True, None

    def _run(self, sport: str, trigger: str) -> RefreshOutcome:
        started = self.clock()
        run_id = self.store.start_run(sport, trigger, started)
        warnings: list[str] = []
        kp = fresh_odds = None
        credits_cost, credits_remaining = 0, None

        try:
            with self.kalshi_factory() as k:
                kp = fetch_kalshi(sport, self.s, k)
            self.store.save_kalshi(run_id, sport, kp)
        except Exception as e:  # any failure: keep going with stale data and say so
            log.exception("Kalshi pull failed")
            warnings.append(f"Kalshi pull failed: {e}")

        want, why = self._odds_decision(sport, trigger)
        if why:
            warnings.append(why)
        if want:
            try:
                with self.odds_factory() as oc:
                    fresh_odds = fetch_odds(sport, oc)
                    credits_cost = oc.usage.last_cost or 0
                    credits_remaining = oc.usage.remaining
                self.store.save_odds(run_id, sport, fresh_odds)
            except Exception as e:
                log.exception("Odds pull failed")
                warnings.append(f"Odds pull failed: {e}")

        odds: OddsPull | None = fresh_odds or self.store.latest_odds(sport)
        edges = False
        if kp and odds:
            skew = abs((kp.fetched_at - odds.fetched_at).total_seconds())
            if skew <= self.s.max_skew_s:
                res = compute(sport, kp, odds, self.s, now=self.clock(), model=ModelService(self.store))
                unmatched = [(u.source, u.key, u.reason) for u in res.report.unmatched]
                unmatched += [("edge", sk.key, sk.reason) for sk in res.skips]
                self.store.save_results(run_id, res.rows, unmatched)
                edges = True
            else:
                warnings.append(
                    f"Latest odds are {skew / 60:.0f} min older than Kalshi prices (max {self.s.max_skew_s}s apart); "
                    "edges not recomputed."
                )
        elif kp and not odds:
            warnings.append("No odds data available; edges not computed.")

        failed = [w for w in warnings if "failed" in w]
        status = "failed" if kp is None and fresh_odds is None else ("partial" if failed else "ok")
        self.store.finish_run(
            run_id,
            status=status,
            kalshi_fetched_at=kp.fetched_at if kp else None,
            odds_fetched_at=fresh_odds.fetched_at if fresh_odds else None,
            odds_source_at=odds.fetched_at if (edges and odds) else None,
            edges_computed=int(edges),
            credits_cost=credits_cost,
            credits_remaining=credits_remaining,
            warnings=warnings,
        )
        return RefreshOutcome(status, self.view(sport), credits_cost)
