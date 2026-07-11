"""
Unit tests for strategies.py: structural TP selection (never invents a price)
and protected-swing SL geometry (BUG-06 — nearest pool, not widest, with an
ATR cap).
"""

import pytest

from strategies import _select_structural_tp, compute_trade_geometry


# ---------------- _select_structural_tp ---------------- #

def test_select_structural_tp_prefers_nearest_2r_level():
    cands = [103, 108, 112, 130]  # entry=100, risk=5 -> 2R target = 110
    tp, note = _select_structural_tp(cands, entry=100, risk=5, direction="bullish")
    assert tp == 112
    assert "2R" in note


def test_select_structural_tp_falls_back_to_farthest_when_no_2r_level():
    cands = [102, 104, 106]  # none reach 110 (2R)
    tp, note = _select_structural_tp(cands, entry=100, risk=5, direction="bullish")
    assert tp == 106
    assert "honest" in note


def test_select_structural_tp_bearish_direction():
    cands = [97, 92, 88, 70]  # entry=100, risk=5 -> 2R target = 90
    tp, note = _select_structural_tp(cands, entry=100, risk=5, direction="bearish")
    assert tp == 88
    assert "2R" in note


def test_select_structural_tp_no_candidates_returns_none():
    tp, note = _select_structural_tp([], entry=100, risk=5, direction="bullish")
    assert tp is None
    assert note is None


# ---------------- compute_trade_geometry SL selection (BUG-06) ---------------- #

def _pa_context(**overrides):
    base = {
        "value_area_1h": {"vah": 0, "val": 0, "poc": 0},
        "untested_pocs_4h": [], "untested_pocs_1d": [],
        "pwh": None, "pwl": None, "avwap": {},
    }
    base.update(overrides)
    return base


def test_geometry_sl_uses_nearest_protected_swing_not_widest_pool_long():
    """BUG-06: SL must be the nearest SSL below entry, never the farthest/widest pool."""
    smc_context = {
        "15m": {
            "order_blocks": {"bullish": [], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"sell_side": [90.0, 80.0, 70.0], "buy_side": [115.0, 130.0]},
            "dealing_range": {"low": 75.0, "high": 105.0},
        },
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=smc_context, pa_context=_pa_context(),
        atr_abs=20.0, regime="Trending Bullish", score_mode="trend",
    )
    assert result["valid"] is True
    assert result["stop_loss"] == pytest.approx(80.0)  # nearest pool (90) - buffer(10), not 70/130
    assert "nearest protected" in result["stop_loss_source"]


def test_geometry_sl_uses_nearest_protected_swing_not_widest_pool_short():
    smc_context = {
        "15m": {
            "order_blocks": {"bullish": [], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"buy_side": [110.0, 120.0, 130.0], "sell_side": [50.0, 35.0]},
            "dealing_range": {"low": 95.0, "high": 125.0},
        },
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }
    result = compute_trade_geometry(
        "bearish", price=100.0, smc_context=smc_context, pa_context=_pa_context(),
        atr_abs=20.0, regime="Trending Bearish", score_mode="trend",
    )
    assert result["valid"] is True
    assert result["stop_loss"] == pytest.approx(120.0)  # nearest pool (110) + buffer(10), not 130
    assert "nearest protected" in result["stop_loss_source"]


def test_geometry_sl_respects_atr_cap():
    """BUG-06: a pool farther than SL_ATR_CAP x ATR must be capped, not used raw."""
    smc_context = {
        "15m": {
            "order_blocks": {"bullish": [], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"sell_side": [10.0], "buy_side": [115.0]},
            "dealing_range": {"low": 5.0, "high": 105.0},
        },
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }
    # atr_abs=1.0 -> sl_cap = 3.0 * 1.0 = 3.0; the only pool (10) is ~90 away -> must be capped.
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=smc_context, pa_context=_pa_context(),
        atr_abs=1.0, regime="Trending Bullish", score_mode="trend",
    )
    assert result["valid"] is True
    assert result["stop_loss"] == pytest.approx(97.0)  # entry(100) - sl_cap(3.0)
    assert "ATR-capped" in result["stop_loss_source"]
