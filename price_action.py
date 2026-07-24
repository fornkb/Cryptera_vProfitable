"""
Cryptera v3.2 - Price Action (PA) Engine

Volume profile is range-distributed (each candle's volume spread across its
high-low range), not close-only. Session POCs use true non-overlapping windows.
Candle-pattern detection is limited to confirmation-grade reversal patterns.
"""

from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
from smc import find_swings, fractal_swing_mask
from config import SWING_WIDTHS, DEFAULT_WIDTH


def _atr_pct(df: pd.DataFrame, fallback: float = 0.005) -> float:
    if df is None or df.empty or "ATR" not in df.columns:
        return fallback
    atr = df["ATR"].iloc[-1]
    price = float(df["close"].iloc[-1])
    if pd.isna(atr) or price <= 0:
        return fallback
    return max(float(atr) / price, 0.0005)


def detect_candle_pattern(df: pd.DataFrame) -> str:
    """
    Confirmation-grade pattern over the last 1-3 candles. Limited to patterns
    with genuine reversal/continuation signal at a level: pin bars, engulfing,
    morning/evening star. Pure-noise single-bar patterns (doji, inside bar) are
    intentionally excluded.
    """
    if len(df) < 3:
        return "none"
    c2, c1, c0 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    def props(c):
        body = abs(c["close"] - c["open"])
        rng = c["high"] - c["low"] if c["high"] - c["low"] > 0 else 0.001
        return body, rng, c["close"] > c["open"], c["close"] < c["open"], \
            min(c["open"], c["close"]) - c["low"], c["high"] - max(c["open"], c["close"])

    b0, r0, g0, r_0, lw0, uw0 = props(c0)
    b1, r1, g1, r_1, lw1, uw1 = props(c1)
    b2, r2, g2, r_2, lw2, uw2 = props(c2)

    if r0 > 0 and (lw0 / r0 >= 0.6) and (b0 / r0 <= 0.3):
        return "pin_bar_bull"
    if r0 > 0 and (uw0 / r0 >= 0.6) and (b0 / r0 <= 0.3):
        return "pin_bar_bear"
    if r_1 and g0 and (c0["close"] >= c1["open"]) and (c0["open"] <= c1["close"]):
        return "bullish_engulfing"
    if g1 and r_0 and (c0["close"] <= c1["open"]) and (c0["open"] >= c1["close"]):
        return "bearish_engulfing"
    # Small-body test uses the MIDDLE candle's OWN range (b1/r1), not the first
    # candle's (BUG-20) — a large first candle previously masked a fat middle body.
    if r_2 and r1 > 0 and (b1 / r1 <= 0.3) and g0 and (c0["close"] >= (c2["open"] + c2["close"]) / 2):
        return "morning_star"
    if g2 and r1 > 0 and (b1 / r1 <= 0.3) and r_0 and (c0["close"] <= (c2["open"] + c2["close"]) / 2):
        return "evening_star"
    return "none"


# ---------------- range-distributed volume profile ---------------- #

def _build_profile(df: pd.DataFrame, lookback: int, buckets: int):
    """
    Distribute each candle's volume uniformly across its [low, high] range into a
    price histogram. Returns (hist, bin_edges) or (None, None) on degenerate input.
    """
    recent = df.tail(lookback)
    lows = recent["low"].to_numpy(dtype=float)
    highs = recent["high"].to_numpy(dtype=float)
    vols = recent["volume"].to_numpy(dtype=float)

    p_min = float(np.min(lows))
    p_max = float(np.max(highs))
    if not np.isfinite(p_min) or not np.isfinite(p_max) or p_max <= p_min:
        return None, None

    edges = np.linspace(p_min, p_max, buckets + 1)
    hist = np.zeros(buckets, dtype=float)
    bin_w = (p_max - p_min) / buckets

    for lo, hi, vol in zip(lows, highs, vols):
        if vol <= 0:
            continue
        if hi <= lo:
            # zero-range bar: dump into its single bin
            b = min(int((lo - p_min) / bin_w), buckets - 1)
            hist[b] += vol
            continue
        lo_b = int((lo - p_min) / bin_w)
        hi_b = min(int((hi - p_min) / bin_w), buckets - 1)
        lo_b = max(lo_b, 0)
        span = hi_b - lo_b + 1
        # spread this candle's volume evenly across the bins it covers
        hist[lo_b:hi_b + 1] += vol / span

    return hist, edges


