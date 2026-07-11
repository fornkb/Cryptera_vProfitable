"""Unit tests for price_action.py: range-distributed volume-profile value area."""

import pandas as pd

from price_action import get_value_area


def _profile_df(prices, vol=1.0):
    n = len(prices)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": prices, "close": prices,
        "high": [p + 0.1 for p in prices],
        "low": [p - 0.1 for p in prices],
        "volume": [vol] * n,
    })


def test_value_area_poc_lands_in_high_volume_cluster():
    # Most bars cluster tightly around 100; a couple of outliers spread the range.
    prices = [90.0, 95.0] + [100.0] * 40 + [105.0, 110.0]
    df = _profile_df(prices)
    va = get_value_area(df, lookback=len(prices), buckets=50)
    assert 99.0 <= va["poc"] <= 101.0
    assert va["val"] <= va["poc"] <= va["vah"]


def test_value_area_near_flat_price_collapses_around_price():
    prices = [50.0] * 10
    df = _profile_df(prices)
    va = get_value_area(df, lookback=10, buckets=20)
    assert va["val"] <= 50.0 <= va["vah"]


def test_value_area_zero_range_bars_fallback_to_last_close():
    """Degenerate input (p_max <= p_min) must fall back to the flat price, not crash."""
    n = 5
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": [50.0] * n, "close": [50.0] * n,
        "high": [50.0] * n, "low": [50.0] * n,
        "volume": [1.0] * n,
    })
    va = get_value_area(df, lookback=n, buckets=10)
    assert va == {"vah": 50.0, "val": 50.0, "poc": 50.0}
