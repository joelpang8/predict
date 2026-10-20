"""NFL game history from nflverse (via nflreadpy).

The brief asked for nfl_data_py; it is unmaintained (last release 2024-09) and
requires pandas<2 / numpy<2, which this stack can't use. nflreadpy is the
nflverse project's maintained Python reader; `load_schedules` downloads the
same nflverse "schedules/games" dataset.

Field meanings (nflreadr data dictionary for schedules):
  gametime: kickoff, 24h, US/Eastern regardless of venue
  location: "Home" or "Neutral"
  result:   home score - away score (null if not played)
  *_rest:   days of rest coming into the game
  *_moneyline: closing moneylines (available 2006+)
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# nflverse team abbreviation -> Kalshi code of the same franchise. Relocated
# franchises map to their current code so ratings carry over.
NFLVERSE_TO_KALSHI = {
    "JAX": "JAC",
    "LA": "LAR",
    "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV",
}


def kalshi_code(team: str) -> str:
    return NFLVERSE_TO_KALSHI.get(team, team)


def _kickoff_utc(gameday: str, gametime: str | None) -> str | None:
    if not gameday or not gametime:
        return None
    return datetime.fromisoformat(f"{gameday}T{gametime}").replace(tzinfo=ET).astimezone(ZoneInfo("UTC")).isoformat()


def normalise(row: dict) -> dict:
    return {
        "sport": "nfl",
        "game_id": row["game_id"],
        "season": row["season"],
        "game_type": row["game_type"],
        "week": row["week"],
        "gameday": row["gameday"],
        "kickoff_utc": _kickoff_utc(row["gameday"], row.get("gametime")),
        "home": kalshi_code(row["home_team"]),
        "away": kalshi_code(row["away_team"]),
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "neutral": int(row.get("location") == "Neutral"),
        "home_rest": row.get("home_rest"),
        "away_rest": row.get("away_rest"),
        "home_ml": row.get("home_moneyline"),
        "away_ml": row.get("away_moneyline"),
        "spread_line": row.get("spread_line"),
        "home_starter": row.get("home_qb_name"),
        "away_starter": row.get("away_qb_name"),
    }


def download() -> list[dict]:
    """All seasons available from nflverse (1999 onward), normalised."""
    import nflreadpy

    return [normalise(r) for r in nflreadpy.load_schedules().to_dicts()]
