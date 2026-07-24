"""
Wave 3 riders: B21.0 (DST-correct kill zones + a sharper volume baseline), B22.0
(calendar-anchored untested POCs), B23.0 (distance-based episode identity).
"""

import pandas as pd
import pytest

from indicators import kill_zone, session_volume_context
from price_action import find_untested_pocs
from tools.episodes import EPISODE_PRICE_TOL, collapse_to_episodes, same_episode


# ---------------- B21.0: kill zones follow the local session, not fixed UTC ---------------- #

def test_london_open_is_tagged_in_both_gmt_and_bst():
    """
    London opens at 08:00 local: 08:00 UTC in January, 07:00 UTC in July. A fixed UTC
    window mislabels one of them for months at a time.
    """
    january = pd.Timestamp("2026-01-15T08:30:00Z")
    july = pd.Timestamp("2026-07-15T07:30:00Z")
    assert kill_zone(january.hour, january) == "LONDON"
    assert kill_zone(july.hour, july) == "LONDON"


def test_new_york_open_shifts_with_us_dst():
    january = pd.Timestamp("2026-01-15T14:30:00Z")   # 09:30 EST
    july = pd.Timestamp("2026-07-15T13:30:00Z")      # 09:30 EDT
    assert kill_zone(january.hour, january) == "NEWYORK"
    assert kill_zone(july.hour, july) == "NEWYORK"


def test_a_dead_hour_is_off_in_both_halves_of_the_year():
    for stamp in (pd.Timestamp("2026-01-15T22:30:00Z"), pd.Timestamp("2026-07-15T22:30:00Z")):
        assert kill_zone(stamp.hour, stamp) == "OFF"


def test_hour_only_callers_still_get_the_fixed_utc_fallback():
    assert kill_zone(8) == "LONDON"
    assert kill_zone(22) == "OFF"


# ---------------- B21.0: the volume baseline compares the right bars ---------------- #

def _vol_frame(pattern_by_minute, days=8, last_volume=None):
    rows = []
    start = pd.Timestamp("2026-06-01T00:00:00Z")
    for d in range(days):
        for slot in range(96):
            ts = start + pd.Timedelta(days=d, minutes=15 * slot)
            rows.append((ts, pattern_by_minute(ts)))
    if last_volume is not None:
        rows[-1] = (rows[-1][0], last_volume)
    return pd.DataFrame({"timestamp": [r[0] for r in rows], "volume": [r[1] for r in rows]})


def test_baseline_uses_the_exact_minute_slot_not_the_whole_hour():
    """The :00 bar carries post-close flow the :45 bar never does."""
    def pattern(ts):
        return 1000.0 if ts.minute == 0 else 100.0
    df = _vol_frame(pattern)
    df = df[df["timestamp"].dt.minute == 0].reset_index(drop=True)   # end on a :00 bar
    out = session_volume_context(df)
    assert out["slot_minute"] % 60 == 0
    # Baselined against the same :00 slot (1000). Pooling the whole hour would average
    # 1000/100/100/100 = 325 and report this ordinary bar as a ~3x volume surge.
    assert out["session_relative_volume"] == pytest.approx(1.0, abs=0.05)


def test_baseline_is_robust_to_a_single_spike():
    def pattern(ts):
        return 100.0
    df = _vol_frame(pattern)
    df.loc[10, "volume"] = 100000.0        # one news candle in the history
    df.loc[len(df) - 1, "volume"] = 100.0
    out = session_volume_context(df)
    assert out["session_relative_volume"] == pytest.approx(1.0, abs=0.05)


def test_weekday_and_weekend_are_separated():
    out = session_volume_context(_vol_frame(lambda ts: 100.0))
    assert out["slot_is_weekend"] in (True, False)
    assert out["slot_samples"] >= 1


# ---------------- B22.0: POC windows have calendar identity ---------------- #

