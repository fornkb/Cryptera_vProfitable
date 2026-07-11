"""
Cryptera v3.2 - Confluence Scoring Engine

Regime-conditional scoring:
  * Trending regimes  -> trend-continuation rubric (C1-C8).
  * Ranging regimes   -> mean-reversion rubric (M1-M8) that fades value-area
                         extremes instead of chasing trend.

Order-flow / funding / OI / sentiment are wired in as bounded score modifiers
(no longer dead inputs). The engine also proposes deterministic trade geometry
(entry / SL / TP) so the LLM refines rather than invents. An optional calibration
file (data/calibration.json) supplies per-component weights and a
score -> empirical-win-rate mapping.
"""

import os
import json
import math
import pandas as pd
import numpy as np

_CALIBRATION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calibration.json")
_CALIBRATION_CACHE = None


# ---------------- calibration ---------------- #

def load_calibration() -> dict:
    """Load optional calibration; gracefully no-op (equal weights) when absent."""
    global _CALIBRATION_CACHE
    if _CALIBRATION_CACHE is not None:
        return _CALIBRATION_CACHE
    data = {"weights": {}, "mr_weights": {}, "score_to_winrate": [], "logistic": {}}
    try:
        if os.path.exists(_CALIBRATION_PATH):
            with open(_CALIBRATION_PATH) as f:
                loaded = json.load(f)
            data.update({k: loaded.get(k, data[k]) for k in data})
    except Exception:
        pass
    _CALIBRATION_CACHE = data
    return data


def _empirical_winrate(score: int, calib: dict):
    for b in calib.get("score_to_winrate", []) or []:
        if b.get("min", 0) <= score <= b.get("max", 100):
            return b.get("win_rate")
    return None


def _logistic_winrate(breakdown: dict, score_mode: str, calib: dict):
    """
    Calibration v2 (BUG-12): win probability from a standardized logistic model
    fitted per rubric over the confluence_breakdown components. Returns None when
    no logistic model is present (engine then falls back to the score bucket).
    """
    model = (calib.get("logistic") or {}).get(score_mode)
    if not model:
        return None
    coef = model.get("coef") or {}
    mean = model.get("mean") or {}
    std = model.get("std") or {}
    z = float(model.get("intercept", 0.0))
    for k, w in coef.items():
        mu = float(mean.get(k, 0.0))
        sd = float(std.get(k, 1.0)) or 1.0
        x = float(breakdown.get(k, 0.0) or 0.0)
        z += float(w) * ((x - mu) / sd)
    try:
        return round(1.0 / (1.0 + math.exp(-z)), 4)
    except OverflowError:
        return 1.0 if z > 0 else 0.0


# ---------------- helpers ---------------- #

def _atr_abs(df: pd.DataFrame) -> float:
    if df is None or df.empty or "ATR" not in df.columns:
        return 0.0
    atr = df["ATR"].iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0


def _structure_bias(structure: str) -> str:
    """HH/HL -> bull, LH/LL -> bear, everything else (incl. HH/LL, LH/HL) -> mixed (fixes B10)."""
    if not structure:
        return "mixed"
    if "HH" in structure and "HL" in structure:
        return "bull"
    if "LH" in structure and "LL" in structure:
        return "bear"
    return "mixed"


def derive_trend_bias(smc_context: dict, df_4h: pd.DataFrame = None) -> str:
    """4H structure first; EMA(50/200) on 4H as a directional fallback when structure is mixed."""
    struct_4h = smc_context.get("4h", {}).get("structure", "NEUTRAL") if smc_context else "NEUTRAL"
    b = _structure_bias(struct_4h)
    if b == "bull":
        return "bullish"
    if b == "bear":
        return "bearish"
    if df_4h is not None and not df_4h.empty and "EMA_50" in df_4h.columns and "EMA_200" in df_4h.columns:
        latest = df_4h.iloc[-1]
        if pd.notna(latest["EMA_50"]) and pd.notna(latest["EMA_200"]):
            return "bullish" if latest["EMA_50"] > latest["EMA_200"] else "bearish"
    return "neutral"


def _sign(x) -> int:
    try:
        v = float(x)
    except Exception:
        return 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


# ---------------- multi-timeframe liquidity sweep (T1-7) ---------------- #

def detect_mtf_sweep(df_15m: pd.DataFrame, smc_context: dict, pdh: float, pdl: float,
                     trend_bias: str) -> tuple[int, str, str]:
    """
    Scan the last 5 closed 15m candles for a sweep + reclaim of a liquidity pool
    drawn from ALL timeframes (4H/1H/15M pools + PDH/PDL). HTF sweeps score the
    full 10; a 15m-only sweep scores 6. Returns (points, note, tier).
    """
    if df_15m is None or df_15m.empty or len(df_15m) < 6 or trend_bias not in ("bullish", "bearish"):
        return 0, "no sweep", "none"

    # Build candidate pools with tier weights (higher tier = more significant).
    pools = []  # (price, side, tier_name, tier_rank)
    tier_rank = {"4h": 3, "1h": 2, "pd": 2, "15m": 1}
    for tf in ("4h", "1h", "15m"):
        liq = smc_context.get(tf, {}).get("liquidity_levels", {}) or {}
        for lvl in liq.get("buy_side", []) or []:
            pools.append((lvl, "buy", tf, tier_rank[tf]))
        for lvl in liq.get("sell_side", []) or []:
            pools.append((lvl, "sell", tf, tier_rank[tf]))
    if pdh:
        pools.append((pdh, "buy", "pd", tier_rank["pd"]))
    if pdl:
        pools.append((pdl, "sell", "pd", tier_rank["pd"]))

    if not pools:
        return 0, "no pools", "none"

    # A genuine stop-run (Judas swing) is aggressive: it spikes through the pool and
    # gets rejected within the SAME candle, leaving a dominant rejection wick. Require
    # the rejection wick beyond the pool to be >= 40% of the candle range (P4) so slow
    # "acceptance" reclaims with tiny wicks don't qualify.
    WICK_FRAC = 0.40
    recent = df_15m.tail(5)
    best_rank = 0
    best_tier = "none"
    for _, candle in recent.iterrows():
        c_high, c_low = float(candle["high"]), float(candle["low"])
        c_open, c_close = float(candle["open"]), float(candle["close"])
        rng = c_high - c_low
        if rng <= 0:
            continue
        if trend_bias == "bullish":
            lower_wick = min(c_open, c_close) - c_low
            if lower_wick < WICK_FRAC * rng:
                continue  # weak reclaim, not an aggressive sweep
            for price, side, tier, rank in pools:
                if side == "sell" and c_low < price and c_close > price and rank > best_rank:
                    best_rank, best_tier = rank, tier
        else:
            upper_wick = c_high - max(c_open, c_close)
            if upper_wick < WICK_FRAC * rng:
                continue
            for price, side, tier, rank in pools:
                if side == "buy" and c_high > price and c_close < price and rank > best_rank:
                    best_rank, best_tier = rank, tier

    if best_rank >= 2:
        return 10, f"HTF sweep+reclaim, wick-confirmed ({best_tier})", best_tier
    if best_rank == 1:
        return 6, "15m sweep+reclaim, wick-confirmed", "15m"
    return 0, "no aggressive sweep", "none"


