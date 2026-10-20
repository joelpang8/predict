"""Model probabilities for live Kalshi games, from the stored history.

A Kalshi game is tied to a schedule row with the same discipline as the odds
matcher: same two teams (Kalshi codes) and same US/Eastern date, exactly one
row, or no model estimate (with a reason).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime

from edgefinder.matching.matcher import KalshiGame
from edgefinder.models.elo import EloParams, EloState, predict, run
from edgefinder.store.db import Store


@dataclass(frozen=True)
class ModelProb:
    probs: dict[str, float]  # team code -> P(win), ties counted half (= Kalshi YES value)
    game_id: str
    week: int | None
    note: str  # e.g. starters, early-season warning


# Fitted on training seasons only; out-of-sample results in docs/model.md.
NFL_PARAMS = EloParams(k=20.0, hfa=35.0, bye_bonus=25.0, season_regress=0.4, playoff_mult=1.2)
MLB_PARAMS = EloParams(
    k=6.0, hfa=24.0, bye_bonus=0.0, season_regress=1 / 3, playoff_mult=1.0, mov="none", pitcher_k=4.0, pitcher_weight=1.0
)
NBA_PARAMS = EloParams(k=20.0, hfa=70.0, bye_bonus=0.0, season_regress=0.25, playoff_mult=1.0, mov="nba", b2b_penalty=50.0)
PARAMS = {"nfl": NFL_PARAMS, "nba": NBA_PARAMS, "mlb": MLB_PARAMS}
START_TOLERANCE_S = 20 * 60
# Below this many games played this season by either team, ratings are mostly last season's.
EARLY_SEASON_GAMES = {"nba": 10, "mlb": 15}


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

    def probs(self, games: list[KalshiGame], events: dict | None = None) -> dict[str, ModelProb | str]:
        """event_ticker -> ModelProb, or a reason string when there's no estimate.

        `events` (event_ticker -> matched OddsEvent) lets NBA games without a
        schedule row be predicted from the odds event's home/away teams.
        """
        out: dict[str, ModelProb | str] = {}
        if not games:
            return out
        sport = games[0].sport
        st = self._state(sport)
        if st is None:
            return {g.event_ticker: f"no history loaded (run: history load --sport {sport})" for g in games}
        state, hist = st
        by_day: dict[str, list[dict]] = {}
        played: dict[tuple[int, str], list[str]] = {}
        for h in hist:
            by_day.setdefault(h["gameday"], []).append(h)
            if h["home_score"] is not None:
                for t in (h["home"], h["away"]):
                    played.setdefault((h["season"], t), []).append(h["gameday"])
        for g in games:
            cands = [h for h in by_day.get(g.game_date.isoformat(), []) if {h["home"], h["away"]} == set(g.markets)]
            if g.start is not None and len(cands) > 1:  # doubleheader: pick by start time
                cands = [h for h in cands if h.get("kickoff_utc") and abs(
                    (datetime.fromisoformat(h["kickoff_utc"]) - g.start).total_seconds()) <= START_TOLERANCE_S]
            synthetic = False
            if not cands and sport == "nba" and events and g.event_ticker in events:
                cands = [self._synthesize(g, events[g.event_ticker], hist)]
                synthetic = True
            if len(cands) != 1:
                out[g.event_ticker] = f"{len(cands)} schedule rows for {'/'.join(sorted(g.markets))} on {g.game_date}"
                continue
            h = cands[0]
            if h["home_score"] is not None:
                out[g.event_ticker] = "schedule shows the game as already played"
                continue
            p_home = predict(state, h)
            notes = []
            if sport == "nfl" and h.get("week") is not None and h["week"] <= 2 and h.get("game_type") == "REG":
                notes.append("early season: ratings mostly last year's")
            if sport in EARLY_SEASON_GAMES:
                n = min(sum(d < h["gameday"] for d in played.get((h["season"], t), [])) for t in (h["home"], h["away"]))
                if n < EARLY_SEASON_GAMES[sport]:
                    notes.append(f"early season: ratings mostly last year's ({n} games played)")
            if sport == "nfl":
                starters = [x for x in (h.get("away_starter"), h.get("home_starter")) if x]
                notes.append(f"QBs: {' @ '.join(starters)}" if len(starters) == 2 else "starting QBs not listed")
            if sport == "mlb":
                starters = [x for x in (h.get("away_starter"), h.get("home_starter")) if x]
                notes.append(f"SPs: {' @ '.join(starters)}" if len(starters) == 2 else "starting pitchers not listed")
            if sport == "nba" and synthetic:
                notes.append("not in loaded schedule: home/away from the odds feed")
            out[g.event_ticker] = ModelProb({h["home"]: p_home, h["away"]: 1 - p_home}, h["game_id"], h.get("week"), "; ".join(notes))
        return out

    @staticmethod
    def _synthesize(g: KalshiGame, event, hist: list[dict]) -> dict:
        from edgefinder.matching.teams import team_code

        home, away = team_code(g.sport, event.home_team), team_code(g.sport, event.away_team)
        last = {}
        for h in hist:
            if h["gameday"] < g.game_date.isoformat():
                for t in (h["home"], h["away"]):
                    last[t] = h["gameday"]
        def rest(t):
            return (g.game_date - date.fromisoformat(last[t])).days if t in last else None
        season = g.game_date.year + 1 if g.game_date.month >= 8 else g.game_date.year
        return {
            "game_id": f"odds:{event.id}", "season": season, "game_type": "REG", "week": None,
            "gameday": g.game_date.isoformat(), "kickoff_utc": event.commence_time.isoformat(), "home": home, "away": away,
            "home_score": None, "away_score": None, "neutral": 0, "home_rest": rest(home), "away_rest": rest(away),
        }
