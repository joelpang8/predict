"""Dashboard tests: data helpers, and the Streamlit app run headless (AppTest) on a
temporary database filled by the fake-API refresh from test_refresh. The app's
Refresh button is never pressed here, so no real API is called."""

from __future__ import annotations

import pathlib

import pytest

from edgefinder import dashboard as dash
from edgefinder.store.db import Store
from tests.test_refresh import env  # noqa: F401  (fixture)

APP = str(pathlib.Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")


@pytest.fixture
def filled(env, monkeypatch):  # noqa: F811
    f, s, store, make, _ = env
    make().refresh("nfl")
    # The fake book has a 9c spread and $250 at the ask: loosen those two guardrails for the test.
    store.set_overrides({"max_spread_cents": "20", "min_liquidity_usd": "100"})
    monkeypatch.setenv("DB_PATH", s.db_path)
    monkeypatch.setenv("KALSHI_SERIES_NFL", "KXNFLGAME")
    monkeypatch.setenv("ODDS_API_KEY", "")
    return f, s, store


def test_book_comparison_for_one_game(filled):
    f, s, store = filled
    row = next(r for r in store.edge_rows(store.last_run("nfl", True)["id"]) if r["team"] == "CHI" and r["side"] == "YES")
    books = dash.book_comparison(store, row)
    assert {b["book"] for b in books} == {"pinnacle", "draftkings", "fanduel"}
    assert all(b["no-vig % for this side"] == pytest.approx(58.0, abs=0.5) for b in books)


def test_history_frame_has_all_three_series(filled):
    f, s, store = filled
    row = next(r for r in store.edge_rows(store.last_run("nfl", True)["id"]) if r["team"] == "CHI" and r["side"] == "YES")
    candles = [{"end_period_ts": int(__import__("time").time()) - 3600, "volume_fp": "1", "yes_ask": {"close_dollars": "0.55"},
                "yes_bid": {"close_dollars": "0.54"}}]
    store.upsert_history([], __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    frame = dash.history_frame(store, row, candles)
    assert {r["series"] for r in frame} >= {"Kalshi ask", "Sportsbook fair"}
    assert any(r["source"] == "Kalshi hourly candle" for r in frame) and any(r["source"] == "our snapshot" for r in frame)


def test_app_renders_picks_and_logs_paper_bet(filled):
    from streamlit.testing.v1 import AppTest

    f, s, store = filled
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Top Picks", "All Markets", "Market Detail", "Paper Trading", "Settings"]
    assert "not financial advice" in at.warning[0].value and "never places orders" in at.warning[0].value
    cards = [m.value for m in at.markdown if m.value.startswith("**#")]
    assert cards and "CHI to win" in cards[0]
    log = next(b for b in at.button if b.label == "Log paper bet")
    log.click().run()
    assert not at.exception
    assert len(Store(s.db_path).paper_bets()) == 1


def test_app_shows_no_good_bets_with_reason(filled):
    from streamlit.testing.v1 import AppTest

    f, s, store = filled
    store.clear_overrides()  # default guardrails: the fake book's 9c spread blocks everything
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert any(h.value == "No good bets today" for h in at.subheader)
    summary = next(i.value for i in at.info if i.value.startswith("No good bets today"))
    assert "0 of 4 sides" in summary and "under $500 at the ask: 4" in summary and "spread over 3c: 2" in summary


def test_settings_save_persists_override(filled):
    from streamlit.testing.v1 import AppTest

    f, s, store = filled
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.number_input(key="set-bankroll").set_value(2500.0)
    next(b for b in at.button if b.label == "Save").click().run()
    assert Store(s.db_path).get_overrides()["bankroll"] == "2500.0"
