"""
Cryptera v3.2 - Smart Money Concepts (SMC) Engine

Single fractal swing definition shared with the indicator layer (no more dual
swing systems). Developing swings are a separate early-warning channel and are
never allowed to redefine structure. BOS / CHoCH are detected as the first
close beyond the reference swing (event semantics), with optional displacement.
Adaptive thresholds scale with ATR-as-a-fraction-of-price.
"""

import pandas as pd
import numpy as np

from config import (SWING_WIDTHS, DEFAULT_WIDTH, TRANSITION_EXPIRY_BARS,
                    POOL_MIN_TOUCHES, POOL_MIN_SEPARATION_BARS)


def _atr_pct(df: pd.DataFrame, fallback: float = 0.005) -> float:
    """ATR as a fraction of current close. Used to scale tolerances per asset/TF."""
    if df is None or df.empty or "ATR" not in df.columns:
        return fallback
    atr = df["ATR"].iloc[-1]
    price = float(df["close"].iloc[-1])
    if pd.isna(atr) or price <= 0:
        return fallback
    return max(float(atr) / price, 0.0005)


def _equal_tol(df: pd.DataFrame) -> float:
    """
    Relative tolerance for calling two extremes 'equal' (B17.0): a quarter of the
    ATR-as-fraction-of-price, capped at 20bps. Scales with the asset/TF the same way
    the liquidity-pool cluster tolerance does, instead of float equality.
    """
    return max(0.0, min(0.002, _atr_pct(df) * 0.25))


# ---------------- unified fractal swing detection ---------------- #

def _dedup_plateau(mask: pd.Series, prices: pd.Series, span: int, tol: float) -> pd.Series:
    """
    Collapse a run of equal extremes inside one fractal half-window to its LAST bar
    (B17.0). `high == roll_high` flags EVERY bar of a flat stretch, so a plateau used
    to report N swings — junk pools, inflated S/R touch counts, degenerate dealing
    ranges. A flat stretch is ONE pivot; the equal extremes are EQH/EQL liquidity,
    which the typed pools surface separately. Touches more than `span` bars apart are
    left alone — those are genuine repeat tests, not a plateau.
    """
    idxs = np.flatnonzero(mask.to_numpy())
    if idxs.size < 2:
        return mask
    vals = prices.to_numpy()
    drop = []
    for a in range(idxs.size - 1):
        i, j = int(idxs[a]), int(idxs[a + 1])
        pi, pj = float(vals[i]), float(vals[j])
        if (j - i) <= span and abs(pj - pi) <= max(abs(pi), 1e-12) * tol:
            drop.append(i)
    if not drop:
        return mask
    out = mask.copy()
    out.iloc[drop] = False
    return out


def fractal_swing_mask(high: pd.Series, low: pd.Series, left: int, right: int,
                       tol: float = 0.0):
    """
    Vectorised fractal pivots. A bar is a swing high if its high is the maximum
    over [i-left, i+right]; swing low symmetrically. The last `right` bars are
    NaN/False (no look-ahead). Returns (is_high: bool Series, is_low: bool Series).

    `tol` is a RELATIVE price tolerance for treating neighbouring extremes as equal
    (B17.0 plateau dedup); 0.0 keeps exact-equality dedup, which is what the mask
    actually produces on flat tape.
    """
    window = left + right + 1
    # rolling(window).max() at q covers [q-window+1, q]; shift(-right) re-centres
    roll_high = high.rolling(window).max().shift(-right)
    roll_low = low.rolling(window).min().shift(-right)
    is_high = (high == roll_high) & roll_high.notna()
    is_low = (low == roll_low) & roll_low.notna()
    span = max(1, int(right))
    return (_dedup_plateau(is_high, high, span, tol),
            _dedup_plateau(is_low, low, span, tol))


def _confirmed_swings(df: pd.DataFrame, left: int, right: int):
    """Return confirmed swings as ordered lists of dicts {idx, ts, price}."""
    if df is None or len(df) < (left + right + 1):
        return [], []
    is_high, is_low = fractal_swing_mask(df["high"], df["low"], left, right,
                                         tol=_equal_tol(df))

    def _collect(mask, col):
        out = []
        for idx in np.flatnonzero(mask.to_numpy()):
            ts = df["timestamp"].iloc[idx]
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            out.append({"idx": int(idx), "ts": ts_str, "price": float(df[col].iloc[idx])})
        return out

    return _collect(is_high, "high"), _collect(is_low, "low")


