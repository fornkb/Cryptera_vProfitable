"""
Cryptera v3.2 - Rule-based SMC engine + Gemini AI structured JSON narration.
"""

import pandas as pd
import asyncio
import copy
import requests
import json
import logging
import os
import threading
import time
import numpy as np
from collections import OrderedDict
from datetime import datetime, timezone
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

from indicators import (calculate_indicators, detect_regime, detect_volatility_regime,
                        compute_regime_with_hysteresis, session_volume_context)
from strategies import evaluate_strategies
from smc import build_smc_context
from price_action import build_pa_context, calculate_previous_day, get_value_area
from config import (SCHEMA_VERSION, DATA_DIR, SNAPSHOTS_DIR, OI_HISTORY_PATH,
                    EVENTS_PATH, REGIME_STATE_PATH, REGIME_HYSTERESIS_RUNS,
                    SNAPSHOT_RETENTION, HORIZON_HOURS, MARKET, TIMEFRAMES,
                    REQUIRED_TIMEFRAMES, LIMITS, WINDOW_SIZE, TF_MS, CHART_BARS,
                    OHLCV_CACHE_MAX, CACHE_TTL_SECONDS, EVENT_GUARD_WINDOW_HOURS,
                    EVENT_CALENDAR_STALE_DAYS,
                    GEMINI_MODEL_NAME, GEMINI_THINKING_LEVEL,
                    GEMINI_KEY_COOLDOWN_SECONDS, GEMINI_TRANSIENT_RETRIES,
                    GEMINI_TRANSIENT_BACKOFF_SECONDS)

logger = logging.getLogger("cryptera")


# ---------------- utilities ---------------- #

def to_native(obj):
    """Recursively convert numpy / pandas types to native Python."""
    if isinstance(obj, (np.bool_, np.integer, np.floating)):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_native(v) for v in obj]
    return obj


def _atr_last(df) -> float:
    """Last closed-bar absolute ATR, or None."""
    if df is None or df.empty or "ATR" not in df.columns:
        return None
    v = df["ATR"].iloc[-1]
    return round(float(v), 6) if pd.notna(v) else None


def _price_decimals(price: float) -> int:
    """
    Rounding precision scaled to price magnitude (BUG-14) — a flat round(x, 2)
    collapses a sub-cent asset's close_series_last_5 to [0.0, ...], destroying
    the signal for custom low-priced pairs.
    """
    if price is None or price <= 0:
        return 4
    if price >= 100:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 6
    if price >= 0.0001:
        return 8
    return 10


def get_window_dict(df: pd.DataFrame, size: int) -> dict:
    """Compressed per-TF statistical summary fed to the LLM."""
    recent = df.tail(size)
    closes = recent["close"].tolist()
    start_p = closes[0] if closes else 1.0
    end_p = closes[-1] if closes else 1.0
    decimals = _price_decimals(end_p)
    price_change_pct = round(((end_p - start_p) / start_p) * 100, 2) if start_p else 0.0

    def _safe(col, default=0.0):
        if col in recent.columns and not pd.isna(recent[col].iloc[-1]):
            return round(float(recent[col].iloc[-1]), 4)
        return default

    rsi_latest = _safe("RSI", 50.0)
    vol_latest = _safe("volume", 0.0)
    vol_mean = float(recent["volume"].mean()) if "volume" in recent.columns else 1.0
    vol_relative = round(vol_latest / max(vol_mean, 0.1), 2)
    supertrend_latest = int(recent["SUPERTd_10_3.0"].iloc[-1]) if "SUPERTd_10_3.0" in recent.columns and not pd.isna(recent["SUPERTd_10_3.0"].iloc[-1]) else 0
    stochrsi_k = _safe("STOCHRSIk_14_14_3_3", 50.0)
    stochrsi_d = _safe("STOCHRSId_14_14_3_3", 50.0)
    adx_latest = _safe("ADX", 0.0)
    atr_pct_latest = _safe("ATR_Pct", 0.5)

    cvd_change = 0.0
    if "cvd" in recent.columns:
        cvd_vals = recent["cvd"].dropna().tolist()
        if len(cvd_vals) >= 2:
            cvd_change = round(cvd_vals[-1] - cvd_vals[0], 2)
    cvd_real = bool(recent["cvd_real"].iloc[-1]) if "cvd_real" in recent.columns and not recent.empty else False

    recent_5 = df.tail(5)
    close_series = [round(float(x), decimals) for x in recent_5["close"].tolist()]
    rsi_series = [round(float(x), 2) for x in recent_5["RSI"].tolist()] if "RSI" in recent_5.columns else []

    # Single-bar (last CLOSED bar) price change — matches per-bar OI deltas so the
    # OF modifier compares like windows (BUG-10). For 4h this is a true ~4h change.
    bar_change_pct = 0.0
    if len(closes) >= 2 and closes[-2]:
        bar_change_pct = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 3)

    return {
        "current_price": round(end_p, decimals),
        "window_price_change_pct": price_change_pct,
        "bar_price_change_pct": bar_change_pct,
        "rsi": rsi_latest,
        "relative_volume": vol_relative,
        "supertrend_direction": supertrend_latest,
        "stochrsi_k": stochrsi_k,
        "stochrsi_d": stochrsi_d,
        "cvd_window_delta": cvd_change,
        "cvd_is_real": cvd_real,
        "adx": adx_latest,
        "atr_percentile": atr_pct_latest,
        "close_series_last_5": close_series,
        "rsi_series_last_5": rsi_series,
    }


# ---------------- chart series (for the dashboard candlestick chart) ---------------- #

def build_chart_series(data: dict) -> dict:
    """
    Compact OHLC arrays per timeframe for the frontend price chart. Attached to
    the snapshot AFTER the Gemini call so it never bloats the LLM prompt. Times
    are UNIX seconds (UTC) as lightweight-charts expects.
    """
    out = {}
    for tf, df in data.items():
        if df is None or df.empty:
            out[tf] = []
            continue
        sub = df.tail(CHART_BARS.get(tf, 150))
        times = (sub["timestamp"].astype("int64") // 10**9).tolist()
        o = sub["open"].astype(float).round(6).tolist()
        h = sub["high"].astype(float).round(6).tolist()
        l = sub["low"].astype(float).round(6).tolist()
        c = sub["close"].astype(float).round(6).tolist()
        out[tf] = [{"time": int(t), "open": oo, "high": hh, "low": ll, "close": cc}
                   for t, oo, hh, ll, cc in zip(times, o, h, l, c)]
    return out


# ---------------- caching ---------------- #

OHLCV_CACHE = OrderedDict()


def _cache_get(key):
    if key in OHLCV_CACHE:
        OHLCV_CACHE.move_to_end(key)
        return OHLCV_CACHE[key]
    return None


def _cache_put(key, value):
    OHLCV_CACHE[key] = value
    OHLCV_CACHE.move_to_end(key)
    while len(OHLCV_CACHE) > OHLCV_CACHE_MAX:
        OHLCV_CACHE.popitem(last=False)


# ---------------- market data ---------------- #

def fetch_futures_klines(symbol, tf, limit):
    """
    USDT-perp klines from fapi (single market namespace, BUG-01): OHLCV and
    taker-buy base volume come from the SAME futures bars, so real CVD
    (2*taker_buy - volume) is internally consistent — never mixed with spot.
    Returns a DataFrame[timestamp, open, high, low, close, volume,
    taker_buy_base] or None.
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol.replace("/", ""), "interval": tf, "limit": limit},
            timeout=8,
        ).json()
        if not isinstance(r, list) or not r:
            return None
        rows = []
        for k in r:
            # [0]=openTime [1..4]=OHLC [5]=volume [9]=takerBuyBaseVol
            rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                         float(k[4]), float(k[5]), float(k[9])))
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low",
                                         "close", "volume", "taker_buy_base"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        # Demoted to debug: the caller retries up to 3x and logs one warning
        # only if every attempt fails ("warn-once", BUG-23).
        logger.debug(f"Futures klines fetch attempt failed for {tf}: {e}")
        return None


def fetch_live_price(symbol: str):
    """
    Freshest (possibly still-forming) 15m close, independent of the OHLCV
    cache TTL — used to refresh `live_price` on a 15m cache hit (BUG-19),
    which previously fell back to the last CLOSED bar's close (stale by up
    to one bar). Returns None on failure.
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol.replace("/", ""), "interval": "15m", "limit": 1},
            timeout=5,
        ).json()
        if isinstance(r, list) and r:
            return float(r[-1][4])
    except Exception as e:
        logger.warning(f"Live price refresh failed: {e}")
    return None