# ---------------- trend-continuation components (C1-C8) ---------------- #

def _c1_trend(smc_context, windowed):
    s4 = smc_context.get("4h", {}).get("structure", "NEUTRAL")
    s1 = smc_context.get("1h", {}).get("structure", "NEUTRAL")
    adx = float(windowed.get("4h", {}).get("adx", 0.0) or 0.0)
    b4, b1 = _structure_bias(s4), _structure_bias(s1)
    if not (b4 == b1 and b4 in ("bull", "bear")):
        return 10, f"4H/1H conflict ({s4} vs {s1})"
    if adx < 25:
        return 10, f"weak ADX {adx:.1f}"
    if adx < 41:
        return 15, f"medium ADX {adx:.1f}"
    if adx < 56:
        return 20, f"strong ADX {adx:.1f}"
    return 25, f"very strong ADX {adx:.1f}"


def _c2_ob_prox(price, smc_context, bias, atr_abs):
    """ATR-scaled OB/FVG proximity (P2). Falls back to fixed % only if ATR is missing."""
    if bias not in ("bullish", "bearish"):
        return 0, "neutral bias"
    direction = "bullish" if bias == "bullish" else "bearish"
    best, label = None, "none"  # best = ABSOLUTE distance
    for tf in ("15m", "1h"):
        ctx = smc_context.get(tf, {}) or {}
        for ob in ctx.get("order_blocks", {}).get(direction, []) or []:
            edge = ob.get("bottom") if direction == "bullish" else ob.get("top")
            if edge is None:
                continue
            d = abs(price - edge)
            if best is None or d < best:
                best, label = d, f"{tf.upper()} {direction} OB"
        fv = (ctx.get("fvg", {}) or {}).get(f"nearest_{direction}_fvg")
        if fv and not fv.get("filled", True):
            edge = fv.get("top") if direction == "bullish" else fv.get("bottom")
            if edge is not None:
                d = abs(price - edge)
                if best is None or d < best:
                    best, label = d, f"{tf.upper()} {direction} FVG"
    if best is None:
        return 0, "no nearby OB/FVG"
    if atr_abs and atr_abs > 0:
        d_atr = best / atr_abs
        if d_atr <= 0.5:
            return 15, f"{label} {d_atr:.2f} ATR away"
        if d_atr <= 1.0:
            return 10, f"{label} {d_atr:.2f} ATR away"
        if d_atr <= 2.0:
            return 5, f"{label} {d_atr:.2f} ATR away"
        return 0, f"nearest {label} {d_atr:.2f} ATR (>2 ATR)"
    pct = best / price if price else 1.0
    if pct <= 0.003:
        return 15, f"{label} {pct*100:.2f}% away"
    if pct <= 0.005:
        return 10, f"{label} {pct*100:.2f}% away"
    if pct <= 0.01:
        return 5, f"{label} {pct*100:.2f}% away"
    return 0, f"nearest {label} {pct*100:.2f}% (>1%)"


def _c4_momentum(df_1h, windowed, bias):
    if df_1h is None or df_1h.empty:
        return 0, "no 1H data"
    latest = df_1h.iloc[-1]
    rsi = float(latest.get("RSI", 50.0) or 50.0)
    macd, sig = latest.get("MACD"), latest.get("MACD_SIGNAL")
    macd_ok = pd.notna(macd) and pd.notna(sig)
    base = 0
    if bias == "bullish" and rsi < 65 and macd_ok and macd > sig:
        base = 10
    elif bias == "bearish" and rsi > 35 and macd_ok and macd < sig:
        base = 10
    series = windowed.get("1h", {}).get("rsi_series_last_5", []) or []
    adj, lbl = 0, "no slope"
    if len(series) >= 3:
        slope = float(series[-1]) - float(series[-3])
        if slope > 5:
            adj, lbl = 5, f"slope +{slope:.1f}"
        elif slope < -5:
            adj, lbl = -5, f"slope {slope:.1f}"
        else:
            lbl = f"slope {slope:+.1f}"
    return max(0, min(15, base + adj)), f"RSI {rsi:.1f}, {lbl}"