def find_developing_swings(df: pd.DataFrame):
    """
    Early-warning swings on the unconfirmed tail (volume + wick rejection).
    Returned as a SEPARATE channel; never fed into structure / BOS / CHoCH.
    """
    if df is None or df.empty:
        return [], []
    n = len(df)
    right = 3
    atr = df["ATR"].iloc[-1] if "ATR" in df.columns and not pd.isna(df["ATR"].iloc[-1]) else (df["high"] - df["low"]).mean()
    vol_mean = df["volume"].rolling(20).mean().iloc[-1] if n >= 20 else df["volume"].mean()
    dev_highs, dev_lows = [], []
    for idx in range(max(0, n - right), n):
        c = df.iloc[idx]
        high, low, open_p, close = float(c["high"]), float(c["low"]), float(c["open"]), float(c["close"])
        vol = float(c["volume"])
        body = abs(close - open_p)
        upper_wick = high - max(open_p, close)
        lower_wick = min(open_p, close) - low
        ts = c["timestamp"]
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if upper_wick > body * 1.2 and upper_wick > atr * 0.4 and vol > vol_mean * 1.1:
            dev_highs.append({"idx": idx, "ts": ts_str, "price": high})
        if lower_wick > body * 1.2 and lower_wick > atr * 0.4 and vol > vol_mean * 1.1:
            dev_lows.append({"idx": idx, "ts": ts_str, "price": low})
    return dev_highs, dev_lows


def find_swings(df: pd.DataFrame, left=5, right=5):
    """
    Back-compatible accessor: returns the last 5 CONFIRMED swings as
    (ts, price) tuples for highs and lows. Structure-grade only.
    """
    highs, lows = _confirmed_swings(df, left, right)
    sh = [(h["ts"], h["price"]) for h in highs][-5:]
    sl = [(l["ts"], l["price"]) for l in lows][-5:]
    return sh, sl


# ---------------- structure / BOS / CHoCH ---------------- #

def _has_displacement_fvg(df: pd.DataFrame, start_idx: int, end_idx: int, direction: str) -> bool:
    """True if a same-direction 3-candle FVG exists within (start_idx, end_idx]."""
    lo = max(2, start_idx)
    hi = min(len(df) - 1, end_idx)
    for i in range(lo, hi + 1):
        if direction == "BULLISH":
            if float(df["low"].iloc[i]) > float(df["high"].iloc[i - 2]):
                return True
        else:
            if float(df["high"].iloc[i]) < float(df["low"].iloc[i - 2]):
                return True
    return False


def _seq_state(prices) -> int:
    """
    Classify a swing-price sequence as rising(+1) / falling(-1) / mixed(0) using
    the last up-to-4 confirmed points (majority of step directions). A wider
    window than the previous last-2 comparison makes structure resistant to a
    single equal-high or one deep pullback flipping the label (BUG-08).
    """
    pts = list(prices)[-4:]
    if len(pts) < 2:
        return 0
    ups = sum(1 for a, b in zip(pts, pts[1:]) if b > a)
    downs = sum(1 for a, b in zip(pts, pts[1:]) if b < a)
    if ups > downs:
        return 1
    if downs > ups:
        return -1
    return 0


def _hl_label(prices, up_tok, dn_tok) -> str:
    """
    Majority-vote state -> token. A tie is NOT directional (B17.0): it used to fall
    back to the last-two comparison, which broke exact ties to the down-token, so flat
    tape read LH/LL and the router shorted chop. Equal extremes are liquidity, not
    structure -> neutral 'EQ' token, which no bias test matches.
    """
    st = _seq_state(prices)
    if st > 0:
        return up_tok
    if st < 0:
        return dn_tok
    return "EQ"


def _seed_trend(highs, lows):
    """Trend seed from swing progression — only a clean HH/HL or LH/LL seeds the machine."""
    if len(highs) < 2 or len(lows) < 2:
        return None
    hs = _hl_label([h["price"] for h in highs], "HH", "LH")
    ls = _hl_label([l["price"] for l in lows], "HL", "LL")
    if hs == "HH" and ls == "HL":
        return "bullish"
    if hs == "LH" and ls == "LL":
        return "bearish"
    return None


def _impulse_origin(highs, lows, ref_idx, break_idx, direction):
    """
    Origin of the BREAKING impulse: the most recent opposite-side pivot between the
    broken swing and the break bar (the pullback the move launched from); falls back to
    the last one at/before the broken swing. Displacement is certified inside this span
    only (B7.0) — an unrelated FVG dozens of bars before the actual impulse no longer
    counts as displacement.
    """
    pool = lows if direction == "BULLISH" else highs
    inside = [p for p in pool if ref_idx < p["idx"] < break_idx]
    if inside:
        return inside[-1]
    before = [p for p in pool if p["idx"] <= ref_idx]
    return before[-1] if before else None


def _event(kind, direction, ref, break_idx, highs, lows, df, n, recent_bars):
    """Build one structural event with impulse-anchored displacement."""
    origin = _impulse_origin(highs, lows, ref["idx"], break_idx, direction)
    origin_idx = origin["idx"] if origin else ref["idx"]
    return {
        "type": kind,
        "direction": direction,
        "level": float(ref["price"]),
        "ref_idx": int(ref["idx"]),
        "break_idx": int(break_idx),
        "origin_idx": int(origin_idx),
        "origin_price": float(origin["price"]) if origin else None,
        "bars_ago": int(n - 1 - break_idx),
        "fresh": (n - 1 - break_idx) <= recent_bars,
        "displacement": _has_displacement_fvg(df, origin_idx, break_idx, direction),
    }


