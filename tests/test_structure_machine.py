"""
I3.0 golden scenarios for the structural event state machine (smc.structural_state_machine).

Every scenario is asserted on BOTH mirrored sides: the same tape reflected through a
constant must produce the same events with inverted directions. That mirror property is
I3.0's stated exit criterion — it is what proves BOS/CHoCH classification carries no
directional bias (the class of bug B5.0/B17.0 came from).
"""

import pandas as pd
import pytest

from smc import _confirmed_swings, get_market_structure, structural_state_machine

LEFT, RIGHT = 2, 2
MIRROR_K = 400.0


def _frame(points, per_leg=4, wick=0.05):
    """
    Zigzag OHLC frame walking linearly between successive pivot prices. `wick` sets the
    high/low padding: small wicks leave 3-candle gaps in each leg (displacement present),
    a wick wider than the per-bar step closes them (no displacement).
    """
    closes = [float(points[0])]
    for a, b in zip(points, points[1:]):
        step = (b - a) / per_leg
        closes.extend(round(a + step * k, 6) for k in range(1, per_leg + 1))
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC"),
        "open": closes,
        "high": [c + wick for c in closes],
        "low": [c - wick for c in closes],
        "close": closes,
        "volume": [1.0] * len(closes),
    })


def _mirror(df):
    """Reflect the tape through MIRROR_K: highs become lows, up-legs become down-legs."""
    out = df.copy()
    out["close"] = MIRROR_K - df["close"]
    out["open"] = MIRROR_K - df["open"]
    out["high"] = MIRROR_K - df["low"]
    out["low"] = MIRROR_K - df["high"]
    return out


def _run(df):
    highs, lows = _confirmed_swings(df, LEFT, RIGHT)
    return structural_state_machine(highs, lows, df)


def _flip(direction):
    return {"BULLISH": "BEARISH", "BEARISH": "BULLISH"}.get(direction)


def _assert_mirrored(points, **kw):
    """
    The mirror property: identical event timeline, inverted directions, identical
    displacement flags and identical resulting state. Returns the upright machine result.
    """
    df = _frame(points, **kw)
    up, dn = _run(df), _run(_mirror(df))

    assert len(up["events"]) == len(dn["events"])
    for a, b in zip(up["events"], dn["events"]):
        assert a["type"] == b["type"]
        assert b["direction"] == _flip(a["direction"])
        assert a["break_idx"] == b["break_idx"]
        assert a["origin_idx"] == b["origin_idx"]
        assert a["displacement"] == b["displacement"]
    assert up["state"] == dn["state"]
    assert {"bullish": "bearish", "bearish": "bullish"}.get(up["trend"]) == dn["trend"]
    assert {"bullish": "bearish", "bearish": "bullish"}.get(
        up["pending_direction"]) == dn["pending_direction"]
    return up


# ---------------- G1: clean continuation ---------------- #

def test_g1_continuation_emits_bos_and_never_a_choch():
    st = _assert_mirrored([100, 90, 110, 98, 120, 112, 135])
    assert st["state"] == "trending"
    assert st["trend"] == "bullish"
    assert [e["type"] for e in st["events"]] == ["BOS"] * len(st["events"])
    assert all(e["direction"] == "BULLISH" for e in st["events"])


# ---------------- G2: B7.0 — one break is ONE event ---------------- #

DOWNTREND_THEN_CHOCH = [130, 110, 124, 104, 118, 96, 122, 114]


def test_g2_counter_break_is_choch_only_not_also_bos():
    """
    B7.0: in a bearish structure the first close above the last lower-high used to be
    emitted as bullish BOS *and* bullish CHoCH from the same bar.
    """
    st = _assert_mirrored(DOWNTREND_THEN_CHOCH)
    kinds = [(e["type"], e["direction"], e["break_idx"]) for e in st["events"]]
    assert ("BOS", "BEARISH") == kinds[0][:2]
    assert ("CHOCH", "BULLISH") == kinds[-1][:2]
    # no bar produced two events
    assert len({k[2] for k in kinds}) == len(kinds)
    assert st["state"] == "transition"
    assert st["trend"] == "bearish"          # label has NOT flipped yet
    assert st["pending_direction"] == "bullish"


