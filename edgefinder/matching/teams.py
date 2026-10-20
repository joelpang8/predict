"""Team name normalisation. Exact lookups only: no fuzzy matching.

Codes are Kalshi's (KXNFLGAME / KXNBAGAME / KXMLBGAME tickers, verified live
2026-10-20; MLB from this season's settled markets). Each team lists every
name we accept for it: Kalshi's `yes_sub_title`, the odds feed's full name,
and former names used by the history sources. Anything not listed is unknown
and gets logged, never guessed.
"""

from __future__ import annotations

import re

NFL: dict[str, tuple[str, ...]] = {
    "ARI": ("Arizona", "Arizona Cardinals"),
    "ATL": ("Atlanta", "Atlanta Falcons"),
    "BAL": ("Baltimore", "Baltimore Ravens"),
    "BUF": ("Buffalo", "Buffalo Bills"),
    "CAR": ("Carolina", "Carolina Panthers"),
    "CHI": ("Chicago", "Chicago Bears"),
    "CIN": ("Cincinnati", "Cincinnati Bengals"),
    "CLE": ("Cleveland", "Cleveland Browns"),
    "DAL": ("Dallas", "Dallas Cowboys"),
    "DEN": ("Denver", "Denver Broncos"),
    "DET": ("Detroit", "Detroit Lions"),
    "GB": ("Green Bay", "Green Bay Packers"),
    "HOU": ("Houston", "Houston Texans"),
    "IND": ("Indianapolis", "Indianapolis Colts"),
    "JAC": ("Jacksonville", "Jacksonville Jaguars"),
    "KC": ("Kansas City", "Kansas City Chiefs"),
    "LAC": ("Los Angeles C", "Los Angeles Chargers"),
    "LAR": ("Los Angeles R", "Los Angeles Rams"),
    "LV": ("Las Vegas", "Las Vegas Raiders"),
    "MIA": ("Miami", "Miami Dolphins"),
    "MIN": ("Minnesota", "Minnesota Vikings"),
    "NE": ("New England", "New England Patriots"),
    "NO": ("New Orleans", "New Orleans Saints"),
    "NYG": ("New York G", "New York Giants"),
    "NYJ": ("New York J", "New York Jets"),
    "PHI": ("Philadelphia", "Philadelphia Eagles"),
    "PIT": ("Pittsburgh", "Pittsburgh Steelers"),
    "SEA": ("Seattle", "Seattle Seahawks"),
    "SF": ("San Francisco", "San Francisco 49ers"),
    "TB": ("Tampa Bay", "Tampa Bay Buccaneers"),
    "TEN": ("Tennessee", "Tennessee Titans"),
    "WAS": ("Washington", "Washington Commanders"),
}

NBA: dict[str, tuple[str, ...]] = {
    "ATL": ("Atlanta", "Atlanta Hawks"),
    "BKN": ("Brooklyn", "Brooklyn Nets", "New Jersey Nets"),
    "BOS": ("Boston", "Boston Celtics"),
    "CHA": ("Charlotte", "Charlotte Hornets", "Charlotte Bobcats"),
    "CHI": ("Chicago", "Chicago Bulls"),
    "CLE": ("Cleveland", "Cleveland Cavaliers"),
    "DAL": ("Dallas", "Dallas Mavericks"),
    "DEN": ("Denver", "Denver Nuggets"),
    "DET": ("Detroit", "Detroit Pistons"),
    "GSW": ("Golden State", "Golden State Warriors"),
    "HOU": ("Houston", "Houston Rockets"),
    "IND": ("Indiana", "Indiana Pacers"),
    "LAC": ("Los Angeles C", "Los Angeles Clippers", "LA Clippers"),
    "LAL": ("Los Angeles L", "Los Angeles Lakers"),
    "MEM": ("Memphis", "Memphis Grizzlies", "Vancouver Grizzlies"),
    "MIA": ("Miami", "Miami Heat"),
    "MIL": ("Milwaukee", "Milwaukee Bucks"),
    "MIN": ("Minnesota", "Minnesota Timberwolves"),
    "NOP": ("New Orleans", "New Orleans Pelicans", "New Orleans Hornets", "New Orleans/Oklahoma City Hornets"),
    "NYK": ("New York", "New York Knicks"),
    "OKC": ("Oklahoma City", "Oklahoma City Thunder", "Seattle SuperSonics"),
    "ORL": ("Orlando", "Orlando Magic"),
    "PHI": ("Philadelphia", "Philadelphia 76ers"),
    "PHX": ("Phoenix", "Phoenix Suns"),
    "POR": ("Portland", "Portland Trail Blazers"),
    "SAC": ("Sacramento", "Sacramento Kings"),
    "SAS": ("San Antonio", "San Antonio Spurs"),
    "TOR": ("Toronto", "Toronto Raptors"),
    "UTA": ("Utah", "Utah Jazz"),
    "WAS": ("Washington", "Washington Wizards"),
}

