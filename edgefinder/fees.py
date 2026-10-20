"""Kalshi trading fees. THE place to update when Kalshi changes its fees.

Source: Kalshi Fee Schedule PDF, "Last updated and effective: July 7, 2026"
(https://kalshi.com/docs/kalshi-fee-schedule.pdf), page 2:

    Trading (taker) fee = round up(M x 0.07   x C x P x (1-P))
    Maker fee           = round up(M x 0.0175 x C x P x (1-P))
    P = price in dollars, C = contracts, M = multiplier.
    "round up = rounds up such that the fee + positionCost is rounded to a centicent"

Plus docs.kalshi.com "Fee Rounding": the trade fee is rounded up to $0.000001,
then a rounding fee brings the balance change to the member's balance
precision: $0.0001 for direct members, $0.01 for non-direct (retail) members.
The PDF's own General Trading Fees Table matches whole-cent rounding (e.g.
1 contract at $0.50 -> $0.02; 100 at $0.05 -> $0.34), so the default precision
is $0.01. Kalshi rebates accumulated rounding across fills of one order;
we ignore rebates, which can only overstate the fee (the safe direction).

The multiplier comes from the series (`fee_multiplier`), overridden per event
(`fee_multiplier_override`). We only take liquidity (buy at the ask), so only
the taker fee applies. Fee types other than the quadratic family are not in
the PDF, so they raise and the market is skipped.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")
QUADRATIC_FEE_TYPES = frozenset({"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"})
RETAIL_PRECISION = Decimal("0.01")
DIRECT_PRECISION = Decimal("0.0001")
_MICRO = Decimal("0.000001")


class UnsupportedFeeType(ValueError):
    pass


def _ceil(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def taker_fee(
    price: Decimal,
    contracts: Decimal,
    multiplier: Decimal = Decimal(1),
    fee_type: str = "quadratic",
    precision: Decimal = RETAIL_PRECISION,
) -> Decimal:
    """Total taker fee in dollars for buying `contracts` at `price` in one order."""
    if fee_type not in QUADRATIC_FEE_TYPES:
        raise UnsupportedFeeType(f"fee_type {fee_type!r} is not covered by the fee schedule formula")
    if not (Decimal(0) < price < Decimal(1)) or contracts <= 0:
        raise ValueError("price must be in (0, 1) and contracts > 0")
    model_fee = multiplier * TAKER_RATE * contracts * price * (1 - price)
    trade_fee = _ceil(model_fee, _MICRO)
    position_cost = price * contracts
    return _ceil(position_cost + trade_fee, precision) - position_cost


def taker_fee_per_contract(price: Decimal, contracts: Decimal, **kw) -> Decimal:
    return taker_fee(price, contracts, **kw) / contracts
