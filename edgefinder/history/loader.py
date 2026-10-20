"""Load/refresh historical data into the store (CLI command and nightly job)."""

from __future__ import annotations

from datetime import datetime, timezone

from edgefinder.history import mlb, nba, nfl
from edgefinder.store.db import Store

LOADERS = {"nfl": nfl.download, "nba": nba.download, "mlb": mlb.download}
# Nightly reloads only need the current season (full history is loaded once).
NIGHTLY = {
    "nfl": nfl.download,
    "nba": lambda: nba.download([nba.current_season_end()]),
    "mlb": lambda: mlb.download([mlb.datetime.now(mlb.ET).year]),
}


def load(store: Store, sport: str, nightly: bool = False) -> int:
    if sport not in LOADERS:
        raise ValueError(f"no history loader for {sport} yet")
    fn = NIGHTLY[sport] if nightly else LOADERS[sport]
    return store.upsert_history(fn(), datetime.now(timezone.utc))