def _c5_fvg_magnet(price, smc_context, bias, atr_map):
    """ATR-scaled FVG-magnet range (P2): within 3x the FVG's own-TF ATR. % fallback."""
    if bias not in ("bullish", "bearish"):
        return 0, "neutral bias"
    direction = "bullish" if bias == "bullish" else "bearish"
    for tf in ("1h", "4h"):
        fv = (smc_context.get(tf, {}).get("fvg", {}) or {}).get(f"nearest_{direction}_fvg")
        if fv and not fv.get("filled", True):
            top, bot = fv.get("top"), fv.get("bottom")
            if top is None or bot is None:
                continue
            mid = (top + bot) / 2
            if mid <= 0:
                continue
            dist = abs(price - mid)
            atr_tf = (atr_map or {}).get(tf, 0.0)
            limit = (3.0 * atr_tf) if (atr_tf and atr_tf > 0) else (price * 0.03)
            if dist <= limit:
                unit = f"{dist/atr_tf:.2f} ATR" if (atr_tf and atr_tf > 0) else f"{dist/price*100:.2f}%"
                return 15, f"{tf.upper()} {direction} FVG {unit} away"
    return 0, "no FVG magnet within range"


def _c6_ote(smc_context, bias):
    """OTE credit only when the retracement leg points the same way as the trade (BUG-05)."""
    for tf in ("1h", "4h"):
        pdz = smc_context.get(tf, {}).get("premium_discount", {}) or {}
        if pdz.get("in_ote", False) and pdz.get("leg_direction") == bias:
            return 10, f"in OTE on {tf.upper()} ({bias} leg)"
    return 0, "not in OTE"


def _c7_cvd(windowed, bias):
    if bias not in ("bullish", "bearish"):
        return 0, "neutral bias", False
    target = 1 if bias == "bullish" else -1
    c4 = _sign(windowed.get("4h", {}).get("cvd_window_delta", 0))
    c1 = _sign(windowed.get("1h", {}).get("cvd_window_delta", 0))
    c15 = _sign(windowed.get("15m", {}).get("cvd_window_delta", 0))
    absorption = c15 != 0 and c4 != 0 and c15 == -c4
    if absorption:
        return 0, "15m CVD opposes 4H (absorption)", True
    matches = sum(1 for s in (c4, c1, c15) if s == target)
    # Rebudgeted to 15 max (BUG-08 rubric decorrelation): CVD is orthogonal
    # evidence, so full 3-TF alignment earns more than the location cluster.
    if matches == 3:
        return 15, "strong CVD alignment (3 TF)", False
    if matches == 2:
        return 10, "CVD aligned 2 TFs", False
    return 0, f"CVD aligned {matches} TF(s)", False


def _c8_stoch(windowed, bias):
    k = float(windowed.get("1h", {}).get("stochrsi_k", 50.0) or 50.0)
    if bias == "bearish" and k > 80:
        return 5, f"overbought K {k:.1f}"
    if bias == "bullish" and k < 20:
        return 5, f"oversold K {k:.1f}"
    return 0, f"K {k:.1f} neutral"


# ---------------- mean-reversion components (ranging regime) ---------------- #

def _mr_setup(price, pa_context, smc_context):
    """
    Decide the mean-revert direction from value-area position:
    near/above VAH -> fade SHORT, near/below VAL -> fade LONG. Returns
    (direction, edge_price, va) or (None, None, va).
    """
    va = pa_context.get("value_area_1h") or {}
    vah, val = va.get("vah"), va.get("val")
    if not vah or not val or vah <= val:
        return None, None, va
    span = vah - val
    upper_band = vah - 0.15 * span
    lower_band = val + 0.15 * span
    if price >= upper_band:
        return "bearish", vah, va
    if price <= lower_band:
        return "bullish", val, va
    return None, None, va


def _m1_edge_distance(price, edge, va):
    if not edge or not va or va.get("vah", 0) <= va.get("val", 0):
        return 0, "mid-range"
    span = va["vah"] - va["val"]
    d = abs(price - edge) / span
    if d <= 0.05:
        return 25, f"at edge ({d*100:.1f}% of VA)"
    if d <= 0.15:
        return 18, f"near edge ({d*100:.1f}% of VA)"
    if d <= 0.30:
        return 10, f"approaching edge ({d*100:.1f}% of VA)"
    return 0, "mid-range"


def _m2_edge_sweep(df_15m, smc_context, pdh, pdl, mr_dir):
    pts, note, _ = detect_mtf_sweep(df_15m, smc_context, pdh, pdl, mr_dir)
    return (15 if pts >= 10 else (10 if pts >= 6 else 0)), f"edge sweep: {note}"


def _m3_cvd_absorption(windowed, mr_dir):
    """Reward CVD absorption at the edge (delta opposing the prior push)."""
    d15 = _sign(windowed.get("15m", {}).get("cvd_window_delta", 0))
    target = 1 if mr_dir == "bullish" else -1  # we want buyers absorbing at lows / sellers at highs
    if d15 == target:
        return 15, "CVD absorption supports fade"
    if d15 == 0:
        return 5, "flat CVD"
    return 0, "CVD against fade"


def _m4_stoch_extreme(windowed, mr_dir):
    k = float(windowed.get("1h", {}).get("stochrsi_k", 50.0) or 50.0)
    if mr_dir == "bearish" and k > 80:
        return 15, f"overbought K {k:.1f}"
    if mr_dir == "bullish" and k < 20:
        return 15, f"oversold K {k:.1f}"
    if mr_dir == "bearish" and k > 65:
        return 8, f"elevated K {k:.1f}"
    if mr_dir == "bullish" and k < 35:
        return 8, f"depressed K {k:.1f}"
    return 0, f"K {k:.1f} neutral"


def _m5_rejection(pa_context, mr_dir):
    pat = pa_context.get("last_candle_pattern_15m", "none")
    bull_pats = {"pin_bar_bull", "bullish_engulfing", "morning_star"}
    bear_pats = {"pin_bar_bear", "bearish_engulfing", "evening_star"}
    if mr_dir == "bullish" and pat in bull_pats:
        return 10, f"rejection: {pat}"
    if mr_dir == "bearish" and pat in bear_pats:
        return 10, f"rejection: {pat}"
    return 0, "no rejection candle"


