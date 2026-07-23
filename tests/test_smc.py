"""Unit tests for smc.py: fractal swings, market structure, OTE direction."""

import pandas as pd
import pytest

from smc import fractal_swing_mask, get_market_structure, get_premium_discount


def _close_df(closes):
    """get_market_structure's BOS/CHoCH displacement check also reads high/low."""
    return pd.DataFrame({
        "close": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
    })


def test_fractal_swing_mask_finds_isolated_peak():
    highs = [10, 11, 12, 15, 12, 11, 10, 9, 8, 9]
    lows = [9, 10, 11, 14, 11, 10, 9, 8, 7, 8]
    is_high, is_low = fractal_swing_mask(
        pd.Series(highs, dtype=float), pd.Series(lows, dtype=float), left=2, right=2)
    assert bool(is_high.iloc[3]) is True
    assert int(is_high.sum()) == 1


def test_fractal_swing_mask_finds_isolated_trough():
    highs = [10, 9, 8, 5, 8, 9, 10, 11, 12, 11]
    lows = [9, 8, 7, 4, 7, 8, 9, 10, 11, 10]
    is_high, is_low = fractal_swing_mask(
        pd.Series(highs, dtype=float), pd.Series(lows, dtype=float), left=2, right=2)
    assert bool(is_low.iloc[3]) is True
    assert int(is_low.sum()) == 1


def test_fractal_swing_mask_no_lookahead_on_tail():
    """Last `right` bars can never be confirmed swings (no future data)."""
    highs = [10, 12, 11, 13, 20]
    lows = [9, 11, 10, 12, 19]
    is_high, is_low = fractal_swing_mask(
        pd.Series(highs, dtype=float), pd.Series(lows, dtype=float), left=1, right=1)
    assert bool(is_high.iloc[-1]) is False
    assert bool(is_low.iloc[-1]) is False


def test_get_market_structure_bull_bos():
    # Confirmed swings forming HH/HL (uptrend); a later close beyond the last
    # confirmed high is a bullish BOS.
    confirmed_highs = [
        {"idx": 1, "ts": "t1", "price": 100.0},
        {"idx": 4, "ts": "t4", "price": 105.0},
        {"idx": 7, "ts": "t7", "price": 110.0},
    ]
    confirmed_lows = [
        {"idx": 2, "ts": "t2", "price": 95.0},
        {"idx": 5, "ts": "t5", "price": 98.0},
        {"idx": 8, "ts": "t8", "price": 102.0},
    ]
    closes = [90, 100, 96, 102, 105, 99, 108, 110, 103, 112]
    df = _close_df(closes)
    structure, bos, choch = get_market_structure(confirmed_highs, confirmed_lows, df, recent_bars=12)
    assert structure == "HH/HL"
    assert bos["direction"] == "BULLISH"
    assert bos["level"] == 110.0


def test_get_market_structure_bear_choch():
    # HH/HL structure, then price closes below the last confirmed higher-low
    # -> bearish CHoCH (counter-trend break of the protected swing).
    confirmed_highs = [
        {"idx": 1, "ts": "t1", "price": 100.0},
        {"idx": 4, "ts": "t4", "price": 105.0},
    ]
    confirmed_lows = [
        {"idx": 2, "ts": "t2", "price": 95.0},
        {"idx": 5, "ts": "t5", "price": 98.0},
    ]
    closes = [90, 100, 96, 102, 105, 99, 97, 96]  # idx 6 closes below 98 (last HL)
    df = _close_df(closes)
    structure, bos, choch = get_market_structure(confirmed_highs, confirmed_lows, df, recent_bars=12)
    assert structure == "HH/HL"
    assert choch["detected"] is True
    assert choch["direction"] == "BEARISH"
    assert choch["level"] == 98.0


def test_get_market_structure_needs_at_least_two_swings_per_side():
    structure, bos, choch = get_market_structure(
        [{"idx": 1, "ts": "t1", "price": 100.0}], [], df=None)
    assert structure == "NEUTRAL"
    assert bos["level"] is None
    assert choch["detected"] is False


def test_get_premium_discount_bullish_leg_ote_is_discount_side():
    result = get_premium_discount(swing_high=110.0, swing_low=100.0, current_price=103.0,
                                  leg_direction="bullish")
    assert result["ote_zone"]["low"] == pytest.approx(102.14)
    assert result["ote_zone"]["high"] == pytest.approx(103.82)
    assert result["in_ote"] is True
    assert result["zone"] == "discount"