def structural_state_machine(confirmed_highs, confirmed_lows, df, recent_bars=12,
                             expiry_bars=TRANSITION_EXPIRY_BARS):
    """
    Chronological structure state machine (I3.0, absorbing B7.0/B8.0).

    Walks closed bars once, holding (state, trend, protected swings) and emitting
    MUTUALLY EXCLUSIVE, trend-conditioned events — the same break can no longer be
    reported as both BOS and CHoCH:

      * in a bullish trend, a close above the last swing high is a bullish **BOS**
        (continuation) and a close below the protected low is a bearish **CHoCH**;
        mirrored for bearish. One close cannot be both.
      * a CHoCH opens a **transition** state. The trend label does NOT flip until a
        confirming BOS in the new direction arrives WITH displacement (or a second
        confirming break — the market has decided even if the impulse was untidy).
      * while in transition, a BOS in the OLD trend direction cancels it (failed
        change of character); after `expiry_bars` with neither, it decays to neutral.
      * displacement is certified inside the breaking impulse only (`_impulse_origin`).

    Reference levels float to the most recent confirmed swing on each side, so the
    protected low ratchets up as new higher-lows form. Break detection can never look
    ahead: a fractal swing high has no higher high inside its own right window, so the
    first close beyond it necessarily lands after the swing is confirmable.

    Returns {"state","trend","pending_direction","protected_high","protected_low",
             "bars_in_state","events"} — `events` chronological.
    """
    n = 0 if df is None else len(df)
    if n == 0 or len(confirmed_highs) < 2 or len(confirmed_lows) < 2:
        return {"state": "neutral", "trend": None, "pending_direction": None,
                "protected_high": None, "protected_low": None,
                "bars_in_state": None, "events": []}

    closes = df["close"].to_numpy()
    trend, state, pending = None, "neutral", None
    state_since = 0
    confirm_runs = 0
    consumed = set()          # (side, swing idx) already spent as an event reference
    events = []
    hi_ptr = lo_ptr = 0
    last_high = last_low = None

    for i in range(n):
        while hi_ptr < len(confirmed_highs) and confirmed_highs[hi_ptr]["idx"] < i:
            last_high = confirmed_highs[hi_ptr]
            hi_ptr += 1
        while lo_ptr < len(confirmed_lows) and confirmed_lows[lo_ptr]["idx"] < i:
            last_low = confirmed_lows[lo_ptr]
            lo_ptr += 1
        if last_high is None or last_low is None:
            continue

        # Seed the trend from swing progression until the first event fires, so a window
        # that opens mid-trend reads its first counter-break as a CHoCH, not a BOS.
        if not events and state == "neutral":
            seeded = _seed_trend(confirmed_highs[:hi_ptr], confirmed_lows[:lo_ptr])
            if seeded:
                trend, state, state_since = seeded, "trending", i

        c = float(closes[i])
        up = c > last_high["price"] and ("H", last_high["idx"]) not in consumed
        dn = c < last_low["price"] and ("L", last_low["idx"]) not in consumed
        if up and dn:
            # degenerate (last low above last high): keep the trend-consistent read
            if trend == "bearish":
                up = False
            else:
                dn = False
        if not (up or dn):
            if state == "transition" and (i - state_since) > expiry_bars:
                state, trend, pending, confirm_runs, state_since = "neutral", None, None, 0, i
            continue

        direction = "BULLISH" if up else "BEARISH"
        ref = last_high if up else last_low
        side = "H" if up else "L"

        if state == "transition":
            if direction.lower() == pending:
                confirm_runs += 1
                ev = _event("BOS", direction, ref, i, confirmed_highs[:hi_ptr],
                            confirmed_lows[:lo_ptr], df, n, recent_bars)
                if ev["displacement"] or confirm_runs >= 2:
                    trend, state, pending, confirm_runs, state_since = pending, "trending", None, 0, i
            else:
                # the old trend reasserts itself — the change of character failed
                ev = _event("BOS", direction, ref, i, confirmed_highs[:hi_ptr],
                            confirmed_lows[:lo_ptr], df, n, recent_bars)
                state, pending, confirm_runs, state_since = "trending", None, 0, i
        elif trend is None:
            ev = _event("BOS", direction, ref, i, confirmed_highs[:hi_ptr],
                        confirmed_lows[:lo_ptr], df, n, recent_bars)
            trend = "bullish" if up else "bearish"
            state, state_since = "trending", i
        elif direction.lower() == trend:
            ev = _event("BOS", direction, ref, i, confirmed_highs[:hi_ptr],
                        confirmed_lows[:lo_ptr], df, n, recent_bars)
        else:
            ev = _event("CHOCH", direction, ref, i, confirmed_highs[:hi_ptr],
                        confirmed_lows[:lo_ptr], df, n, recent_bars)
            state, pending, confirm_runs, state_since = "transition", ("bullish" if up else "bearish"), 0, i

        consumed.add((side, ref["idx"]))
        events.append(ev)

    return {
        "state": state,
        "trend": trend,
        "pending_direction": pending,
        "protected_high": float(last_high["price"]) if last_high else None,
        "protected_low": float(last_low["price"]) if last_low else None,
        "bars_in_state": int(n - 1 - state_since),
        "events": events,
    }


