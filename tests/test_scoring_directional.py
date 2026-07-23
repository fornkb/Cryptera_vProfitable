"""
Directional-component scoring: B5.0 (RSI-slope sign), B6.0 (OB/FVG distance
parity), B13.0 (C1 conflict floor) — plus the mirror-property pattern the audit
mandates on every directional component: invert the series AND the trade direction
and the score must be identical. A whole class of long/short-asymmetry bugs cannot
survive this test.
"""

import pandas as pd
from pytest import approx

from strategies import _c1_trend, _c2_ob_prox, _c4_momentum, _zone_distance


def _df_1h(rsi, macd, sig):
    return pd.DataFrame([{"RSI": rsi, "MACD": macd, "MACD_SIGNAL": sig}])


def _win(rsi_series, adx=0.0):
    return {"1h": {"rsi_series_last_5": rsi_series}, "4h": {"adx": adx}}


# ---------------- B5.0: RSI-slope is read in the trade's direction ---------------- #

def test_c4_bearish_rewards_falling_rsi_penalizes_rising():
    """The exact inversion the audit found: a bearish setup with FALLING RSI (momentum
    with the short) must score high; RISING RSI (against it) must score low."""
    df = _df_1h(rsi=45, macd=-1.0, sig=0.0)                 # bearish base = 10
    with_mom = _c4_momentum(df, _win([50, 50, 44]), "bearish")[0]   # slope -6, with bias
    against = _c4_momentum(df, _win([50, 52, 58]), "bearish")[0]    # slope +8, against bias
    assert with_mom == 15                                   # was 5 under the old sign bug
    assert against == 5                                     # was 15


def test_c4_mirror_long_up_equals_short_down():
    """Mirror property: a long with rising RSI scores the same as the mirrored short
    with falling RSI."""
    long_df = _df_1h(rsi=55, macd=1.0, sig=0.0)
    short_df = _df_1h(rsi=45, macd=-1.0, sig=0.0)           # RSI mirrored about 50, MACD flipped
    long_score = _c4_momentum(long_df, _win([50, 52, 58]), "bullish")[0]   # slope +8
    short_score = _c4_momentum(short_df, _win([50, 48, 42]), "bearish")[0]  # slope -8
    assert long_score == short_score == 15


# ---------------- B6.0: one distance rule for OB and FVG ---------------- #

def _ctx_ob(direction, bottom, top):
    return {"15m": {"order_blocks": {direction: [{"bottom": bottom, "top": top}]}, "fvg": {}}}


def _ctx_fvg(direction, bottom, top):
    return {"15m": {"order_blocks": {}, "fvg": {
        f"nearest_{direction}_fvg": {"bottom": bottom, "top": top, "filled": False}}}}


def test_c2_ob_and_fvg_score_the_same_zone_identically():
    """The audit's case: an identical zone [97.0, 99.8] at price 100, ATR 1.0. Under the
    old far-edge OB / near-edge FVG split it scored 0 as an OB and 15 as an FVG."""
    ob = _c2_ob_prox(100.0, _ctx_ob("bullish", 97.0, 99.8), "bullish", atr_abs=1.0)[0]
    fvg = _c2_ob_prox(100.0, _ctx_fvg("bullish", 97.0, 99.8), "bullish", atr_abs=1.0)[0]
    assert ob == fvg == 15                                  # 0.20 ATR to the near boundary


def test_c2_price_inside_zone_scores_max():
    inside = _c2_ob_prox(98.5, _ctx_ob("bullish", 97.0, 99.8), "bullish", atr_abs=1.0)[0]
    assert inside == 15                                     # distance 0 when price is in the zone


def test_c2_mirror_bullish_below_equals_bearish_above():
    """Mirror: a bullish OB just below price scores the same as a bearish OB the same
    distance above price."""
    bull = _c2_ob_prox(100.0, _ctx_ob("bullish", 97.0, 99.8), "bullish", atr_abs=1.0)[0]
    bear = _c2_ob_prox(100.0, _ctx_ob("bearish", 100.2, 103.0), "bearish", atr_abs=1.0)[0]
    assert bull == bear == 15


def test_zone_distance_basics():
    assert _zone_distance(100.0, 97.0, 99.8) == approx(0.2)   # nearest boundary
    assert _zone_distance(98.5, 97.0, 99.8) == 0.0            # inside
    assert _zone_distance(100.0, 99.8, 97.0) == approx(0.2)   # order-agnostic
    assert _zone_distance(100.0, None, 99.8) is None


# ---------------- B13.0: C1 alignment is the premise, not a bonus ---------------- #

def _ctx_struct(s4, s1):
    return {"4h": {"structure": s4}, "1h": {"structure": s1}}


def test_c1_full_conflict_scores_zero():
    score, note = _c1_trend(_ctx_struct("HH/HL", "LH/LL"), _win([], adx=45))
    assert score == 0
    assert "tf_conflict" in note


def test_c1_partial_scores_five():
    assert _c1_trend(_ctx_struct("HH/HL", "NEUTRAL"), _win([], adx=45))[0] == 5


def test_c1_both_neutral_scores_zero():
    assert _c1_trend(_ctx_struct("NEUTRAL", "NEUTRAL"), _win([], adx=45))[0] == 0


def test_c1_aligned_uses_adx_tiering_and_mirrors():
    for s4, s1 in (("HH/HL", "HH/HL"), ("LH/LL", "LH/LL")):   # bull and its short mirror
        assert _c1_trend(_ctx_struct(s4, s1), _win([], adx=20))[0] == 10   # weak
        assert _c1_trend(_ctx_struct(s4, s1), _win([], adx=30))[0] == 15   # medium
        assert _c1_trend(_ctx_struct(s4, s1), _win([], adx=45))[0] == 20   # strong
        assert _c1_trend(_ctx_struct(s4, s1), _win([], adx=60))[0] == 25   # very strong
