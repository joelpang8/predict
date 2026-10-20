"""Optional background poller (APScheduler) that keeps data warm.

Every minute it checks each configured sport and refreshes it when due:
every `poll_interval_s` (default 5 min), or every `poll_interval_near_kickoff_s`
(default 2 min) when a game kicks off within `near_kickoff_h` hours. Each poll
stores a Kalshi snapshot (price history). Odds are pulled far less often, see
RefreshService._odds_decision.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from edgefinder.jobs.refresh import RefreshService
from edgefinder.store.db import parse_ts

log = logging.getLogger(__name__)


def interval_for(service: RefreshService, sport: str) -> int:
    s, now = service.s, service.clock()
    nk = service.store.next_kickoff(sport, now)
    if nk and nk - now <= timedelta(hours=s.near_kickoff_h):
        return s.poll_interval_near_kickoff_s
    return s.poll_interval_s


def tick(service: RefreshService) -> list[tuple[str, str]]:
    """Refresh every sport that is due. Returns (sport, status) for those refreshed."""
    done = []
    for sport, series in service.s.kalshi_series.items():
        if not series:
            continue
        last = service.store.last_run(sport)
        due = last is None or (service.clock() - parse_ts(last["started_at"])).total_seconds() >= interval_for(service, sport)
        if due:
            out = service.refresh(sport, trigger="poller")
            log.info("poller refreshed %s: %s %s", sport, out.status, out.view.warnings)
            done.append((sport, out.status))
    return done


def _add_job(sched, service: RefreshService) -> None:
    sched.add_job(tick, "interval", seconds=60, args=[service], max_instances=1, coalesce=True, id="edgefinder-poll")


def run_blocking(service: RefreshService) -> None:
    sched = BlockingScheduler(timezone="UTC")
    _add_job(sched, service)
    tick(service)
    sched.start()


def start_background(service: RefreshService) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    _add_job(sched, service)
    sched.start()
    return sched
