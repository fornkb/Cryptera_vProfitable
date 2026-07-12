"""
Unit tests for strategies.py: structural TP selection (never invents a price)
and protected-swing SL geometry (BUG-06 — nearest pool, not widest, with an
ATR cap).
"""

import pytest

from strategies import _select_structural_tp, compute_trade_geometry, _rank_targets
from config import MAX_TARGET_ATR_1H_MULTIPLE


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


# ---------------- AVWAP confluence labels ---------------- #

def test_avwap_swing_high_low_get_distinct_confluence_labels():
    """
    AVWAP swing-high/swing-low anchors used to share one label ("15m AVWAP"), so
    two close-together anchors merged into one candidate with a literal duplicate
    string in `confluence` -- inflating confluence_count for what read as a single
    source. Labels must be distinct even when the anchors still merge by price.
    """
    smc_context = {
        "15m": {
            "order_blocks": {"bullish": [], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"sell_side": [90.0], "buy_side": []},
            "dealing_range": {"low": 95.0, "high": 105.0},
        },
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }
    pa_context = _pa_context(avwap={"15m": {"from_swing_high": 110.05, "from_swing_low": 110.0}})
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=smc_context, pa_context=pa_context,
        atr_abs=1.0, regime="Trending Bullish", score_mode="trend",
    )
    targets = result["candidates"]["targets"]
    assert len(targets) == 1  # the two close anchors still merge into one candidate
    confluence = targets[0]["confluence"]
    assert len(confluence) == len(set(confluence)), f"duplicate label in {confluence}"
    assert confluence[0] != confluence[1]


# ---------------- _rank_targets ATR-1h distance cap ---------------- #

def test_rank_targets_drops_candidates_beyond_atr_1h_cap():
    atr_1h = 1.0
    cap = MAX_TARGET_ATR_1H_MULTIPLE * atr_1h
    near = 100 + (cap - 1)
    far = 100 + (cap + 50)
    labeled = [(near, "near target"), (far, "absurdly far target")]
    targets = _rank_targets(labeled, entry=100, risk=5, direction="bullish",
                            ref_price=100, atr_1h=atr_1h)
    prices = [t["price"] for t in targets]
    assert near in prices
    assert far not in prices


def test_rank_targets_fails_open_without_atr_1h():
    labeled = [(105.0, "near target"), (500.0, "absurdly far target")]
    targets = _rank_targets(labeled, entry=100, risk=5, direction="bullish",
                            ref_price=100, atr_1h=None)
    prices = [t["price"] for t in targets]
    assert 105.0 in prices
    assert 500.0 in prices


# ---------------- FA-04: primary TP honours the ATR-1h reachability gate ---------------- #

def _trend_smc(buy_side):
    return {
        "15m": {
            "order_blocks": {"bullish": [], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"sell_side": [98.0], "buy_side": buy_side},
            "dealing_range": {"low": 95.0, "high": 105.0},
        },
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }


def test_primary_tp_far_only_targets_invalidates_trade():
    """FA-04: when every target is beyond the ATR-1h cap, no primary may be picked
    from the unfiltered list — the trade is structurally unsound (valid=False)."""
    atr_1h = 1.0
    far = 100.0 + MAX_TARGET_ATR_1H_MULTIPLE * atr_1h + 50.0
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=_trend_smc([far]), pa_context=_pa_context(),
        atr_abs=1.0, regime="Trending Bullish", score_mode="trend", atr_1h=atr_1h,
    )
    assert result["valid"] is False


def test_primary_tp_excludes_far_target_and_agrees_with_candidates():
    atr_1h = 1.0
    near = 100.0 + MAX_TARGET_ATR_1H_MULTIPLE * atr_1h - 1.0
    far = 100.0 + MAX_TARGET_ATR_1H_MULTIPLE * atr_1h + 50.0
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=_trend_smc([near, far]), pa_context=_pa_context(),
        atr_abs=1.0, regime="Trending Bullish", score_mode="trend", atr_1h=atr_1h,
    )
    assert result["valid"] is True
    assert result["take_profit"] == pytest.approx(near)
    candidate_prices = [t["price"] for t in result["candidates"]["targets"]]
    assert far not in candidate_prices
    assert result["take_profit"] in candidate_prices  # primary present in its own menu