def get_market_structure(confirmed_highs, confirmed_lows, df=None, recent_bars=12):
    """
    Structure label + event-based BOS / CHoCH / state, from the I3.0 state machine.

    Returns (structure, bos, choch, structure_state). The label is now sourced from
    the machine (`HH/HL` bullish, `LH/LL` bearish, `TRANSITION`, else the swing
    progression), so it no longer lags a change of character by several swings
    (B8.0); `bos` and `choch` are the latest event of each type and are mutually
    exclusive per break (B7.0).
    """
    empty_state = {"state": "neutral", "trend": None, "pending_direction": None,
                   "protected_high": None, "protected_low": None,
                   "bars_in_state": None, "events": []}
    bos = {"level": None, "direction": None, "fresh": False, "displacement": False,
           "bars_ago": None, "origin_idx": None, "origin_price": None, "break_idx": None}
    choch = {"detected": False, "direction": None, "level": None, "fresh": False,
             "bars_ago": None, "confirmed": False, "origin_idx": None,
             "origin_price": None, "break_idx": None}
    if len(confirmed_highs) < 2 or len(confirmed_lows) < 2:
        return "NEUTRAL", bos, choch, empty_state

    high_prices = [h["price"] for h in confirmed_highs]
    low_prices = [l["price"] for l in confirmed_lows]
    progression = f"{_hl_label(high_prices, 'HH', 'LH')}/{_hl_label(low_prices, 'HL', 'LL')}"

    if df is None or df.empty:
        return progression, bos, choch, empty_state

    st = structural_state_machine(confirmed_highs, confirmed_lows, df, recent_bars)

    last_bos = next((e for e in reversed(st["events"]) if e["type"] == "BOS"), None)
    last_choch = next((e for e in reversed(st["events"]) if e["type"] == "CHOCH"), None)
    if last_bos:
        bos = {"level": last_bos["level"], "direction": last_bos["direction"],
               "fresh": bool(last_bos["fresh"]), "displacement": bool(last_bos["displacement"]),
               "bars_ago": last_bos["bars_ago"], "origin_idx": last_bos["origin_idx"],
               "origin_price": last_bos["origin_price"], "break_idx": last_bos["break_idx"]}
    if last_choch:
        # 'confirmed' = the machine has since flipped the trend to the CHoCH direction.
        confirmed = (st["state"] == "trending" and st["trend"] == last_choch["direction"].lower())
        choch = {"detected": True, "direction": last_choch["direction"],
                 "level": last_choch["level"], "fresh": bool(last_choch["fresh"]),
                 "bars_ago": last_choch["bars_ago"], "confirmed": bool(confirmed),
                 "origin_idx": last_choch["origin_idx"],
                 "origin_price": last_choch["origin_price"],
                 "break_idx": last_choch["break_idx"]}

    if st["state"] == "trending" and st["trend"] == "bullish":
        structure = "HH/HL"
    elif st["state"] == "trending" and st["trend"] == "bearish":
        structure = "LH/LL"
    elif st["state"] == "transition":
        structure = "TRANSITION"
    else:
        structure = progression

    st = {k: v for k, v in st.items() if k != "events"}
    return structure, bos, choch, st


# ---------------- order blocks ---------------- #