def _drop_forming_candle(df: pd.DataFrame, tf: str, now_ms: int) -> pd.DataFrame:
    """
    Remove the last row if its bar has not closed yet, so all indicators and
    structure are computed on CLOSED candles only (fixes B3/B4/T1-1).
    """
    if df is None or df.empty:
        return df
    last_open_ms = int(df["timestamp"].iloc[-1].value // 1_000_000)
    if now_ms < last_open_ms + TF_MS[tf]:
        return df.iloc[:-1].reset_index(drop=True)
    return df


async def fetch_ohlcv(symbol):
    """
    Fetch USDT-perp OHLCV + taker flow across timeframes (retry + LRU cache),
    drop the forming candle, and compute indicators on closed bars. Required
    timeframes (4h/1h/15m) raise if the symbol never resolves on the futures
    market; the optional 1D leg degrades gracefully to None instead of
    aborting the whole run (BUG-15) — every 1D consumer downstream already
    tolerates a missing daily leg.
    Returns (data: {tf: df|None}, live_price: float).
    """
    raw_dfs = {}
    now = time.time()
    now_ms = int(now * 1000)
    live_price = None

    for tf in TIMEFRAMES:
        cache_key = (symbol, tf)
        cached = _cache_get(cache_key)
        if cached is not None:
            cached_time, cached_df = cached
            if now - cached_time < CACHE_TTL_SECONDS[tf]:
                raw_dfs[tf] = cached_df.copy()
                if tf == "15m":
                    # Cache hit skips the fresh fetch below, so live_price
                    # would otherwise fall back to the last CLOSED close
                    # after the loop (stale by up to one bar) — refresh it
                    # directly instead (BUG-19).
                    live_price = await asyncio.to_thread(fetch_live_price, symbol)
                continue

        df = None
        for attempt in range(3):
            df = await asyncio.to_thread(fetch_futures_klines, symbol, tf, LIMITS[tf])
            if df is not None:
                break
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        if df is None:
            if tf in REQUIRED_TIMEFRAMES:
                raise RuntimeError(
                    f"No {tf} klines for {symbol} on {MARKET} — "
                    "symbol may not exist as a Binance USDT-perp.")
            logger.warning(f"No {tf} klines for {symbol} — continuing without this timeframe (degraded mode).")
            raw_dfs[tf] = None
            continue

        # capture the freshest (possibly forming) 15m close as the live price
        if tf == "15m":
            live_price = float(df["close"].iloc[-1])
        df = _drop_forming_candle(df, tf, now_ms)
        raw_dfs[tf] = df
        _cache_put(cache_key, (now, df.copy()))

    if live_price is None and raw_dfs.get("15m") is not None and not raw_dfs["15m"].empty:
        live_price = float(raw_dfs["15m"]["close"].iloc[-1])

    data = {}
    for tf, df in raw_dfs.items():
        data[tf] = None if df is None else await asyncio.to_thread(calculate_indicators, df, timeframe=tf)
    return data, live_price


def fetch_orderbook(symbol):
    """L2 order book with depth-binned imbalance (±0.25 / 0.5 / 1 / 2%)."""
    default = {"bid_vol": 0.0, "ask_vol": 0.0, "skew": 0.0, "mid_price": 0.0, "depth_bins": []}
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": symbol.replace("/", ""), "limit": 500},
            timeout=8,
        ).json()
    except Exception as e:
        logger.warning(f"Orderbook fetch failed: {e}")
        return default

    bids = [(float(b[0]), float(b[1])) for b in r.get("bids", [])]
    asks = [(float(a[0]), float(a[1])) for a in r.get("asks", [])]
    if not bids or not asks:
        return default

    mid = (bids[0][0] + asks[0][0]) / 2
    if mid <= 0:
        return default

    bands = [0.0025, 0.005, 0.01, 0.02]
    depth_bins = []
    for b in bands:
        bid_band = sum(qty for price, qty in bids if (mid - price) / mid <= b)
        ask_band = sum(qty for price, qty in asks if (price - mid) / mid <= b)
        total = bid_band + ask_band
        imbalance = (bid_band - ask_band) / total if total > 0 else 0.0
        depth_bins.append({
            "band_pct": round(b * 100, 3),
            "bid": round(bid_band, 4),
            "ask": round(ask_band, 4),
            "imbalance": round(imbalance, 4),
        })

    total_bid = sum(qty for _, qty in bids)
    total_ask = sum(qty for _, qty in asks)
    skew = (total_bid - total_ask) / max(total_bid + total_ask, 1e-9)

    return {
        "bid_vol": round(total_bid, 4),
        "ask_vol": round(total_ask, 4),
        "skew": round(skew, 4),
        "mid_price": round(mid, 6),
        "depth_bins": depth_bins,
    }


def _read_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path, obj):
    """Atomic write (FA-03): tmp file + os.replace so a crash/OOM mid-write can
    never leave truncated JSON that the next load silently treats as empty."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Failed to persist {os.path.basename(path)}: {e}")


def apply_regime_hysteresis(symbol: str, raw_regime: str) -> str:
    """
    Persist per-symbol regime state and gate rubric flips through
    compute_regime_with_hysteresis (BUG-08). Direction changes within a rubric
    bucket apply immediately; trend<->range flips need REGIME_HYSTERESIS_RUNS
    consecutive raw runs. Returns the effective regime to score with.
    """
    state = _read_json_file(REGIME_STATE_PATH, {})
    prior = state.get(symbol) or {}
    effective, new_state = compute_regime_with_hysteresis(raw_regime, prior, runs=REGIME_HYSTERESIS_RUNS)
    new_state["updated"] = int(time.time())
    state[symbol] = new_state
    _write_json_file(REGIME_STATE_PATH, state)
    return effective


def _load_oi_history():
    if not os.path.exists(OI_HISTORY_PATH):
        return {}
    try:
        with open(OI_HISTORY_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_oi_history(hist):
    """Atomic write (FA-03): same tmp+replace pattern as _write_json_file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OI_HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(hist, f)
        os.replace(tmp, OI_HISTORY_PATH)
    except Exception as e:
        logger.warning(f"Failed to persist OI history: {e}")


# FA-03: fetch_open_interest runs via asyncio.to_thread — one real OS thread per
# symbol, concurrently (monitor.cycle gathers the whole watchlist). Without a
# lock, two threads' load -> append -> save critical sections interleave and
# lose each other's appends every cycle. apply_regime_hysteresis does NOT need
# this lock: it's always called directly on the event-loop thread (never
# to_thread'd) and contains no `await`, so asyncio's cooperative scheduling
# already makes its read-modify-write atomic with respect to other coroutines.
_OI_HISTORY_LOCK = threading.Lock()


def fetch_open_interest(symbol):
    """OI now + 1h / 4h deltas + ~20-day percentile (history endpoint with local fallback)."""
    sym_clean = symbol.replace("/", "")
    oi_now = None
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": sym_clean},
            timeout=5,
        ).json()
        oi_now = float(r["openInterest"])  # missing key raises -> stays None
    except Exception as e:
        logger.warning(f"OI fetch failed: {e}")

    # FA-05: on failure return nulls instead of fabricating an OI collapse —
    # a 0.0 oi_now would compute -100% deltas, a rank-of-zero percentile, and
    # permanently poison the local history file with {"oi": 0.0} rows.
    if oi_now is None:
        return {"value": None, "change_1h_pct": None,
                "change_4h_pct": None, "percentile_20d": None}

    oi_change_1h = None
    oi_change_4h = None
    oi_percentile_20d = None

    try:
        # Binance caps this endpoint at limit=500 (BUG-04); 500 x 1h ~ 20.8 days.
        r = requests.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": sym_clean, "period": "1h", "limit": 500},
            timeout=8,
        ).json()
        if isinstance(r, list) and len(r) >= 2:
            vals = []
            for x in r:
                try:
                    vals.append((int(x.get("timestamp", 0)), float(x.get("sumOpenInterest", 0))))
                except Exception:
                    continue
            vals.sort(key=lambda t: t[0])
            series = [v for _, v in vals]
            if len(series) >= 2 and series[-2] > 0:
                oi_change_1h = round((oi_now - series[-2]) / series[-2] * 100, 3)
            if len(series) >= 5 and series[-5] > 0:
                oi_change_4h = round((oi_now - series[-5]) / series[-5] * 100, 3)
            if series:
                below = sum(1 for v in series if v <= oi_now)
                oi_percentile_20d = round(below / len(series) * 100, 1)
    except Exception as e:
        logger.warning(f"OI history fetch failed: {e}")

    # Local fallback / augmentation. FA-03: load -> append -> save is a
    # read-modify-write on a file shared by every symbol; lock the whole
    # critical section so concurrent per-symbol threads can't clobber each
    # other's appends.
    with _OI_HISTORY_LOCK:
        history = _load_oi_history()
        sym_hist = history.get(symbol, [])
        sym_hist.append({"ts": int(time.time()), "oi": oi_now})
        sym_hist = sym_hist[-2000:]
        history[symbol] = sym_hist
        _save_oi_history(history)
    if oi_percentile_20d is None and len(sym_hist) >= 10:
        vals_local = [x["oi"] for x in sym_hist]
        below = sum(1 for v in vals_local if v <= oi_now)
        oi_percentile_20d = round(below / len(vals_local) * 100, 1)

    return {
        "value": oi_now,
        "change_1h_pct": oi_change_1h,
        "change_4h_pct": oi_change_4h,
        "percentile_20d": oi_percentile_20d,
    }