def _m6_range_confirmed(smc_context):
    """Bonus when 1H has no fresh BOS/CHoCH (range intact, fade is safer)."""
    bos = smc_context.get("1h", {}).get("bos", {}) or {}
    choch = smc_context.get("1h", {}).get("choch", {}) or {}
    if not bos.get("fresh") and not choch.get("fresh"):
        return 10, "range intact (no fresh break)"
    return 0, "structure breaking - range at risk"


# ---------------- volume gate ---------------- #

# Rubric decorrelation (audit §3.6): C2 (OB proximity), C5 (FVG magnet) and C6
# (OTE) all answer "is price at a good LOCATION" — left uncapped a single location
# could bank 40 pts and dominate the score. They now share one capped budget so
# freed headroom goes to orthogonal evidence (C3 sweep -> 15, C7 CVD -> 15). The
# individual c2/c5/c6 keys are preserved (calibration/labeling stability); only
# their SUM is bounded. Per-regime action thresholds are deferred until eval data
# supports refitting them (current_status Phase 2).
LOCATION_CAP = 25
LOCATION_KEYS = ("c2_ob_proximity", "c5_fvg_magnet", "c6_ote_bonus")


def _rebudget_sweep(pts: int) -> int:
    """Scale the C3 sweep slot to a 15-pt max (HTF sweep 15, 15m-only 9)."""
    if pts >= 10:
        return 15
    if pts >= 6:
        return 9
    return 0


def _apply_location_cap(breakdown: dict) -> tuple[dict, int]:
    """Proportionally scale the location components so their sum <= LOCATION_CAP."""
    total = sum(int(breakdown.get(k, 0) or 0) for k in LOCATION_KEYS)
    if total <= LOCATION_CAP or total <= 0:
        return breakdown, total
    f = LOCATION_CAP / total
    for k in LOCATION_KEYS:
        breakdown[k] = int(round(int(breakdown.get(k, 0) or 0) * f))
    return breakdown, LOCATION_CAP


def _volume_gate(windowed):
    """
    Session-aware volume gate: prefer the same-time-of-day relative volume
    (removes the Asia-hours false LOW_VOL bias) and fall back to the flat
    trailing relative volume when session data is unavailable.
    """
    w15 = windowed.get("15m", {}) or {}
    rel = float(w15.get("relative_volume", 1.0) or 1.0)
    srel = w15.get("session_relative_volume")
    gate_val = float(srel) if srel is not None else rel
    state = "HARD_GATE" if gate_val < 0.1 else ("LOW_VOL_WARNING" if gate_val < 0.3 else "CLEAR")
    return {"state": state, "rel_vol_15m": rel, "session_rel_vol_15m": srel,
            "kill_zone": w15.get("kill_zone", "OFF"), "gate_basis": "session" if srel is not None else "trailing"}


# ---------------- order-flow / positioning modifiers (T1-4) ---------------- #

def _order_flow_modifier(direction, orderbook, funding, open_interest, sentiment, windowed,
                         btc_dominance=None):
    """Bounded [-8, +8] confirmation/veto from non-price data. direction in bull/bear."""
    if direction not in ("bullish", "bearish"):
        return 0, ["neutral bias - order flow not scored"]
    notes = []
    mod = 0

    # 1) L2 depth imbalance at the tightest band (de-weighted: L2 is spoofable)
    bins = (orderbook or {}).get("depth_bins") or []
    if bins:
        imb = float(bins[0].get("imbalance", 0.0))  # +bid heavy
        want = 1 if direction == "bullish" else -1
        agree = (imb * want) > 0
        if abs(imb) >= 0.25:
            mod += 2 if agree else -2
            notes.append(f"depth {'supports' if agree else 'opposes'} ({imb:+.2f} @±{bins[0].get('band_pct')}%)")
        elif abs(imb) >= 0.1:
            mod += 1 if agree else -1
            notes.append(f"depth mild {'support' if agree else 'opposition'} ({imb:+.2f})")

    # 2) Funding: penalise entering WITH the crowd at an extreme; factor trajectory
    f = funding or {}
    pct = f.get("percentile_window")
    cur = f.get("current")
    trend = (f.get("trend") or "").lower()
    if pct is not None and cur is not None:
        if direction == "bullish":
            if pct >= 80 and cur > 0:
                mod -= 3
                note = f"crowded longs (funding p{pct:.0f}"
                if trend == "rising":
                    mod -= 1
                    note += ", rising - squeeze risk"
                notes.append(note + ")")
            elif pct <= 20 and cur < 0:
                mod += 2
                notes.append(f"shorts paying (funding p{pct:.0f}) - long tailwind")
        else:
            if pct <= 20 and cur < 0:
                mod -= 3
                note = f"crowded shorts (funding p{pct:.0f}"
                if trend == "falling":
                    mod -= 1
                    note += ", falling - squeeze risk"
                notes.append(note + ")")
            elif pct >= 80 and cur > 0:
                mod += 2
                notes.append(f"longs paying (funding p{pct:.0f}) - short tailwind")

    # 3) Open interest vs price (continuation confirm / squeeze warn).
    #    Match the OI window to the price window (BUG-10): change_4h_pct is a
    #    single-4H-bar delta, so compare it against the last CLOSED 4H bar's price
    #    change, not the ~40h window_price_change_pct.
    oi = open_interest or {}
    oi_chg = oi.get("change_4h_pct")
    w4 = windowed.get("4h", {}) or {}
    px_chg = float(w4.get("bar_price_change_pct", w4.get("window_price_change_pct", 0.0)) or 0.0)
    if oi_chg is not None:
        px_dir = 1 if px_chg > 0 else (-1 if px_chg < 0 else 0)
        want = 1 if direction == "bullish" else -1
        if oi_chg > 1 and px_dir == want:
            mod += 2
            notes.append(f"OI building with trend (+{oi_chg:.1f}% 4h)")
        elif oi_chg > 1 and px_dir == -want:
            mod -= 2
            notes.append(f"OI building against - squeeze risk (+{oi_chg:.1f}% 4h)")

    # 4) Fear & Greed extremes
    if isinstance(sentiment, (int, float)):
        if direction == "bullish" and sentiment >= 80:
            mod -= 2
            notes.append(f"extreme greed F&G {sentiment} - late long")
        elif direction == "bullish" and sentiment <= 20:
            mod += 2
            notes.append(f"extreme fear F&G {sentiment} - contrarian long")
        elif direction == "bearish" and sentiment <= 20:
            mod -= 2
            notes.append(f"extreme fear F&G {sentiment} - late short")
        elif direction == "bearish" and sentiment >= 80:
            mod += 2
            notes.append(f"extreme greed F&G {sentiment} - contrarian short")

    # 5) BTC dominance (alts only): rising BTC.D = rotation into BTC, alts bleed.
    #    `btc_dominance` is None for BTC itself, so this is skipped there.
    btcd = btc_dominance or {}
    dchg = btcd.get("change_4h_pct")
    if dchg is not None:
        if direction == "bullish":
            if dchg >= 0.5:
                mod -= 4
                notes.append(f"BTC.D rising +{dchg:.2f}% 4h - macro headwind for alt long")
            elif dchg <= -0.5:
                mod += 2
                notes.append(f"BTC.D falling {dchg:.2f}% 4h - alt tailwind")
        else:
            if dchg >= 0.5:
                mod += 2
                notes.append(f"BTC.D rising +{dchg:.2f}% 4h - alt short tailwind")
            elif dchg <= -0.5:
                mod -= 3
                notes.append(f"BTC.D falling {dchg:.2f}% 4h - alts strong, short headwind")

    mod = max(-8, min(8, mod))
    if not notes:
        notes.append("order flow neutral")
    return mod, notes


