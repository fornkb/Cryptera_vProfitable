"""
I6.0 / B9.0: OTE anchored to the impulse leg of the last structural break, and the
re-bracketed dealing range no longer keeping the pre-breakout leg direction.
"""

import pandas as pd
import pytest

from smc import _current_dealing_range, build_smc_context, impulse_leg_ote

from test_structure_machine import _frame, _mirror


def _bos(direction, origin_idx, origin_price, break_idx=None):
    return {"direction": direction, "origin_idx": origin_idx, "origin_price": origin_price,
            "break_idx": break_idx if break_idx is not None else origin_idx + 3,
            "level": origin_price, "fresh": True}


def _no_choch():
    return {"detected": False, "break_idx": None}


# ---------------- B9.0: the re-bracketed range keeps an honest leg ---------------- #

def test_rebracketed_range_recomputes_the_leg_direction():
    """
    Verified case from the audit: highs [100,105], lows [95,98], price 96 -> the range
    is correctly re-bracketed to [95,100] but `leg` used to still say bullish because the
    most recent confirmed swing happened to be a high.
    """
    highs = [{"idx": 1, "ts": "t1", "price": 100.0}, {"idx": 5, "ts": "t5", "price": 105.0}]
    lows = [{"idx": 2, "ts": "t2", "price": 95.0}, {"idx": 6, "ts": "t6", "price": 98.0}]
    hi, lo, leg, brackets = _current_dealing_range(highs, lows, current_price=96.0)
    assert (hi, lo) == (100.0, 95.0)
    assert brackets is True
    # the chosen bracket is high@t1 -> low@t2: a bearish leg, not the bullish one the
    # latest confirmed swing implied
    assert leg == "bearish"


def test_price_beyond_all_structure_has_no_leg():
    highs = [{"idx": 1, "ts": "t1", "price": 100.0}, {"idx": 5, "ts": "t5", "price": 105.0}]
    lows = [{"idx": 2, "ts": "t2", "price": 95.0}, {"idx": 6, "ts": "t6", "price": 98.0}]
    hi, lo, leg, brackets = _current_dealing_range(highs, lows, current_price=120.0)
    assert brackets is False
    assert leg is None


# ---------------- I6.0: OTE on the impulse leg ---------------- #

def test_bullish_impulse_ote_is_the_discount_band_of_that_leg():
    df = _frame([100, 90, 110, 98, 120, 112, 135])
    band = impulse_leg_ote(df, _bos("BULLISH", origin_idx=20, origin_price=112.0),
                           _no_choch(), current_price=120.0)
    assert band["leg_direction"] == "bullish"
    assert band["leg_source"] == "bos_impulse"
    assert band["leg_low"] == pytest.approx(112.0)
    # the leg extreme is the highest high since the impulse origin
    assert band["leg_high"] == pytest.approx(float(df["high"].iloc[20:].max()))
    assert band["ote_zone"]["low"] < band["ote_zone"]["high"] < band["leg_high"]


def test_bearish_impulse_ote_mirrors():
    df = _mirror(_frame([100, 90, 110, 98, 120, 112, 135]))
    origin_price = float(df["low"].iloc[20]) + (float(df["high"].iloc[20]) - float(df["low"].iloc[20]))
    band = impulse_leg_ote(df, _bos("BEARISH", origin_idx=20, origin_price=origin_price),
                           _no_choch(), current_price=float(df["close"].iloc[-1]))
    assert band["leg_direction"] == "bearish"
    assert band["leg_high"] == pytest.approx(origin_price)
    assert band["leg_low"] == pytest.approx(float(df["low"].iloc[20:].min()))
    assert band["ote_zone"]["low"] > band["leg_low"]


def test_a_fully_retraced_leg_has_no_ote():
    """A retracement past 100% is not a retracement — the leg is void, not inverted."""
    df = _frame([100, 90, 110, 98, 120, 112, 135])
    assert impulse_leg_ote(df, _bos("BULLISH", 20, 112.0), _no_choch(),
                           current_price=105.0) is None


def test_the_more_recent_event_owns_the_leg():
    df = _frame([100, 90, 110, 98, 120, 112, 135])
    bos = _bos("BULLISH", origin_idx=20, origin_price=112.0, break_idx=22)
    choch = {"detected": True, "direction": "BEARISH", "origin_idx": 16,
             "origin_price": 200.0, "break_idx": 25}
    band = impulse_leg_ote(df, bos, choch, current_price=120.0)
    assert band["leg_direction"] == "bearish"     # the CHoCH broke later
    assert band["leg_source"] == "choch_impulse"


def test_no_event_means_no_impulse_leg():
    df = _frame([100, 90, 110, 98, 120, 112, 135])
    assert impulse_leg_ote(df, {"break_idx": None}, _no_choch(), 120.0) is None


# ---------------- assembled context ---------------- #

def test_context_ote_direction_agrees_with_the_break_that_produced_it():
    for points in ([100, 90, 110, 98, 120, 112, 135], [135, 112, 120, 98, 110, 90, 100]):
        df = _frame(points)
        ctx = build_smc_context(df, df, df)["15m"]
        pdz = ctx["premium_discount"]
        bos_dir = (ctx["bos"].get("direction") or "").lower()
        if pdz.get("leg_source", "").endswith("_impulse") and bos_dir:
            assert pdz["leg_direction"] == bos_dir
        # premium/discount stays a property of the bracketing range, always reported
        assert pdz["zone"] in ("premium", "discount", "neutral")
