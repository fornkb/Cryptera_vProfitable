"""
Unit tests for tools/label_snapshots.py::_label_one — trigger detection
(including the BUG-18 gap-through-entry fix), same-bar SL/TP conservatism,
and timeout handling.
"""

import pandas as pd

from tools.label_snapshots import _label_one, _floor_to_15m, _resolve_1m


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


def test_label_one_long_tp_first():
    df = _candles([
        (98, 102, 100),   # entry(100) triggers here
        (99, 105, 102),
        (101, 111, 108),  # tp(110) hit
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "tp_first"
    assert result["bars_to_close"] == 2


def test_label_one_both_touched_same_bar_worst_case_without_resolver():
    """No 1m resolver supplied -> the pessimistic sl_first assumption is preserved
    (and the ambiguity is flagged)."""
    df = _candles([
        (98, 102, 100),   # entry triggers
        (90, 120, 110),   # low<=95(SL) AND high>=110(TP) in the SAME bar
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["outcome"] == "sl_first"
    assert result["ambiguous_15m"] is True
    assert result["bars_to_close"] == 1


# ---------------- 1m intrabar resolution of the SL-vs-TP race ---------------- #

def _candles_ts(rows, start_ms=1_760_000_400_000):
    """rows: (low, high, close); adds a 15m-spaced UTC timestamp column so the
    ambiguous-bar drill can compute the bar's 1m window."""
    df = pd.DataFrame(rows, columns=["low", "high", "close"])
    df["timestamp"] = pd.to_datetime(
        [start_ms + i * 900_000 for i in range(len(rows))], unit="ms", utc=True)
    return df


def _one_min(rows):
    """rows: (low, high) -> the 1m DataFrame a resolver would return."""
    return pd.DataFrame(rows, columns=["low", "high"])


def test_resolve_1m_tp_before_sl():
    df1 = _one_min([(99, 105), (101, 111), (90, 100)])   # bar1 hits tp(110) first
    assert _resolve_1m(df1, "long", sl=95, tp=110) == "tp_first"


def test_resolve_1m_sl_before_tp():
    df1 = _one_min([(99, 105), (90, 100), (101, 111)])   # bar1 hits sl(95) first
    assert _resolve_1m(df1, "long", sl=95, tp=110) == "sl_first"


def test_resolve_1m_missing_or_still_ambiguous_falls_back_worst():
    assert _resolve_1m(None, "long", 95, 110) == "sl_first"
    assert _resolve_1m(_one_min([(90, 120)]), "long", 95, 110) == "sl_first"  # 1m bar straddles both


def test_label_one_ambiguous_bar_resolved_tp_first_via_1m():
    df = _candles_ts([(98, 102, 100), (90, 120, 110)])   # bar1 straddles both at 15m
    resolver = lambda ms: _one_min([(99, 105), (101, 111), (90, 100)])  # 1m says tp first
    r = _label_one("long", entry=100, sl=95, tp=110, df=df, intrabar_resolver=resolver)
    assert r["ambiguous_15m"] is True
    assert r["outcome"] == "tp_first"          # was sl_first under the old worst-case rule


def test_label_one_ambiguous_bar_resolved_sl_first_via_1m():
    df = _candles_ts([(98, 102, 100), (90, 120, 110)])
    resolver = lambda ms: _one_min([(99, 105), (90, 100), (101, 111)])  # 1m says sl first
    r = _label_one("long", entry=100, sl=95, tp=110, df=df, intrabar_resolver=resolver)
    assert r["outcome"] == "sl_first"


def test_label_one_not_triggered_no_gap():
    # Entry=100 is always above every bar's high, and price never gaps across it.
    df = _candles([
        (80, 90, 85),
        (82, 92, 88),
        (85, 95, 90),
    ])
    result = _label_one("long", entry=100, sl=70, tp=110, df=df)
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"


def test_label_one_gap_through_entry_counts_as_triggered():
    """BUG-18: a limit level the price gapped THROUGH (no single bar's range
    contains it) must still count as filled, not silently dropped."""
    df = _candles([
        (103, 106, 105),  # closes at 105, above entry(100) -- no touch yet
        (90, 96, 93),      # gapped down: prev close 105 -> this bar's range [90,96]
                           # never contains 100, but price crossed through it.
        (91, 97, 94),
    ])
    result = _label_one("long", entry=100, sl=85, tp=110, df=df)
    assert result["triggered"] is True


def test_label_one_timeout_when_neither_side_hit():
    df = _candles([
        (98, 102, 100),   # triggers
        (99, 104, 102),
        (100, 106, 104),  # never reaches sl(95) or tp(110)
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "timeout"
    assert result["mfe_pct"] > 0


def test_label_one_short_side_tp_first():
    df = _candles([
        (98, 102, 100),  # entry(100) triggers
        (85, 99, 90),     # tp(90) hit for a short
    ])
    result = _label_one("short", entry=100, sl=110, tp=90, df=df)
    assert result["triggered"] is True
    assert result["outcome"] == "tp_first"


def test_label_one_no_data_returns_no_data_outcome():
    result = _label_one("long", entry=100, sl=95, tp=110, df=pd.DataFrame())
    assert result == {"triggered": False, "outcome": "no_data"}