def find_order_blocks(df: pd.DataFrame, value_area: dict, lookback=50):
    """
    Unmitigated Order Blocks. Impulse threshold scales with ATR. HVN tag uses the
    pre-computed value_area for this TF (passed in — no recompute, fixes B11).
    """
    vah, val, poc = value_area["vah"], value_area["val"], value_area["poc"]
    atr_pct = _atr_pct(df)
    hvn_tol = max(0.003, min(0.01, atr_pct * 0.6))

    bullish_obs, bearish_obs = [], []
    n = len(df)
    start_idx = max(3, n - lookback)

    def _tag_hvn(mid):
        for level in (vah, val, poc):
            if level > 0 and abs(mid - level) / level <= hvn_tol:
                return True
        return False

    for i in range(start_idx, n):
        # bullish impulse
        bull, b_start = False, -1
        for k in (1, 2, 3):
            prev_c, curr_c = df["close"].iloc[i - k], df["close"].iloc[i]
            atr_i = df["ATR"].iloc[i - k] if "ATR" in df.columns else np.nan
            thr = (prev_c * max(atr_pct, 0.005) * 1.5) if (pd.isna(atr_i) or atr_i <= 0) else 2.0 * atr_i
            if (curr_c - prev_c) >= thr:
                bull, b_start = True, i - k
                break
        if bull:
            for j in range(b_start - 1, max(0, b_start - 10), -1):
                if df["close"].iloc[j] < df["open"].iloc[j]:
                    hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
                    ts = df["timestamp"].iloc[j]
                    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    if not any(o["timestamp"] == ts_str for o in bullish_obs):
                        bullish_obs.append({"timestamp": ts_str, "top": hi, "bottom": lo,
                                            "created_at_idx": j, "high_volume_node": _tag_hvn((hi + lo) / 2)})
                    break

        # bearish impulse
        bear, s_start = False, -1
        for k in (1, 2, 3):
            prev_c, curr_c = df["close"].iloc[i - k], df["close"].iloc[i]
            atr_i = df["ATR"].iloc[i - k] if "ATR" in df.columns else np.nan
            thr = (prev_c * max(atr_pct, 0.005) * 1.5) if (pd.isna(atr_i) or atr_i <= 0) else 2.0 * atr_i
            if (prev_c - curr_c) >= thr:
                bear, s_start = True, i - k
                break
        if bear:
            for j in range(s_start - 1, max(0, s_start - 10), -1):
                if df["close"].iloc[j] > df["open"].iloc[j]:
                    hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
                    ts = df["timestamp"].iloc[j]
                    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    if not any(o["timestamp"] == ts_str for o in bearish_obs):
                        bearish_obs.append({"timestamp": ts_str, "top": hi, "bottom": lo,
                                            "created_at_idx": j, "high_volume_node": _tag_hvn((hi + lo) / 2)})
                    break

    # Mitigation is wick-aware (BUG-17): an OB whose zone was traded through by a
    # WICK (stops taken) counts as mitigated even if no candle CLOSED beyond it,
    # so a swept level is not offered as a fresh unswept entry candidate.
    unmit_bull = []
    for ob in bullish_obs:
        sub = df["low"].iloc[ob["created_at_idx"] + 1:]
        if sub.empty or sub.min() >= ob["bottom"]:
            unmit_bull.append({k: v for k, v in ob.items() if k != "created_at_idx"})

    unmit_bear = []
    for ob in bearish_obs:
        sub = df["high"].iloc[ob["created_at_idx"] + 1:]
        if sub.empty or sub.max() <= ob["top"]:
            unmit_bear.append({k: v for k, v in ob.items() if k != "created_at_idx"})

    return {"bullish": unmit_bull[-3:], "bearish": unmit_bear[-3:]}


# ---------------- fair value gaps ---------------- #

def find_fvg(df: pd.DataFrame, lookback=50):
    """3-candle FVGs; nearest unfilled above/below current price."""
    bullish, bearish = [], []
    n = len(df)
    start_idx = max(2, n - lookback)
    current_price = float(df["close"].iloc[-1])

    for i in range(start_idx, n):
        high_2, low_0 = float(df["high"].iloc[i - 2]), float(df["low"].iloc[i])
        if low_0 > high_2:
            sub = df.iloc[i + 1:]
            filled = (not sub.empty) and (sub["low"].min() <= high_2)
            ts = df["timestamp"].iloc[i]
            bullish.append({"top": low_0, "bottom": high_2,
                            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                            "filled": bool(filled)})
        low_2, high_0 = float(df["low"].iloc[i - 2]), float(df["high"].iloc[i])
        if low_2 > high_0:
            sub = df.iloc[i + 1:]
            filled = (not sub.empty) and (sub["high"].max() >= low_2)
            ts = df["timestamp"].iloc[i]
            bearish.append({"top": low_2, "bottom": high_0,
                            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                            "filled": bool(filled)})

    def _gap_dist(f):
        """0 when price sits inside the gap (straddling), else distance to the nearest edge."""
        if f["bottom"] <= current_price <= f["top"]:
            return 0.0
        return min(abs(current_price - f["top"]), abs(current_price - f["bottom"]))

    def _straddles(f):
        return bool(f["bottom"] <= current_price <= f["top"])

    # A gap straddling current price is the most actionable one — include it
    # (BUG-16). Demand (bull) gaps at/below price; supply (bear) gaps at/above.
    nearest_bull = None
    cand_bull = [f for f in bullish if not f["filled"] and f["bottom"] <= current_price]
    if cand_bull:
        nearest_bull = min(cand_bull, key=_gap_dist)
        nearest_bull = {**nearest_bull, "straddles_price": _straddles(nearest_bull)}
    nearest_bear = None
    cand_bear = [f for f in bearish if not f["filled"] and f["top"] >= current_price]
    if cand_bear:
        nearest_bear = min(cand_bear, key=_gap_dist)
        nearest_bear = {**nearest_bear, "straddles_price": _straddles(nearest_bear)}

    return {
        "nearest_bullish_fvg": nearest_bull,
        "nearest_bearish_fvg": nearest_bear,
        "bullish_fvgs": bullish[-5:],
        "bearish_fvgs": bearish[-5:],
    }


# ---------------- liquidity pools ---------------- #

