"""
Indicator Engine - SMC + Momentum + Volume + Volatility

CVD is computed from real taker-initiated volume when the kline `taker_buy_base`
column is present (see main.fetch_futures_klines); otherwise it falls back to a
labelled candle-position proxy.
"""

import pandas as pd
import pandas_ta as ta
import numpy as np

from config import ADX_TREND_FLOOR, KILL_ZONES


def calculate_indicators(df: pd.DataFrame, timeframe: str = None) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True)

    # === TREND ===
    df["EMA_50"] = ta.ema(df["close"], length=50)
    df["EMA_200"] = ta.ema(df["close"], length=200)

    # === VOLATILITY ===
    df["ATR"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["ATR_Pct"] = df["ATR"].rolling(100, min_periods=20).rank(pct=True)

    # === MOMENTUM ===
    df["RSI"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    if macd is not None:
        df["MACD"] = macd["MACD_12_26_9"]
        df["MACD_SIGNAL"] = macd["MACDs_12_26_9"]
    else:
        df["MACD"] = np.nan
        df["MACD_SIGNAL"] = np.nan

    # === SUPERTREND ===
    try:
        st = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
        if st is not None:
            df["SUPERT_10_3.0"] = st["SUPERT_10_3.0"]
            df["SUPERTd_10_3.0"] = st["SUPERTd_10_3.0"]
        else:
            df["SUPERT_10_3.0"] = np.nan
            df["SUPERTd_10_3.0"] = np.nan
    except Exception:
        df["SUPERT_10_3.0"] = np.nan
        df["SUPERTd_10_3.0"] = np.nan

    # === STOCHASTIC RSI ===
    try:
        stochrsi = ta.stochrsi(df["close"], length=14, rsi_length=14, k=3, d=3)
        if stochrsi is not None:
            df["STOCHRSIk_14_14_3_3"] = stochrsi["STOCHRSIk_14_14_3_3"]
            df["STOCHRSId_14_14_3_3"] = stochrsi["STOCHRSId_14_14_3_3"]
        else:
            df["STOCHRSIk_14_14_3_3"] = np.nan
            df["STOCHRSId_14_14_3_3"] = np.nan
    except Exception:
        df["STOCHRSIk_14_14_3_3"] = np.nan
        df["STOCHRSId_14_14_3_3"] = np.nan

    # === ADX ===
    try:
        adx = ta.adx(df["high"], df["low"], df["close"], length=14)
        df["ADX"] = adx["ADX_14"] if adx is not None else np.nan
    except Exception:
        df["ADX"] = np.nan

    # === BOLLINGER BANDS (squeeze) ===
    try:
        bb = ta.bbands(df["close"], length=20, std=2.0)
        if bb is not None:
            cols = list(bb.columns)
            lower = next((c for c in cols if c.startswith("BBL_")), None)
            middle = next((c for c in cols if c.startswith("BBM_")), None)
            upper = next((c for c in cols if c.startswith("BBU_")), None)
            if lower and middle and upper:
                df["BB_LOWER"], df["BB_MIDDLE"], df["BB_UPPER"] = bb[lower], bb[middle], bb[upper]
                df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / df["BB_MIDDLE"].replace(0, np.nan)
            else:
                df["BB_WIDTH"] = np.nan
        else:
            df["BB_WIDTH"] = np.nan
    except Exception:
        df["BB_WIDTH"] = np.nan

    # === CVD: real taker flow if available, else candle-position proxy ===
    if "taker_buy_base" in df.columns and df["taker_buy_base"].notna().any():
        taker_buy = df["taker_buy_base"].fillna(df["volume"] / 2.0)
        df["cvd_delta_bar"] = (2.0 * taker_buy) - df["volume"]   # buy - sell
        df["cvd"] = df["cvd_delta_bar"].cumsum()
        df["cvd_real"] = True
    else:
        rng = df["high"] - df["low"]
        mult = np.where(rng > 0, ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng, 0.0)
        df["cvd_delta_bar"] = df["volume"] * mult
        df["cvd"] = df["cvd_delta_bar"].cumsum()
        df["cvd_real"] = False

    return df


def detect_volatility_regime(df: pd.DataFrame) -> dict:
    """Volatility regime from ATR percentile + BB width."""
    if df is None or df.empty:
        return {"regime": "unknown", "atr_percentile": None, "bb_width": None}
    atr_pct = df["ATR_Pct"].iloc[-1] if "ATR_Pct" in df.columns else None
    bb_width = df["BB_WIDTH"].iloc[-1] if "BB_WIDTH" in df.columns else None

    if atr_pct is None or pd.isna(atr_pct):
        regime = "unknown"
    elif atr_pct < 0.25:
        regime = "compressed"
    elif atr_pct > 0.75:
        regime = "expanded"
    else:
        regime = "normal"

    return {
        "regime": regime,
        "atr_percentile": float(atr_pct) if atr_pct is not None and not pd.isna(atr_pct) else None,
        "bb_width": float(bb_width) if bb_width is not None and not pd.isna(bb_width) else None,
    }


def _adx_last(df) -> float:
    if df is None or df.empty or "ADX" not in df.columns:
        return None
    v = df["ADX"].iloc[-1]
    return float(v) if pd.notna(v) else None


REGIME_BULL = "Trending Bullish"
REGIME_BEAR = "Trending Bearish"
REGIME_RANGE = "Ranging / Sideways"
REGIME_TRANSITION = "Transition"
REGIME_UNTRADEABLE = "Untradeable"


def detect_regime(df_4h, df_1h, df_15m, smc_context: dict = None,
                  volatility: dict = None) -> str:
    """
    4-state router (I3.0): trend / balanced range / transition / untradeable.

    The old router assigned "Ranging / Sideways" on FAILURE of the trend test, so early
    trends, post-CHoCH transitions and low-ADX directional drifts all routed into the
    VAH/VAL fade rubric and got faded. A range must now be EARNED by positive evidence
    of balance — no trend on either HTF, price contained by a bracketing 4H dealing
    range, nothing breaking, volatility not expanding. Everything left over is
    `Transition`: no continuation rubric, no fade rubric, HOLD with a named reason,
    which is what a discretionary trader does in that tape.

    `volatility` is a `detect_volatility_regime` block (computed today, previously
    unused as a routing input): an expanding tape is not a fadeable range, and a
    structureless expanding tape is not tradable at all.

    Hysteresis (compute_regime_with_hysteresis) is applied by the caller across runs.
    """
    adx_4h = _adx_last(df_4h)
    adx_1h = _adx_last(df_1h)
    trend_ok = adx_4h is None or adx_4h >= ADX_TREND_FLOOR
    vol = (volatility or {}).get("regime")

    if smc_context:
        c4 = smc_context.get("4h") or {}
        c1 = smc_context.get("1h") or {}
        st4 = c4.get("structure_state") or {}

        bull4 = st4.get("state") == "trending" and st4.get("trend") == "bullish"
        bear4 = st4.get("state") == "trending" and st4.get("trend") == "bearish"
        no_read_4h = st4.get("state") == "neutral" and not st4.get("trend")

        quiet = ((adx_4h is None or adx_4h < ADX_TREND_FLOOR)
                 and (adx_1h is None or adx_1h < ADX_TREND_FLOOR))
        contained = bool((c4.get("dealing_range") or {}).get("brackets_price"))
        fresh_break = any((ctx.get("bos") or {}).get("fresh")
                          or (ctx.get("choch") or {}).get("fresh")
                          for ctx in (c1, c4))

        # (1) balanced range — earned, not defaulted to. An early trend fails this on
        #     `fresh_break` (that break IS the early trend) or on containment (price has
        #     left the range), which is exactly the case the old router mis-faded.
        if quiet and contained and not fresh_break and vol != "expanded":
            return REGIME_RANGE
        # (2) trend
        if trend_ok and bull4:
            return REGIME_BULL
        if trend_ok and bear4:
            return REGIME_BEAR
        # (3) untradeable — no structural read at all AND a tape that can't be faded
        if no_read_4h and vol in ("expanded", "unknown", None):
            return REGIME_UNTRADEABLE
        # (4) directional structure with sub-floor ADX, unconfirmed changes of character,
        #     everything else: a transition, NOT a range.
        return REGIME_TRANSITION

    def tf_score(df):
        if df is None or df.empty or "EMA_50" not in df.columns or "EMA_200" not in df.columns:
            return 0
        latest = df.iloc[-1]
        score = 0
        if pd.notna(latest["EMA_50"]) and pd.notna(latest["EMA_200"]):
            score += 2 if latest["EMA_50"] > latest["EMA_200"] else -2
        if "RSI" in df.columns and pd.notna(latest["RSI"]):
            if latest["RSI"] > 60:
                score += 1
            elif latest["RSI"] < 40:
                score -= 1
        return score

    # Degraded path (no SMC context): EMA/RSI only. Without structure there is no
    # evidence of balance, so the non-trend answer is Transition, never a fade.
    weighted = 0.5 * tf_score(df_4h) + 0.3 * tf_score(df_1h) + 0.2 * tf_score(df_15m)
    if trend_ok and weighted > 0.8:
        return REGIME_BULL
    if trend_ok and weighted < -0.8:
        return REGIME_BEAR
    return REGIME_TRANSITION


def regime_bucket(regime: str) -> str:
    """
    Rubric bucket a regime maps to (I3.0, 4-state): 'trending' (continuation rubric),
    'ranging' (mean-revert rubric), or the two no-rubric states 'transition' and
    'untradeable', which route to a deterministic HOLD.
    """
    r = regime or ""
    if "Ranging" in r or "Sideways" in r:
        return "ranging"
    if "Transition" in r:
        return "transition"
    if "Untradeable" in r:
        return "untradeable"
    return "trending"


def is_tradable_bucket(bucket: str) -> bool:
    """True for the two buckets that carry a scoring rubric."""
    return bucket in ("trending", "ranging")


def compute_regime_with_hysteresis(raw_regime: str, prior_state: dict, runs: int = 2,
                                   bar_id=None):
    """
    Pure hysteresis over the RUBRIC bucket (trending vs ranging). A rubric flip is
    the most expensive kind of regime noise (it changes trade-direction logic), so
    require `runs` consecutive raw classifications in the new bucket before the
    effective regime switches buckets. Direction changes WITHIN the same bucket
    (e.g. Trending Bullish -> Trending Bearish) apply immediately. IO-free so it
    unit-tests without disk; the caller persists `new_state` keyed by symbol.

    B16.0: hysteresis must advance on new MARKET information (a new closed 15m bar),
    not per invocation. `bar_id` is the last-closed-15m-bar open ms; re-processing the
    same bar (service restart, --once smoke run, a CLI run alongside the monitor) is
    idempotent — it returns the prior state without advancing `candidate_runs`, so two
    calls inside one bar can no longer flip the rubric with no new data.

    prior_state: {"effective","candidate","candidate_runs","last_bar_id"} (or {} first).
    Returns (effective_regime, new_state).
    """
    prior = prior_state or {}
    effective = prior.get("effective")

    if bar_id is not None and effective and prior.get("last_bar_id") == bar_id:
        # same closed bar already processed -> no new information, replay prior state
        return effective, {"effective": effective, "candidate": prior.get("candidate"),
                           "candidate_runs": int(prior.get("candidate_runs", 0) or 0),
                           "last_bar_id": bar_id}

    if not effective:
        eff, state = raw_regime, {"effective": raw_regime, "candidate": None, "candidate_runs": 0}
    elif regime_bucket(raw_regime) == regime_bucket(effective):
        # same rubric -> accept immediately (picks up direction changes too)
        eff, state = raw_regime, {"effective": raw_regime, "candidate": None, "candidate_runs": 0}
    elif not is_tradable_bucket(regime_bucket(raw_regime)):
        # I3.0: hysteresis exists to stop the engine flip-flopping INTO a rubric on
        # noise. Standing DOWN into transition/untradeable is never delayed — a
        # confirmed change of character should not be traded for two more bars.
        eff, state = raw_regime, {"effective": raw_regime, "candidate": None, "candidate_runs": 0}
    else:
        # candidate is in the opposite rubric bucket
        prev_cand = prior.get("candidate")
        cand_runs = int(prior.get("candidate_runs", 0) or 0)
        if prev_cand is not None and regime_bucket(prev_cand) == regime_bucket(raw_regime):
            cand_runs += 1
        else:
            cand_runs = 1
        if cand_runs >= runs:
            eff, state = raw_regime, {"effective": raw_regime, "candidate": None, "candidate_runs": 0}
        else:
            eff, state = effective, {"effective": effective, "candidate": raw_regime,
                                     "candidate_runs": cand_runs}

    state["last_bar_id"] = bar_id
    return eff, state


# ---------------- session-aware volume + ICT kill zones ---------------- #

def kill_zone(hour_utc: int) -> str:
    """ICT kill-zone tag for a UTC hour (fixed windows, config.KILL_ZONES)."""
    for start, end, name in KILL_ZONES:
        if start <= hour_utc < end:
            return name
    return "OFF"


def session_volume_context(df_15m: pd.DataFrame) -> dict:
    """
    Session-aware relative volume: compare the last closed 15m bar's volume to the
    mean volume of bars sharing its UTC hour-of-day over the available history,
    instead of the trailing-30-bar mean. This removes the time-of-day bias that
    made the flat-lookback gate flag normal Asia-session bars as LOW_VOL. Also
    tags the current ICT kill zone. Falls back gracefully on thin data.
    """
    out = {"session_relative_volume": None, "kill_zone": "OFF", "hour_utc": None, "slot_samples": 0}
    if df_15m is None or df_15m.empty or "volume" not in df_15m.columns:
        return out
    ts = pd.to_datetime(df_15m["timestamp"])
    hours = ts.dt.hour.to_numpy()
    vols = df_15m["volume"].to_numpy(dtype=float)
    last_hour = int(hours[-1])
    last_vol = float(vols[-1])
    same = vols[:-1][hours[:-1] == last_hour] if len(vols) > 1 else vols[:0]
    base = float(same.mean()) if same.size else float(vols.mean() if vols.size else 1.0)
    srel = round(last_vol / max(base, 1e-9), 2) if base > 0 else None
    out.update({
        "session_relative_volume": srel,
        "kill_zone": kill_zone(last_hour),
        "hour_utc": last_hour,
        "slot_samples": int(same.size),
    })
    return out