def fetch_funding(symbol):
    """Current funding + 24-period trajectory + percentile-within-window."""
    default = {"current": 0.0, "avg_24h": 0.0, "trend": "unknown",
               "sign_changes": 0, "percentile_window": None, "window_periods": 0}
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            # Binance caps this endpoint at limit=1000 (BUG-22): 24 samples (~8
            # days) made the percentile-within-window noisy for a ±3/-4 modifier;
            # widen to the max for a stable percentile. avg_24h still only uses
            # the last 3 samples (~24h at the standard 8h funding interval).
            params={"symbol": symbol.replace("/", ""), "limit": 1000},
            timeout=5,
        ).json()
    except Exception as e:
        logger.warning(f"Funding fetch failed: {e}")
        return default

    if not isinstance(r, list) or not r:
        return default

    try:
        rates = [float(x["fundingRate"]) for x in r]
    except Exception:
        return default

    current = rates[-1]
    avg_24h = sum(rates[-3:]) / max(len(rates[-3:]), 1)
    sign_changes = sum(1 for i in range(1, len(rates)) if (rates[i] > 0) != (rates[i - 1] > 0))
    delta = current - avg_24h
    if abs(delta) < 1e-6:
        trend = "flat"
    elif delta > 0:
        trend = "rising"
    else:
        trend = "falling"
    below = sum(1 for v in rates if v <= current)
    percentile = round(below / len(rates) * 100, 1)

    return {
        "current": round(current, 6),
        "avg_24h": round(avg_24h, 6),
        "trend": trend,
        "sign_changes": int(sign_changes),
        "percentile_window": percentile,
        "window_periods": len(rates),
    }


def fetch_sentiment():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=5).json()
        return int(r["data"][0]["value"])
    except Exception:
        logger.warning("Fear & Greed API failed. Defaulting to neutral 50.")
        return 50


def fetch_btc_dominance_proxy(symbol):
    """BTCDOM perp 4H/24H change as a dominance proxy. Skipped when symbol is BTC."""
    if "BTC" in symbol.upper().split("/")[0]:
        return None
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": "BTCDOMUSDT", "interval": "4h", "limit": 10},
            timeout=8,
        ).json()
        if not isinstance(r, list) or len(r) < 2:
            return None
        # FA-08: closed-candle discipline — drop the still-forming 4H bar so
        # change_4h/24h are full-bar deltas, not "0-to-4h so far".
        now_ms = int(time.time() * 1000)
        if int(r[-1][0]) + TF_MS["4h"] > now_ms:
            r = r[:-1]
        if len(r) < 2:
            return None
        closes = [float(x[4]) for x in r]
        change_4h = (closes[-1] - closes[-2]) / closes[-2] * 100
        ref_idx = -7 if len(closes) >= 7 else 0  # 6 closed 4H intervals = 24h
        change_24h = (closes[-1] - closes[ref_idx]) / closes[ref_idx] * 100
        return {
            "btcdom_close": round(closes[-1], 4),
            "change_4h_pct": round(change_4h, 3),
            "change_24h_pct": round(change_24h, 3),
        }
    except Exception as e:
        logger.warning(f"BTC.D proxy fetch failed: {e}")
        return None