def _normalize_swings(swings):
    """Accept confirmed-swing dicts or legacy (ts, price) tuples."""
    out = []
    for s in swings or []:
        if isinstance(s, dict):
            out.append({"idx": s.get("idx"), "price": float(s["price"])})
        else:
            out.append({"idx": None, "price": float(s[1])})
    return out


def _cluster_touches(swings, tol, take_max, min_separation):
    """
    Group swing extremes within `tol` (relative) into one level and count DISTINCT
    touches — separate tests of the same level, not bars of the same pivot. Returns
    [{price, touches, first_idx, last_idx}].
    """
    pts = sorted(swings, key=lambda s: s["price"])
    out, used = [], set()
    for i, a in enumerate(pts):
        if i in used:
            continue
        members = [a]
        for j in range(i + 1, len(pts)):
            if j in used:
                continue
            if a["price"] > 0 and (pts[j]["price"] - a["price"]) / a["price"] <= tol:
                members.append(pts[j])
                used.add(j)
        idxs = sorted(m["idx"] for m in members if m["idx"] is not None)
        touches, last_counted = (1 if idxs else len(members)), None
        for k in idxs:
            if last_counted is None:
                last_counted = k
            elif k - last_counted >= min_separation:
                touches += 1
                last_counted = k
        out.append({
            "price": max(m["price"] for m in members) if take_max
            else min(m["price"] for m in members),
            "touches": int(touches),
            "first_idx": idxs[0] if idxs else None,
            "last_idx": idxs[-1] if idxs else None,
        })
    return out


def _swept_idx(df, price, after_idx, side):
    """First bar after the last touch whose WICK took the level (liquidity consumed)."""
    if df is None or df.empty or after_idx is None:
        return None
    start = int(after_idx) + 1
    if start >= len(df):
        return None
    series = (df["high"] if side == "buy" else df["low"]).to_numpy()[start:]
    hit = np.flatnonzero(series > price if side == "buy" else series < price)
    return int(start + hit[0]) if hit.size else None


def find_liquidity_levels(swing_highs, swing_lows, atr_pct: float = 0.002,
                          current_price: float = None, df=None, tf: str = None,
                          min_touches: int = POOL_MIN_TOUCHES,
                          min_separation: int = POOL_MIN_SEPARATION_BARS):
    """
    Typed liquidity pools (I5.0 / B12.0). Resting liquidity in the SMC sense is
    ENGINEERED equal highs/lows — plural touches of the same level — not any fractal
    pivot. Every single-touch swing used to qualify, so one noisy pivot simultaneously
    became sweep evidence, a "protected" stop anchor and a liquidity target; and a pool
    a wick had already consumed stayed on the menu as a future draw.

    Emits three layers:
      * `pools`     — {price, side, touches, first_idx, last_idx, swept_idx, tf, strength}
                      for levels with >= `min_touches` distinct touches (separated by
                      >= `min_separation` bars). Swept pools are KEPT here, flagged, so
                      sweep history stays available to C3/M2 — they are just not draws.
      * `swing_levels` — single-touch extremes. Still valid structure (stop anchors),
                      explicitly NOT liquidity.
      * `buy_side` / `sell_side` — the draw menu, unswept and on the correct side of
                      price (BSL only above, SSL only below), pools ranked first.

    `strength` is a provisional ranking aid (touch count + timeframe), not a gate; Wave 4
    calibration is what should decide its weight.
    """
    tolerance = max(0.001, min(0.01, atr_pct * 0.5))
    tf_rank = {"1d": 4, "4h": 3, "1h": 2, "15m": 1}.get(tf or "", 1)
    highs = _normalize_swings(swing_highs)
    lows = _normalize_swings(swing_lows)

    pools, singles = [], {"highs": [], "lows": []}
    for swings, side, take_max, bucket in ((highs, "buy", True, "highs"),
                                           (lows, "sell", False, "lows")):
        for cl in _cluster_touches(swings, tolerance, take_max, min_separation):
            if cl["touches"] < min_touches:
                singles[bucket].append({"price": round(cl["price"], 6),
                                        "last_idx": cl["last_idx"]})
                continue
            pools.append({
                "price": round(cl["price"], 6),
                "side": side,
                "touches": cl["touches"],
                "first_idx": cl["first_idx"],
                "last_idx": cl["last_idx"],
                "swept_idx": _swept_idx(df, cl["price"], cl["last_idx"], side),
                "tf": tf,
                "strength": round(min(1.0, 0.15 * min(cl["touches"], 4) + 0.1 * tf_rank), 3),
            })

    def _draws(side, above):
        cands = [p["price"] for p in pools if p["side"] == side and p["swept_idx"] is None]
        # Single-touch extremes still fill the draw menu, but only while unswept: a level
        # a wick has already taken is liquidity that no longer exists. (I7.0 moves the
        # menu onto the typed layer proper.)
        extra = [s["price"] for s in singles["highs" if above else "lows"]
                 if _swept_idx(df, s["price"], s["last_idx"], side) is None]
        if current_price is None:
            return sorted(set(cands + extra), reverse=above)[:3]
        keep = [p for p in cands if (p > current_price) == above and p != current_price]
        fill = [p for p in extra if (p > current_price) == above and p != current_price]
        seen, out = set(), []
        for p in sorted(keep, reverse=not above) + sorted(fill, reverse=not above):
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out[:3]

    return {
        "buy_side": _draws("buy", above=True),
        "sell_side": _draws("sell", above=False),
        "pools": sorted(pools, key=lambda p: -p["strength"])[:6],
        "swing_levels": {
            "highs": sorted((s["price"] for s in singles["highs"]), reverse=True)[:3],
            "lows": sorted(s["price"] for s in singles["lows"])[:3],
        },
    }


