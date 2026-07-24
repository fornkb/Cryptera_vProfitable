"""
I4.0: setups as ORDERED sequences with an expiry (balance -> raid -> MSS for the fade;
displacement break -> pullback -> return-to-zone for continuation), plus B18.0's
absorption-vs-initiative relabel. Directional logic asserted on both mirrored sides.
"""

import pandas as pd
import pytest

import strategies
from config import (BALANCE_MIN_ROTATIONS, CONTINUATION_SETUP_EXPIRY_BARS,
                    MR_SETUP_EXPIRY_BARS, SWEEP_MIN_PENETRATION_ATR)
from strategies import (_count_rotations, _detect_mss, _detect_raid, _m3_cvd_absorption,
                        detect_continuation_sequence, detect_mr_sequence, evaluate_strategies)

VA = {"vah": 104.0, "val": 96.0}
ATR = 2.0


def _tape(closes, highs=None, lows=None):
    highs = highs if highs is not None else [c + 0.3 for c in closes]
    lows = lows if lows is not None else [c - 0.3 for c in closes]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "volume": [1.0] * len(closes), "ATR": [ATR] * len(closes)})


def _rotating(cycles=3):
    """A tape that traverses the value area `cycles` times."""
    closes = []
    for _ in range(cycles):
        closes += [97.0, 100.0, 103.0, 100.0]
    return closes


def _ev(direction, break_idx, bars_ago, displacement=True, kind="bos"):
    ev = {"direction": direction, "break_idx": break_idx, "bars_ago": bars_ago,
          "displacement": displacement, "level": 100.0, "fresh": True}
    if kind == "choch":
        ev["detected"] = True
    return ev


def _ctx15(bos=None, choch=None):
    return {"15m": {"bos": bos or {"break_idx": None, "direction": None},
                    "choch": choch or {"detected": False, "break_idx": None}}}


# ---------------- balance ---------------- #

def test_rotations_count_traversals_not_touches():
    assert _count_rotations(_tape(_rotating(3)), VA) >= BALANCE_MIN_ROTATIONS
    one_way = _tape([96.0 + i * 0.5 for i in range(30)])
    assert _count_rotations(one_way, VA) < BALANCE_MIN_ROTATIONS


# ---------------- raid ---------------- #

@pytest.mark.parametrize("mr_dir,edge", [("bearish", 104.0), ("bullish", 96.0)])
def test_a_deep_raid_that_closes_back_inside_is_found(mr_dir, edge):
    closes = _rotating(3) + [100.0] * 3
    df = _tape(closes)
    i = len(closes) - 2
    pen = SWEEP_MIN_PENETRATION_ATR * ATR + 0.5
    if mr_dir == "bearish":
        df.loc[i, "high"] = edge + pen
    else:
        df.loc[i, "low"] = edge - pen
    idx, extreme = _detect_raid(df, edge, mr_dir, ATR)
    assert idx == i
    assert extreme == pytest.approx(edge + pen if mr_dir == "bearish" else edge - pen)


def test_a_graze_is_not_a_raid():
    df = _tape(_rotating(3) + [100.0] * 3)
    i = len(df) - 2
    df.loc[i, "high"] = 104.0 + SWEEP_MIN_PENETRATION_ATR * ATR * 0.4
    assert _detect_raid(df, 104.0, "bearish", ATR) == (None, None)


def test_a_close_outside_the_edge_is_a_breakout_not_a_raid():
    closes = _rotating(3) + [100.0, 106.0]
    df = _tape(closes)
    df.loc[len(df) - 1, "high"] = 107.0
    assert _detect_raid(df, 104.0, "bearish", ATR) == (None, None)


# ---------------- MSS ---------------- #

def test_mss_must_come_after_the_raid_and_in_the_fade_direction():
    after = _ctx15(bos=_ev("BEARISH", break_idx=20, bars_ago=3))
    before = _ctx15(bos=_ev("BEARISH", break_idx=5, bars_ago=3))
    wrong = _ctx15(bos=_ev("BULLISH", break_idx=20, bars_ago=3))
    assert _detect_mss(after, "bearish", after_idx=10) is not None
    assert _detect_mss(before, "bearish", after_idx=10) is None
    assert _detect_mss(wrong, "bearish", after_idx=10) is None


# ---------------- the sequence ---------------- #

def _ready_case(mr_dir):
    edge = 104.0 if mr_dir == "bearish" else 96.0
    closes = _rotating(3) + [100.0] * 3
    df = _tape(closes)
    raid_i = len(closes) - 2
    pen = SWEEP_MIN_PENETRATION_ATR * ATR + 0.5
    if mr_dir == "bearish":
        df.loc[raid_i, "high"] = edge + pen
    else:
        df.loc[raid_i, "low"] = edge - pen
    direction = "BEARISH" if mr_dir == "bearish" else "BULLISH"
    ctx = _ctx15(bos=_ev(direction, break_idx=raid_i + 1, bars_ago=1))
    return df, ctx, edge


@pytest.mark.parametrize("mr_dir", ["bearish", "bullish"])
def test_a_complete_ordered_sequence_is_ready_on_both_sides(mr_dir):
    df, ctx, edge = _ready_case(mr_dir)
    seq = detect_mr_sequence(df, ctx, mr_dir, edge, VA, ATR)
    assert seq["valid"] is True
    assert seq["stage"] == "ready"
    assert seq["mss"] is True and seq["displacement"] is True
    assert seq["expires_in_bars"] == MR_SETUP_EXPIRY_BARS - 1


