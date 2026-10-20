"""Sportsbook moneylines -> no-vig fair probabilities -> weighted consensus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def american_to_prob(odds: float) -> float:
    """Implied probability (vig included) of American odds."""
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    if odds <= -100:
        return -odds / (-odds + 100.0)
    raise ValueError(f"invalid American odds {odds!r}")


def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Remove the vig by normalising both sides to sum to 1 (multiplicative method)."""
    pa, pb = american_to_prob(odds_a), american_to_prob(odds_b)
    total = pa + pb
    if total < 1.0 - 1e-9:
        # Both sides summing below 100% is an arb or bad data, not a real market.
        raise ValueError(f"implied probabilities sum to {total:.4f} < 1")
    return pa / total, pb / total


@dataclass(frozen=True)
class BookLine:
    book: str
    prob_a: float  # no-vig probability of outcome A
    last_update: datetime | None


@dataclass(frozen=True)
class Consensus:
    prob_a: float
    books: tuple[str, ...]
    weight_total: float
    oldest_update: datetime | None
    spread: float  # max - min no-vig prob across books: disagreement signal


def consensus(lines: list[BookLine], weights: dict[str, float], default_weight: float = 1.0) -> Consensus | None:
    """Weighted average of per-book no-vig probabilities. Sharp books get larger weights (config)."""
    usable = [ln for ln in lines if weights.get(ln.book, default_weight) > 0]
    if not usable:
        return None
    wsum = sum(weights.get(ln.book, default_weight) for ln in usable)
    p = sum(weights.get(ln.book, default_weight) * ln.prob_a for ln in usable) / wsum
    updates = [ln.last_update for ln in usable if ln.last_update]
    probs = [ln.prob_a for ln in usable]
    return Consensus(
        prob_a=p,
        books=tuple(sorted(ln.book for ln in usable)),
        weight_total=wsum,
        oldest_update=min(updates) if updates else None,
        spread=max(probs) - min(probs),
    )