def get_poc(df: pd.DataFrame, lookback=50, buckets=100) -> float:
    hist, edges = _build_profile(df, lookback, buckets)
    if hist is None:
        return float(df["close"].iloc[-1])
    idx = int(np.argmax(hist))
    return float((edges[idx] + edges[idx + 1]) / 2)


def get_value_area(df: pd.DataFrame, lookback=50, buckets=100) -> dict:
    hist, edges = _build_profile(df, lookback, buckets)
    if hist is None:
        price = float(df["close"].iloc[-1])
        return {"vah": price, "val": price, "poc": price}

    max_idx = int(np.argmax(hist))
    poc = (edges[max_idx] + edges[max_idx + 1]) / 2
    total = hist.sum()
    if total <= 0:
        return {"vah": float(poc), "val": float(poc), "poc": float(poc)}

    target = total * 0.70
    selected = {max_idx}
    cur = hist[max_idx]
    left, right = max_idx - 1, max_idx + 1
    while cur < target:
        lv = hist[left] if left >= 0 else 0
        rv = hist[right] if right < buckets else 0
        if lv == 0 and rv == 0:
            break
        if lv >= rv:
            selected.add(left)
            cur += lv
            left -= 1
        else:
            selected.add(right)
            cur += rv
            right += 1

    val = edges[min(selected)]
    vah = edges[max(selected) + 1]
    return {"vah": float(vah), "val": float(val), "poc": float(poc)}


def get_sr_levels(swing_highs, swing_lows, current_price, value_area=None, atr_pct: float = 0.003) -> dict:
    """Cluster swings into S/R zones with touch counts and VA-aligned confidence flags."""
    tolerance = max(0.0015, min(0.01, atr_pct * 0.6))
    hvn_tol = max(0.002, min(0.01, atr_pct * 0.7))

    prices = sorted([s[1] for s in swing_highs] + [s[1] for s in swing_lows])
    if not prices:
        return {"support": [], "resistance": []}

    clusters, current = [], []
    for p in prices:
        if not current:
            current.append(p)
        else:
            base = current[0]
            if base > 0 and (p - base) / base <= tolerance:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
    if current:
        clusters.append(current)

    zones = []
    for c in clusters:
        avg = sum(c) / len(c)
        high_conf = False
        if value_area:
            for level in (value_area["vah"], value_area["val"], value_area["poc"]):
                if level > 0 and abs(avg - level) / level <= hvn_tol:
                    high_conf = True
                    break
        zones.append({"price": float(avg), "touches": len(c), "high_confidence": high_conf})

    supports = sorted([z for z in zones if z["price"] < current_price], key=lambda x: x["price"], reverse=True)[:3]
    resistances = sorted([z for z in zones if z["price"] > current_price], key=lambda x: x["price"])[:3]
    return {"support": supports, "resistance": resistances}


def calculate_previous_day(df: pd.DataFrame, now: datetime = None):
    """Previous UTC day's high/low/close, selected by CALENDAR identity relative to `now`,
    not by position (B10.0). The old `iloc[-2]` returned TWO days ago during 00:00-01:00 UTC,
    when the current day has no closed 1H bar yet so the last date-group is still yesterday.
    Previous day = the group whose date == (now-1d); fall back to the most recent day strictly
    before today on a gap."""
    try:
        if now is None:
            now = datetime.now(timezone.utc)
        today = now.date()
        prev_date = today - timedelta(days=1)
        dates = df["timestamp"].dt.date
        daily = df.groupby(dates).agg({"high": "max", "low": "min", "close": "last"}).sort_index()
        if prev_date in daily.index:
            row = daily.loc[prev_date]
            return float(row["high"]), float(row["low"]), float(row["close"])
        past = daily[daily.index < today]           # gap: most recent fully-past day
        if len(past) >= 1:
            row = past.iloc[-1]
            return float(row["high"]), float(row["low"]), float(row["close"])
        return float(df["high"].max()), float(df["low"].min()), float(df["close"].iloc[-1])
    except Exception:
        return float(df["high"].max()), float(df["low"].min()), float(df["close"].iloc[-1])


