"""
I5.0 / B12.0: typed liquidity pools (touches / age / swept state) and sweep quality
(typed pools only, ATR-scaled minimum penetration, longer scan with age decay).
"""

import pandas as pd
import pytest

from config import SWEEP_FRESH_BARS, SWEEP_MIN_PENETRATION_ATR
from smc import find_liquidity_levels
from strategies import detect_mtf_sweep


def _sw(idx, price):
    return {"idx": idx, "ts": f"t{idx}", "price": float(price)}


def _df(highs, lows, closes=None, atr=1.0):
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "volume": [1.0] * len(highs), "ATR": [atr] * len(highs)})


# ---------------- typed pools ---------------- #

def test_single_touch_pivot_is_a_swing_level_not_a_pool():
    """B12.0: one high at 105 / one low at 95 used to produce BSL [105] / SSL [95]."""
    out = find_liquidity_levels([_sw(2, 105.0)], [_sw(5, 95.0)],
                                current_price=100.0, df=_df([106] * 8, [94] * 8))
    assert out["pools"] == []
    assert 105.0 in out["swing_levels"]["highs"]
    assert 95.0 in out["swing_levels"]["lows"]


def test_two_separated_touches_make_a_pool_with_metadata():
    highs = [_sw(2, 105.0), _sw(20, 105.02)]
    df = _df([104.0] * 30, [99.0] * 30)
    out = find_liquidity_levels(highs, [_sw(5, 95.0), _sw(25, 95.01)],
                                current_price=100.0, df=df, tf="1h")
    bsl = [p for p in out["pools"] if p["side"] == "buy"]
    assert len(bsl) == 1
    pool = bsl[0]
    assert pool["touches"] == 2
    assert (pool["first_idx"], pool["last_idx"]) == (2, 20)
    assert pool["tf"] == "1h" and pool["swept_idx"] is None
    assert 0 < pool["strength"] <= 1.0


def test_touches_inside_one_pivot_do_not_count_twice():
    """Two bars of the same pivot are one test of the level, not two."""
    out = find_liquidity_levels([_sw(10, 105.0), _sw(11, 105.0)], [_sw(5, 95.0)],
                                current_price=100.0, df=_df([104.0] * 20, [96.0] * 20))
    assert out["pools"] == []


def test_a_wick_consumed_pool_is_retired_as_a_draw_but_kept_as_history():
    """B12.0: pools taken by a wick used to stay on the menu as future draws."""
    highs = [_sw(2, 105.0), _sw(6, 105.02)]
    # bar 8 wicks above 105 and closes back below -> liquidity consumed
    hs = [104.0] * 8 + [106.0] + [104.0] * 3
    df = _df(hs, [99.0] * len(hs))
    out = find_liquidity_levels(highs, [_sw(4, 95.0), _sw(9, 95.01)],
                                current_price=100.0, df=df, tf="1h")
    pool = [p for p in out["pools"] if p["side"] == "buy"][0]
    assert pool["swept_idx"] == 8            # history preserved for sweep evidence
    assert 105.0 not in out["buy_side"]      # but no longer offered as a draw
    assert 105.02 not in out["buy_side"]


def test_draws_stay_side_correct():
    df = _df([104.0] * 30, [96.0] * 30)
    out = find_liquidity_levels([_sw(2, 105.0), _sw(20, 105.02)],
                                [_sw(5, 95.0), _sw(25, 95.01)],
                                current_price=100.0, df=df, tf="1h")
    assert all(p > 100.0 for p in out["buy_side"])
    assert all(p < 100.0 for p in out["sell_side"])


# ---------------- sweep quality ---------------- #

def _sweep_ctx(price, side, tf="1h"):
    return {tf: {"liquidity_levels": {
        "pools": [{"price": price, "side": side, "touches": 2, "tf": tf,
                   "swept_idx": None, "strength": 0.5}],
        "buy_side": [], "sell_side": []}}}


def _sweep_df(n, pool, penetration, *, bars_ago, atr=1.0, bullish=True):
    """Flat tape with one aggressive sweep candle `bars_ago` from the end."""
    body = pool + 2.0 if bullish else pool - 2.0
    highs = [body + 0.2] * n
    lows = [body - 0.2] * n
    closes = [body] * n
    opens = [body] * n
    i = n - 1 - bars_ago
    if bullish:                       # sweep of sell-side liquidity: spike below, close above
        lows[i] = pool - penetration
        highs[i] = pool + 0.2
        opens[i] = closes[i] = pool + 0.1
    else:
        highs[i] = pool + penetration
        lows[i] = pool - 0.2
        opens[i] = closes[i] = pool - 0.1
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": [1.0] * n, "ATR": [atr] * n})


@pytest.mark.parametrize("bullish,side", [(True, "sell"), (False, "buy")])
def test_a_deep_fresh_sweep_scores_the_full_htf_tier(bullish, side):
    pool = 100.0
    df = _sweep_df(20, pool, penetration=1.0, bars_ago=1, atr=1.0, bullish=bullish)
    pts, note, tier = detect_mtf_sweep(df, _sweep_ctx(pool, side), None, None,
                                       "bullish" if bullish else "bearish")
    assert pts == 10 and tier == "1h"
    assert "1 bars ago" in note


def test_a_graze_is_not_a_stop_run():
    pool = 100.0
    shallow = SWEEP_MIN_PENETRATION_ATR * 1.0 * 0.5      # half the required ATR fraction
    df = _sweep_df(20, pool, penetration=shallow, bars_ago=1, atr=1.0)
    pts, _, _ = detect_mtf_sweep(df, _sweep_ctx(pool, "sell"), None, None, "bullish")
    assert pts == 0


def test_a_stale_htf_sweep_demotes_instead_of_paying_full():
    pool = 100.0
    df = _sweep_df(20, pool, penetration=1.0, bars_ago=SWEEP_FRESH_BARS + 3, atr=1.0)
    pts, note, _ = detect_mtf_sweep(df, _sweep_ctx(pool, "sell"), None, None, "bullish")
    assert pts == 6 and "stale" in note


def test_a_stale_15m_only_sweep_stops_counting():
    pool = 100.0
    df = _sweep_df(20, pool, penetration=1.0, bars_ago=SWEEP_FRESH_BARS + 3, atr=1.0)
    pts, _, tier = detect_mtf_sweep(df, _sweep_ctx(pool, "sell", tf="15m"), None, None, "bullish")
    assert pts == 0 and tier == "none"


def test_sweeps_beyond_the_old_five_bar_window_are_now_seen():
    pool = 100.0
    df = _sweep_df(20, pool, penetration=1.0, bars_ago=9, atr=1.0)
    pts, _, _ = detect_mtf_sweep(df, _sweep_ctx(pool, "sell"), None, None, "bullish")
    assert pts == 6            # found, and honestly discounted for age


def test_single_touch_levels_are_no_longer_sweep_evidence():
    """The typed layer is what C3/M2 read: a lone pivot cannot manufacture a sweep."""
    pool = 100.0
    df = _sweep_df(20, pool, penetration=1.0, bars_ago=1, atr=1.0)
    ctx = {"1h": {"liquidity_levels": {"pools": [], "buy_side": [], "sell_side": [pool]}}}
    pts, _, _ = detect_mtf_sweep(df, ctx, None, None, "bullish")
    assert pts == 0