# ---------------- deterministic trade geometry (T1-5) ---------------- #

def _dist_atr(level, ref_price, atr_abs):
    """Absolute distance from a reference price in 15m-ATR multiples (None if no ATR)."""
    if atr_abs and atr_abs > 0 and level is not None and ref_price is not None:
        return round(abs(float(level) - float(ref_price)) / atr_abs, 2)
    return None


def _nearest_level(cands, entry, direction):
    """Nearest real structural level beyond entry in the trade direction, or None."""
    levels = [c for c in cands if c is not None]
    if direction == "bullish":
        beyond = [c for c in levels if c > entry]
        return min(beyond) if beyond else None
    beyond = [c for c in levels if c < entry]
    return max(beyond) if beyond else None


def _select_structural_tp(cands, entry, risk, direction):
    """
    Pick the NEAREST real structural level that yields >= 2R. If none reaches 2R,
    return the FARTHEST available real level (honest sub-2R R:R). Never invents a
    price out of thin air (P1 fix). Returns (tp, source_note) or (None, None).
    """
    levels = [c for c in cands if c is not None]
    if direction == "bullish":
        beyond = sorted(c for c in levels if c > entry)
    else:
        beyond = sorted((c for c in levels if c < entry), reverse=True)
    if not beyond:
        return None, None
    for c in beyond:
        if abs(c - entry) >= 2 * risk:
            return c, "nearest >=2R structural level"
    return beyond[-1], "farthest structural level (<2R, honest)"


_TF_RANK = {"4h": 3, "1h": 2, "15m": 1}
SL_ATR_CAP = 3.0  # max structural SL width in 15m-ATR multiples (BUG-06)


def _tf_of_source(src):
    """Infer the level's timeframe from its source label (VAH/VAL/POC are 1H)."""
    s = (src or "").lower()
    if "4h" in s:
        return "4h"
    if "15m" in s:
        return "15m"
    if "1h" in s:
        return "1h"
    return "1h"


def _zone_of(level, dealing_range):
    """Premium / discount position of `level` inside the current dealing range."""
    lo = (dealing_range or {}).get("low")
    hi = (dealing_range or {}).get("high")
    if lo is None or hi is None or hi <= lo:
        return "neutral"
    return "discount" if level < (lo + 0.5 * (hi - lo)) else "premium"


def _merge_confluent(rows, tol=0.0015):
    """
    Merge candidates whose price is within `tol` (fraction) into ONE — this turns
    a stack of coincident levels (e.g. VAH + BSL + untested POC) into a single
    high-confluence candidate instead of hiding it via de-dup (#2). The first
    (already nearest-first) is the representative; stacked sources, the strongest
    TF, and any HVN flag are accumulated. `rows` are dicts with {price, source, hvn}.
    """
    merged = []
    for r in rows:
        hit = next((m for m in merged
                    if abs(r["price"] - m["price"]) / max(abs(m["price"]), 1e-9) <= tol), None)
        if hit:
            hit["_sources"].append(r["source"])
            hit["_hvn"] = hit["_hvn"] or bool(r.get("hvn"))
        else:
            merged.append({"price": r["price"], "source": r["source"],
                           "_sources": [r["source"]], "_hvn": bool(r.get("hvn"))})
    for m in merged:
        srcs = m.pop("_sources")
        m["confluence"] = srcs
        m["confluence_count"] = len(srcs)
        m["tf"] = max((_tf_of_source(s) for s in srcs), key=lambda t: _TF_RANK.get(t, 1))
        m["hvn"] = bool(m.pop("_hvn"))
    return merged


