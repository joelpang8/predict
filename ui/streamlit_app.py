"""Kalshi Sports Edge Finder dashboard.  Run:  streamlit run ui/streamlit_app.py

Read-only analysis tool. No order is ever placed from here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from edgefinder import dashboard as dash  # noqa: E402
from edgefinder.config import EDITABLE, apply_overrides, load_settings  # noqa: E402
from edgefinder.jobs import poller  # noqa: E402
from edgefinder.jobs.refresh import RefreshService  # noqa: E402
from edgefinder.matching.matcher import ET  # noqa: E402
from edgefinder.paper import ledger  # noqa: E402
from edgefinder.scoring import rank  # noqa: E402
from edgefinder.sources.kalshi import KalshiClient  # noqa: E402
from edgefinder.store.db import Store, parse_ts  # noqa: E402

st.set_page_config(page_title="Kalshi Sports Edge Finder", page_icon="📈", layout="wide")

# Validated categorical slots 1-3 (dataviz reference palette), light / dark.
SERIES = ["Kalshi ask", "Sportsbook fair", "Elo model"]
COLORS = {"light": ["#2a78d6", "#eb6834", "#1baf7a"], "dark": ["#3987e5", "#d95926", "#199e70"]}


@st.cache_resource
def _store(path: str) -> Store:
    return Store(path)


@st.cache_resource
def _poller_holder(path: str) -> dict:
    return {"sched": None}


@st.cache_data(ttl=300, show_spinner=False)
def _candles(ticker: str, hours: int) -> list[dict]:
    base = load_settings()
    end = int(datetime.now(timezone.utc).timestamp())
    with KalshiClient(base) as k:
        return k.get_candlesticks([ticker], end - hours * 3600, end, 60).get(ticker, [])


base = load_settings()
store = _store(base.db_path)
settings = apply_overrides(base, store.get_overrides())
service = RefreshService(settings, store)
sports = [s for s, series in settings.kalshi_series.items() if series] or ["nfl"]

# -- header ------------------------------------------------------------------------------

st.title("Kalshi Sports Edge Finder")
st.warning(dash.DISCLAIMER, icon="⚠️")

# -- sidebar: sport, refresh, freshness, poller --------------------------------------------

with st.sidebar:
    sport = st.selectbox("Sport", sports, format_func=str.upper)

    @st.fragment(run_every="1s")
    def refresh_panel() -> None:
        left = service.cooldown_remaining(sport)
        label = "Refresh" if left == 0 else f"Refresh (cooldown {left}s)"
        if st.button(label, type="primary", disabled=left > 0, width="stretch"):
            with st.spinner("Pulling Kalshi and sportsbook odds..."):
                out = service.refresh(sport, trigger="manual")
            st.session_state["last_outcome"] = (out.status, out.credits_cost)
            st.rerun()
        v = service.view(sport)
        st.caption(f"Kalshi data: **{dash.age_text(v.kalshi_fetched)}**")
        st.caption(f"Odds data: **{dash.age_text(v.odds_fetched)}**")
        st.caption(f"Odds credits today: {v.credits_spent_today}/{v.credits_budget}"
                   + (f" · account left: {v.credits_remaining}" if v.credits_remaining is not None else ""))

    refresh_panel()
    if "last_outcome" in st.session_state:
        status, cost = st.session_state["last_outcome"]
        st.caption(f"Last refresh: {status}, {cost} credit(s)")

    st.divider()
    holder = _poller_holder(settings.db_path)
    run_poller = st.toggle("Background poller", value=holder["sched"] is not None,
                           help=f"Kalshi every {settings.poll_interval_s // 60} min ({settings.poll_interval_near_kickoff_s // 60} min near kickoff); odds at most every {settings.poller_odds_interval_s // 3600} h within the daily budget.")
    if run_poller and holder["sched"] is None:
        holder["sched"] = poller.start_background(RefreshService(settings, store))
    elif not run_poller and holder["sched"] is not None:
        holder["sched"].shutdown(wait=False)
        holder["sched"] = None

view = service.view(sport)
for w in view.warnings:
    (st.error if w.startswith("STALE") else st.warning)(w)

now = datetime.now(timezone.utc)
open_bets = store.paper_bets(status="open")
ranking = rank(view.rows, settings, now, stale=view.stale, open_bets=open_bets)
by_key = {(p.row["market_ticker"], p.row["side"]): p for p in ranking.rows}

tab_picks, tab_all, tab_detail, tab_paper, tab_settings = st.tabs(
    ["Top Picks", "All Markets", "Market Detail", "Paper Trading", "Settings"]
)


def _kick(r: dict) -> str:
    return parse_ts(r["kickoff"]).astimezone(ET).strftime("%a %b %d, %I:%M %p ET")


def _log_bet(p, key: str) -> None:
    if st.button("Log paper bet", key=key, help="Records the current ask, fee and size. No order is placed."):
        try:
            bet_id = ledger.log_bet(store, settings, p.row["sport"], p.row["market_ticker"], p.row["side"],
                                    datetime.now(timezone.utc), contracts=p.contracts if p.contracts > 0 else None)
            st.success(f"Paper bet #{bet_id} logged.")
        except ledger.PaperError as e:
            st.error(f"Not logged: {e}")


# -- Top Picks -----------------------------------------------------------------------------

with tab_picks:
    st.caption(f"Kalshi data {dash.age_text(view.kalshi_fetched)} · odds data {dash.age_text(view.odds_fetched)} · "
               f"min net edge {settings.min_net_edge_pts:g} pts · bankroll {dash.money(settings.bankroll)} · "
               f"{settings.kelly_multiplier:g} Kelly, max {settings.max_bet_pct:.0%} per bet")
    if not ranking.picks:
        st.subheader("No good bets today")
        st.info(ranking.summary)
    for i, p in enumerate(ranking.picks, 1):
        r = p.row
        with st.container(border=True):
            st.markdown(f"**#{i} · {r['label']}** · `{r['side']} {r['market_ticker']}` · {_kick(r)}")
            c = st.columns(6)
            c[0].metric("Kalshi ask", f"{Decimal(r['ask']) * 100:.0f}¢")
            c[1].metric("Fair (books)", f"{r['fair'] * 100:.1f}%")
            c[2].metric("Elo", f"{r['model_fair'] * 100:.1f}%" if r.get("model_fair") is not None else "–")
            c[3].metric("Net edge", f"{p.net_edge * 100:+.1f} pts")
            c[4].metric("Suggested", f"{p.contracts:.0f} ct", dash.money(p.stake), delta_color="off")
            c[5].metric("Score", f"{p.score * 100:.0f}")
            st.write(p.reason)
            st.caption(f"Updated: Kalshi {parse_ts(r['kalshi_fetched']):%H:%M:%S} UTC · odds {parse_ts(r['odds_fetched']):%H:%M:%S} UTC")
            with st.expander("Score breakdown"):
                w = {"edge": settings.w_edge, "model": settings.w_model, "liquidity": settings.w_liquidity,
                     "movement": settings.w_movement, "freshness": settings.w_freshness}
                st.dataframe(pd.DataFrame(
                    [{"part": k, "value 0-1": round(v, 2), "weight": w[k], "points": round(w[k] * v * 100, 1)} for k, v in p.components.items()]
                    + [{"part": f"penalty: {n}", "value 0-1": None, "weight": None, "points": round(-v * 100, 1)} for n, v in p.penalties]
                ), hide_index=True, width="stretch")
            _log_bet(p, f"log-{r['market_ticker']}-{r['side']}")
    near = sorted((p for p in ranking.rows if not p.eligible), key=lambda p: -p.net_edge)[:5]
    if near:
        with st.expander("Closest misses and why"):
            st.dataframe(pd.DataFrame([{
                "bet": f"{p.row['side']} {p.row['market_ticker']}", "means": p.row["label"],
                "net edge pts": round(p.net_edge * 100, 1), "blocked by": "; ".join(p.blocked)} for p in near]),
                hide_index=True, width="stretch")

# -- All Markets -----------------------------------------------------------------------------

with tab_all:
    f1, f2, f3 = st.columns(3)
    sport_filter = f1.multiselect("Sport", sorted({r["sport"] for r in view.rows}) or [sport],
                                  default=sorted({r["sport"] for r in view.rows}) or [sport])
    min_edge = f2.slider("Min net edge (pts)", -10.0, 10.0, -10.0, 0.5)
    min_liq = f3.number_input("Min liquidity at ask ($)", 0, 1_000_000, 0, step=250)
    table = []
    for r in view.rows:
        p = by_key[(r["market_ticker"], r["side"])]
        liq = float(p.liquidity_usd) if p.liquidity_usd is not None else None
        if r["sport"] not in sport_filter or p.net_edge * 100 < min_edge or (liq or 0) < min_liq:
            continue
        table.append({
            "game": r["event_ticker"].split("-", 1)[1], "kickoff": _kick(r), "side": f"{r['side']} {r['team']}",
            "means": r["label"], "Kalshi ¢": float(Decimal(r["ask"]) * 100), "fair %": round(r["fair"] * 100, 1),
            "Elo %": round(r["model_fair"] * 100, 1) if r.get("model_fair") is not None else None,
            "net edge pts": round(p.net_edge * 100, 2), "EV per $": round(p.net_edge / float(Decimal(r["ask"]) + p.fee_per_contract) * 100, 1),
            "liquidity $": liq, "trend x": r.get("trend_score"), "6h move": r.get("move_6h"),
            "size": int(p.contracts), "stake $": float(p.stake),
            "Kalshi updated": dash.age_text(r["kalshi_fetched"], now), "odds updated": dash.age_text(r["odds_fetched"], now),
            "eligible": p.eligible,
        })
    st.caption(f"{len(table)} of {len(view.rows)} sides")
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", column_config={
        "net edge pts": st.column_config.NumberColumn(format="%+.2f"),
        "EV per $": st.column_config.NumberColumn(format="%+.1f%%"),
        "liquidity $": st.column_config.NumberColumn(format="$%,.0f"),
        "stake $": st.column_config.NumberColumn(format="$%.2f"),
        "trend x": st.column_config.NumberColumn(format="%.1fx"),
        "6h move": st.column_config.NumberColumn(format="%+.0f"),
    })

# -- Market Detail ---------------------------------------------------------------------------

with tab_detail:
    if not view.rows:
        st.info("No markets yet. Press Refresh.")
    else:
        opts = {f"{r['label']} · {r['side']} {r['market_ticker']}": r for r in view.rows}
        r = opts[st.selectbox("Market", list(opts))]
        p = by_key[(r["market_ticker"], r["side"])]
        hours = st.radio("Window", [24, 72], index=1, horizontal=True, format_func=lambda h: f"{h} h")
        try:
            candles = _candles(r["market_ticker"], hours)
        except Exception as e:  # history is optional context
            candles = []
            st.warning(f"Kalshi price history unavailable: {e}")
        hist = pd.DataFrame(dash.history_frame(store, r, candles, hours))
        mode = "dark" if getattr(st.context.theme, "type", "light") == "dark" else "light"
        if hist.empty:
            st.info("No history yet for this market.")
        else:
            present = [s for s in SERIES if s in set(hist["series"])]
            color = alt.Color("series:N", scale=alt.Scale(domain=SERIES, range=COLORS[mode]),
                              legend=alt.Legend(orient="top", title=None, symbolSize=90))
            shape = alt.Shape("series:N", scale=alt.Scale(domain=SERIES, range=["circle", "square", "diamond"]), legend=None)
            lo, hi = hist["value"].min(), hist["value"].max()
            pad = max(2.0, (hi - lo) * 0.15)
            base_chart = alt.Chart(hist).encode(
                x=alt.X("time:T", title=None, axis=alt.Axis(grid=False)),
                y=alt.Y("value:Q", title="probability / price (%)", scale=alt.Scale(domain=[lo - pad, hi + pad]),
                        axis=alt.Axis(gridWidth=1)),
                color=color,
                tooltip=[alt.Tooltip("series:N"), alt.Tooltip("time:T", format="%b %d %H:%M"),
                         alt.Tooltip("value:Q", format=".1f", title="%"), alt.Tooltip("source:N")],
            )
            lines = base_chart.mark_line(strokeWidth=2, strokeCap="round", strokeJoin="round")
            # Fair and Elo are recorded once per refresh, so they are sparse: always show their markers.
            markers = base_chart.mark_point(size=80, filled=True, opacity=1).encode(shape=shape).transform_filter(
                alt.datum.series != "Kalshi ask")
            hover = alt.selection_point(fields=["time"], nearest=True, on="pointerover", empty=False)
            rule = alt.Chart(hist).mark_rule(strokeWidth=1, color="#8a8984").encode(x="time:T").transform_filter(hover)
            dots = base_chart.mark_circle(size=64).encode(opacity=alt.condition(hover, alt.value(1), alt.value(0))).add_params(hover)
            st.altair_chart((lines + markers + dots + rule).properties(height=320), width="stretch")
            st.caption(f"Series shown: {', '.join(present)}. Kalshi ask = what you'd pay for {r['side']}; fair and Elo are recorded at each refresh.")
            with st.expander("Table view"):
                st.dataframe(hist.assign(value=hist["value"].round(1)), hide_index=True, width="stretch")
        st.subheader("Sportsbooks vs Kalshi")
        st.caption(f"Kalshi ask {Decimal(r['ask']) * 100:.0f}¢ for this side · net edge {p.net_edge * 100:+.1f} pts after fees · "
                   f"odds pulled {dash.age_text(r['odds_fetched'], now)}")
        books = dash.book_comparison(store, r)
        st.dataframe(pd.DataFrame(books), hide_index=True, width="stretch") if books else st.info("No book lines stored for this game.")
        if r.get("flags"):
            st.write("Signals: " + "; ".join(r["flags"]))
        _log_bet(p, f"detail-log-{r['market_ticker']}-{r['side']}")

# -- Paper Trading ----------------------------------------------------------------------------

with tab_paper:
    bets = store.paper_bets()
    stats = ledger.stats(bets)
    c = st.columns(6)
    c[0].metric("Bets", stats.bets)
    c[1].metric("Open", stats.open)
    c[2].metric("Hit rate", f"{stats.hit_rate:.0%}" if stats.hit_rate is not None else "–")
    c[3].metric("P&L", dash.money(stats.pnl))
    c[4].metric("ROI", f"{stats.roi:+.1%}" if stats.roi is not None else "–")
    c[5].metric("Avg CLV", f"{stats.avg_clv_cents:+.1f}¢" if stats.avg_clv_cents is not None else "–",
                help="Kalshi closing ask for your side minus the price you got. Positive = you beat the close.")
    if st.button("Settle finished games", help="Reads Kalshi results; only 'finalized' markets settle."):
        with KalshiClient(settings) as k:
            done = ledger.settle(store, k, datetime.now(timezone.utc))
        st.success(f"Settled {len(done)} bet(s).")
        st.rerun()
    if bets:
        st.dataframe(pd.DataFrame(bets)[["id", "created_at", "label", "side", "market_ticker", "contracts", "price",
                                         "fee_per_contract", "net_edge_at_entry", "status", "result", "pnl", "closing_ask"]],
                     hide_index=True, width="stretch")
    else:
        st.info("No paper bets yet. Use 'Log paper bet' on a pick.")

# -- Settings --------------------------------------------------------------------------------

LABELS = {
    "bankroll": "Bankroll ($)", "kelly_multiplier": "Kelly fraction (0.25 = quarter Kelly)",
    "max_bet_pct": "Max per bet (fraction of bankroll)", "min_net_edge_pts": "Min net edge (pts)",
    "min_liquidity_usd": "Min $ at the ask", "max_spread_cents": "Max bid-ask spread (¢)",
    "near_start_min": "Near-kickoff window (min)", "near_start_max_age_s": "...max data age inside it (s)",
    "max_daily_exposure_pct": "Max exposure per day (fraction of bankroll)",
    "max_sport_exposure_pct": "Max exposure per sport per day (fraction)", "top_n": "Picks to show",
    "max_data_age_s": "Stale after (s)", "w_edge": "Weight: net edge", "w_model": "Weight: model agreement",
    "w_liquidity": "Weight: liquidity", "w_movement": "Weight: line movement", "w_freshness": "Weight: freshness",
}

with tab_settings:
    st.caption("Saved in the local database and applied immediately to ranking and sizing. Defaults come from .env.")
    with st.form("settings"):
        vals = {}
        cols = st.columns(3)
        for i, k in enumerate(EDITABLE):
            cur = getattr(settings, k)
            with cols[i % 3]:
                if isinstance(cur, int) and not isinstance(cur, bool):
                    vals[k] = st.number_input(LABELS[k], value=int(cur), step=1, key=f"set-{k}")
                else:
                    vals[k] = st.number_input(LABELS[k], value=float(cur), format="%.4g", key=f"set-{k}")
        a, b = st.columns(2)
        save = a.form_submit_button("Save", type="primary")
        reset = b.form_submit_button("Reset to .env defaults")
    if save:
        store.set_overrides({k: v for k, v in vals.items()})
        st.success("Saved.")
        st.rerun()
    if reset:
        store.clear_overrides()
        st.success("Reset.")
        st.rerun()