def get_premium_discount(swing_high, swing_low, current_price, leg_direction="bullish"):
    """
    Premium/discount + direction-aware OTE (61.8%-78.6% retracement of the leg).
    Bullish leg (low->high): OTE is the discount-side pullback band below the high.
    Bearish leg (high->low): OTE is the mirrored premium-side band above the low
    (BUG-05 — previously shorts were credited in the long/discount zone).
    """
    if swing_high <= swing_low:
        return {"zone": "neutral", "ote_zone": {"low": 0, "high": 0},
                "in_ote": False, "leg_direction": leg_direction}
    rng = swing_high - swing_low
    midpoint = swing_low + 0.5 * rng
    zone = "premium" if current_price > midpoint else "discount"
    if leg_direction == "bearish":
        ote_low = swing_low + 0.618 * rng
        ote_high = swing_low + 0.786 * rng
    else:
        ote_low = swing_high - 0.786 * rng
        ote_high = swing_high - 0.618 * rng
    return {
        "zone": zone,
        "ote_zone": {"low": round(ote_low, 4), "high": round(ote_high, 4)},
        "in_ote": ote_low <= current_price <= ote_high,
        "leg_direction": leg_direction,
    }


def _latest_event(bos, choch):
    """Whichever structural event broke most recently (they are now distinct events)."""
    cands = []
    if (bos or {}).get("break_idx") is not None:
        cands.append(("BOS", bos))
    if (choch or {}).get("break_idx") is not None and (choch or {}).get("detected"):
        cands.append(("CHOCH", choch))
    if not cands:
        return None, None
    kind, ev = max(cands, key=lambda c: c[1]["break_idx"])
    return kind, ev


def impulse_leg_ote(df, bos, choch, current_price):
    """
    OTE on the IMPULSE LEG that produced the last structural break (I6.0 / B9.0).

    OTE belongs to the leg that did the breaking — impulse origin swing -> the extreme
    that leg has reached — not to "whichever confirmed swings happen to bracket price".
    The old implementation drew it on the dealing range, whose `leg_direction` was taken
    from the latest confirmed swing and survived re-bracketing, so after a breakdown the
    band could be drawn as a pullback-to-BUY zone inside what was now a bearish leg.

    A retracement past 100% is not a retracement: once price trades back through the
    impulse origin the leg is void and there is no OTE. Returns None when no event, no
    origin, or a violated leg — the caller then reports no OTE rather than guessing.
    """
    kind, ev = _latest_event(bos, choch)
    if not ev or ev.get("origin_price") is None or df is None or df.empty:
        return None
    origin_idx = ev.get("origin_idx")
    direction = (ev.get("direction") or "").upper()
    if origin_idx is None or direction not in ("BULLISH", "BEARISH"):
        return None

    origin = float(ev["origin_price"])
    if direction == "BULLISH":
        extreme = float(df["high"].iloc[origin_idx:].max())
        if current_price is not None and current_price <= origin:
            return None                     # leg fully retraced -> void
        hi, lo, leg = extreme, origin, "bullish"
    else:
        extreme = float(df["low"].iloc[origin_idx:].min())
        if current_price is not None and current_price >= origin:
            return None
        hi, lo, leg = origin, extreme, "bearish"
    if hi <= lo:
        return None

    band = get_premium_discount(hi, lo, current_price, leg_direction=leg)
    band["leg_source"] = f"{kind.lower()}_impulse"
    band["leg_high"], band["leg_low"] = round(hi, 6), round(lo, 6)
    return band