def _rank_entries(labeled, ref_price, dealing_range, atr_abs=None):
    """
    labeled: [(price, source, hvn)] -> confluence-merged, nearest-first entry
    candidates, each annotated with tf, hvn, confluence stack, premium/discount
    zone (#1), and ATR distance from current price (LLM does no arithmetic).
    """
    rows = [{"price": round(float(p), 6), "source": s, "hvn": bool(h)}
            for p, s, h in labeled if p is not None]
    rows.sort(key=lambda x: abs(x["price"] - ref_price))
    merged = _merge_confluent(rows)
    for m in merged:
        m["zone"] = _zone_of(m["price"], dealing_range)
        m["dist_from_price_atr"] = _dist_atr(m["price"], ref_price, atr_abs)
    return merged


def _rank_targets(labeled, entry, risk, direction, ref_price=None, atr_abs=None):
    """
    labeled: [(price, source)] -> confluence-merged targets strictly beyond entry,
    nearest first, each with R:R, tf, confluence stack, and ATR distance from
    current price (reachability within the horizon). HVN is not meaningful for
    targets so it is dropped.
    """
    rows = []
    for p, s in labeled:
        if p is None:
            continue
        if direction == "bullish" and p <= entry:
            continue
        if direction == "bearish" and p >= entry:
            continue
        rows.append({"price": round(float(p), 6), "source": s, "hvn": False})
    rows.sort(key=lambda x: abs(x["price"] - entry))
    merged = _merge_confluent(rows)
    for m in merged:
        m.pop("hvn", None)
        m["rr"] = round(abs(m["price"] - entry) / risk, 2) if risk > 0 else None
        m["dist_from_price_atr"] = _dist_atr(m["price"], ref_price if ref_price is not None else entry, atr_abs)
    return merged


