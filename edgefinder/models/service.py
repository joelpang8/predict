"""Model probabilities for live Kalshi games, from the stored history.

A Kalshi game is tied to a schedule row with the same discipline as the odds
matcher: same two teams (Kalshi codes) and same US/Eastern date, exactly one
row, or no model estimate (with a reason).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from edgefinder.matching.matcher import KalshiGame
from edgefinder.models.elo import EloParams, EloState, predict, run
from edgefinder.store.db import Store


@dataclass(frozen=True)
class ModelProb:
    probs: dict[str, float]  # team code -> P(win), ties counted half (= Kalshi YES value)
    game_id: str
    week: int | None
    note: str  # e.g. starters, early-season warning


# Fit 2015-2021, out-of-sample 2022-2025 log loss 0.633 (market 0.608). See docs/model.md.
NFL_PARAMS = EloParams(k=20.0, hfa=35.0, bye_bonus=25.0, season_regress=0.4, playoff_mult=1.2)
PARAMS = {"nfl": NFL_PARAMS}


class ModelService:
    _cache: dict[tuple[str, str], tuple[datetime | None, EloState, list[dict]]] = {}
    _lock = threading.Lock()

    def __init__(self, store: Store):
        self.store = store

    def _state(self, sport: str) -> tuple[EloState, list[dict]] | None:
        loaded = self.store.history_loaded_at(sport)
        if loaded is None or sport not in PARAMS:
            return None
        key = (self.store.path, sport)
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] == loaded:
                return hit[1], hit[2]
            games = self.store.history(sport)
            state = run(games, PARAMS[sport])
            self._cache[key] = (loaded, state, games)
            return state, games

    def probs(self, games: list[KalshiGame]) -> dict[str, ModelProb | str]:
        """event_ticker -> ModelProb, or a reason string when there's no estimate."""
        out: dict[str, ModelProb | str] = {}
        if not games:
            return out
        st = self._state(games[0].sport)
        if st is None:
            return {g.event_ticker: "no history loaded (run: history load)" for g in games}
        state, hist = st
        by_day: dict[str, list[dict]] = {}
        for h in hist:
            by_day.setdefault(h["gameday"], []).append(h)
        for g in games:
            cands = [h for h in by_day.get(g.game_date.isoformat(), []) if {h["home"], h["away"]} == set(g.markets)]
            if len(cands) != 1:
                out[g.event_ticker] = f"{len(cands)} schedule rows for {'/'.join(sorted(g.markets))} on {g.game_date}"
                continue
            h = cands[0]
            if h["home_score"] is not None:
                out[g.event_ticker] = "schedule shows the game as already played"
                continue
            p_home = predict(state, h)
            notes = []
            if h.get("week") is not None and h["week"] <= 2 and h.get("game_type") == "REG":
                notes.append("early season: ratings mostly last year's")
            starters = [s for s in (h.get("away_starter"), h.get("home_starter")) if s]
            notes.append(f"QBs: {' @ '.join(starters)}" if len(starters) == 2 else "starting QBs not listed")
            out[g.event_ticker] = ModelProb({h["home"]: p_home, h["away"]: 1 - p_home}, h["game_id"], h.get("week"), "; ".join(notes))
        return out