def fetch_event_window(now_ts: float = None, window_hours: int = EVENT_GUARD_WINDOW_HOURS) -> dict:
    """Active high-impact macro events near current time (data/events.json)."""
    if now_ts is None:
        now_ts = time.time()
    if not os.path.exists(EVENTS_PATH):
        return {"active": False, "events": [], "window_hours": window_hours}
    try:
        with open(EVENTS_PATH) as f:
            events = json.load(f)
    except Exception:
        return {"active": False, "events": [], "window_hours": window_hours}

    active = []
    window_s = window_hours * 3600
    for e in events if isinstance(events, list) else []:
        try:
            # Naive (offset-less) datetimes are ASSUMED UTC (BUG-13) — parsing
            # them as machine-local time shifted the ±window_hours guard by the
            # host's UTC offset. data/events.json is operator-maintained in UTC.
            dt = datetime.fromisoformat(e["datetime"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ev_ts = dt.timestamp()
        except Exception:
            continue
        if abs(ev_ts - now_ts) <= window_s:
            active.append({
                "name": e.get("name", "unknown"),
                "impact": e.get("impact", "medium"),
                "datetime": e["datetime"],
                "minutes_until": round((ev_ts - now_ts) / 60, 1),
            })
    return {"active": len(active) > 0, "events": active, "window_hours": window_hours}


def _calendar_health(now_ts: float = None, horizon_days: int = EVENT_CALENDAR_STALE_DAYS) -> str:
    """FA-11: `fetch_event_window` only reports whether an event is active RIGHT
    NOW -- against the placeholder events.json (two `EXAMPLE` entries dated
    2026-12-31) that reads as a healthy, empty calendar every cycle until New
    Year's Eve, so the guard is silently blind. Flag EMPTY/PLACEHOLDER/STALE
    explicitly for data_health_report instead of trusting an unpopulated file."""
    if now_ts is None:
        now_ts = time.time()
    if not os.path.exists(EVENTS_PATH):
        return "EMPTY -- data/events.json not found"
    try:
        with open(EVENTS_PATH) as f:
            events = json.load(f)
    except Exception:
        return "EMPTY -- data/events.json unreadable"
    if not isinstance(events, list) or not events:
        return "EMPTY -- data/events.json has no entries"
    if any("EXAMPLE" in (e.get("name") or "") for e in events):
        return "PLACEHOLDER -- data/events.json still has EXAMPLE entries"

    horizon_s = horizon_days * 86400
    for e in events:
        try:
            dt_ = datetime.fromisoformat(e["datetime"])
            if dt_.tzinfo is None:
                dt_ = dt_.replace(tzinfo=timezone.utc)
            ev_ts = dt_.timestamp()
        except Exception:
            continue
        if 0 <= (ev_ts - now_ts) <= horizon_s:
            return "OK"
    return f"STALE -- no event within {horizon_days}d"


# ---------------- Gemini ---------------- #

GEMINI_SYSTEM_INSTRUCTION = """
ICT/SMC institutional trade engine. Return a JSON object matching the enforced schema. Signals are computed on CLOSED candles; `live_price` is the current price for distance/trigger context only.

Rules
1. Use only PRICES present in the snapshot. Never invent levels. Distances/R:R you may compute yourself: every `engine_trade` candidate already carries `dist_from_price_atr` (distance from live price in 15m-ATR units) — prefer it; for other snapshot levels compute the distance when you need it. `atr_context` gives atr_15m/1h/4h and the horizon_hours: reject any target more than a few x atr_1h away as unreachable in-window. `live_vs_close_pct` is how far the forming bar has drifted from the last closed 15m close — if it exceeds ~1 ATR, phrase entry triggers relative to live_price.
2. `strategies.confluence_score` is authoritative (already includes the order-flow modifier). Echo it into `header.score`; only set `header.score_override` on a specific factual error. `header.action` is decided from the FINAL score in `header.score` (the overridden value when you set `score_override`, else `confluence_score`) — apply the thresholds to THAT number.
3. TRADE PLAN = SELECT THE BEST REAL CANDIDATE, NEVER INVENT. This is the highest-stakes decision in the output — the score only says *whether*; the candidate you pick decides the actual outcome. Choose deliberately.
   `strategies.engine_trade` gives a deterministic primary (entry/stop_loss/take_profit/rr) AND a candidate menu in `engine_trade.candidates`. Each candidate carries quality metadata — USE IT to rank, do not just take the nearest:
     - `candidates.entries[]` {price, source, tf, hvn, zone, confluence, confluence_count} — choose ONE as `trade_decision.entry`
     - `candidates.stops[]`   {price, source, tf} — choose ONE as `trade_decision.stop_loss`
     - `candidates.targets[]` {price, source, rr, tf, confluence, confluence_count} — choose targets; `rr` is from the primary entry/SL
   RANKING RULES (apply in this order):
     a. Higher `confluence_count` wins — a level where multiple sources stack (e.g. VAH+BSL+POC) is far stronger than an isolated one.
     b. `hvn: true` order blocks > plain OBs; higher `tf` (4h > 1d > 1h > 15m) levels are more significant — 4h is the anchor of this intraday system; daily/weekly levels (`tf: "1d"`) rank just below it.
     c. ENTRIES: prefer `zone` matching the trade — `discount` for longs, `premium` for shorts.
     d. TARGETS: a target sitting just BEFORE a higher-TF level/resistance is safer than one at/through it.
     e. `rr` is the TIE-BREAKER between otherwise-equal candidates, not the primary filter.
   Every price you output (entry / stop_loss / take_profit / tp1 / tp2 / move_to_breakeven_at) MUST be a value from these candidate lists or the engine primary. Never output a price not in the snapshot.
   Default to the engine primary UNLESS a candidate is clearly higher-quality by the rules above. Set `trade_management.selected_from_engine` = true if you echo the primary, false otherwise, and in `selection_rationale` COMPARE your pick against the primary and the next-best candidate ("chose X (confluence 3, HVN) over primary Y because …").
   `engine_trade.rr` / `rr_passed` is the HONEST structural R:R — the engine never inflates TP to hit 2R. Never move a TP farther out to manufacture R:R: report the honest (possibly sub-2R) number and weigh it in `confidence_pct` instead. Candidate `rr` values are relative to the ENGINE PRIMARY entry/SL — if you select a different entry or stop, recompute `rr.*`, `tp1_rr`/`tp2_rr` and `sl_width_pct` from YOUR chosen levels. If `engine_trade.valid` is false there is no sound structural trade → HOLD.
   TRADE MANAGEMENT: `tp1` = nearest HIGH-CONFLUENCE target (the first strong stacked level), `tp2` = a farther runner (>= 2R if available); set `tp1_rr`/`tp2_rr` from the candidate's `rr`; `move_to_breakeven_at` = tp1 or a candidate level between entry and tp1; `scale_out` describes the split (e.g. "50% at TP1, move SL to BE, 50% runner to TP2").

Regime-conditional scoring — read `strategies.score_mode` (context for narrative/reasoning;
header.score_breakdown is filled by the engine, not by you):
 score_mode == "trend" (trending regime): components C1-C8 measure trend continuation.
   C1 trend(25) ADX-tiered on 4H/1H agreement · C2 ob_prox + C5 fvg_magnet + C6 ote share ONE
   LOCATION composite capped at 25 total (a single good location can no longer bank 40)
   C3 sweep(15) HTF sweep+reclaim (9 if 15m-only) · C4 momentum(15) 1H RSI/MACD ±slope
   C7 cvd(15) real taker-flow CVD (3-TF aligned = 15, 2-TF = 10) · C8 stoch(5)
 score_mode == "mean_revert" (ranging regime): FADE the value-area edge, do not chase trend.
   M1 edge_distance(25) proximity to VAH/VAL · M2 edge_sweep(15) sweep of the edge
   M3 cvd_absorption(15) · M4 stoch_extreme(15) · M5 rejection candle(10) · M6 range_intact(10)
   Direction = SELL near VAH, BUY near VAL. `setup_direction` carries this.

Order flow: `strategies.order_flow_modifier` (already in the score) reflects L2 depth imbalance,
funding crowding + trajectory, OI build, F&G extremes, and BTC-dominance (alt macro headwind) —
cite `order_flow_notes` in the sentiment reasoning. `funding.current` is a decimal rate per 8h
(0.0001 = 0.01%), not a percentage — do not misread it. `price_action.avwap` holds anchored-VWAP
dynamic S/R from the last swing high/low; cite it as a level when relevant.

HTF draws: `smc_context.1d` gives daily structure/BOS and daily liquidity pools; `price_action`
carries `pwh`/`pwl` (previous-week high/low), `weekly_open`, and `untested_pocs_1d`. These are the
higher-timeframe targets the geometry menu draws toward — the strongest runners sit at daily pools.
Sessions: `volume_gate.kill_zone` (LONDON / NEWYORK = high-liquidity ICT windows, ASIA / OFF = thin);
`session_rel_vol_15m` is the same-time-of-day relative volume the gate is based on.

Thresholds
 score >= 60 → ACTIVE_TRADE   45-59 → CONDITIONAL_ENTRY   < 45 → HOLD

Gates
 volume_gate.HARD_GATE        → force HOLD regardless of score
 volume_gate.LOW_VOL_WARNING  → position_size = "50% low-vol"; require +1 confluence
 event_guard.active == true   → cap action at CONDITIONAL_ENTRY; reduce confidence by 15
 cvd_absorption_warning       → reduce confidence by 10; cite explicitly
 setup_direction == "neutral" → HOLD (no clean setup)

Action-specific filling
 HOLD               → trade_decision entry/stop_loss/take_profit/rr.* and all trade_management price fields = null; selected_from_engine=true, scale_out="n/a". Forward scenario filled.
 CONDITIONAL_ENTRY  → select levels from engine_trade.candidates; entry_trigger states the exact required condition; fill trade_management.
 ACTIVE_TRADE       → select levels from engine_trade.candidates (never invent a TP); fill trade_management with tp1/tp2 + BE + scale_out.

Field definitions
 header.bias : the engine's directional read — `setup_direction` uppercased when not neutral, else `trend_bias`; NEUTRAL only when both are neutral.
 primary.probability : when `strategies.empirical_win_rate` is non-null, anchor to it; otherwise your calibrated estimate from score + confluence quality (0-100).
 confidence_pct : conviction in the overall read. Start near primary.probability, then apply the gate reductions above (event −15, cvd_absorption −10) and reduce for sub-2R geometry or counter-structure trades.
 position_size : "100%" default · "50% low-vol" on LOW_VOL_WARNING · "25% cascade" when two or more risk reducers stack (e.g. low-vol + event guard, or sub-2R + counter-structure) · "reduced - conflict" when order flow contradicts the setup direction. On HOLD emit "100%" (unused).
 counter_structure : true when the trade direction opposes the 4H structure.

Field formats
 mtf_context : ONLY two fields — `cross_tf_momentum` (one-line 1D/4H/1H/15M momentum read) and `htf_draw` = the dominant daily-level target and side (e.g. "PWH 152.4 — draw above"). The per-timeframe table is filled by the engine after your call.
 forward_scenario.key_levels.<tf> : ["<price> — <label>", ...] sourced from snapshot.
 narrative.summary : ≤ 2 sentences.
""".strip()


# GEMINI_MODEL_NAME / GEMINI_THINKING_LEVEL live in config.py (single source of
# truth for the model name + thinking-level, imported at module top).

_INT = {"type": "integer"}
_NUM = {"type": "number"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_NUM_NULL = {"type": "number", "nullable": True}
_STR_ARR = {"type": "array", "items": _STR}


GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "header": {
            "type": "object",
            "properties": {
                "pair": _STR,
                "price": _NUM,
                "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
                "regime": _STR,
                "score": _INT,
                "action": {"type": "string", "enum": ["ACTIVE_TRADE", "CONDITIONAL_ENTRY", "HOLD"]},
                "volume_gate": {"type": "string", "enum": ["HARD_GATE", "LOW_VOL_WARNING", "CLEAR"]},
                # score_breakdown is NOT LLM output -- strategies.confluence_breakdown is
                # spliced into header.score_breakdown after the call (_splice_score_breakdown)
                # since it's already in the input snapshot verbatim with no override
                # mechanism; asking the model to retype it wasted output tokens and was a
                # pure transcription-error surface.
                "score_override": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "from": _INT,
                        "to": _INT,
                        "reason": _STR,
                    },
                },
            },
            "required": ["pair", "price", "bias", "regime", "score", "action",
                         "volume_gate"],
        },
        # The per-timeframe table (structure/bos/cvd/pd_zone/OB/FVG/indicators) is
        # engine data spliced in after the call (_splice_mtf_context) — asking the
        # model to retype it wasted output tokens and was a transcription-error
        # surface, exactly like score_breakdown. Only the two judgment fields
        # remain LLM output.
        "mtf_context": {
            "type": "object",
            "properties": {
                "cross_tf_momentum": _STR,
                "htf_draw": _STR,
            },
            "required": ["cross_tf_momentum", "htf_draw"],
        },
        "narrative": {
            "type": "object",
            "properties": {
                "summary": _STR,
                "primary_draw": _STR,
            },
            "required": ["summary", "primary_draw"],
        },
        "trade_decision": {
            "type": "object",
            "properties": {
                "primary": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                        "probability": _INT,
                    },
                    "required": ["direction", "probability"],
                },
                "alternative": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                        "probability": _INT,
                        "trigger": _STR,
                    },
                    "required": ["direction", "probability", "trigger"],
                },
                "entry": _NUM_NULL,
                "entry_source": _STR,
                "stop_loss": _NUM_NULL,
                "stop_loss_source": _STR,
                "take_profit": _NUM_NULL,
                "take_profit_source": _STR,
                "rr": {
                    "type": "object",
                    "properties": {
                        "tp_distance": _NUM_NULL,
                        "sl_distance": _NUM_NULL,
                        "ratio": _NUM_NULL,
                        "passed": _BOOL,
                    },
                    "required": ["tp_distance", "sl_distance", "ratio", "passed"],
                },
                "sl_width_pct": _NUM_NULL,
                "confidence_pct": _INT,
                "position_size": {
                    "type": "string",
                    "enum": ["100%", "50% low-vol", "25% cascade", "reduced - conflict"],
                },
                "entry_trigger": _STR,
                "invalidation": _STR,
                "counter_structure": _BOOL,
                "trade_management": {
                    "type": "object",
                    "properties": {
                        "selected_from_engine": _BOOL,
                        "selection_rationale": _STR,
                        "tp1": _NUM_NULL,
                        "tp1_source": _STR,
                        "tp1_rr": _NUM_NULL,
                        "tp2": _NUM_NULL,
                        "tp2_source": _STR,
                        "tp2_rr": _NUM_NULL,
                        "move_to_breakeven_at": _NUM_NULL,
                        "scale_out": _STR,
                    },
                    "required": ["selected_from_engine", "selection_rationale",
                                 "tp1", "tp1_source", "tp1_rr", "tp2", "tp2_source", "tp2_rr",
                                 "move_to_breakeven_at", "scale_out"],
                },
                "reasoning": {
                    "type": "object",
                    "properties": {
                        "structure": _STR,
                        "liquidity": _STR,
                        "momentum": _STR,
                        "sentiment": _STR,
                    },
                    "required": ["structure", "liquidity", "momentum", "sentiment"],
                },
            },
            "required": ["primary", "alternative", "entry", "entry_source",
                         "stop_loss", "stop_loss_source", "take_profit", "take_profit_source",
                         "rr", "sl_width_pct", "confidence_pct", "position_size",
                         "entry_trigger", "invalidation", "counter_structure",
                         "trade_management", "reasoning"],
        },
        "forward_scenario": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["LONG", "SHORT", "NEUTRAL"]},
                "key_levels": {
                    "type": "object",
                    "properties": {"h4": _STR_ARR, "h1": _STR_ARR, "m15": _STR_ARR},
                    "required": ["h4", "h1", "m15"],
                },
                "trigger": _STR,
                "entry": _NUM_NULL,
                "stop_loss": _NUM_NULL,
                "take_profit": _NUM_NULL,
                "rr": _NUM_NULL,
                "volume_condition": _STR,
                "supporting_confluence": _STR,
            },
            "required": ["direction", "key_levels", "trigger", "entry", "stop_loss",
                         "take_profit", "rr", "volume_condition", "supporting_confluence"],
        },
    },
    "required": ["header", "mtf_context", "narrative", "trade_decision", "forward_scenario"],
}


