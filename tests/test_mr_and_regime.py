"""
B2.0 (ranging-regime breakout fades + dead-on-arrival geometry) and B16.0
(bar-keyed regime hysteresis).
"""

from strategies import (
    _is_naked_breakout, _m1_edge_distance, compute_trade_geometry,
)
from indicators import compute_regime_with_hysteresis


VA = {"vah": 100.0, "val": 90.0, "poc": 95.0}   # span 10


# ---------------- B2.0: naked-breakout invalidation ---------------- #

def test_naked_breakout_short_above_vah_no_reclaim_is_rejected():
    # price well above VAH (span 10 -> out_tol = max(0.25*atr, 1.0)); 105 > 100 + 1
    assert _is_naked_breakout("bearish", 105.0, VA, atr_1h=0.8, reclaimed=False) is True


def test_failed_breakout_with_reclaim_survives():
    assert _is_naked_breakout("bearish", 105.0, VA, atr_1h=0.8, reclaimed=True) is False


def test_edge_touch_within_tolerance_is_still_a_fade():
    # price just above VAH but inside OUT_TOL -> a legitimate edge fade, not a breakout
    assert _is_naked_breakout("bearish", 100.5, VA, atr_1h=0.8, reclaimed=False) is False


def test_naked_breakout_long_below_val_mirror():
    assert _is_naked_breakout("bullish", 85.0, VA, atr_1h=0.8, reclaimed=False) is True
    assert _is_naked_breakout("bullish", 89.5, VA, atr_1h=0.8, reclaimed=False) is False


# ---------------- B2.0: sign-aware M1 location credit ---------------- #

def test_m1_outside_edge_no_reclaim_scores_zero():
    # price 10% of the VA span ABOVE VAH, short fade -> the worst place to pay "at edge"
    score, note = _m1_edge_distance(101.0, 100.0, VA, mr_dir="bearish", reclaimed=False)
    assert score == 0
    assert "outside" in note


def test_m1_outside_edge_with_reclaim_scores_by_proximity():
    score, _ = _m1_edge_distance(101.0, 100.0, VA, mr_dir="bearish", reclaimed=True)
    assert score > 0                              # reclaim-confirmed -> score the reclaim


def test_m1_inside_band_at_edge_scores_max_and_mirrors():
    short = _m1_edge_distance(100.0, 100.0, VA, mr_dir="bearish", reclaimed=False)[0]
    long = _m1_edge_distance(90.0, 90.0, VA, mr_dir="bullish", reclaimed=False)[0]
    assert short == long == 25                    # exactly at the edge, inside


# ---------------- B2.0: universal dead-on-arrival geometry guard ---------------- #

def _mr_geo(direction, price, mr_edge):
    return compute_trade_geometry(
        direction, price, smc_context={}, pa_context={"value_area_1h": VA},
        atr_abs=0.8, regime="Ranging", score_mode="mean_revert", mr_edge=mr_edge, atr_1h=None)


def test_geometry_rejects_dead_on_arrival_short():
    # the audit's case: price 105, VAH 100 -> SELL entry 100 / sl ~100.8; price already
    # 4+ above the stop -> DOA, valid must be False.
    out = _mr_geo("bearish", 105.0, mr_edge=100.0)
    assert out["valid"] is False


def test_geometry_rejects_dead_on_arrival_long_mirror():
    out = _mr_geo("bullish", 85.0, mr_edge=90.0)   # price below the long's stop
    assert out["valid"] is False


def test_geometry_accepts_live_short_inside_the_stop():
    # price 100.5 is above entry(100) but below the 100.8 stop -> a live, valid fade
    out = _mr_geo("bearish", 100.5, mr_edge=100.0)
    assert out["valid"] is True
    assert out["direction"] == "SELL"


# ---------------- B16.0: bar-keyed hysteresis idempotency ---------------- #

BULL = "Trending Bullish"
RANGE = "Ranging / Sideways"


def test_hysteresis_idempotent_within_one_bar():
    prior = {"effective": BULL, "candidate": None, "candidate_runs": 0, "last_bar_id": 0}
    eff1, s1 = compute_regime_with_hysteresis(RANGE, prior, runs=2, bar_id=1)
    assert eff1 == BULL and s1["candidate_runs"] == 1           # first count toward the flip
    eff2, s2 = compute_regime_with_hysteresis(RANGE, s1, runs=2, bar_id=1)
    assert eff2 == BULL and s2["candidate_runs"] == 1           # SAME bar -> no double-count
    eff3, s3 = compute_regime_with_hysteresis(RANGE, s2, runs=2, bar_id=2)
    assert eff3 == RANGE                                        # a NEW bar flips it


def test_hysteresis_without_bar_id_preserves_legacy_advance():
    prior = {"effective": BULL, "candidate": None, "candidate_runs": 0}
    _, s1 = compute_regime_with_hysteresis(RANGE, prior, runs=2)
    _, s2 = compute_regime_with_hysteresis(RANGE, s1, runs=2)
    assert s2["effective"] == RANGE                             # two calls flip, as before


def test_hysteresis_same_bucket_direction_change_is_immediate():
    prior = {"effective": BULL, "candidate": None, "candidate_runs": 0, "last_bar_id": 1}
    eff, _ = compute_regime_with_hysteresis("Trending Bearish", prior, runs=2, bar_id=2)
    assert eff == "Trending Bearish"


# ---------------- B4.0: ENTRY_ATR_CAP on entry candidates ---------------- #

def _trend_geo(ob_top, ob_bottom, price=100.0, atr_abs=1.0):
    smc = {"15m": {"order_blocks": {"bullish": [{"top": ob_top, "bottom": ob_bottom}]},
                   "fvg": {}, "dealing_range": {},
                   "liquidity_levels": {"buy_side": [116.0], "sell_side": [95.0]}}}
    pa = {"value_area_1h": {"vah": 102.0, "val": 98.0, "poc": 100.0}}
    return compute_trade_geometry("bullish", price, smc, pa, atr_abs=atr_abs,
                                  regime="Trending Bullish", score_mode="trend", atr_1h=None)


def test_stale_ob_beyond_cap_falls_back_to_market_entry():
    out = _trend_geo(ob_top=90.5, ob_bottom=89.5)   # OB mid 90 = 10 ATR from price 100
    assert out["valid"] is True
    assert out["entry"] == 100.0                    # market fallback, not the stale 90 OB


def test_ob_within_cap_is_used_as_entry():
    out = _trend_geo(ob_top=99.5, ob_bottom=99.0)   # OB mid 99.25 = 0.75 ATR from price
    assert out["valid"] is True
    assert abs(out["entry"] - 99.25) < 1e-6