def _current_dealing_range(confirmed_highs, confirmed_lows, current_price=None):
    """
    Most recent dealing range that BRACKETS current price (BUG-09). Start from the
    latest confirmed swing high/low; if that pair does not bracket price (e.g.
    after a breakout price sits outside the last leg), walk out to the nearest
    confirmed high above price and nearest confirmed low below price so the
    premium/discount + OTE math is computed on a range that actually contains
    price. Returns (high, low, leg_direction, brackets_price). `brackets_price`
    is False when price is beyond all confirmed structure (strong breakout) — the
    caller should treat premium/discount as directional context only.
    """
    if not confirmed_highs or not confirmed_lows:
        return None, None, None, False
    last_high = confirmed_highs[-1]
    last_low = confirmed_lows[-1]
    leg = "bullish" if last_high["ts"] >= last_low["ts"] else "bearish"
    hi = last_high["price"]
    lo = last_low["price"]
    if hi <= lo:
        # Degenerate ordering — widen using the prior swing on the deficient side.
        if len(confirmed_highs) >= 2:
            hi = max(hi, confirmed_highs[-2]["price"])
        if len(confirmed_lows) >= 2:
            lo = min(lo, confirmed_lows[-2]["price"])

    brackets = True
    if current_price is not None and not (lo <= current_price <= hi):
        highs_above = [h for h in confirmed_highs if h["price"] >= current_price]
        lows_below = [l for l in confirmed_lows if l["price"] <= current_price]
        if highs_above:
            hi_swing = min(highs_above, key=lambda h: h["price"])
            hi = hi_swing["price"]         # nearest confirmed resistance above price
        if lows_below:
            lo_swing = max(lows_below, key=lambda l: l["price"])
            lo = lo_swing["price"]         # nearest confirmed support below price
        brackets = bool(highs_above and lows_below and lo <= current_price <= hi)
        # B9.0: `leg` was pinned to the LATEST confirmed swings and survived the
        # re-bracket walk, so a re-bracketed range kept the pre-breakout leg direction
        # (verified: highs [100,105], lows [95,98], price 96 -> range [95,100] labelled
        # bullish). Re-derive it from the swings actually chosen; when price is beyond
        # all confirmed structure there is no leg to speak of.
        if highs_above and lows_below:
            leg = "bullish" if hi_swing["ts"] >= lo_swing["ts"] else "bearish"
        else:
            leg = None
    return hi, lo, leg, brackets


# ---------------- context assembly ---------------- #

def build_smc_context(df_4h, df_1h, df_15m, df_1d=None, value_areas: dict = None):
    """
    Build multi-timeframe SMC context. `value_areas` is an optional
    {tf: {vah,val,poc}} dict computed once upstream to avoid recomputation (B11).
    The optional 1D leg adds daily structure + daily liquidity pools that the
    geometry engine uses as higher-timeframe targets.
    """
    from price_action import get_value_area

    context = {}
    value_areas = value_areas or {}
    for tf, df in (("1d", df_1d), ("4h", df_4h), ("1h", df_1h), ("15m", df_15m)):
        if df is None or df.empty:
            continue

        current_price = float(df["close"].iloc[-1])
        left_p, right_p = SWING_WIDTHS.get(tf, DEFAULT_WIDTH)

        confirmed_highs, confirmed_lows = _confirmed_swings(df, left_p, right_p)
        sh = [(h["ts"], h["price"]) for h in confirmed_highs][-5:]
        sl = [(l["ts"], l["price"]) for l in confirmed_lows][-5:]

        struct, bos, choch, struct_state = get_market_structure(confirmed_highs, confirmed_lows, df)

        va = value_areas.get(tf) or get_value_area(df)
        obs = find_order_blocks(df, va)
        fvgs = find_fvg(df)
        liq = find_liquidity_levels(confirmed_highs, confirmed_lows, atr_pct=_atr_pct(df),
                                    current_price=current_price, df=df, tf=tf)

        dealing_high, dealing_low, leg_dir, brackets = _current_dealing_range(
            confirmed_highs, confirmed_lows, current_price=current_price)

        # I6.0: premium/discount stays on the bracketing dealing range (that is what the
        # zone means), but OTE is drawn on the impulse leg of the last structural break.
        if dealing_high is not None and dealing_low is not None:
            pd_zone = get_premium_discount(dealing_high, dealing_low, current_price,
                                           leg_direction=leg_dir or "bullish")
            pd_zone["leg_source"] = "dealing_range"
        else:
            pd_zone = {"zone": "neutral", "in_ote": False, "ote_zone": {"low": 0, "high": 0},
                       "leg_direction": None, "leg_source": None}
        impulse = impulse_leg_ote(df, bos, choch, current_price)
        if impulse is not None:
            pd_zone.update({k: impulse[k] for k in
                            ("ote_zone", "in_ote", "leg_direction", "leg_source",
                             "leg_high", "leg_low")})
        elif leg_dir is None or not brackets:
            # no impulse leg AND no honest dealing-range leg -> no OTE, not a guess
            pd_zone.update({"ote_zone": {"low": 0, "high": 0}, "in_ote": False,
                            "leg_direction": None, "leg_source": None})

        dev_h, dev_l = find_developing_swings(df)

        context[tf] = {
            "current_price": current_price,
            "structure": struct,
            "structure_state": struct_state,
            "bos": bos,
            "choch": choch,
            "order_blocks": obs,
            "fvg": fvgs,
            "liquidity_levels": liq,
            "premium_discount": pd_zone,
            "dealing_range": {"high": dealing_high, "low": dealing_low, "brackets_price": brackets},
            "developing_swings": {
                "highs": [d["price"] for d in dev_h],
                "lows": [d["price"] for d in dev_l],
            },
        }

    return context