# Module-level lazy client pool. Round-robins across up to 3 API keys
# (GEMINI_API_KEY / GEMINI_API_KEY_2 / GEMINI_API_KEY_3 in .env). A key that
# raises on a call is benched for GEMINI_KEY_COOLDOWN_SECONDS so subsequent
# rotations skip it while it's likely still rate-limited; `google-genai`
# clients are reusable across calls, so one is built per key on first use and
# cached.
_GEMINI_KEYS: "list[str] | None" = None
_GEMINI_CLIENTS: "dict[str, genai.Client]" = {}
_GEMINI_COOLDOWN_UNTIL: "dict[str, float]" = {}
_GEMINI_RR_INDEX = 0


def _load_gemini_keys() -> "list[str]":
    global _GEMINI_KEYS
    if _GEMINI_KEYS is not None:
        return _GEMINI_KEYS
    load_dotenv()
    _GEMINI_KEYS = [v for v in (
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
        os.getenv("GEMINI_API_KEY_3"),
    ) if v]
    return _GEMINI_KEYS


def _gemini_client_for_key(api_key: str) -> "genai.Client":
    client = _GEMINI_CLIENTS.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _GEMINI_CLIENTS[api_key] = client
    return client


def _iter_gemini_keys():
    """
    Yield (index, api_key) for every configured key, starting at the current
    round-robin position and wrapping once. Keys still on cooldown from a
    recent failure are tried last rather than skipped outright, so a run
    still proceeds (degraded) if every key is currently benched.
    """
    keys = _load_gemini_keys()
    n = len(keys)
    if n == 0:
        return
    now = time.monotonic()
    order = [(_GEMINI_RR_INDEX + i) % n for i in range(n)]
    ready = [i for i in order if _GEMINI_COOLDOWN_UNTIL.get(keys[i], 0.0) <= now]
    benched = [i for i in order if i not in ready]
    for i in ready + benched:
        yield i, keys[i]


def _mark_gemini_key_failed(index: int, api_key: str):
    _GEMINI_COOLDOWN_UNTIL[api_key] = time.monotonic() + GEMINI_KEY_COOLDOWN_SECONDS


def _advance_gemini_rotation(last_used_index: int):
    """Point the next top-level call at the following key (true round robin)."""
    global _GEMINI_RR_INDEX
    n = len(_load_gemini_keys())
    if n:
        _GEMINI_RR_INDEX = (last_used_index + 1) % n


def _build_generate_config(include_schema: bool = True) -> "genai_types.GenerateContentConfig":
    """
    GenerateContentConfig for Gemini 3.5 Flash:
      * `system_instruction` lives inside the config in the new SDK.
      * `response_mime_type` + `response_schema` enforce JSON shape.
      * `thinking_config.thinking_level` replaces the deprecated numeric budget.
      * Sampling knobs (temperature/top_p/top_k/candidate_count) are
        deliberately omitted — 3.x is tuned for its own defaults and the docs
        warn against forced sampling for rule-following tasks.
    """
    kwargs = {
        "system_instruction": GEMINI_SYSTEM_INSTRUCTION,
        "response_mime_type": "application/json",
        "thinking_config": genai_types.ThinkingConfig(thinking_level=GEMINI_THINKING_LEVEL),
    }
    if include_schema:
        kwargs["response_schema"] = GEMINI_RESPONSE_SCHEMA
    return genai_types.GenerateContentConfig(**kwargs)


def _extract_response_dict(response) -> dict | None:
    """
    Prefer `response.parsed` when schema enforcement produced a dict /
    pydantic model; otherwise fall back to JSON-parsing `response.text`,
    with defensive code-fence stripping for the rare case a model still
    wraps JSON in markdown.
    """
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, dict):
            return parsed
        if hasattr(parsed, "model_dump"):
            return parsed.model_dump()
        try:
            return dict(parsed)
        except Exception:
            pass

    raw = getattr(response, "text", None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].lstrip()
        try:
            return json.loads(cleaned)
        except Exception:
            return {"error": "JSON parse failed", "raw_text": raw}


