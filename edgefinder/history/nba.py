"""NBA game history.

Primary source is nba_api (stats.nba.com), as specified. stats.nba.com does not
answer requests from many cloud/data-centre IPs (it hung from the build
environment), so on failure the loader falls back to Basketball-Reference
schedule pages, which also list upcoming games with US/Eastern tip times.
Set NBA_SOURCE=nba_api or bbref to force one.

Team names map to Kalshi codes through the exact alias table in
`edgefinder.matching.teams`, so relocated/renamed franchises (New Jersey Nets,
Seattle SuperSonics, Charlotte Bobcats, New Orleans Hornets) carry over.
Seasons start in 2005 so the old Charlotte Hornets (now NOP) never appear.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from edgefinder.history.common import add_rest_days
from edgefinder.matching.teams import team_code

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
FIRST_SEASON = 2005  # season ending year
BBREF = "https://www.basketball-reference.com"
BBREF_DELAY_S = 3.5  # Sports-Reference allows about 20 requests a minute


def current_season_end(today: datetime | None = None) -> int:
    d = today or datetime.now(ET)
    return d.year + 1 if d.month >= 8 else d.year


def _code(name: str) -> str:
    code = team_code("nba", name)
    if code is None:
        raise ValueError(f"unknown NBA team name {name!r}")
    return code


# -- Basketball-Reference ------------------------------------------------------------------


def parse_bbref_month(page: str, season: int) -> list[dict]:
    """Rows of a /leagues/NBA_<season>_games-<month>.html page."""
    out = []
    playoffs = False
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        if "Playoffs" in row and "data-stat" not in row.replace("data-stat=\"date_game\"", ""):
            playoffs = True
            continue
        cells = {k: html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
                 for k, v in re.findall(r'data-stat="([^"]+)"[^>]*>(.*?)</t[hd]>', row, re.S)}
        if not cells.get("date_game") or not cells.get("home_team_name") or cells["date_game"] == "Date":
            continue
        day = datetime.strptime(cells["date_game"], "%a, %b %d, %Y").date()
        kickoff = None
        t = cells.get("game_start_time", "")
        m = re.match(r"(\d{1,2}):(\d{2})([ap])", t)
        if m:
            hh = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
            kickoff = datetime(day.year, day.month, day.day, hh, int(m.group(2)), tzinfo=ET).astimezone(ZoneInfo("UTC")).isoformat()
        home, away = _code(cells["home_team_name"]), _code(cells["visitor_team_name"])
        hs, as_ = cells.get("home_pts"), cells.get("visitor_pts")
        out.append({
            "sport": "nba", "game_id": f"{day.isoformat()}-{away}-{home}", "season": season,
            "game_type": "POST" if playoffs else "REG", "week": None, "gameday": day.isoformat(),
            "kickoff_utc": kickoff, "home": home, "away": away,
            "home_score": int(hs) if hs else None, "away_score": int(as_) if as_ else None,
            "neutral": 0, "home_ml": None, "away_ml": None, "spread_line": None,
            "home_starter": None, "away_starter": None,
        })
    return out


def download_bbref(seasons: list[int], client: httpx.Client | None = None, delay: float = BBREF_DELAY_S) -> list[dict]:
    own = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0 (kalshi-edge-finder; research)"})
    games = []
    try:
        for season in seasons:
            first = client.get(f"{BBREF}/leagues/NBA_{season}_games.html")
            if first.status_code == 404:
                continue
            first.raise_for_status()
            months = list(dict.fromkeys(re.findall(rf'href="(/leagues/NBA_{season}_games-[a-z]+\.html)"', first.text)))
            for path in months or [f"/leagues/NBA_{season}_games.html"]:
                time.sleep(delay)
                r = client.get(BBREF + path)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                games.extend(parse_bbref_month(r.text, season))
            time.sleep(delay)
    finally:
        if own:
            client.close()
    return add_rest_days(games)


# -- nba_api ---------------------------------------------------------------------------------


def pair_nba_api_rows(rows: list[dict], season: int, game_type: str) -> list[dict]:
    """LeagueGameFinder returns one row per team per game; join them into games."""
    by_game: dict[str, list[dict]] = {}
    for r in rows:
        by_game.setdefault(r["GAME_ID"], []).append(r)
    out = []
    for gid, rs in by_game.items():
        if len(rs) != 2:
            continue
        home = next((r for r in rs if " vs. " in r["MATCHUP"]), None)
        away = next((r for r in rs if " @ " in r["MATCHUP"]), None)
        if not home or not away:
            continue
        out.append({
            "sport": "nba", "game_id": gid, "season": season, "game_type": game_type, "week": None,
            "gameday": home["GAME_DATE"][:10], "kickoff_utc": None,
            "home": _code(home["TEAM_NAME"]), "away": _code(away["TEAM_NAME"]),
            "home_score": int(home["PTS"]), "away_score": int(away["PTS"]), "neutral": 0,
            "home_ml": None, "away_ml": None, "spread_line": None, "home_starter": None, "away_starter": None,
        })
    return out


def download_nba_api(seasons: list[int], timeout: int = 20) -> list[dict]:
    from nba_api.stats.endpoints import leaguegamefinder

    games = []
    for season in seasons:
        label = f"{season - 1}-{str(season)[2:]}"
        for stype, gtype in (("Regular Season", "REG"), ("Playoffs", "POST")):
            r = leaguegamefinder.LeagueGameFinder(
                season_nullable=label, league_id_nullable="00", season_type_nullable=stype, timeout=timeout
            )
            games.extend(pair_nba_api_rows(r.get_normalized_dict()["LeagueGameFinderResults"], season, gtype))
            time.sleep(1)
    return add_rest_days(games)


def download(seasons: list[int] | None = None) -> list[dict]:
    seasons = seasons or list(range(FIRST_SEASON, current_season_end() + 1))
    source = os.getenv("NBA_SOURCE", "auto").lower()
    if source in ("auto", "nba_api"):
        try:
            return download_nba_api(seasons)
        except Exception as e:
            if source == "nba_api":
                raise
            log.warning("nba_api failed (%s); falling back to Basketball-Reference", type(e).__name__)
    return download_bbref(seasons)
