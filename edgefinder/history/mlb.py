"""MLB game history and upcoming schedule from MLB's official Stats API.

The brief named pybaseball. Its game-log function reads Retrosheet files from
GitHub paths that now return 404 (and it needs GitHub API access), and its
Baseball-Reference schedule pages have no starting pitchers. MLB's own Stats
API (statsapi.mlb.com, no key) returns, per season in one request: final
scores, UTC start times, the official date, doubleheader game numbers, game
type, and the probable (for finished games, the announced) starting pitchers.

Postponed/cancelled entries are dropped: the rescheduled game appears as its
own entry.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from edgefinder.history.common import add_rest_days
from edgefinder.matching.teams import team_code

ET = ZoneInfo("America/New_York")
STATSAPI = "https://statsapi.mlb.com/api/v1/schedule"
FIRST_SEASON = 2010
GAME_TYPES = {"R": "REG", "F": "POST", "D": "POST", "L": "POST", "W": "POST"}  # regular + postseason rounds
SKIP_STATES = {"Postponed", "Cancelled", "Suspended"}


def _code(name: str) -> str:
    code = team_code("mlb", name)
    if code is None:
        raise ValueError(f"unknown MLB team name {name!r}")
    return code


def parse_schedule(data: dict, season: int) -> list[dict]:
    out = []
    for day in data.get("dates") or []:
        for g in day.get("games") or []:
            gtype = GAME_TYPES.get(g.get("gameType"))
            state = (g.get("status") or {}).get("detailedState", "")
            if gtype is None or state in SKIP_STATES:
                continue
            home, away = g["teams"]["home"], g["teams"]["away"]
            final = (g.get("status") or {}).get("abstractGameState") == "Final" and home.get("score") is not None
            out.append({
                "sport": "mlb", "game_id": str(g["gamePk"]), "season": season, "game_type": gtype,
                "week": int(g.get("gameNumber") or 1),  # doubleheader game number (week unused for MLB)
                "gameday": g.get("officialDate"), "kickoff_utc": datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00")).isoformat(),
                "home": _code(home["team"]["name"]), "away": _code(away["team"]["name"]),
                "home_score": home.get("score") if final else None, "away_score": away.get("score") if final else None,
                "neutral": 0, "home_ml": None, "away_ml": None, "spread_line": None,
                "home_starter": (home.get("probablePitcher") or {}).get("fullName"),
                "away_starter": (away.get("probablePitcher") or {}).get("fullName"),
            })
    return out


def download(seasons: list[int] | None = None, client: httpx.Client | None = None) -> list[dict]:
    now = datetime.now(ET)
    seasons = seasons or list(range(FIRST_SEASON, now.year + 1))
    own = client is None
    client = client or httpx.Client(timeout=60)
    games = []
    try:
        for season in seasons:
            r = client.get(STATSAPI, params={
                "sportId": 1, "startDate": f"{season}-02-15", "endDate": f"{season}-11-30",
                "gameType": ",".join(GAME_TYPES), "hydrate": "probablePitcher",
            })
            r.raise_for_status()
            games.extend(parse_schedule(r.json(), season))
    finally:
        if own:
            client.close()
    return add_rest_days(games)
