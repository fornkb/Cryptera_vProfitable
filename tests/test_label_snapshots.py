"""
Unit tests for tools/label_snapshots.py::_label_one — the B1.0 chronological
order-state machine: trigger detection (incl. BUG-18 gap-through-entry),
entry-vs-exit reconstruction (intrabar_unknown), already-open SL/TP conservatism,
the entry-bar excursion fix, the strict horizon cut, and end_rr / label fields.
"""

import pandas as pd

from tools.label_snapshots import (
    _label_one, _floor_to_15m, _resolve_1m, _resolve_entry_1m,
    _truncate_to_horizon, LABEL_VERSION,
)


# ---------------- FA-09: replay starts at the snapshot bar's open ---------------- #

def test_floor_to_15m_boundary():
    boundary = 1760000000000 - (1760000000000 % 900_000)  # an exact 15m open
    assert _floor_to_15m(boundary) == boundary
    assert _floor_to_15m(boundary + 30_000) == boundary    # snapshot ~30s after close
    assert _floor_to_15m(boundary + 899_999) == boundary   # anywhere inside the bar
    assert _floor_to_15m(boundary + 900_000) == boundary + 900_000


def _candles(rows):
    """rows: list of (low, high, close) tuples."""
    return pd.DataFrame(rows, columns=["low", "high", "close"])


def _candles_ts(rows, start_ms=1_760_000_400_000):
    """rows: (low, high, close); adds a 15m-spaced UTC timestamp column so an
    ambiguous bar's 1m window can be located."""
    df = pd.DataFrame(rows, columns=["low", "high", "close"])
    df["timestamp"] = pd.to_datetime(
        [start_ms + i * 900_000 for i in range(len(rows))], unit="ms", utc=True)
    return df


def _one_min(rows):
    """rows: (low, high) -> the 1m DataFrame a resolver would return."""
    return pd.DataFrame(rows, columns=["low", "high"])


# ---------------- clean fills, timeout, gap-through, no-data ---------------- #

