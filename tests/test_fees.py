from decimal import Decimal as D

import pytest

from edgefinder.fees import DIRECT_PRECISION, UnsupportedFeeType, taker_fee, taker_fee_per_contract

# Every row of the "General Trading Fees Table" in the Kalshi Fee Schedule
# (effective July 7, 2026): price -> (fee for 1 contract, fee for 100 contracts).
PDF_TABLE = {
    "0.01": ("0.01", "0.07"), "0.05": ("0.01", "0.34"), "0.10": ("0.01", "0.63"),
    "0.15": ("0.01", "0.90"), "0.20": ("0.02", "1.12"), "0.25": ("0.02", "1.32"),
    "0.30": ("0.02", "1.47"), "0.35": ("0.02", "1.60"), "0.40": ("0.02", "1.68"),
    "0.45": ("0.02", "1.74"), "0.50": ("0.02", "1.75"), "0.55": ("0.02", "1.74"),
    "0.60": ("0.02", "1.68"), "0.65": ("0.02", "1.60"), "0.70": ("0.02", "1.47"),
    "0.75": ("0.02", "1.32"), "0.80": ("0.02", "1.12"), "0.85": ("0.01", "0.90"),
    "0.90": ("0.01", "0.63"), "0.95": ("0.01", "0.34"), "0.99": ("0.01", "0.07"),
}


@pytest.mark.parametrize("price,expected", PDF_TABLE.items())
def test_matches_official_fee_table(price, expected):
    one, hundred = expected
    assert taker_fee(D(price), D(1)) == D(one)
    assert taker_fee(D(price), D(100)) == D(hundred)


def test_multiplier_scales_and_zero_multiplier_is_free():
    assert taker_fee(D("0.50"), D(100), multiplier=D(2)) == D("3.50")
    assert taker_fee(D("0.50"), D(100), multiplier=D(0)) == D(0)


def test_direct_member_precision_is_centicent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 exactly
    assert taker_fee(D("0.50"), D(1), precision=DIRECT_PRECISION) == D("0.0175")


def test_subpenny_price_rounds_fee_plus_cost():
    # cost 0.4325, model fee 0.07*.4325*.5675 = 0.017180... -> total 0.449680.. -> 0.45
    assert taker_fee(D("0.4325"), D(1)) == D("0.45") - D("0.4325")


def test_per_contract_fee_shrinks_with_size():
    assert taker_fee_per_contract(D("0.50"), D(1)) == D("0.02")
    assert taker_fee_per_contract(D("0.50"), D(100)) == D("0.0175")


def test_unknown_fee_type_refuses():
    with pytest.raises(UnsupportedFeeType):
        taker_fee(D("0.5"), D(1), fee_type="flat")


@pytest.mark.parametrize("p", ["0", "1", "-0.1"])
def test_invalid_price(p):
    with pytest.raises(ValueError):
        taker_fee(D(p), D(1))
