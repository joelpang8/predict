from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from edgefinder.devig import BookLine, american_to_prob, consensus, devig_two_way
from edgefinder.sizing import kelly_fraction, suggested_contracts


@pytest.mark.parametrize(
    "odds,prob",
    [(100, 0.5), (-100, 0.5), (-110, 110 / 210), (150, 0.4), (-200, 2 / 3), (240, 100 / 340)],
)
def test_american_to_prob(odds, prob):
    assert american_to_prob(odds) == pytest.approx(prob)


@pytest.mark.parametrize("odds", [0, 50, -50, 99])
def test_invalid_american_odds(odds):
    with pytest.raises(ValueError):
        american_to_prob(odds)


def test_devig_symmetric_vig_is_fifty_fifty():
    a, b = devig_two_way(-110, -110)
    assert a == pytest.approx(0.5) and b == pytest.approx(0.5)


def test_devig_normalises_to_one():
    a, b = devig_two_way(-200, 170)
    assert a + b == pytest.approx(1.0)
    raw_a = 200 / 300
    raw_b = 100 / 270
    assert a == pytest.approx(raw_a / (raw_a + raw_b))
    assert a < raw_a  # vig removed from the favourite too


def test_devig_rejects_sub_100_percent_book():
    with pytest.raises(ValueError):
        devig_two_way(150, 150)


def test_consensus_weights_sharp_books():
    t = datetime(2026, 10, 20, tzinfo=timezone.utc)
    lines = [BookLine("pinnacle", 0.60, t), BookLine("draftkings", 0.50, t)]
    c = consensus(lines, {"pinnacle": 3.0})
    assert c.prob_a == pytest.approx((3 * 0.60 + 0.50) / 4)
    assert c.books == ("draftkings", "pinnacle")
    assert c.spread == pytest.approx(0.10)


def test_consensus_zero_weight_excludes_book():
    lines = [BookLine("a", 0.6, None), BookLine("b", 0.4, None)]
    assert consensus(lines, {"b": 0}).prob_a == pytest.approx(0.6)
    assert consensus([], {}) is None


# -- Kelly ------------------------------------------------------------------------


def test_kelly_formula():
    # p=0.6 at cost 0.5: b=1, f*=(0.6-0.5)/(0.5)=0.2
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.5, 0.5) == 0
    assert kelly_fraction(0.4, 0.5) == 0
    assert kelly_fraction(0.9, 1.0) == 0


def test_quarter_kelly_and_cap():
    # full Kelly 0.2 -> quarter 0.05 -> capped at 2% of $1000 = $20 -> 40 contracts at $0.50
    n, stake = suggested_contracts(0.6, D("0.50"), D(1000), 0.25, 0.02)
    assert n == 40 and stake == D("20.00")


def test_uncapped_quarter_kelly():
    # p=0.52 at 0.50: f*=0.04, quarter=0.01 -> $10 -> 20 contracts
    n, stake = suggested_contracts(0.52, D("0.50"), D(1000), 0.25, 0.05)
    assert n == 20 and stake == D("10.00")


def test_no_edge_no_size():
    assert suggested_contracts(0.45, D("0.50"), D(1000)) == (0, 0)
