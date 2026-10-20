"""Shared helpers for history loaders."""

from __future__ import annotations

from datetime import date


def add_rest_days(games: list[dict]) -> list[dict]:
    """Fill home_rest / away_rest = days since each team's previous game (None for its first)."""
    last: dict[str, date] = {}
    for g in sorted(games, key=lambda g: (g["gameday"], g.get("kickoff_utc") or "", g["game_id"])):
        d = date.fromisoformat(g["gameday"])
        for side in ("home", "away"):
            prev = last.get(g[side])
            g[f"{side}_rest"] = (d - prev).days if prev else None
            last[g[side]] = d
    return games
