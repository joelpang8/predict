"""Out-of-sample evaluation of the Elo model: log loss, Brier, calibration, and
the de-vigged closing moneyline as a benchmark on the same games.

The phase 7 backtesting module builds on this (CLV, more sports).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, replace

from edgefinder.devig import devig_two_way
from edgefinder.models.elo import EloParams, run

EPS = 1e-12


def log_loss(pairs: list[tuple[float, float]]) -> float:
    """Mean log loss. Ties (actual 0.5) score as half a win and half a loss."""
    if not pairs:
        return float("nan")
    tot = 0.0
    for p, y in pairs:
        p = min(max(p, EPS), 1 - EPS)
        tot -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return tot / len(pairs)


def brier(pairs: list[tuple[float, float]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def calibration(pairs: list[tuple[float, float]], bins: int = 10) -> list[dict]:
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        b = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if b:
            out.append(
                {"bin": f"{lo:.1f}-{hi:.1f}", "n": len(b), "predicted": sum(p for p, _ in b) / len(b), "actual": sum(y for _, y in b) / len(b)}
            )
    return out


@dataclass
class Report:
    seasons: tuple[int, int]
    n: int
    model_log_loss: float
    model_brier: float
    market_n: int
    market_log_loss: float | None  # on the subset with closing moneylines
    model_log_loss_on_market_games: float | None
    base_rate_log_loss: float  # always predicting the training home-win rate
    calibration: list[dict]


def market_prob(g: dict) -> float | None:
    if g.get("home_ml") is None or g.get("away_ml") is None:
        return None
    try:
        p, _ = devig_two_way(float(g["home_ml"]), float(g["away_ml"]))
    except ValueError:
        return None
    return p


def evaluate(games: list[dict], params: EloParams, first: int, last: int) -> Report:
    """Walk-forward: ratings use only earlier games; score seasons first..last."""
    state = run(games, params)
    by_id = {g["game_id"]: g for g in games}
    test = [(gid, p, y) for gid, season, p, y in state.history if first <= season <= last]
    pairs = [(p, y) for _, p, y in test]
    train_home = [y for _, season, _, y in state.history if season < first]
    base = sum(train_home) / len(train_home) if train_home else 0.5

    mk = [(market_prob(by_id[gid]), p, y) for gid, p, y in test]
    mk = [(m, p, y) for m, p, y in mk if m is not None]
    return Report(
        seasons=(first, last),
        n=len(pairs),
        model_log_loss=log_loss(pairs),
        model_brier=brier(pairs),
        market_n=len(mk),
        market_log_loss=log_loss([(m, y) for m, _, y in mk]) if mk else None,
        model_log_loss_on_market_games=log_loss([(p, y) for _, p, y in mk]) if mk else None,
        base_rate_log_loss=log_loss([(base, y) for _, y in pairs]),
        calibration=calibration(pairs),
    )


def tune(
    games: list[dict],
    first: int,
    last: int,
    grid: dict[str, list[float]] | None = None,
    base: EloParams = EloParams(),
) -> tuple[EloParams, float]:
    """Grid-search parameters on seasons first..last (training only). Returns best params and log loss."""
    grid = grid or {"k": [15, 20, 25], "hfa": [30, 40, 48, 55], "season_regress": [0.25, 1 / 3, 0.4]}
    best, best_ll = base, float("inf")
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = replace(base, **dict(zip(keys, combo)))
        ll = evaluate(games, params, first, last).model_log_loss
        if ll < best_ll:
            best, best_ll = params, ll
    return best, best_ll
