from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from edgefinder.config import Settings
from edgefinder.devig import devig_two_way
from edgefinder.edge import FeeParams, Skip, compute_edges, fair_yes_by_team
from edgefinder.matching.matcher import MatchReport, build_games, match_games
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook
from edgefinder.sources.odds_api import BookOdds, OddsApiClient, OddsApiError, OddsEvent, expected_cost
from tests.test_matching import KICK, kmarket

UTC = timezone.utc
NOW = KICK - timedelta(hours=6)
FEES = FeeParams("quadratic_with_maker_fees", D(1))
S = Settings(kalshi_base_url="x", nfl_tie_prob=0.0, min_books=2)


def make_match(books, chi_quote=("0.55", "0.47"), ne_quote=("0.47", "0.55")):
    ms = []
    for code, title, (yes, no) in (("CHI", "Chicago", chi_quote), ("NE", "New England", ne_quote)):
        m = kmarket(code=code, title=title)
        ms.append(KalshiMarket.from_api({**m.raw, "yes_ask_dollars": yes, "no_ask_dollars": no}))
    ev = OddsEvent("ev1", "americanfootball_nfl", KICK, "Chicago Bears", "New England Patriots", tuple(books))
    rep = MatchReport()
    match_games("nfl", build_games("nfl", ms, rep), [ev], rep)
    assert len(rep.matches) == 1
    return rep.matches[0]


def book(key, chi, ne):
    return BookOdds(key, NOW, {"Chicago Bears": chi, "New England Patriots": ne})


def edges(match, settings=S, books=None, k=NOW, o=NOW, now=NOW):
    return compute_edges(match, FEES, books or {}, k, o, now, settings)


def test_fair_is_weighted_devig_consensus():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -140, 120)])
    fair, cons = fair_yes_by_team(m, S)
    p1, _ = devig_two_way(-150, 130)
    p2, _ = devig_two_way(-140, 120)
    assert fair["CHI"] == pytest.approx((3 * p1 + p2) / 4)
    assert fair["CHI"] + fair["NE"] == pytest.approx(1)


def test_tie_adjustment_pulls_toward_half():
    m = make_match([book("pinnacle", -300, 250), book("draftkings", -300, 250)])
    plain, _ = fair_yes_by_team(m, S)
    tied, _ = fair_yes_by_team(m, replace(S, nfl_tie_prob=0.01))
    assert tied["CHI"] == pytest.approx(plain["CHI"] * 0.99 + 0.005)
    assert tied["CHI"] < plain["CHI"] and tied["NE"] > plain["NE"]
    assert tied["CHI"] + tied["NE"] == pytest.approx(1)


def test_too_few_books_skips():
    m = make_match([book("pinnacle", -150, 130)])
    assert isinstance(edges(m), Skip)


def test_books_missing_a_side_are_ignored():
    partial = BookOdds("fanduel", NOW, {"Chicago Bears": -150})
    m = make_match([book("pinnacle", -150, 130), partial, book("draftkings", -150, 130)])
    _, cons = fair_yes_by_team(m, S)
    assert cons.books == ("draftkings", "pinnacle")


def test_four_rows_both_sides_and_net_edge_math():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)])
    rows = edges(m)
    assert {(r.team, r.side) for r in rows} == {("CHI", "YES"), ("CHI", "NO"), ("NE", "YES"), ("NE", "NO")}
    chi_yes = next(r for r in rows if r.team == "CHI" and r.side == "YES")
    p, _ = devig_two_way(-150, 130)
    assert chi_yes.fair == pytest.approx(p)
    assert chi_yes.gross_edge == pytest.approx(p - 0.55)
    assert chi_yes.net_edge == pytest.approx(p - 0.55 - float(chi_yes.fee_per_contract))
    assert chi_yes.ev_per_dollar == pytest.approx(chi_yes.net_edge / float(D("0.55") + chi_yes.fee_per_contract))
    chi_no = next(r for r in rows if r.team == "CHI" and r.side == "NO")
    assert chi_no.fair == pytest.approx(1 - p)


def test_positive_edge_gets_size_and_negative_does_not():
    # Fair ~58%, YES ask 50c -> real edge; NO side at 52c is negative.
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)], chi_quote=("0.50", "0.52"))
    rows = {(r.team, r.side): r for r in edges(m)}
    assert rows[("CHI", "YES")].net_edge > 0.05
    assert rows[("CHI", "YES")].contracts > 0
    assert rows[("CHI", "NO")].contracts == 0


