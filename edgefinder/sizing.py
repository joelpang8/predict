"""Fractional Kelly sizing for a $1-payout binary contract."""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def kelly_fraction(prob: float, cost: float) -> float:
    """Full-Kelly bankroll fraction for paying `cost` (price + fee) to win $1 with probability `prob`.

    Net odds b = (1 - cost) / cost, so f* = (p*b - (1-p)) / b = (p - cost) / (1 - cost).
    Returns 0 when there is no positive edge.
    """
    if not (0 < cost < 1):
        return 0.0
    return max(0.0, (prob - cost) / (1 - cost))


def suggested_contracts(
    prob: float,
    cost_per_contract: Decimal,
    bankroll: Decimal,
    kelly_multiplier: float = 0.25,
    max_bet_pct: float = 0.02,
) -> tuple[Decimal, Decimal]:
    """(whole contracts, dollars at risk) under fractional Kelly with a hard per-bet cap."""
    f = kelly_fraction(prob, float(cost_per_contract)) * kelly_multiplier
    f = min(f, max_bet_pct)
    stake = bankroll * Decimal(str(f))
    if f <= 0 or cost_per_contract <= 0:
        return Decimal(0), Decimal(0)
    n = (stake / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR)
    return n, n * cost_per_contract