def test_the_sequence_names_what_it_is_waiting_for():
    df, ctx, edge = _ready_case("bearish")
    one_way = _tape([96.0 + i * 0.4 for i in range(30)])
    assert detect_mr_sequence(one_way, ctx, "bearish", edge, VA, ATR)["stage"] == "no_balance"

    no_raid = _tape(_rotating(3) + [100.0] * 3)
    assert detect_mr_sequence(no_raid, ctx, "bearish", edge, VA, ATR)["stage"] == "awaiting_raid"

    seq = detect_mr_sequence(df, _ctx15(), "bearish", edge, VA, ATR)
    assert seq["stage"] == "awaiting_mss" and seq["valid"] is False


def test_a_completed_setup_expires():
    df, _, edge = _ready_case("bearish")
    raid_i = len(df) - 2
    stale = _ctx15(bos=_ev("BEARISH", break_idx=raid_i + 1,
                           bars_ago=MR_SETUP_EXPIRY_BARS + 1))
    seq = detect_mr_sequence(df, stale, "bearish", edge, VA, ATR)
    assert seq["stage"] == "expired" and seq["valid"] is False


# ---------------- the hard gate ---------------- #

def test_no_sequence_means_no_mr_trade_however_high_the_score(monkeypatch):
    """I4.0's headline rule: order matters, an additive score cannot substitute for it."""
    monkeypatch.setattr(strategies, "_CALIBRATION_CACHE", {
        "weights": {}, "mr_weights": {}, "score_to_winrate": [], "logistic": {}})
    df = _tape(_rotating(3) + [103.9] * 3)      # parked at the fade edge, never raided
    out = evaluate_strategies(
        df_4h=None, df_1h=None, df_15m=df, orderbook=None, funding=None, sentiment=None,
        open_interest=None,
        smc_context={"4h": {"structure": "NEUTRAL"}, "1h": {"structure": "NEUTRAL"},
                     **_ctx15(),
                     "15m": {**_ctx15()["15m"],
                             "order_blocks": {"bullish": [], "bearish": []},
                             "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                             "liquidity_levels": {"buy_side": [], "sell_side": []},
                             "dealing_range": {}}},
        windowed_indicators={}, pa_context={"value_area_1h": VA},
        market_regime="Ranging / Sideways")
    assert out["setup_direction"] == "neutral"
    assert out["confluence_score"] == 0
    assert out["setup_sequence"]["valid"] is False
    assert out["setup_sequence"]["stage"] == "awaiting_raid"


# ---------------- B18.0: absorption vs initiative ---------------- #

@pytest.mark.parametrize("mr_dir,against", [("bullish", -1), ("bearish", 1)])
def test_absorption_is_flow_against_the_fade_while_the_edge_holds(mr_dir, against):
    edge = 96.0 if mr_dir == "bullish" else 104.0
    holding = _tape([100.0, 100.0, 100.0, 100.0])
    pts, note = _m3_cvd_absorption({"15m": {"cvd_window_delta": against * 100}},
                                   mr_dir, holding, edge)
    assert pts == 15 and "absorption" in note


@pytest.mark.parametrize("mr_dir,with_fade", [("bullish", 1), ("bearish", -1)])
def test_initiative_flow_is_scored_lower_and_named_honestly(mr_dir, with_fade):
    """B18.0: this used to pay 15 and assert 'CVD absorption' to the LLM."""
    edge = 96.0 if mr_dir == "bullish" else 104.0
    pts, note = _m3_cvd_absorption({"15m": {"cvd_window_delta": with_fade * 100}},
                                   mr_dir, _tape([100.0] * 4), edge)
    assert pts == 10
    assert "initiative" in note and "absorption" not in note.replace("not absorption", "")


def test_flow_against_the_fade_with_a_failing_edge_pays_nothing():
    breaking = _tape([100.0, 99.0, 98.0, 97.0])     # new lows each bar
    pts, _ = _m3_cvd_absorption({"15m": {"cvd_window_delta": -100}}, "bullish", breaking, 96.0)
    assert pts == 0


# ---------------- continuation sequence ---------------- #

def _cont_ctx(bars_ago, leg=(90.0, 110.0), in_ote=False, zone=False):
    lo, hi = leg
    ctx = {"1h": {"bos": _ev("BULLISH", break_idx=50, bars_ago=bars_ago),
                  "premium_discount": {"leg_source": "bos_impulse", "leg_high": hi,
                                       "leg_low": lo, "in_ote": in_ote}}}
    ctx["15m"] = {"order_blocks": {"bullish": [{"top": 101.0, "bottom": 99.0}] if zone else [],
                                   "bearish": []},
                  "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None}}
    return ctx


def test_continuation_stages_break_then_pullback_then_return_to_zone():
    assert detect_continuation_sequence(_cont_ctx(3), "bullish", 109.5)["stage"] == "break"
    pulled = detect_continuation_sequence(_cont_ctx(3), "bullish", 100.0)
    assert pulled["stage"] == "pullback" and pulled["valid"] is False
    ready = detect_continuation_sequence(_cont_ctx(3, zone=True), "bullish", 100.0)
    assert ready["stage"] == "return_to_zone" and ready["valid"] is True


def test_continuation_setups_expire_too():
    stale = detect_continuation_sequence(
        _cont_ctx(CONTINUATION_SETUP_EXPIRY_BARS + 1, zone=True), "bullish", 100.0)
    assert stale["stage"] == "expired" and stale["valid"] is False