def test_primary_tp_fails_open_without_atr_1h():
    far = 100.0 + MAX_TARGET_ATR_1H_MULTIPLE * 1.0 + 50.0
    result = compute_trade_geometry(
        "bullish", price=100.0, smc_context=_trend_smc([far]), pa_context=_pa_context(),
        atr_abs=1.0, regime="Trending Bullish", score_mode="trend", atr_1h=None,
    )
    assert result["valid"] is True
    assert result["take_profit"] == pytest.approx(far)


# ---------------- FA-02 / FA-07: raw breakdown persisted, logistic fed raw ---------------- #

import pandas as pd

import strategies
from strategies import evaluate_strategies


def _df_15m_flat(n=7, price=100.0):
    return pd.DataFrame({
        "open": [price] * n, "high": [price + 1] * n,
        "low": [price - 1] * n, "close": [price] * n, "volume": [1.0] * n,
    })


def _eval_kwargs(**overrides):
    kwargs = dict(
        df_4h=None, df_1h=None, df_15m=_df_15m_flat(),
        orderbook=None, funding=None, sentiment=None, open_interest=None,
        smc_context={
            "4h": {"structure": "HH/HL"},
            "1h": {"structure": "HH/HL"},
            "15m": {"order_blocks": {"bullish": [], "bearish": []},
                    "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                    "liquidity_levels": {"buy_side": [], "sell_side": []},
                    "dealing_range": {}},
        },
        windowed_indicators={},
        pa_context=_pa_context(),
        market_regime="Trending Bullish",
    )
    kwargs.update(overrides)
    return kwargs


def test_persists_raw_breakdown_and_feeds_logistic_raw(monkeypatch):
    """FA-02: with weights != 1 the persisted breakdown is weight-scaled but
    `confluence_breakdown_raw` stays pre-weight, and the logistic model is fed
    the RAW dict (train/inference feature match)."""
    monkeypatch.setattr(strategies, "_CALIBRATION_CACHE", {
        "weights": {"c1_trend_alignment": 0.5}, "mr_weights": {},
        "score_to_winrate": [], "logistic": {}})
    captured = {}

    def _spy(breakdown, score_mode, calib):
        captured["breakdown"] = breakdown
        return None

    monkeypatch.setattr(strategies, "_logistic_winrate", _spy)
    out = evaluate_strategies(**_eval_kwargs())
    # aligned 4H/1H bull structure with no ADX data scores C1 = 10 raw
    assert out["confluence_breakdown_raw"]["c1_trend_alignment"] == 10
    assert out["confluence_breakdown"]["c1_trend_alignment"] == 5   # 10 x 0.5 weight
    assert out["confluence_breakdown"] != out["confluence_breakdown_raw"]
    assert captured["breakdown"] == out["confluence_breakdown_raw"]


def test_no_fade_branch_emits_canonical_keys(monkeypatch):
    """FA-07: the ranging no-fade branch must use the full m1..m6 component names
    (and carry the raw dict too), not short m1..m6 keys."""
    monkeypatch.setattr(strategies, "_CALIBRATION_CACHE", {
        "weights": {}, "mr_weights": {}, "score_to_winrate": [], "logistic": {}})
    out = evaluate_strategies(**_eval_kwargs(market_regime="Ranging / Sideways"))
    canonical = {"m1_edge_distance", "m2_edge_sweep", "m3_cvd_absorption",
                 "m4_stoch_extreme", "m5_rejection", "m6_range_intact"}
    assert set(out["confluence_breakdown"]) == canonical
    assert out["confluence_breakdown_raw"] == out["confluence_breakdown"]
    assert set(out["confluence_notes"]) <= canonical


# ---------------- _tf_of_source / _TF_RANK (1d levels) ---------------- #

def test_tf_of_source_recognizes_daily_and_weekly_levels():
    from strategies import _tf_of_source
    assert _tf_of_source("1d BSL") == "1d"
    assert _tf_of_source("untested daily POC") == "1d"
    assert _tf_of_source("PWH") == "1d"
    assert _tf_of_source("PWL") == "1d"
    assert _tf_of_source("untested 4H POC") == "4h"
    assert _tf_of_source("15m bull FVG mid") == "15m"
    assert _tf_of_source("VAH") == "1h"


def test_tf_rank_keeps_4h_as_intraday_anchor():
    """4h stays most significant; 1d ranks above 1h but below 4h."""
    from strategies import _TF_RANK
    assert _TF_RANK["4h"] > _TF_RANK["1d"] > _TF_RANK["1h"] > _TF_RANK["15m"]
