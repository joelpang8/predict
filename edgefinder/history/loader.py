"""Load/refresh historical data into the store (CLI command and nightly job)."""

from __future__ import annotations

from datetime import datetime, timezone

from edgefinder.history import nfl
from edgefinder.store.db import Store

LOADERS = {"nfl": nfl.download}


def load(store: Store, sport: str) -> int:
    if sport not in LOADERS:
        raise ValueError(f"no history loader for {sport} yet")
    return store.upsert_history(LOADERS[sport](), datetime.now(timezone.utc))
