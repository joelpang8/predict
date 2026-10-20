"""Team-strength Elo model: a second, independent estimate next to the sportsbook consensus.

Standard Elo with the adjustments popularised by FiveThirtyEight's NFL model
(home field, rest/bye, margin-of-victory multiplier with autocorrelation
correction, regression to the mean between seasons, playoff scaling).
The parameter values below are starting points, not facts. `tune()` fits them
on training seasons and `edgefinder.models.evaluate` reports out-of-sample
log loss and calibration.

Target: home win = 1, tie = 0.5, loss = 0. That is exactly how Kalshi settles
a team market (ties pay $0.50), so the model's probability is directly a fair
value for YES on the home team; no tie adjustment is needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EloParams:
    k: float = 20.0
    hfa: float = 48.0  # Elo points for a true home game; 0 at neutral sites
    bye_bonus: float = 25.0  # coming off 13+ days of rest
    season_regress: float = 1 / 3  # fraction pulled back to the mean each new season
    playoff_mult: float = 1.2  # Elo difference scaled up in playoffs
    mean: float = 1500.0


@dataclass
class EloState:
    params: EloParams
    ratings: dict[str, float] = field(default_factory=dict)
    games: dict[str, int] = field(default_factory=dict)  # games played per team (sample size)
    season: int | None = None
    # per completed game: (game_id, season, p_home, actual) for evaluation
    history: list[tuple[str, int, float, float]] = field(default_factory=list)

    def rating(self, team: str) -> float:
        return self.ratings.get(team, self.params.mean)


def _rested(rest, week) -> bool:
    return rest is not None and week is not None and week > 1 and rest >= 13


def elo_diff(state: EloState, g: dict) -> float:
    """Home-minus-away Elo difference including situational adjustments."""
    p = state.params
    d = state.rating(g["home"]) - state.rating(g["away"])
    if not g.get("neutral"):
        d += p.hfa
    d += p.bye_bonus * (_rested(g.get("home_rest"), g.get("week")) - _rested(g.get("away_rest"), g.get("week")))
    if g.get("game_type") not in (None, "REG"):
        d *= p.playoff_mult
    return d


def win_prob(diff: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def _new_season(state: EloState, season: int) -> None:
    p = state.params
    for t, r in state.ratings.items():
        state.ratings[t] = r + (p.mean - r) * p.season_regress
    state.season = season


def predict(state: EloState, g: dict) -> float:
    """P(home wins) with ties counted half, using ratings as of now."""
    if state.season is not None and g["season"] > state.season:
        # Rating as it would stand after the off-season regression, without mutating state.
        p = state.params
        tmp = EloState(p, {t: r + (p.mean - r) * p.season_regress for t, r in state.ratings.items()}, state.games, g["season"])
        return win_prob(elo_diff(tmp, g))
    return win_prob(elo_diff(state, g))


def update(state: EloState, g: dict) -> None:
    """Apply one completed game (must be called in kickoff order)."""
    if state.season is None:
        state.season = g["season"]
    elif g["season"] > state.season:
        _new_season(state, g["season"])
    diff = elo_diff(state, g)
    p_home = win_prob(diff)
    margin = g["home_score"] - g["away_score"]
    actual = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
    # MOV multiplier with autocorrelation correction: blowouts by favourites count for less.
    winner_diff = diff if margin > 0 else -diff if margin < 0 else 0.0
    mov = math.log(max(abs(margin), 1) + 1) * (2.2 / (winner_diff * 0.001 + 2.2))
    shift = state.params.k * mov * (actual - p_home)
    state.ratings[g["home"]] = state.rating(g["home"]) + shift
    state.ratings[g["away"]] = state.rating(g["away"]) - shift
    for t in (g["home"], g["away"]):
        state.games[t] = state.games.get(t, 0) + 1
    state.history.append((g["game_id"], g["season"], p_home, actual))


def completed(g: dict) -> bool:
    return g.get("home_score") is not None and g.get("away_score") is not None


def run(games: list[dict], params: EloParams = EloParams()) -> EloState:
    """Replay every completed game in kickoff order."""
    state = EloState(params)
    for g in sorted((g for g in games if completed(g)), key=lambda g: (g["kickoff_utc"] or g["gameday"], g["game_id"])):
        update(state, g)
    return state