def compute_trade_geometry(direction, price, smc_context, pa_context, atr_abs,
                           regime, score_mode, mr_edge=None):
    """
    Deterministic trade geometry. Emits a single reproducible PRIMARY entry/SL/TP
    (for backtesting/calibration) PLUS a ranked set of REAL candidate levels
    (entries / stops / targets, each with source and R:R). The LLM selects and
    manages a trade FROM this candidate menu — it never invents prices. Returns a
    dict; 'valid' False when no sound structural trade exists.
    """
    out = {"valid": False, "direction": None, "entry": None, "stop_loss": None,
           "take_profit": None, "rr": None, "rr_passed": False,
           "entry_source": None, "stop_loss_source": None, "take_profit_source": None,
           "candidates": {"entries": [], "stops": [], "targets": []}}
    if direction not in ("bullish", "bearish") or price <= 0:
        return out

    buf = max(atr_abs * 0.5, price * 0.001)  # SL buffer
    sl_cap = SL_ATR_CAP * max(atr_abs, price * 0.001)  # max SL width (BUG-06)
    ctx15 = smc_context.get("15m", {}) or {}
    liq15 = ctx15.get("liquidity_levels", {}) or {}
    liq1h = smc_context.get("1h", {}).get("liquidity_levels", {}) or {}
    liq1d = smc_context.get("1d", {}).get("liquidity_levels", {}) or {}
    pa = pa_context or {}
    va = pa.get("value_area_1h", {}) or {}
    untested = [u["poc"] for u in pa.get("untested_pocs_4h", []) or []]
    untested_1d = [u["poc"] for u in pa.get("untested_pocs_1d", []) or []]
    pwh, pwl = pa.get("pwh"), pa.get("pwl")  # previous-week high/low (HTF draws)

    # Anchored VWAP levels are real dynamic S/R — valid structural targets (P4)
    avwap = pa.get("avwap", {}) or {}
    avwap_pairs = []
    for tfk in ("15m", "1h"):
        for kk in ("from_swing_high", "from_swing_low"):
            v = (avwap.get(tfk) or {}).get(kk)
            if v:
                avwap_pairs.append((v, f"{tfk} AVWAP"))

    entry_labeled, stop_labeled, target_labeled = [], [], []

    if score_mode == "mean_revert" and mr_edge:
        if direction == "bearish":
            entry, e_src = mr_edge, "VAH (range top)"
            sl, sl_src = mr_edge + buf * 2, "above VAH + ATR buffer"
            entry_labeled = [(mr_edge, "VAH (range top)", False), (price, "current price", False)]
            stop_labeled = [(sl, sl_src)]
            target_labeled = [(va.get("poc"), "POC"), (va.get("val"), "VAL")]
            tp_src = "POC / VAL fade target"
        else:
            entry, e_src = mr_edge, "VAL (range bottom)"
            sl, sl_src = mr_edge - buf * 2, "below VAL + ATR buffer"
            entry_labeled = [(mr_edge, "VAL (range bottom)", False), (price, "current price", False)]
            stop_labeled = [(sl, sl_src)]
            target_labeled = [(va.get("poc"), "POC"), (va.get("vah"), "VAH")]
            tp_src = "POC / VAH fade target"
    else:
        # trend continuation: pull back to OB/FVG, target next opposing liquidity
        obs = ctx15.get("order_blocks", {}).get(direction, []) or []
        fvg = (ctx15.get("fvg", {}) or {}).get(f"nearest_{direction}_fvg")
        if direction == "bullish":
            # Enter at OB MIDPOINT rather than the near edge (audit §3.5) — a more
            # realistic fill than assuming the extreme edge always tags.
            ob_entries = [((ob["top"] + ob["bottom"]) / 2.0, "15m bull OB mid",
                           bool(ob.get("high_volume_node"))) for ob in obs]
            if fvg and not fvg.get("filled", True):
                ob_entries.append(((fvg["top"] + fvg["bottom"]) / 2.0, "15m bull FVG mid", False))
            valid_entries = [(p, s, h) for p, s, h in ob_entries if p <= price * 1.002]
            entry = max((p for p, _, _ in valid_entries), default=price)
            e_src = next((s for p, s, _ in valid_entries if p == entry), "current price")
            entry_labeled = valid_entries + [(price, "current price", False)]
            # Nearest protected swing BELOW entry (BUG-06) — not the widest pool.
            dr_low = ctx15.get("dealing_range", {}).get("low")
            ssl_below = [l for l in (liq15.get("sell_side", []) or []) if l and l < entry]
            swing_low = max(ssl_below) if ssl_below else (dr_low if dr_low and dr_low < entry else None)
            sl = (swing_low - buf) if swing_low else entry - buf * 3
            sl_src = "below nearest protected 15m swing low - buffer"
            if entry - sl > sl_cap:
                sl = entry - sl_cap
                sl_src = f"ATR-capped ({SL_ATR_CAP:g}x 15m ATR below entry)"
            stop_labeled = [(sl, sl_src)]
            if dr_low and dr_low < entry and abs((dr_low - buf) - sl) / max(sl, 1e-9) > 0.0005:
                stop_labeled.append((dr_low - buf, "below dealing-range low - buffer"))
            target_labeled = [(l, "15m BSL") for l in (liq15.get("buy_side", []) or [])] + \
                             [(l, "1h BSL") for l in (liq1h.get("buy_side", []) or [])] + \
                             [(l, "1d BSL") for l in (liq1d.get("buy_side", []) or [])] + \
                             [(l, "untested 4H POC") for l in untested] + \
                             [(l, "untested daily POC") for l in untested_1d] + \
                             ([(pwh, "PWH")] if pwh else []) + \
                             avwap_pairs + ([(va.get("vah"), "VAH")] if va.get("vah") else [])
            tp_src = "BSL / untested POC / PWH / AVWAP / VAH"
        else:
            ob_entries = [((ob["top"] + ob["bottom"]) / 2.0, "15m bear OB mid",
                           bool(ob.get("high_volume_node"))) for ob in obs]
            if fvg and not fvg.get("filled", True):
                ob_entries.append(((fvg["top"] + fvg["bottom"]) / 2.0, "15m bear FVG mid", False))
            valid_entries = [(p, s, h) for p, s, h in ob_entries if p >= price * 0.998]
            entry = min((p for p, _, _ in valid_entries), default=price)
            e_src = next((s for p, s, _ in valid_entries if p == entry), "current price")
            entry_labeled = valid_entries + [(price, "current price", False)]
            # Nearest protected swing ABOVE entry (BUG-06) — not the widest pool.
            dr_high = ctx15.get("dealing_range", {}).get("high")
            bsl_above = [l for l in (liq15.get("buy_side", []) or []) if l and l > entry]
            swing_high = min(bsl_above) if bsl_above else (dr_high if dr_high and dr_high > entry else None)
            sl = (swing_high + buf) if swing_high else entry + buf * 3
            sl_src = "above nearest protected 15m swing high + buffer"
            if sl - entry > sl_cap:
                sl = entry + sl_cap
                sl_src = f"ATR-capped ({SL_ATR_CAP:g}x 15m ATR above entry)"
            stop_labeled = [(sl, sl_src)]
            if dr_high and dr_high > entry and abs((dr_high + buf) - sl) / max(sl, 1e-9) > 0.0005:
                stop_labeled.append((dr_high + buf, "above dealing-range high + buffer"))
            target_labeled = [(l, "15m SSL") for l in (liq15.get("sell_side", []) or [])] + \
                             [(l, "1h SSL") for l in (liq1h.get("sell_side", []) or [])] + \
                             [(l, "1d SSL") for l in (liq1d.get("sell_side", []) or [])] + \
                             [(l, "untested 4H POC") for l in untested] + \
                             [(l, "untested daily POC") for l in untested_1d] + \
                             ([(pwl, "PWL")] if pwl else []) + \
                             avwap_pairs + ([(va.get("val"), "VAL")] if va.get("val") else [])
            tp_src = "SSL / untested POC / PWL / AVWAP / VAL"

    if entry is None or sl is None or entry == sl:
        return out
    risk = abs(entry - sl)
    if risk <= 0:
        return out

    # PRIMARY target — real structural levels only, never invented (P1 fix).
    tp_prices = [p for p, _ in target_labeled]
    if score_mode == "mean_revert":
        tp = _nearest_level(tp_prices, entry, direction)
        tp_note = "nearest fade target"
    else:
        tp, tp_note = _select_structural_tp(tp_prices, entry, risk, direction)
    if tp is None:
        return out  # no structural target in the trade direction -> no sound trade
    tp_src = f"{tp_src} [{tp_note}]"

    reward = abs(tp - entry)
    rr = round(reward / risk, 2) if risk > 0 else None

    out.update({
        "valid": True,
        "direction": "BUY" if direction == "bullish" else "SELL",
        "entry": round(float(entry), 6),
        "stop_loss": round(float(sl), 6),
        "take_profit": round(float(tp), 6),
        "rr": rr,
        "rr_passed": bool(rr is not None and rr >= 2.0),
        "sl_width_pct": round(risk / entry * 100, 3) if entry else None,
        "entry_source": e_src,
        "stop_loss_source": sl_src,
        "take_profit_source": tp_src,
        "candidates": {
            "entries": _rank_entries(entry_labeled, price, ctx15.get("dealing_range", {}), atr_abs=atr_abs),
            "stops": [{"price": round(float(p), 6), "source": s, "tf": _tf_of_source(s),
                       "dist_from_price_atr": _dist_atr(p, price, atr_abs)}
                      for p, s in stop_labeled if p is not None],
            "targets": _rank_targets(target_labeled, entry, risk, direction,
                                     ref_price=price, atr_abs=atr_abs),
        },
    })
    return out


# ---------------- main evaluation ---------------- #

