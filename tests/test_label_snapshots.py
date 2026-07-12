"""
Unit tests for tools/label_snapshots.py::_label_one — trigger detection
(including the BUG-18 gap-through-entry fix), same-bar SL/TP conservatism,
and timeout handling.
"""

import pandas as pd

from tools.label_snapshots import _label_one, _floor_to_15m


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


def test_label_one_both_touched_same_bar_is_conservative_sl_first():
    df = _candles([
        (98, 102, 100),   # entry triggers
        (90, 120, 110),   # low<=95(SL) AND high>=110(TP) in the SAME bar
    ])
    result = _label_one("long", entry=100, sl=95, tp=110, df=df)
    assert result["outcome"] == "sl_first"
    assert result["bars_to_close"] == 1


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