def find_untested_pocs(df: pd.DataFrame, n_sessions: int = 4, session_bars: int = 50,
                       period: str = "D") -> list:
    """
    Session POCs (uniform intra-bar volume spread — an approximation) that have not been
    re-tested since they formed. Returns up to 5 magnets sorted by proximity to price.

    B22.0: windows are anchored to CALENDAR periods (UTC days by default, or ISO weeks),
    not to blocks of N bars counted back from the newest one. Tail-anchored blocks shifted
    every boundary on every new candle, so a "naked POC" was a different level from one
    cycle to the next — unstable targets in the menu, magnets appearing and disappearing on
    the dashboard, and perturbed episode identity. A calendar period gives a POC an
    identity that survives the next bar. The developing (current) period is skipped: it has
    no post-formation data to test against.

    The retest band scales with the profile's own volatility (0.25x period ATR proxy)
    instead of a flat 0.5%, so a quiet day and a violent one are not judged alike.
    """
    if df is None or df.empty or "timestamp" not in df.columns:
        return []
    current_price = float(df["close"].iloc[-1])
    ts = pd.to_datetime(df["timestamp"])
    keys = ts.dt.strftime("%G-W%V") if period == "W" else ts.dt.strftime("%Y-%m-%d")
    ordered = list(dict.fromkeys(keys.tolist()))
    closed = ordered[:-1]                      # drop the developing period
    out = []
    for key in reversed(closed[-n_sessions:]):
        mask = (keys == key).to_numpy()
        window = df[mask]
        if window.empty or len(window) < 2:
            continue
        poc = get_poc(window, lookback=len(window))
        end = int(np.flatnonzero(mask)[-1]) + 1
        post = df.iloc[end:]
        rng = float(window["high"].max() - window["low"].min())
        band = max(0.25 * rng / max(len(window), 1) * 3, poc * 0.001, 1e-6)
        tested = (not post.empty) and ((post["high"] >= poc - band) & (post["low"] <= poc + band)).any()
        if not tested:
            t = window["timestamp"].iloc[-1]
            out.append({
                "poc": float(poc),
                "period": key,
                "session_end_ts": t.isoformat() if hasattr(t, "isoformat") else str(t),
                "distance_pct": round(abs(current_price - poc) / current_price * 100, 3) if current_price > 0 else None,
            })
    # de-dup near-identical POCs and sort by proximity
    deduped = []
    for p in sorted(out, key=lambda x: (x["distance_pct"] if x["distance_pct"] is not None else 1e9)):
        if all(abs(p["poc"] - q["poc"]) / max(q["poc"], 1e-9) > 0.002 for q in deduped):
            deduped.append(p)
    return deduped[:5]


def weekly_levels(df_1d: pd.DataFrame, now: datetime = None) -> dict:
    """
    Previous-week high/low and current-week open from daily candles (HTF draws for the 1D
    timeframe). Weeks are selected by ISO-week CALENDAR identity relative to `now`, not by
    position (B10.0). The old `groups[-1]` treated the last CLOSED daily week as "current";
    on Monday (all day, before Monday's daily bar closes) that is LAST week, so PWH/PWL were
    two weeks old and weekly_open was last week's. `now - 7d` is always in the previous ISO
    week, so this is robust across the ISO-year rollover.
    """
    out = {"pwh": None, "pwl": None, "weekly_open": None}
    if df_1d is None or df_1d.empty:
        return out
    if now is None:
        now = datetime.now(timezone.utc)
    ts = pd.to_datetime(df_1d["timestamp"])
    iso = ts.dt.isocalendar()
    wk_key = (iso["year"].astype(int) * 100 + iso["week"].astype(int)).to_numpy()
    d = df_1d.assign(_wk=wk_key)
    by_key = {int(k): g for k, g in d.groupby("_wk", sort=True)}
    if not by_key:
        return out
    cur_iso, prev_iso = now.isocalendar(), (now - timedelta(days=7)).isocalendar()
    cur_key = cur_iso[0] * 100 + cur_iso[1]
    prev_key = prev_iso[0] * 100 + prev_iso[1]
    prev = by_key.get(prev_key)
    if prev is None:                                # gap: latest week strictly before now's
        past = sorted(k for k in by_key if k < cur_key)
        prev = by_key[past[-1]] if past else None
    if prev is not None:
        out["pwh"] = round(float(prev["high"].max()), 6)
        out["pwl"] = round(float(prev["low"].min()), 6)
    cur = by_key.get(cur_key)                        # None on Monday (no closed daily yet) ->
    if cur is not None:                              # leave weekly_open None rather than
        out["weekly_open"] = round(float(cur["open"].iloc[0]), 6)  # reporting last week's
    return out