def _ts_to_bars_ago(ts_str, tf: str, now_utc: datetime):
    """Closed-bar age of a naive-UTC bar timestamp in whole `tf` bars (None on parse failure)."""
    try:
        dt = datetime.fromisoformat(str(ts_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((now_utc - dt).total_seconds() * 1000 // TF_MS[tf]))
    except Exception:
        return None


def _round_floats(node, ndigits: int = 6):
    """Recursively round floats — strips 1e-14 repr noise (e.g. 78.03540000000001)."""
    if isinstance(node, float):
        return round(node, ndigits)
    if isinstance(node, dict):
        return {k: _round_floats(v, ndigits) for k, v in node.items()}
    if isinstance(node, list):
        return [_round_floats(v, ndigits) for v in node]
    return node


def _build_llm_view(snapshot: dict) -> dict:
    """
    Token-lean copy of the snapshot for the LLM prompt (audit §4). The full
    snapshot is still persisted to disk for the dashboard; this trims only what
    the model is sent:
      * drop the duplicate top-level `engine_trade` (kept under `strategies`),
      * prune per-TF order blocks to the 2 most recent per side,
      * drop the raw FVG lists (the actionable `nearest_*` gaps are retained),
      * drop developing-swing arrays (early-warning channel, not rubric input),
      * drop `supertrend.level` (dead field; the pill only needs direction).
      * collapse `bos`/`choch` to `null` when nothing was detected (the common
        case) instead of an always-5-key all-null object,
      * drop `windowed_indicators.<tf>.current_price` (exact duplicate of
        `smc_context.<tf>.current_price`).
      * drop post-call / replay-only fields entirely (FA-01): `chart_series`,
        `analysis`, `outcome`, and `strategies.confluence_breakdown_raw`.
      * drop exact duplicates: `previous_day` (= price_action pdh/pdl/pdc),
        `volatility_regime.<tf>.atr_percentile` (= windowed_indicators'),
        the 15m session fields already carried by `strategies.volume_gate`,
        and `raw_regime` when hysteresis isn't diverging it from `market_regime`,
      * drop `atr_context.note` (constant sentence; the system instruction
        already states the rule),
      * OB/FVG timestamps -> `bars_ago`, untested-POC session_end_ts ->
        `age_hours` (ints beat ISO strings for both tokens and model date math),
      * round float repr noise (78.03540000000001 -> 78.0354).
    """
    view = copy.deepcopy(snapshot)
    now_utc = datetime.now(timezone.utc)
    view.pop("engine_trade", None)
    # FA-01 (defense in depth): fields attached around/after the LLM call must
    # never reach the prompt — `chart_series` (72% of the snapshot) is attached
    # by monitor.cycle before its alert path narrates the same dict, and
    # tools/ab_thinking replays SAVED snapshots that carry `analysis` (the
    # previous answer) and `outcome` (the realized result — answer leakage).
    view.pop("chart_series", None)
    view.pop("analysis", None)
    view.pop("outcome", None)
    if isinstance(view.get("strategies"), dict):
        view["strategies"].pop("confluence_breakdown_raw", None)  # calibration-only (FA-02)
    view.pop("previous_day", None)
    if view.get("raw_regime") == view.get("market_regime"):
        view.pop("raw_regime", None)
    if isinstance(view.get("atr_context"), dict):
        view["atr_context"].pop("note", None)
    for vr in (view.get("volatility_regime") or {}).values():
        if isinstance(vr, dict):
            vr.pop("atr_percentile", None)
    smc = view.get("smc_context") or {}
    windowed = view.get("windowed_indicators") or {}
    for tf, ctx in smc.items():
        if not isinstance(ctx, dict):
            continue
        obs = ctx.get("order_blocks")
        if isinstance(obs, dict):
            for side in ("bullish", "bearish"):
                if isinstance(obs.get(side), list):
                    obs[side] = obs[side][-2:]
                    for ob in obs[side]:
                        if isinstance(ob, dict) and "timestamp" in ob:
                            ob["bars_ago"] = _ts_to_bars_ago(ob.pop("timestamp"), tf, now_utc)
        fvg = ctx.get("fvg")
        if isinstance(fvg, dict):
            fvg.pop("bullish_fvgs", None)
            fvg.pop("bearish_fvgs", None)
            for key in ("nearest_bullish_fvg", "nearest_bearish_fvg"):
                gap = fvg.get(key)
                if isinstance(gap, dict) and "timestamp" in gap:
                    gap["bars_ago"] = _ts_to_bars_ago(gap.pop("timestamp"), tf, now_utc)
        ctx.pop("developing_swings", None)
        if (ctx.get("bos") or {}).get("level") is None:
            ctx["bos"] = None
        if not (ctx.get("choch") or {}).get("detected"):
            ctx["choch"] = None
        wtf = windowed.get(tf)
        if isinstance(wtf, dict):
            wtf.pop("current_price", None)
    w15 = windowed.get("15m")
    if isinstance(w15, dict):
        for key in ("session_relative_volume", "kill_zone", "hour_utc", "slot_samples"):
            w15.pop(key, None)  # already on strategies.volume_gate (or sample-count meta)
    pa = view.get("price_action") or {}
    for key in ("untested_pocs_4h", "untested_pocs_1d"):
        for poc in pa.get(key) or []:
            if isinstance(poc, dict) and "session_end_ts" in poc:
                bars = _ts_to_bars_ago(poc.pop("session_end_ts"),
                                       "4h" if key.endswith("4h") else "1d", now_utc)
                poc["age_hours"] = bars * (4 if key.endswith("4h") else 24) if bars is not None else None
    st = view.get("supertrend")
    if isinstance(st, dict):
        st.pop("level", None)
    return _round_floats(view)


def _schema_skeleton(node: dict):
    """
    Compact shape skeleton derived from GEMINI_RESPONSE_SCHEMA, appended to the
    prompt on the plain-json fallback tier: without `response_schema` the model
    otherwise has no view of the required output fields (the system instruction
    describes rules, not the full field list) and would return an
    unparseable-downstream shape.
    """
    t = node.get("type")
    if t == "object":
        return {k: _schema_skeleton(v) for k, v in (node.get("properties") or {}).items()}
    if t == "array":
        return [_schema_skeleton(node.get("items") or {})]
    if "enum" in node:
        return "|".join(node["enum"])
    base = {"integer": "<int>", "number": "<number>", "string": "<string>",
            "boolean": "<bool>"}.get(t, "<value>")
    return f"{base}|null" if node.get("nullable") else base


_TRANSIENT_MARKERS = ("503", "unavailable", "overloaded", "high demand")


def _call_gemini_with_key(client: "genai.Client", prompt: str) -> "tuple[dict | None, str | None, bool]":
    """
    Try schema then plain-json config tiers on a single client. Returns
    (result, error, is_key_error): `is_key_error` is True only when the SDK
    call itself raised (rate limit, auth, network, ...) -- the caller should
    bench that key and fail over. A parsed-but-empty response is not a key
    problem, so it's surfaced immediately instead of triggering rotation.

    Transient model-wide errors (503 UNAVAILABLE / overloaded) get a short
    in-call backoff retry before failing over: key rotation can't fix an
    overloaded model, and the snapshot is already computed by call time, so
    losing the call wastes the whole run.
    """
    for label, include_schema in (("schema", True), ("plain-json", False)):
        contents = prompt if include_schema else (
            prompt + "\n\nOUTPUT JSON SHAPE (every field required unless |null):\n"
            + json.dumps(_schema_skeleton(GEMINI_RESPONSE_SCHEMA), separators=(",", ":"))
        )
        error = None
        for attempt in range(1 + GEMINI_TRANSIENT_RETRIES):
            try:
                response = client.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=contents,
                    config=_build_generate_config(include_schema=include_schema),
                )
            except Exception as e:
                msg = str(e).lower()
                if include_schema and ("schema" in msg or "unknown field" in msg):
                    logger.warning(f"Gemini schema rejected ({e}); retrying without schema.")
                    error = "schema rejected"
                    break  # drop to the plain-json tier
                if (any(m in msg for m in _TRANSIENT_MARKERS)
                        and attempt < GEMINI_TRANSIENT_RETRIES):
                    delay = GEMINI_TRANSIENT_BACKOFF_SECONDS * (attempt + 1)
                    logger.warning(f"Gemini transient error ({e}); retrying in {delay}s.")
                    time.sleep(delay)
                    continue
                return None, f"{label}: {e}", True

            result = _extract_response_dict(response)
            if result is None:
                return None, "empty response", False
            return result, None, False
        if error != "schema rejected":
            break

    return None, "all config tiers failed", False


def query_gemini(snapshot: dict) -> dict:
    """
    Call Gemini 3.5 Flash via the google-genai SDK with schema-enforced JSON
    output and a thinking level. Soft-falls-back to plain JSON-mode (no
    schema) if the SDK rejects our dict-form schema — keeps the engine
    running while a schema-shape issue is diagnosed.

    Rotates across up to 3 API keys (round robin between calls). If a key's
    call raises, that key is benched for GEMINI_KEY_COOLDOWN_SECONDS and the
    next configured key is tried immediately within the same call.
    """
    keys = _load_gemini_keys()
    if not keys:
        return {"error": "GEMINI_API_KEY not set"}

    # Compact separators (no indentation): indentation alone is ~15-20% of prompt
    # tokens on a snapshot this size (audit §4).
    prompt = f"DATA SNAPSHOT:\n{json.dumps(snapshot, separators=(',', ':'))}"

    last_error = "no keys available"
    for index, api_key in _iter_gemini_keys():
        client = _gemini_client_for_key(api_key)
        result, error, is_key_error = _call_gemini_with_key(client, prompt)
        if result is not None:
            _advance_gemini_rotation(index)
            return result

        last_error = error
        if not is_key_error:
            break  # parsing/response issue, not a key issue -- rotating won't help

        logger.warning(
            f"Gemini key #{index + 1}/{len(keys)} failed ({error}); cooling down "
            f"{GEMINI_KEY_COOLDOWN_SECONDS}s and trying next key."
        )
        _mark_gemini_key_failed(index, api_key)

    return {"error": f"Gemini call failed: {last_error}"}


def _splice_score_breakdown(analysis: dict, strategies: dict) -> dict:
    """
    header.score_breakdown is engine data (strategies.confluence_breakdown), not an
    LLM judgment -- spliced in here instead of round-tripping it through generation
    (see GEMINI_RESPONSE_SCHEMA comment). No-ops on an error/empty response (no
    `header` key).
    """
    if not isinstance(analysis, dict) or "header" not in analysis:
        return analysis
    analysis["header"]["score_breakdown"] = strategies.get("confluence_breakdown")
    return analysis


def _adx_tier(adx) -> str:
    """Same tiering as the C1 rubric (strategies._score_trend)."""
    if adx is None:
        return "WEAK"
    if adx < 25:
        return "WEAK"
    if adx < 41:
        return "MEDIUM"
    if adx < 56:
        return "STRONG"
    return "VERY_STRONG"


def _fmt_bos(bos) -> str:
    if not isinstance(bos, dict) or bos.get("level") is None:
        return "NONE"
    ago = bos.get("bars_ago")
    ago_txt = f" ({ago} bars ago)" if ago is not None else ""
    return f"{bos.get('direction')} BOS @ {bos.get('level')}{ago_txt}"


def _fmt_nearest_zone(zones, price) -> str:
    """Nearest-to-price '<low>-<high>' among [(bottom, top), ...] or 'NONE'."""
    zones = [z for z in zones if z and z[0] is not None and z[1] is not None]
    if not zones or price is None:
        return "NONE"
    bottom, top = min(zones, key=lambda z: abs((z[0] + z[1]) / 2 - price))
    return f"{bottom}-{top}"


def _build_mtf_context(snapshot: dict) -> dict:
    """
    Deterministic per-timeframe context table (structure / BOS / CVD / pd_zone /
    nearest OB+FVG / indicators) built from the snapshot. This used to be LLM
    output; it was pure transcription of the input, so it is now engine data and
    only `cross_tf_momentum` + `htf_draw` come from the model.
    """
    smc = snapshot.get("smc_context") or {}
    win = snapshot.get("windowed_indicators") or {}
    pa = snapshot.get("price_action") or {}

    def tf_block(tf):
        ctx = smc.get(tf) or {}
        w = win.get(tf) or {}
        price = ctx.get("current_price") or snapshot.get("live_price")
        obs = ctx.get("order_blocks") or {}
        ob_zones = [(ob.get("bottom"), ob.get("top"))
                    for side in ("bullish", "bearish")
                    for ob in (obs.get(side) or [])]
        fvg = ctx.get("fvg") or {}
        fvg_zones = [(g.get("bottom"), g.get("top"))
                     for g in (fvg.get("nearest_bullish_fvg"), fvg.get("nearest_bearish_fvg"))
                     if isinstance(g, dict)]
        pd_ctx = ctx.get("premium_discount") or {}
        return {
            "structure": ctx.get("structure", "NEUTRAL"),
            "bos": _fmt_bos(ctx.get("bos")),
            "cvd_delta": w.get("cvd_window_delta"),
            "price_change_pct": w.get("window_price_change_pct"),
            "pd_zone": pd_ctx.get("zone", "neutral"),
            "in_ote": bool(pd_ctx.get("in_ote")),
            "nearest_ob": _fmt_nearest_zone(ob_zones, price),
            "nearest_fvg": _fmt_nearest_zone(fvg_zones, price),
        }

    d1 = tf_block("1d")
    h4 = tf_block("4h")
    h4["adx"] = (win.get("4h") or {}).get("adx")
    h4["adx_tier"] = _adx_tier(h4["adx"])
    h1 = tf_block("1h")
    w1h = win.get("1h") or {}
    h1["adx"] = w1h.get("adx")
    h1["rsi"] = w1h.get("rsi")
    rsi_series = w1h.get("rsi_series_last_5") or []
    h1["rsi_slope"] = (round(rsi_series[-1] - rsi_series[-2], 2)
                       if len(rsi_series) >= 2 else None)
    h1["stochrsi_k"] = w1h.get("stochrsi_k")
    m15 = tf_block("15m")
    m15["rel_volume"] = (win.get("15m") or {}).get("relative_volume")
    m15["candle_pattern"] = pa.get("last_candle_pattern_15m", "none")
    return {"d1": d1, "h4": h4, "h1": h1, "m15": m15}


def _splice_mtf_context(analysis: dict, snapshot: dict) -> dict:
    """
    Replace the LLM's two-field mtf_context with the full engine-built table,
    keeping the model's `cross_tf_momentum` and `htf_draw` judgments (htf_draw
    lives on the d1 block, where the old schema carried it).
    """
    if not isinstance(analysis, dict) or "header" not in analysis:
        return analysis
    llm_mtf = analysis.get("mtf_context") or {}
    mtf = _build_mtf_context(snapshot)
    mtf["d1"]["htf_draw"] = llm_mtf.get("htf_draw", "NONE")
    mtf["cross_tf_momentum"] = llm_mtf.get("cross_tf_momentum", "n/a")
    analysis["mtf_context"] = mtf
    return analysis


def _finalize_analysis(analysis: dict, snapshot: dict) -> dict:
    """Post-call engine splices: score_breakdown + per-TF mtf_context table."""
    analysis = _splice_score_breakdown(analysis, snapshot.get("strategies") or {})
    return _splice_mtf_context(analysis, snapshot)


# ---------------- snapshot retention ---------------- #

def _enforce_snapshot_retention(symbol: str, retention: int = SNAPSHOT_RETENTION):
    """
    Keep at most `retention` snapshot files per symbol (oldest deleted first).
    Sorts by the filename's embedded UTC timestamp, not mtime (BUG-21) —
    label_snapshots.py rewrites files in place, which resets mtime and could
    otherwise delete newer snapshots ahead of older relabelled ones. The
    `YYYYMMDD_HHMMSS` suffix is fixed-width and zero-padded, so a plain
    lexicographic sort on the filename is already chronological.
    """
    if not os.path.isdir(SNAPSHOTS_DIR):
        return
    prefix = f"snapshot_{symbol.replace('/', '_')}_"
    files = sorted(
        name for name in os.listdir(SNAPSHOTS_DIR)
        if name.startswith(prefix) and name.endswith(".json")
    )
    if len(files) <= retention:
        return
    for name in files[: len(files) - retention]:
        try:
            os.remove(os.path.join(SNAPSHOTS_DIR, name))
        except OSError:
            continue


# ---------------- core pipeline ---------------- #

async def compute_snapshot_no_llm(symbol: str) -> tuple[dict, dict]:
    """Cheap pass: fetch + indicators + SMC/PA + evaluate_strategies + assemble the
    snapshot -- NO Gemini call, no persistence. Returns (snapshot_native, data) so the
    caller can build chart_series later. Used by the Telegram monitor and run_analysis."""
    logger.info(f"Analyzing market data for {symbol}...")

    data, live_price = await fetch_ohlcv(symbol)
    if live_price is not None:
        logger.debug(f"Live price for {symbol}: {live_price:.4f} (signals on closed bars)")

    # External micro / macro data — independent sync REST calls, run concurrently
    # on worker threads (BUG-24) instead of serially (~3-6s of avoidable latency).
    orderbook, funding, sentiment, open_interest, btc_dominance = await asyncio.gather(
        asyncio.to_thread(fetch_orderbook, symbol),
        asyncio.to_thread(fetch_funding, symbol),
        asyncio.to_thread(fetch_sentiment),
        asyncio.to_thread(fetch_open_interest, symbol),
        asyncio.to_thread(fetch_btc_dominance_proxy, symbol),
    )
    event_guard = fetch_event_window()

    # Compute each TF's value area once and reuse (avoids recomputation, B11).
    # Skips a TF that degraded to None (BUG-15) — get_value_area requires a df.
    value_areas = {tf: get_value_area(df) for tf, df in data.items() if df is not None}

    # SMC + PA contexts (1D leg adds daily structure + HTF draws)
    smc_context = build_smc_context(data["4h"], data["1h"], data["15m"],
                                    df_1d=data.get("1d"), value_areas=value_areas)
    pa_context = build_pa_context(data["1h"], data["15m"], df_4h=data["4h"],
                                  df_1d=data.get("1d"), value_areas=value_areas)
    pdh, pdl, pdc = calculate_previous_day(data["1h"])

    # Volatility regimes per TF
    vol_regime = {tf: detect_volatility_regime(data[tf]) for tf in data}

    # Global regime (SMC-driven) with cross-run hysteresis on the rubric (BUG-08)
    raw_regime = detect_regime(data["4h"], data["1h"], data["15m"], smc_context=smc_context)
    regime = apply_regime_hysteresis(symbol, raw_regime)

    # 1H Supertrend pulled out for the dashboard pill
    latest_1h = data["1h"].iloc[-1]
    st_val = float(latest_1h["SUPERT_10_3.0"]) if "SUPERT_10_3.0" in latest_1h and not pd.isna(latest_1h["SUPERT_10_3.0"]) else 0.0
    st_dir_val = latest_1h["SUPERTd_10_3.0"] if "SUPERTd_10_3.0" in latest_1h and not pd.isna(latest_1h["SUPERTd_10_3.0"]) else 0
    supertrend_direction = "BULLISH" if st_dir_val == 1 else ("BEARISH" if st_dir_val == -1 else "NEUTRAL")

    windowed_indicators = {tf: get_window_dict(df, WINDOW_SIZE[tf]) for tf, df in data.items() if df is not None}
    # Session-aware relative volume + ICT kill zone injected into the 15m window so
    # the volume gate uses a same-time-of-day baseline, not a flat 30-bar mean.
    windowed_indicators["15m"].update(session_volume_context(data["15m"]))

    strategies = evaluate_strategies(
        data["4h"], data["1h"], data["15m"],
        orderbook, funding, sentiment, open_interest,
        smc_context=smc_context,
        windowed_indicators=windowed_indicators,
        pa_context=pa_context,
        market_regime=regime,
        btc_dominance=btc_dominance,
    )

    # live_price drift vs the last CLOSED 15m close (entry-trigger phrasing) + a
    # volatility budget line so the LLM can veto targets it can't reach in-horizon.
    close_15m = float(data["15m"]["close"].iloc[-1])
    live_vs_close_pct = round((live_price - close_15m) / close_15m * 100, 3) \
        if (live_price and close_15m) else 0.0
    atr_context = {
        "atr_15m": _atr_last(data.get("15m")),
        "atr_1h": _atr_last(data.get("1h")),
        "atr_4h": _atr_last(data.get("4h")),
        "horizon_hours": HORIZON_HOURS,
        "note": "a target more than a few x atr_1h away is unlikely to fill within horizon_hours",
    }

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "symbol": symbol,
        "market": MARKET,
        "live_price": live_price,
        "live_vs_close_pct": live_vs_close_pct,
        "market_regime": regime,
        "raw_regime": raw_regime,
        "atr_context": atr_context,
        "volatility_regime": vol_regime,
        "event_guard": event_guard,
        "previous_day": {"pdh": pdh, "pdl": pdl, "pdc": pdc},
        "orderbook": orderbook,
        "funding": funding,
        "fear_greed_index": sentiment,
        "open_interest": open_interest,
        "btc_dominance_proxy": btc_dominance,
        "supertrend": {"direction": supertrend_direction, "level": st_val},
        "smc_context": smc_context,
        "price_action": pa_context,
        "strategies": strategies,
        "engine_trade": strategies.get("engine_trade"),
        "windowed_indicators": windowed_indicators,
    }
    snapshot_native = to_native(snapshot)
    return snapshot_native, data


def narrate(snapshot_native: dict) -> dict:
    """The single Gemini call (expensive pass). Returns the raw analysis dict
    (an {"error": ...} dict on failure -- see query_gemini)."""
    logger.info("Generating structured trade plan from Gemini...")
    return query_gemini(_build_llm_view(snapshot_native))


def finalize_analysis(snapshot_native: dict, analysis: dict) -> dict:
    """Post-call engine splices: score_breakdown + per-TF mtf_context table. Thin
    public wrapper around _finalize_analysis so the monitor can call it after a
    Gemini call it made itself (run_analysis uses it the same way)."""
    return _finalize_analysis(analysis, snapshot_native)


def _effective_retention(retention: int = None) -> int:
    """Resolve the per-symbol retention actually applied by a persist.

    Precedence: explicit caller arg (monitor `--retention`, tests) always wins;
    else a box-level `CRYPTERA_RETENTION` env var; else the engine default.

    B14.0/I15.0: the collector VM must NEVER prune -- but only the monitor passes
    `--retention 0`. A manual `python main.py` or dashboard `/api/run` on that same
    box goes through `run_analysis` with `retention=None`, falls to the 1000/symbol
    default, and silently deletes the corpus. Setting `CRYPTERA_RETENTION=0` in the
    box environment (.env / systemd unit) makes every entry point on it unlimited.
    `load_dotenv()` is called lazily (Gemini path only), so consult .env here too so
    the rail holds on the no-LLM / collect-only paths regardless of call order."""
    if retention is not None:
        return retention
    env = os.environ.get("CRYPTERA_RETENTION")
    if env is None:
        load_dotenv()  # idempotent; honors .env on the collector box even if narrate() never ran
        env = os.environ.get("CRYPTERA_RETENTION")
    if env is not None and env.strip():
        try:
            return int(env)
        except ValueError:
            logger.warning("CRYPTERA_RETENTION=%r is not an int; using default %d",
                           env, SNAPSHOT_RETENTION)
    return SNAPSHOT_RETENTION


def _persist_snapshot(snapshot_native: dict, retention: int = None) -> str:
    """Write a completed snapshot to snapshots/ and enforce per-symbol retention.

    retention: None -> box env (CRYPTERA_RETENTION) else engine default
    (SNAPSHOT_RETENTION); <=0 -> unlimited (collector mode). See _effective_retention."""
    symbol = snapshot_native.get("symbol", "UNKNOWN")
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    # UTC filename so the labeler's timestamp parse is timezone-safe (BUG-03)
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_filename = f"snapshot_{symbol.replace('/', '_')}_{timestamp_str}.json"
    snapshot_path = os.path.join(SNAPSHOTS_DIR, snapshot_filename)
    with open(snapshot_path, "w") as f:
        # FA-14: compact -- indented JSON is ~25-30% pure whitespace bloat on a
        # machine-read corpus file (nothing renders this by hand at scale; the
        # dashboard/labeller/eval/calibrate tooling all just parse it).
        json.dump(snapshot_native, f, separators=(",", ":"))
    logger.info(f"Snapshot saved to snapshots/{snapshot_filename}")
    eff_retention = _effective_retention(retention)
    # _enforce_snapshot_retention keeps exactly `retention` files -- 0 would mean "keep
    # none" (delete every file just written). <=0 here means "unlimited" instead (collector
    # mode, --retention 0), so skip the prune entirely rather than reinterpreting the value.
    if eff_retention > 0:
        _enforce_snapshot_retention(symbol, eff_retention)
    return snapshot_path


def data_health_report() -> list:
    """One-line-per-input health banner, printed at CLI/monitor startup so a silently
    degraded config (no calibration.json -> equal weights; placeholder events.json ->
    guard blind) is visible instead of discovered weeks later."""
    lines = []
    calib_path = os.path.join(DATA_DIR, "calibration.json")
    if os.path.exists(calib_path):
        lines.append("calibration: loaded")
    else:
        lines.append("calibration: ABSENT -- equal weights, score-bucket win-rate only "
                     "(label snapshots + run tools.calibrate once a corpus exists)")
    ev = fetch_event_window()
    if ev.get("active"):
        names = ", ".join(e.get("name", "unknown") for e in ev.get("events") or [])
        lines.append(f"event guard: ACTIVE -- {names}")
    else:
        cal = _calendar_health()
        if cal == "OK":
            lines.append("event guard: calendar OK")
        else:
            lines.append(f"event guard: calendar {cal} -- populate data/events.json")
    return lines


async def run_analysis(symbol: str) -> tuple[dict, dict]:
    """End-to-end pipeline (CLI/web). Returns (snapshot_dict, analysis_dict)."""
    snapshot_native, data = await compute_snapshot_no_llm(symbol)

    analysis = narrate(snapshot_native)
    analysis = finalize_analysis(snapshot_native, analysis)
    snapshot_native["analysis"] = analysis

    # Attach chart OHLC AFTER the LLM call so it never inflates the prompt.
    snapshot_native["chart_series"] = build_chart_series(data)
    _persist_snapshot(snapshot_native)
    return snapshot_native, analysis


# ---------------- CLI ---------------- #

async def main():
    import sys
    symbol = "SOL/USDT"

    if len(sys.argv) > 1:
        arg = sys.argv[1].upper().strip()
        if arg in ("SOL", "SOL/USDT", "SOLUSDT"):
            symbol = "SOL/USDT"
        elif arg in ("BTC", "BTC/USDT", "BTCUSDT"):
            symbol = "BTC/USDT"
        elif arg in ("ETH", "ETH/USDT", "ETHUSDT"):
            symbol = "ETH/USDT"
        elif "/" in arg:
            symbol = arg
        else:
            print(f"Unknown argument '{arg}'. Defaulting to SOL/USDT.")
    else:
        print("\n=== Cryptera v3.2 Core Engine ===")
        print("1. SOL/USDT (default)")
        print("2. BTC/USDT")
        print("3. ETH/USDT")
        print("4. Custom symbol")
        try:
            choice = input("Select coin choice (1-4): ").strip()
            if choice == "2":
                symbol = "BTC/USDT"
            elif choice == "3":
                symbol = "ETH/USDT"
            elif choice == "4":
                custom = input("Enter custom symbol (e.g. LINK/USDT): ").strip().upper()
                if "/" in custom:
                    symbol = custom
                else:
                    print("Invalid format. Defaulting to SOL/USDT.")
        except Exception:
            print("Invalid input. Defaulting to SOL/USDT.")

    snapshot_native, analysis = await run_analysis(symbol)
    print("\n" + "=" * 60)
    print(json.dumps(analysis, indent=2))
    print("=" * 60)

    import gc, sys as _sys
    gc.collect()
    _sys.exit(0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except SystemExit:
        pass
