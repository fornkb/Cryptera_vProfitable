"""
B10.0: PDH/PDL and PWH/PWL/weekly_open selected by UTC CALENDAR identity relative
to an explicit `now`, not by position over closed-bar groups. The old code returned
two-days-ago between 00:00-01:00 UTC and last week's levels for all of Monday.
"""

from datetime import datetime, timezone

import pandas as pd

from price_action import calculate_previous_day, weekly_levels


def _hourly(rows):
    """rows: (iso_ts, high, low, close) -- one 1H bar each (tz-naive UTC, like the engine)."""
    return pd.DataFrame([(pd.Timestamp(t), h, l, c) for t, h, l, c in rows],
                        columns=["timestamp", "high", "low", "close"])


def _daily(rows):
    """rows: (iso_ts, open, high, low, close)."""
    return pd.DataFrame([(pd.Timestamp(t), o, h, l, c) for t, o, h, l, c in rows],
                        columns=["timestamp", "open", "high", "low", "close"])


# ---------------- PDH/PDL: the 00:00-01:00 UTC hole ---------------- #

def test_previous_day_at_midnight_utc_is_yesterday_not_two_days_ago():
    # Day A = 2026-01-05 (high 200), Day B = 2026-01-06 (high 300). No Day-C bar has closed.
    df = _hourly([("2026-01-05 12:00", 200, 190, 195),
                  ("2026-01-06 12:00", 300, 290, 295)])
    now = datetime(2026, 1, 7, 0, 30, tzinfo=timezone.utc)   # 00:30 UTC, still Day C
    pdh, pdl, pdc = calculate_previous_day(df, now=now)
    assert (pdh, pdl, pdc) == (300, 290, 295)               # Day B; old iloc[-2] gave Day A (200)


def test_previous_day_midday_is_yesterday():
    df = _hourly([("2026-01-05 12:00", 200, 190, 195),
                  ("2026-01-06 12:00", 300, 290, 295),
                  ("2026-01-07 09:00", 400, 390, 395)])      # today (partial)
    now = datetime(2026, 1, 7, 12, 30, tzinfo=timezone.utc)
    assert calculate_previous_day(df, now=now)[0] == 300     # Day B


def test_previous_day_gap_falls_back_to_most_recent_past_day():
    df = _hourly([("2026-01-05 12:00", 200, 190, 195),
                  ("2026-01-06 12:00", 300, 290, 295)])
    now = datetime(2026, 1, 8, 0, 30, tzinfo=timezone.utc)   # prev_date 01-07 absent
    assert calculate_previous_day(df, now=now)[0] == 300     # most recent past day (Day B)


# ---------------- PWH/PWL: all of Monday ---------------- #

# ISO weeks: 01-05..01-11 = W02, 01-12..01-18 = W03, 01-19.. = W04.
WEEK_A = [("2026-01-06", 580, 600, 575, 590), ("2026-01-08", 585, 595, 580, 588)]   # W02 high 600
WEEK_B = [("2026-01-13", 650, 690, 680, 685), ("2026-01-15", 688, 700, 684, 695)]   # W03 high 700


def test_weekly_on_monday_uses_last_week_not_two_weeks_ago():
    df = _daily(WEEK_A + WEEK_B)
    now = datetime(2026, 1, 19, 9, 0, tzinfo=timezone.utc)   # Monday of W04, no W04 daily closed
    wl = weekly_levels(df, now=now)
    assert wl["pwh"] == 700 and wl["pwl"] == 680             # W03; old code gave W02 (600)
    assert wl["weekly_open"] is None                         # honest: no closed daily this week yet


def test_weekly_midweek_reports_current_week_open_and_prev_week():
    df = _daily(WEEK_A + WEEK_B)
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # Thursday of W03
    wl = weekly_levels(df, now=now)
    assert wl["weekly_open"] == 650                          # W03 first bar's open
    assert wl["pwh"] == 600                                  # W02


def test_weekly_iso_year_rollover():
    # W52-2025 (2025-12-24) then W01-2026 (2026-01-01); now in W01-2026 -> prev is W52-2025.
    df = _daily([("2025-12-24", 850, 900, 840, 890), ("2026-01-01", 940, 950, 930, 945)])
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    wl = weekly_levels(df, now=now)
    assert wl["pwh"] == 900                                  # previous ISO week crosses the year
    assert wl["weekly_open"] == 940