def test_size_capped_by_depth_at_ask():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)], chi_quote=("0.50", "0.52"))
    chi = m.game.markets["CHI"].ticker
    ob = Orderbook.from_api(chi, {"yes_dollars": [["0.48", "500"]], "no_dollars": [["0.50", "7"]]})
    row = next(r for r in edges(m, books={chi: ob}) if r.team == "CHI" and r.side == "YES")
    assert row.ask_depth == 7 and row.contracts == 7


def test_fee_is_charged_at_the_sized_quantity():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)], chi_quote=("0.50", "0.52"))
    row = next(r for r in edges(m) if r.team == "CHI" and r.side == "YES")
    # 2% cap = $20. At the 1-contract fee (2c) that's 38 contracts; the fee for 38
    # is ceil_cent(0.07*38*0.25 = 0.665) = $0.67, i.e. 1.76c each, still 38 contracts.
    assert row.contracts == 38
    assert row.fee_per_contract == D("0.67") / 38
    assert row.stake == D("19.67")


def test_stale_or_skewed_data_refuses():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)])
    assert "apart" in edges(m, k=NOW, o=NOW - timedelta(minutes=10)).reason
    assert "old" in edges(m, k=NOW, o=NOW, now=NOW + timedelta(hours=1)).reason


def test_started_game_refuses():
    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)])
    assert edges(m, k=KICK, o=KICK, now=KICK + timedelta(minutes=1)).reason == "game has started"


# -- odds client -----------------------------------------------------------------------


def test_expected_cost_formula():
    assert expected_cost((), ("us",)) == 1
    assert expected_cost((), ("us", "eu")) == 2
    assert expected_cost(tuple("abcdefghij"), ()) == 1
    assert expected_cost(tuple("abcdefghijk"), ()) == 2


ODDS_DOC_EXAMPLE = [  # shape from the official v4 docs example response
    {
        "id": "bda33adca828c09dc3cac3a856aef176",
        "sport_key": "americanfootball_nfl",
        "commence_time": "2021-09-10T00:20:00Z",
        "home_team": "Tampa Bay Buccaneers",
        "away_team": "Dallas Cowboys",
        "bookmakers": [
            {
                "key": "unibet",
                "title": "Unibet",
                "last_update": "2021-06-10T13:33:18Z",
                "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Dallas Cowboys", "price": 240}, {"name": "Tampa Bay Buccaneers", "price": -303}]},
                    {"key": "spreads", "outcomes": [{"name": "Dallas Cowboys", "price": -109, "point": 6.5}]},
                ],
            }
        ],
    }
]


def test_odds_client_parses_h2h_and_logs_credits():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(req.url.params)
        return httpx.Response(
            200, json=ODDS_DOC_EXAMPLE, headers={"x-requests-last": "1", "x-requests-used": "41", "x-requests-remaining": "459"}
        )

    c = OddsApiClient("k", bookmakers=("pinnacle", "draftkings"), transport=httpx.MockTransport(handler))
    events, fetched = c.get_moneylines("nfl")
    assert seen["markets"] == "h2h" and seen["bookmakers"] == "pinnacle,draftkings" and "regions" not in seen
    assert events[0].books[0].prices == {"Dallas Cowboys": 240.0, "Tampa Bay Buccaneers": -303.0}
    assert c.usage.remaining == 459 and c.usage.calls[0]["cost"] == 1


def test_odds_client_refuses_to_dip_into_reserve():
    def handler(req):
        return httpx.Response(200, json=[], headers={"x-requests-last": "1", "x-requests-remaining": "20"})

    c = OddsApiClient("k", bookmakers=("pinnacle",), min_remaining=20, transport=httpx.MockTransport(handler))
    c.get_moneylines("nfl")
    with pytest.raises(OddsApiError, match="reserve"):
        c.get_moneylines("nfl")


def test_odds_client_requires_key():
    with pytest.raises(OddsApiError):
        OddsApiClient("")


def test_model_estimate_carried_on_both_sides():
    from edgefinder.models.service import ModelProb

    m = make_match([book("pinnacle", -150, 130), book("draftkings", -150, 130)])
    mp = ModelProb({"CHI": 0.62, "NE": 0.38}, "g", 7, "QBs: a @ b")
    rows = {(r.team, r.side): r for r in compute_edges(m, FEES, {}, NOW, NOW, NOW, S, mp)}
    assert rows[("CHI", "YES")].model_fair == pytest.approx(0.62)
    assert rows[("CHI", "NO")].model_fair == pytest.approx(0.38)
    assert rows[("NE", "YES")].model_note == "QBs: a @ b"
    no_model = {(r.team, r.side): r for r in compute_edges(m, FEES, {}, NOW, NOW, NOW, S, "no history loaded")}
    assert no_model[("CHI", "YES")].model_fair is None and no_model[("CHI", "YES")].model_note == "no history loaded"