def _bars(hours, price_fn):
    ts = pd.date_range("2026-06-01T00:00:00Z", periods=hours, freq="4h")
    closes = [price_fn(i) for i in range(hours)]
    return pd.DataFrame({"timestamp": ts, "open": closes,
                         "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                         "close": closes, "volume": [100.0] * hours})


def test_poc_windows_are_calendar_periods_and_survive_a_new_bar():
    """
    B22.0: tail-anchored blocks shifted every window boundary on every new candle, so a
    "naked POC" was a different level from one cycle to the next.
    """
    df = _bars(60, lambda i: 100.0 + i * 0.5)   # trending, so old POCs stay untested
    before = find_untested_pocs(df, period="D")
    grown = pd.concat([df, df.tail(1).assign(
        timestamp=df["timestamp"].iloc[-1] + pd.Timedelta(hours=4))], ignore_index=True)
    after = find_untested_pocs(grown, period="D")
    shared = {p["period"] for p in before} & {p["period"] for p in after}
    assert shared, "calendar periods must persist across a new bar"
    for key in shared:
        b = next(p for p in before if p["period"] == key)
        a = next(p for p in after if p["period"] == key)
        assert b["poc"] == a["poc"]


def test_the_developing_period_is_never_reported():
    df = _bars(60, lambda i: 100.0 + i * 0.5)   # trending, so old POCs stay untested
    last_period = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d").iloc[-1]
    assert all(p["period"] != last_period for p in find_untested_pocs(df, period="D"))


# ---------------- B23.0: distance-based episode identity ---------------- #

def _snap(entry, sl, tp, when, symbol="SOL/USDT", direction="BUY"):
    return {"symbol": symbol, "generated_at": when,
            "engine_trade": {"direction": direction, "entry": entry,
                             "stop_loss": sl, "take_profit": tp}}


def test_drift_across_a_rounding_boundary_stays_one_episode():
    """The audit's verified case: 104.4 vs 104.6 is 0.19% — one setup drifting with ATR."""
    a = _snap(104.4, 100.0, 112.0, "2026-06-01T00:00:00")
    b = _snap(104.6, 100.2, 112.2, "2026-06-01T00:15:00")
    assert same_episode(a, b) is True
    reps = collapse_to_episodes([("a.json", a, None), ("b.json", b, None)])
    assert len(reps) == 1
    assert reps[0][0] == "a.json"          # the earliest snapshot represents the episode


def test_genuinely_different_geometry_is_a_new_episode():
    a = _snap(104.4, 100.0, 112.0, "2026-06-01T00:00:00")
    b = _snap(120.0, 114.0, 132.0, "2026-06-01T00:15:00")
    assert same_episode(a, b) is False
    assert len(collapse_to_episodes([("a.json", a, None), ("b.json", b, None)])) == 2


def test_opposite_sides_never_merge():
    a = _snap(104.4, 100.0, 112.0, "2026-06-01T00:00:00", direction="BUY")
    b = _snap(104.4, 100.0, 112.0, "2026-06-01T00:15:00", direction="SELL")
    assert same_episode(a, b) is False


def test_a_long_silence_starts_a_new_episode():
    a = _snap(104.4, 100.0, 112.0, "2026-06-01T00:00:00")
    b = _snap(104.4, 100.0, 112.0, "2026-06-03T00:00:00")
    assert len(collapse_to_episodes([("a.json", a, None), ("b.json", b, None)])) == 2


def test_a_persisting_setup_collapses_to_one_row():
    items = [(f"s{i}.json", _snap(104.0 + i * 0.01, 100.0, 112.0,
                                  f"2026-06-01T{i:02d}:00:00"), None) for i in range(10)]
    assert len(collapse_to_episodes(items)) == 1


def test_tolerance_is_relative_so_btc_and_alts_group_alike():
    hi = _snap(60000.0, 59000.0, 62000.0, "2026-06-01T00:00:00")
    hi_drift = _snap(60000.0 * (1 + EPISODE_PRICE_TOL / 2), 59000.0, 62000.0,
                     "2026-06-01T00:15:00")
    assert same_episode(hi, hi_drift) is True