def evaluate_strategies(df_4h, df_1h, df_15m, orderbook, funding, sentiment, open_interest,
                        smc_context, windowed_indicators, pa_context, market_regime,
                        btc_dominance=None) -> dict:
    """
    Regime-conditional confluence scoring + order-flow modifiers + deterministic
    trade geometry. All previously-dead inputs (orderbook/funding/sentiment/OI)
    are now used.
    """
    if df_15m is None or df_15m.empty:
        raise ValueError("df_15m is required")

    calib = load_calibration()
    weights = calib.get("weights", {})
    mr_weights = calib.get("mr_weights", {})
    price = float(df_15m["close"].iloc[-1])
    atr_abs = _atr_abs(df_15m)
    atr_map = {"4h": _atr_abs(df_4h), "1h": _atr_abs(df_1h), "15m": atr_abs}
    pdh = (pa_context or {}).get("pdh")
    pdl = (pa_context or {}).get("pdl")

    is_ranging = "Ranging" in (market_regime or "") or "Sideways" in (market_regime or "")
    trend_bias = derive_trend_bias(smc_context, df_4h)

    if is_ranging:
        score_mode = "mean_revert"
        mr_dir, mr_edge, va = _mr_setup(price, pa_context, smc_context)
        direction = mr_dir or "neutral"
        if mr_dir is None:
            breakdown = {f"m{i}": 0 for i in range(1, 7)}
            notes = {"m1": "price mid-range - no fade setup"}
            base = 0
            absorption = False
        else:
            m1, n1 = _m1_edge_distance(price, mr_edge, va)
            m2, n2 = _m2_edge_sweep(df_15m, smc_context, pdh, pdl, mr_dir)
            m3, n3 = _m3_cvd_absorption(windowed_indicators, mr_dir)
            m4, n4 = _m4_stoch_extreme(windowed_indicators, mr_dir)
            m5, n5 = _m5_rejection(pa_context, mr_dir)
            m6, n6 = _m6_range_confirmed(smc_context)
            raw = {"m1_edge_distance": m1, "m2_edge_sweep": m2, "m3_cvd_absorption": m3,
                   "m4_stoch_extreme": m4, "m5_rejection": m5, "m6_range_intact": m6}
            breakdown = {k: int(round(v * mr_weights.get(k, 1.0))) for k, v in raw.items()}
            notes = {"m1_edge_distance": n1, "m2_edge_sweep": n2, "m3_cvd_absorption": n3,
                     "m4_stoch_extreme": n4, "m5_rejection": n5, "m6_range_intact": n6}
            base = sum(breakdown.values())
            absorption = False
        geo_bias = mr_dir
    else:
        score_mode = "trend"
        direction = trend_bias
        c1, n1 = _c1_trend(smc_context, windowed_indicators)
        c2, n2 = _c2_ob_prox(price, smc_context, trend_bias, atr_abs)
        c3, n3, c3_tier = detect_mtf_sweep(df_15m, smc_context, pdh, pdl, trend_bias)
        c4, n4 = _c4_momentum(df_1h, windowed_indicators, trend_bias)
        c5, n5 = _c5_fvg_magnet(price, smc_context, trend_bias, atr_map)
        c6, n6 = _c6_ote(smc_context, trend_bias)
        c7, n7, absorption = _c7_cvd(windowed_indicators, trend_bias)
        c8, n8 = _c8_stoch(windowed_indicators, trend_bias)
        c3 = _rebudget_sweep(c3)  # sweep slot rebudgeted to 15-pt max
        raw = {"c1_trend_alignment": c1, "c2_ob_proximity": c2, "c3_liquidity_sweep": c3,
               "c4_momentum": c4, "c5_fvg_magnet": c5, "c6_ote_bonus": c6,
               "c7_cvd_alignment": c7, "c8_stochrsi": c8}
        breakdown = {k: int(round(v * weights.get(k, 1.0))) for k, v in raw.items()}
        breakdown, loc_total = _apply_location_cap(breakdown)
        notes = {"c1_trend_alignment": n1, "c2_ob_proximity": n2,
                 "c3_liquidity_sweep": f"{n3} [{c3_tier}]", "c4_momentum": n4,
                 "c5_fvg_magnet": n5, "c6_ote_bonus": n6, "c7_cvd_alignment": n7,
                 "c8_stochrsi": n8,
                 "location_composite": f"C2+C5+C6 = {loc_total}/{LOCATION_CAP} (capped)"}
        base = sum(breakdown.values())
        geo_bias = trend_bias

    base = max(0, min(100, base))

    # order-flow / positioning modifier
    of_mod, of_notes = _order_flow_modifier(geo_bias, orderbook, funding, open_interest,
                                            sentiment, windowed_indicators, btc_dominance)
    final = int(max(0, min(100, base + of_mod)))

    gate = _volume_gate(windowed_indicators)

    # deterministic trade geometry
    geometry = compute_trade_geometry(geo_bias, price, smc_context, pa_context, atr_abs,
                                      market_regime, score_mode,
                                      mr_edge=(mr_edge if is_ranging else None))

    win_rate = _logistic_winrate(breakdown, score_mode, calib)
    win_rate_source = "logistic"
    if win_rate is None:
        win_rate = _empirical_winrate(final, calib)
        win_rate_source = "score_bucket" if win_rate is not None else "none"

    return {
        "score_mode": score_mode,
        "trend_bias": trend_bias,
        "setup_direction": direction,
        "liquidity_sweep": breakdown.get("c3_liquidity_sweep", breakdown.get("m2_edge_sweep", 0)) > 0,
        "cvd_absorption_warning": absorption,
        "volume_gate": gate,
        "base_score": int(base),
        "order_flow_modifier": int(of_mod),
        "order_flow_notes": of_notes,
        "confluence_score": final,
        "confluence_breakdown": breakdown,
        "confluence_notes": notes,
        "engine_trade": geometry,
        "empirical_win_rate": win_rate,
        "win_rate_source": win_rate_source,
        "current_price": price,
    }
