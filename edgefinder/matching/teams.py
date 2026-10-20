"""Team name normalisation. Exact lookups only: no fuzzy matching.

Codes are Kalshi's (from KXNFLGAME tickers, verified live 2026-10-20). Each
team lists every name we accept for it: Kalshi's `yes_sub_title` and the
odds feed's full name. Anything not listed is unknown and gets logged, never
guessed. NBA and MLB come in phase 7.
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

TEAMS: dict[str, dict[str, tuple[str, ...]]] = {"nfl": NFL}


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