MLB: dict[str, tuple[str, ...]] = {
    "ATH": ("A's", "Athletics", "Oakland Athletics", "Sacramento Athletics"),
    "ATL": ("Atlanta", "Atlanta Braves"),
    "AZ": ("Arizona", "Arizona Diamondbacks"),
    "BAL": ("Baltimore", "Baltimore Orioles"),
    "BOS": ("Boston", "Boston Red Sox"),
    "CHC": ("Chicago C", "Chicago Cubs"),
    "CIN": ("Cincinnati", "Cincinnati Reds"),
    "CLE": ("Cleveland", "Cleveland Guardians", "Cleveland Indians"),
    "COL": ("Colorado", "Colorado Rockies"),
    "CWS": ("Chicago WS", "Chicago White Sox"),
    "DET": ("Detroit", "Detroit Tigers"),
    "HOU": ("Houston", "Houston Astros"),
    "KC": ("Kansas City", "Kansas City Royals"),
    "LAA": ("Los Angeles A", "Los Angeles Angels", "Los Angeles Angels of Anaheim"),
    "LAD": ("Los Angeles D", "Los Angeles Dodgers"),
    "MIA": ("Miami", "Miami Marlins", "Florida Marlins"),
    "MIL": ("Milwaukee", "Milwaukee Brewers"),
    "MIN": ("Minnesota", "Minnesota Twins"),
    "NYM": ("New York M", "New York Mets"),
    "NYY": ("New York Y", "New York Yankees"),
    "PHI": ("Philadelphia", "Philadelphia Phillies"),
    "PIT": ("Pittsburgh", "Pittsburgh Pirates"),
    "SD": ("San Diego", "San Diego Padres"),
    "SEA": ("Seattle", "Seattle Mariners"),
    "SF": ("San Francisco", "San Francisco Giants"),
    "STL": ("St. Louis", "St. Louis Cardinals"),
    "TB": ("Tampa Bay", "Tampa Bay Rays"),
    "TEX": ("Texas", "Texas Rangers"),
    "TOR": ("Toronto", "Toronto Blue Jays"),
    "WSH": ("Washington", "Washington Nationals"),
}

TEAMS: dict[str, dict[str, tuple[str, ...]]] = {"nfl": NFL, "nba": NBA, "mlb": MLB}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _index(table: dict[str, tuple[str, ...]]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for code, names in table.items():
        for n in names:
            key = _norm(n)
            if key in idx and idx[key] != code:
                raise ValueError(f"alias {n!r} maps to both {idx[key]} and {code}")
            idx[key] = code
    return idx


_INDEX = {sport: _index(t) for sport, t in TEAMS.items()}


def team_code(sport: str, name: str) -> str | None:
    """Canonical code for a team name, or None if unknown."""
    return _INDEX.get(sport, {}).get(_norm(name))


def is_code(sport: str, code: str) -> bool:
    return code in TEAMS.get(sport, {})