def get_anchored_vwap(df: pd.DataFrame, anchor_idx: int):
    """Volume-weighted average price of typical price from `anchor_idx` to now."""
    if df is None or df.empty or anchor_idx is None or anchor_idx < 0 or anchor_idx >= len(df):
        return None
    seg = df.iloc[anchor_idx:]
    vol = seg["volume"].to_numpy(dtype=float)
    cum_vol = vol.sum()
    if cum_vol <= 0:
        return None
    tp = ((seg["high"] + seg["low"] + seg["close"]) / 3.0).to_numpy(dtype=float)
    return float((tp * vol).sum() / cum_vol)


def compute_avwap_levels(df: pd.DataFrame, tf: str) -> dict:
    """
    Anchored VWAPs from the most recent confirmed swing high and swing low.
    These are institutional dynamic S/R levels: price holding above the
    swing-low AVWAP is bullish control; rejecting the swing-high AVWAP is
    bearish control.
    """
    if df is None or df.empty:
        return {}
    left, right = SWING_WIDTHS.get(tf, DEFAULT_WIDTH)
    is_high, is_low = fractal_swing_mask(df["high"], df["low"], left, right)
    out = {}
    hi_idx = np.flatnonzero(is_high.to_numpy())
    lo_idx = np.flatnonzero(is_low.to_numpy())
    if len(hi_idx):
        v = get_anchored_vwap(df, int(hi_idx[-1]))
        if v is not None:
            out["from_swing_high"] = round(v, 6)
    if len(lo_idx):
        v = get_anchored_vwap(df, int(lo_idx[-1]))
        if v is not None:
            out["from_swing_low"] = round(v, 6)
    return out


def build_pa_context(df_1h, df_15m, df_4h=None, df_1d=None, value_areas: dict = None) -> dict:
    """value_areas: optional pre-computed {tf:{vah,val,poc}} to avoid recompute (B11)."""
    if df_15m is None or df_15m.empty:
        return {}

    value_areas = value_areas or {}
    current_price = float(df_15m["close"].iloc[-1])
    last_pattern = detect_candle_pattern(df_15m)

    va_15m = value_areas.get("15m") or get_value_area(df_15m)
    va_1h = value_areas.get("1h") or (get_value_area(df_1h) if df_1h is not None and not df_1h.empty
                                      else {"vah": current_price, "val": current_price, "poc": current_price})
    va_4h = value_areas.get("4h") or (get_value_area(df_4h) if df_4h is not None and not df_4h.empty
                                      else {"vah": current_price, "val": current_price, "poc": current_price})
    va_1d = value_areas.get("1d") or (get_value_area(df_1d) if df_1d is not None and not df_1d.empty
                                      else {"vah": current_price, "val": current_price, "poc": current_price})

    sh, sl = find_swings(df_15m, left=3, right=2)
    sr_levels = get_sr_levels(sh, sl, current_price, value_area=va_1h, atr_pct=_atr_pct(df_15m))

    pdh, pdl, pdc = calculate_previous_day(df_1h) if df_1h is not None and not df_1h.empty \
        else (current_price, current_price, current_price)

    # B22.0: calendar-anchored windows — 4H bars profile UTC DAYS, daily bars profile ISO
    # WEEKS, so a naked POC keeps its identity from one cycle to the next.
    untested_pocs_4h = find_untested_pocs(df_4h, period="D") \
        if df_4h is not None and not df_4h.empty else []
    untested_pocs_1d = find_untested_pocs(df_1d, n_sessions=6, period="W") \
        if df_1d is not None and not df_1d.empty else []
    weekly = weekly_levels(df_1d) if df_1d is not None and not df_1d.empty \
        else {"pwh": None, "pwl": None, "weekly_open": None}

    avwap = {
        "15m": compute_avwap_levels(df_15m, "15m"),
        "1h": compute_avwap_levels(df_1h, "1h") if df_1h is not None and not df_1h.empty else {},
    }

    return {
        "last_candle_pattern_15m": last_pattern,
        "value_area_15m": va_15m,
        "value_area_1h": va_1h,
        "value_area_4h": va_4h,
        "value_area_1d": va_1d,
        "poc_1h": va_1h.get("poc", current_price),
        "sr_levels": sr_levels,
        "avwap": avwap,
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "pwh": weekly["pwh"],
        "pwl": weekly["pwl"],
        "weekly_open": weekly["weekly_open"],
        "untested_pocs_4h": untested_pocs_4h,
        "untested_pocs_1d": untested_pocs_1d,
    }