def test_get_premium_discount_bearish_leg_ote_is_premium_side():
    """BUG-05: shorts must be credited in the mirrored (premium/upper) zone, not discount."""
    result = get_premium_discount(swing_high=110.0, swing_low=100.0, current_price=107.0,
                                  leg_direction="bearish")
    assert result["ote_zone"]["low"] == pytest.approx(106.18)
    assert result["ote_zone"]["high"] == pytest.approx(107.86)
    assert result["in_ote"] is True
    assert result["zone"] == "premium"


def test_get_premium_discount_bearish_leg_not_in_ote_when_deep_discount():
    """A short sitting deep in DISCOUNT (the old bug's failure mode) must NOT read in_ote."""
    result = get_premium_discount(swing_high=110.0, swing_low=100.0, current_price=101.0,
                                  leg_direction="bearish")
    assert result["in_ote"] is False
    assert result["zone"] == "discount"


# ---------------- B17.0: flat-tape swing artifacts ---------------- #

def test_plateau_reports_one_swing_not_a_run():
    """B17.0: every bar of a flat stretch used to qualify as a swing high/low."""
    highs = [1, 2, 3, 10, 10, 10, 10, 10, 3, 2, 1]
    lows = [-h for h in highs]
    is_high, is_low = fractal_swing_mask(
        pd.Series(highs, dtype=float), pd.Series(lows, dtype=float), left=2, right=2)
    assert int(is_high.sum()) == 1
    assert int(is_low.sum()) == 1
    # the LAST bar of the run is the surviving pivot (freshest)
    assert bool(is_high.iloc[7]) is True


def test_equal_highs_far_apart_stay_two_touches():
    """Dedup is plateau-local: genuine repeat tests remain separate swings (EQH pool)."""
    highs = [1, 2, 10, 2, 1, 1, 1, 10, 1, 1]
    lows = [0, 1, 9, 1, 0, 0, 0, 9, 0, 0]
    is_high, _ = fractal_swing_mask(
        pd.Series(highs, dtype=float), pd.Series(lows, dtype=float), left=2, right=2)
    assert int(is_high.sum()) == 2


def test_flat_tape_structure_is_not_bearish():
    """B17.0: an exact tie used to break to LH/LL, so the router shorted chop."""
    from strategies import _structure_bias
    flat_h = [{"idx": i, "ts": f"t{i}", "price": 100.0} for i in (1, 4, 7)]
    flat_l = [{"idx": i, "ts": f"t{i}", "price": 99.0} for i in (2, 5, 8)]
    structure, _, _ = get_market_structure(flat_h, flat_l, df=None)
    assert "LH" not in structure and "LL" not in structure
    assert _structure_bias(structure) not in ("bullish", "bearish")


# ---------------- find_liquidity_levels (side filtering) ---------------- #

def test_liquidity_pools_are_side_filtered():
    """BSL only exists ABOVE price, SSL only BELOW — swept pools must not appear."""
    from smc import find_liquidity_levels
    highs = [(i, p) for i, p in enumerate([95.0, 102.0, 105.0, 110.0])]
    lows = [(i, p) for i, p in enumerate([90.0, 96.0, 103.0, 85.0])]
    pools = find_liquidity_levels(highs, lows, current_price=100.0)
    assert all(p > 100.0 for p in pools["buy_side"])
    assert all(p < 100.0 for p in pools["sell_side"])
    assert 95.0 not in pools["buy_side"]   # swept swing high below price
    assert 103.0 not in pools["sell_side"]  # swing low above price


def test_liquidity_pools_keep_three_nearest_on_correct_side():
    from smc import find_liquidity_levels
    highs = [(i, p) for i, p in enumerate([101.0, 104.0, 120.0, 140.0, 160.0])]
    lows = [(i, p) for i, p in enumerate([99.0, 96.0, 80.0, 60.0, 40.0])]
    pools = find_liquidity_levels(highs, lows, current_price=100.0)
    assert pools["buy_side"] == [101.0, 104.0, 120.0]
    assert pools["sell_side"] == [99.0, 96.0, 80.0]