def test_g2_public_api_reports_bos_and_choch_on_different_bars():
    df = _frame(DOWNTREND_THEN_CHOCH)
    structure, bos, choch, state = get_market_structure(*_confirmed_swings(df, LEFT, RIGHT), df)
    assert bos["direction"] == "BEARISH"
    assert choch["detected"] is True and choch["direction"] == "BULLISH"
    assert bos["break_idx"] != choch["break_idx"]
    assert structure == "TRANSITION"
    assert state["state"] == "transition"


# ---------------- G3: transition confirmation ---------------- #

CONFIRMED_REVERSAL = DOWNTREND_THEN_CHOCH + [138, 130]


def test_g3_displaced_confirming_bos_flips_the_trend():
    st = _assert_mirrored(CONFIRMED_REVERSAL)
    assert st["state"] == "trending"
    assert st["trend"] == "bullish"
    assert st["pending_direction"] is None
    assert [e["type"] for e in st["events"]] == ["BOS", "BOS", "CHOCH", "BOS"]
    assert st["events"][-1]["displacement"] is True


def test_g3_structure_label_follows_the_machine_not_the_swing_majority():
    df = _frame(CONFIRMED_REVERSAL)
    structure, _, choch, _ = get_market_structure(*_confirmed_swings(df, LEFT, RIGHT), df)
    assert structure == "HH/HL"
    assert choch["confirmed"] is True


# ---------------- G4: no displacement -> no flip ---------------- #

def test_g4_undisplaced_confirming_break_holds_the_transition():
    st = _assert_mirrored(CONFIRMED_REVERSAL, wick=8.0)
    assert st["events"][-1]["displacement"] is False
    assert st["state"] == "transition"
    assert st["trend"] == "bearish"


def test_g4_second_confirming_break_flips_without_displacement():
    """Untidy impulses still count once the market has broken structure twice."""
    st = _assert_mirrored(CONFIRMED_REVERSAL + [150, 142, 170, 162], wick=8.0)
    assert all(e["displacement"] is False for e in st["events"])
    assert [e["type"] for e in st["events"]] == ["CHOCH", "BOS", "BOS"]
    assert st["state"] == "trending"
    assert st["trend"] == "bullish"


# ---------------- G5: displacement is anchored to the breaking impulse ---------------- #

def _gapped_then_crawling():
    """
    A hard-gapping rally into the swing high that later gets broken, then a pullback,
    then a CRAWLING (fully overlapping) impulse that does the breaking.
    """
    head = _frame([100, 90, 110, 98, 120, 105, 118, 112], per_leg=4, wick=0.05)
    crawl = _frame([112, 135], per_leg=20, wick=3.0).iloc[1:]
    df = pd.concat([head, crawl], ignore_index=True)
    df["timestamp"] = pd.date_range("2026-01-01", periods=len(df), freq="15min", tz="UTC")
    return df


def test_g5_displacement_ignores_gaps_outside_the_breaking_impulse():
    """
    B7.0 secondary: displacement was certified by ANY same-direction FVG between the
    broken swing and the break bar, so a gap dozens of bars before the actual impulse
    qualified. Anchoring to the impulse origin is what makes the flag mean something.
    """
    from smc import _has_displacement_fvg

    df = _gapped_then_crawling()
    ev = _run(df)["events"][-1]
    assert (ev["type"], ev["direction"]) == ("BOS", "BULLISH")
    # the origin is the pullback low INSIDE the impulse, not the broken swing
    assert ev["origin_idx"] > ev["ref_idx"]
    assert ev["displacement"] is False
    # the old unanchored span (broken swing -> break) still contains a stale gap
    assert _has_displacement_fvg(df, ev["ref_idx"], ev["break_idx"], "BULLISH") is True


def test_g5_mirrors():
    df = _gapped_then_crawling()
    up, dn = _run(df), _run(_mirror(df))
    assert [e["displacement"] for e in up["events"]] == [e["displacement"] for e in dn["events"]]
    assert [_flip(e["direction"]) for e in up["events"]] == [e["direction"] for e in dn["events"]]


# ---------------- G6: degenerate inputs ---------------- #

def test_g6_too_few_swings_is_neutral():
    structure, bos, choch, state = get_market_structure(
        [{"idx": 1, "ts": "t1", "price": 100.0}], [], df=None)
    assert structure == "NEUTRAL"
    assert bos["level"] is None and choch["detected"] is False
    assert state["state"] == "neutral" and state["trend"] is None


def test_g6_flat_tape_produces_no_structural_events():
    df = _frame([100, 100, 100, 100], wick=0.05)
    st = _run(df)
    assert st["events"] == []
    assert st["trend"] is None