def test_label_one_long_tp_first():
    df = _candles([
        (98, 102, 100),   # entry(100) fills here, no exit on the entry bar
        (99, 105, 102),
        (101, 111, 108),  # tp(110) hit
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "tp_first"
    assert result["bars_to_close"] == 2
    assert result["resolution_used"] == "15m"


def test_label_one_short_side_tp_first():
    df = _candles([
        (98, 102, 100),  # entry(100) fills
        (85, 99, 90),     # tp(90) hit for a short
    ])
    result = _label_one("short", entry=100, sl=110, tp=90, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "tp_first"
    assert result["bars_to_close"] == 1


def test_label_one_timeout_when_neither_side_hit():
    df = _candles([
        (98, 102, 100),   # fills
        (99, 104, 102),
        (100, 106, 104),  # never reaches sl(95) or tp(110)
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "timeout"
    assert result["mfe_pct"] > 0
    assert result["exit_time"] is None


def test_label_one_not_triggered_no_gap():
    df = _candles([(80, 90, 85), (82, 92, 88), (85, 95, 90)])
    result = _label_one("long", entry=100, sl=70, tp=110, df=df)
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"


def test_label_one_gap_through_entry_counts_as_triggered():
    """BUG-18: a level price gapped THROUGH (no single bar's range contains it)
    must still count as filled."""
    df = _candles([
        (103, 106, 105),  # closes above entry(100), no touch
        (90, 96, 93),      # gapped down through 100 -> filled at the gap
        (91, 97, 94),
    ])
    result = _label_one("long", entry=100, sl=85, tp=110, df=df)
    assert result["triggered"] is True


def test_label_one_no_data_returns_no_data_outcome():
    result = _label_one("long", entry=100, sl=95, tp=110, df=pd.DataFrame())
    assert result == {"triggered": False, "outcome": "no_data"}


# ---------------- B1.0: entry-vs-exit chronology (the core fix) ---------------- #

def test_short_stop_or_target_before_entry_is_unknown_without_1m():
    """The flagship B1.0 case. A short entry=100/sl=105/tp=90 replayed against ONE
    bar low=89 high=101: the bar opened below entry, so a perfectly ordinary path
    hits tp(90) BEFORE the 100 sell ever fills. The old labeler recorded a confident
    tp_first; the state machine records intrabar_unknown (never a silent win)."""
    df = _candles([(89, 101, 95)])
    r = _label_one("short", entry=100, sl=105, tp=90, df=df)   # no resolver
    assert r["triggered"] is True
    assert r["outcome"] == "intrabar_unknown"
    assert r["ambiguous_15m"] is True
    assert r["bars_to_close"] == 0


def test_long_target_before_entry_is_unknown_without_1m():
    """Mirror: long entry=100/sl=95/tp=110, one bar low=99 high=111 that could hit
    tp before the fill -> intrabar_unknown, not a confident tp_first."""
    df = _candles([(99, 111, 105)])
    r = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert r["outcome"] == "intrabar_unknown"


def test_long_stop_before_entry_is_unknown_without_1m():
    """Mirror on the SL side: long entry=100/sl=95/tp=110, one bar low=94 high=101
    could hit sl before the fill -> intrabar_unknown, not a confident sl_first."""
    df = _candles([(94, 101, 96)])
    r = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert r["outcome"] == "intrabar_unknown"


def test_entry_bar_exit_reconstructed_tp_first_via_1m():
    """With 1m data, the entry-bar order IS reconstructable: fill at 100 first (1m
    bar spanning it, no exit), then tp(90) -> tp_first."""
    df = _candles_ts([(89, 101, 95)])
    resolver = lambda ms: _one_min([(99, 101), (88, 95)])  # fill, then tp
    r = _label_one("short", entry=100, sl=105, tp=90, df=df, intrabar_resolver=resolver)
    assert r["outcome"] == "tp_first"
    assert r["resolution_used"] == "1m"


def test_entry_bar_fill_then_no_exit_continues_open():
    """1m shows the entry filled with no exit inside the bar (open_continue); the
    position stays open and a later 15m bar decides it."""
    df = _candles_ts([(99, 111, 105), (108, 112, 110)])  # bar0 straddles entry+tp
    resolver = lambda ms: _one_min([(99, 101), (100, 105)])  # fill, no exit in bar0
    r = _label_one("long", entry=100, sl=95, tp=110, df=df, intrabar_resolver=resolver)
    assert r["outcome"] == "tp_first"      # decided on bar1
    assert r["bars_to_close"] == 1
    assert r["ambiguous_15m"] is True


def test_entry_bar_fill_and_exit_same_1m_bar_is_unknown():
    """If a single 1m bar straddles both the fill and an exit, the order is still
    unknowable -> intrabar_unknown."""
    df = _candles_ts([(89, 101, 95)])
    resolver = lambda ms: _one_min([(88, 101)])  # one 1m bar spans entry(100) AND tp(90)
    r = _label_one("short", entry=100, sl=105, tp=90, df=df, intrabar_resolver=resolver)
    assert r["outcome"] == "intrabar_unknown"


# ---------------- already-open SL-vs-TP race (unchanged pessimism) ---------------- #

def test_open_bar_both_touched_worst_case_without_resolver():
    """A bar AFTER the fill touching both sl and tp keeps the pessimistic sl_first
    tiebreak (f949ab9) when no 1m is available."""
    df = _candles([(98, 102, 100), (90, 120, 110)])  # bar1 hits sl(95) AND tp(110)
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["outcome"] == "sl_first"
    assert result["ambiguous_15m"] is True
    assert result["bars_to_close"] == 1


def test_open_bar_resolved_tp_first_via_1m():
    df = _candles_ts([(98, 102, 100), (90, 120, 110)])
    resolver = lambda ms: _one_min([(99, 105), (101, 111), (90, 100)])  # 1m says tp first
    r = _label_one("long", entry=100, sl=95, tp=110, df=df, intrabar_resolver=resolver)
    assert r["ambiguous_15m"] is True
    assert r["outcome"] == "tp_first"


def test_open_bar_resolved_sl_first_via_1m():
    df = _candles_ts([(98, 102, 100), (90, 120, 110)])
    resolver = lambda ms: _one_min([(99, 105), (90, 100), (101, 111)])  # 1m says sl first
    r = _label_one("long", entry=100, sl=95, tp=110, df=df, intrabar_resolver=resolver)
    assert r["outcome"] == "sl_first"


# ---------------- resolver units (now return (outcome, resolution)) ---------------- #

def test_resolve_1m_tp_before_sl():
    outcome, res = _resolve_1m(_one_min([(99, 105), (101, 111), (90, 100)]), "long", sl=95, tp=110)
    assert outcome == "tp_first" and res == "1m"


def test_resolve_1m_sl_before_tp():
    outcome, res = _resolve_1m(_one_min([(99, 105), (90, 100), (101, 111)]), "long", sl=95, tp=110)
    assert outcome == "sl_first" and res == "1m"


def test_resolve_1m_missing_or_still_ambiguous_falls_back_worst():
    assert _resolve_1m(None, "long", 95, 110) == ("sl_first", "1m_unavailable")
    assert _resolve_1m(_one_min([(90, 120)]), "long", 95, 110) == ("sl_first", "1m_unresolved")


def test_resolve_entry_1m_ignores_pre_fill_exit_touch():
    """A short whose price dips to tp(90) BEFORE rising through entry(100) must not
    count that pre-fill touch; after the fill, price returns to tp -> tp_first."""
    # bar0 dips to 90 (pre-fill, ignored), bar1 fills at 100, bar2 hits tp(90)
    df1m = _one_min([(90, 92), (99, 101), (88, 95)])
    assert _resolve_entry_1m(df1m, "short", entry=100, sl=105, tp=90) == ("tp_first", "1m")


# ---------------- B1.0: strict horizon (drop the +horizon bar) ---------------- #

def test_truncate_to_horizon_drops_the_plus_horizon_bar():
    since = _floor_to_15m(1_760_000_400_000)
    ts = [since + i * 900_000 for i in range(98)]  # 0..97 -> covers past +24h
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(ts, unit="ms", utc=True),
        "low": 1.0, "high": 2.0, "close": 1.5,
    })
    out = _truncate_to_horizon(df, since, 24)
    assert len(out) == 96                                   # 24h * 4 bars; not 97
    # last KEPT bar opens at +23h45m (closes exactly at +24h); the +24h open is dropped
    assert out["timestamp"].iloc[-1] == pd.Timestamp(since + 95 * 900_000, unit="ms", tz="UTC")


# ---------------- end_rr / label fields ---------------- #

def test_end_rr_and_label_fields_present():
    df = _candles([(98, 102, 100), (99, 104, 102), (100, 106, 104)])  # timeout
    r = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert r["end_rr"] is not None                          # close-at-horizon in R
    assert "entry_time" in r and "resolution_used" in r


def test_end_rr_sign_short_terminal_below_entry_is_favourable():
    # short filled at 100, price closes the window at 96 -> favourable, end_rr > 0
    df = _candles([(98, 102, 100), (95, 99, 96)])
    r = _label_one("short", entry=100, sl=105, tp=80, df=df)  # tp far, so timeout
    assert r["outcome"] == "timeout"
    assert r["end_rr"] > 0


def test_label_version_is_current():
    assert LABEL_VERSION == 2
