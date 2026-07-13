"""
Outcome-label saved snapshots by replaying the realized 15m / 1h candles that
followed each snapshot timestamp.

For every snapshot that contains an `analysis.trade_decision` with non-null
entry / stop_loss / take_profit, we fetch the next `horizon_hours` of candles
and record:
  * triggered:           did price reach the entry level?
  * tp_first:            did TP fill before SL?
  * sl_first:            did SL fill before TP?
  * timeout:             neither side hit within the horizon
  * mfe / mae (pct):     max favourable / adverse excursion vs. entry
  * realized_rr:         (price extreme towards TP - entry) / (entry - sl)

A 15m bar that touches BOTH sl and tp is ambiguous at 15m resolution. Rather than
blindly assuming the loss, we DRILL into that one bar's 1m candles and replay them
to see which side was actually hit first (`_resolve_1m`). The pessimistic sl_first
assumption survives only as the final tiebreak — when 1m data is unavailable or a
1m bar itself still straddles both. `ambiguous_15m` on the outcome flags that the
drill happened, so its frequency/impact is auditable.

Forward-scenario contingent setups are labelled the same way under
`forward_outcome` when their entry/SL/TP are present.

Usage:
    python -m tools.label_snapshots                # label everything
    python -m tools.label_snapshots --symbol BTC/USDT --horizon 24
    python -m tools.label_snapshots --force        # re-label already-labelled snapshots

Output: each snapshot file is rewritten in place with an `outcome` block.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(ROOT_DIR, "snapshots")


def _parse_snapshot_time(filename: str) -> datetime | None:
    """snapshot_<base>_<quote>_<YYYYMMDD>_<HHMMSS>.json -> datetime"""
    parts = filename.replace(".json", "").split("_")
    if len(parts) < 5:
        return None
    date_p, time_p = parts[-2], parts[-1]
    try:
        return datetime.strptime(f"{date_p}{time_p}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _snapshot_time(snapshot: dict, filename: str) -> datetime | None:
    """
    True snapshot moment. Prefer `generated_at` (always UTC) over the filename —
    filenames written before v3.2.4 used the machine-local clock (BUG-03).
    """
    gen = snapshot.get("generated_at")
    if gen:
        try:
            return datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return _parse_snapshot_time(filename)


def _symbol_from_filename(filename: str) -> str | None:
    parts = filename.replace(".json", "").split("_")
    if len(parts) < 5:
        return None
    return f"{parts[1]}/{parts[2]}"


def _floor_to_15m(ms: int) -> int:
    """
    Floor an epoch-ms timestamp to its 15m bar-open boundary (FA-09). Binance
    returns klines with openTime >= startTime, so an unfloored snapshot time
    would exclude the bar forming AT snapshot time — up to 14.5 min of price
    action the engine's levels were quoted against. Snapshots are taken ~30s
    after the close (POLL_SECOND_OFFSET), so flooring admits only that
    negligible pre-snapshot movement; the same-bar SL-first conservatism in
    _label_one covers the snapshot bar itself.
    """
    return ms - ms % 900_000


def _fetch_future_candles(symbol: str, since_ms: int, horizon_hours: int):
    """
    Fetch up to `horizon_hours` of 15m USDT-perp candles starting from
    `since_ms` (expected pre-floored to the 15m boundary, see _floor_to_15m —
    the snapshot bar is the first replay bar). Same market namespace as the
    engine (fapi, BUG-01) — outcomes are replayed on the prices the geometry
    was built from.
    """
    needed = max(4, int(horizon_hours * 4))
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol.replace("/", ""), "interval": "15m",
                    "startTime": since_ms, "limit": min(needed + 4, 1500)},
            timeout=10,
        ).json()
    except Exception as e:
        print(f"[WARN] fetch failed for {symbol} @ {since_ms}: {e}")
        return None
    if not isinstance(r, list) or not r:
        return None
    candles = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
               for k in r]
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    cutoff = pd.Timestamp(since_ms, unit="ms", tz="UTC") + pd.Timedelta(hours=horizon_hours)
    return df[df["timestamp"] <= cutoff].reset_index(drop=True)


def _fetch_1m_window(symbol: str, start_ms: int, minutes: int = 15):
    """Fetch the `minutes` one-minute candles of a single 15m bar (its open at
    `start_ms`) so an ambiguous bar that straddled both SL and TP can be resolved
    at 1m resolution instead of assuming the worst case. Same fapi namespace as
    everything else (BUG-01). Returns a high/low DataFrame, or None on failure."""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol.replace("/", ""), "interval": "1m",
                    "startTime": start_ms, "limit": minutes + 1},
            timeout=10,
        ).json()
    except Exception as e:
        print(f"[WARN] 1m drill fetch failed for {symbol} @ {start_ms}: {e}")
        return None
    if not isinstance(r, list) or not r:
        return None
    # k[2]=high, k[3]=low
    return pd.DataFrame([[float(k[2]), float(k[3])] for k in r], columns=["high", "low"])


def _resolve_1m(df1m, side: str, sl: float, tp: float) -> str:
    """Given the 1m candles of a 15m bar that straddled BOTH sl and tp, walk them
    in order and return which was touched first ('tp_first' | 'sl_first'). Falls
    back to the pessimistic 'sl_first' when 1m data is missing or a single 1m bar
    itself straddles both (still unknowable — keep the conservative tiebreak)."""
    if df1m is None or getattr(df1m, "empty", True):
        return "sl_first"
    for _, r in df1m.iterrows():
        hi, lo = float(r["high"]), float(r["low"])
        if side == "long":
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            sl_hit, tp_hit = hi >= sl, lo <= tp
        if sl_hit and tp_hit:
            return "sl_first"   # still ambiguous even at 1m -> worst case
        if tp_hit:
            return "tp_first"
        if sl_hit:
            return "sl_first"
    return "sl_first"           # both were seen at 15m, so this is unreachable; be safe


def _label_one(side: str, entry: float, sl: float, tp: float, df: pd.DataFrame,
               intrabar_resolver=None) -> dict:
    """
    side: 'long' or 'short'.
    Walk forward bar by bar. A bar that touches BOTH SL and TP is ambiguous at 15m
    resolution: if `intrabar_resolver` is supplied (a callable bar_open_ms -> 1m
    DataFrame) we drill into that bar's 1m candles to decide which was hit first
    (`_resolve_1m`); otherwise, or if 1m data is unavailable, we fall back to the
    pessimistic `sl_first`. A resting limit order also counts as triggered if price
    gapped THROUGH the entry level between two consecutive closed bars, even though
    no single bar's [low, high] range contains it (BUG-18 — previously silently
    under-counted as not-triggered).
    """
    if df is None or df.empty:
        return {"triggered": False, "outcome": "no_data"}

    triggered = False
    entry_idx = None
    prev_close = None
    for i, row in df.iterrows():
        lo, hi = float(row["low"]), float(row["high"])
        if lo <= entry <= hi:
            triggered = True
            entry_idx = i
            break
        if prev_close is not None and ((prev_close < entry < lo) or (prev_close > entry > hi)):
            triggered = True
            entry_idx = i
            break
        prev_close = float(row["close"])

    if not triggered:
        return {"triggered": False, "outcome": "not_triggered",
                "bars_observed": int(len(df))}

    post = df.iloc[entry_idx:].reset_index(drop=True)
    mfe = 0.0
    mae = 0.0
    outcome = "timeout"
    bars_to_close = None
    ambiguous = False

    for i, row in post.iterrows():
        if side == "long":
            fav = (row["high"] - entry) / entry * 100
            adv = (row["low"] - entry) / entry * 100
            mfe = max(mfe, fav)
            mae = min(mae, adv)
            sl_hit = row["low"] <= sl
            tp_hit = row["high"] >= tp
        else:
            fav = (entry - row["low"]) / entry * 100
            adv = (entry - row["high"]) / entry * 100
            mfe = max(mfe, fav)
            mae = min(mae, adv)
            sl_hit = row["high"] >= sl
            tp_hit = row["low"] <= tp

        if sl_hit and tp_hit:
            # Both hit in one 15m bar -> order unknowable at 15m. Drill to 1m if we
            # have a resolver + this bar's open time; else keep the worst-case sl_first.
            ambiguous = True
            if intrabar_resolver is not None and "timestamp" in row:
                bar_open_ms = int(pd.Timestamp(row["timestamp"]).timestamp() * 1000)
                outcome = _resolve_1m(intrabar_resolver(bar_open_ms), side, sl, tp)
            else:
                outcome = "sl_first"
            bars_to_close = i
            break
        if sl_hit:
            outcome = "sl_first"
            bars_to_close = i
            break
        if tp_hit:
            outcome = "tp_first"
            bars_to_close = i
            break

    risk = abs(entry - sl)
    realized_rr = None
    if risk > 0:
        if side == "long":
            extreme = entry * (1 + mfe / 100)
        else:
            extreme = entry * (1 - mfe / 100)
        realized_rr = round(abs(extreme - entry) / risk, 3)

    return {
        "triggered": True,
        "outcome": outcome,
        "bars_to_close": bars_to_close,
        "ambiguous_15m": ambiguous,
        "mfe_pct": round(mfe, 3),
        "mae_pct": round(mae, 3),
        "realized_rr": realized_rr,
        "bars_observed": int(len(df)),
    }


def _direction_to_side(direction: str) -> str | None:
    if not direction:
        return None
    d = direction.upper()
    if d in ("BUY", "LONG"):
        return "long"
    if d in ("SELL", "SHORT"):
        return "short"
    return None


def label_snapshot_file(path: str, horizon_hours: int, force: bool):
    with open(path, "r") as f:
        snapshot = json.load(f)

    if not force and snapshot.get("outcome"):
        return False

    filename = os.path.basename(path)
    snap_time = _snapshot_time(snapshot, filename)
    symbol = snapshot.get("symbol") or _symbol_from_filename(filename)
    if snap_time is None or symbol is None:
        print(f"[SKIP] cannot parse symbol/time from {filename}")
        return False

    # Bail out if the horizon end is in the future
    if snap_time + pd.Timedelta(hours=horizon_hours) > datetime.now(timezone.utc):
        return False

    since_ms = _floor_to_15m(int(snap_time.timestamp() * 1000))
    df = _fetch_future_candles(symbol, since_ms, horizon_hours)
    if df is None or df.empty:
        print(f"[WARN] no future data for {filename}")
        return False

    analysis = snapshot.get("analysis") or {}
    td = analysis.get("trade_decision") or {}
    fwd = analysis.get("forward_scenario") or {}
    # engine_trade is the deterministic geometry — this is what calibration scores
    eng = snapshot.get("engine_trade") or (snapshot.get("strategies") or {}).get("engine_trade") or {}

    # Resolver that drills an ambiguous 15m bar down to its 1m candles (bound to this symbol).
    resolver = lambda bar_open_ms: _fetch_1m_window(symbol, bar_open_ms)  # noqa: E731

    trade_outcome = None
    side = _direction_to_side(td.get("primary", {}).get("direction"))
    if side and td.get("entry") is not None and td.get("stop_loss") is not None and td.get("take_profit") is not None:
        trade_outcome = _label_one(side, float(td["entry"]), float(td["stop_loss"]),
                                   float(td["take_profit"]), df, resolver)

    forward_outcome = None
    fwd_side = _direction_to_side(fwd.get("direction"))
    if fwd_side and fwd.get("entry") is not None and fwd.get("stop_loss") is not None and fwd.get("take_profit") is not None:
        forward_outcome = _label_one(fwd_side, float(fwd["entry"]), float(fwd["stop_loss"]),
                                     float(fwd["take_profit"]), df, resolver)

    engine_outcome = None
    eng_side = _direction_to_side(eng.get("direction"))
    if eng.get("valid") and eng_side and eng.get("entry") is not None \
            and eng.get("stop_loss") is not None and eng.get("take_profit") is not None:
        engine_outcome = _label_one(eng_side, float(eng["entry"]), float(eng["stop_loss"]),
                                    float(eng["take_profit"]), df, resolver)

    snapshot["outcome"] = {
        "labeled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "horizon_hours": horizon_hours,
        "bars_observed": int(len(df)),
        "trade_decision_outcome": trade_outcome,
        "forward_scenario_outcome": forward_outcome,
        "engine_trade_outcome": engine_outcome,
    }

    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=None, help="restrict to one symbol (e.g. BTC/USDT)")
    p.add_argument("--horizon", type=int, default=24, help="hours of look-forward (default 24)")
    p.add_argument("--force", action="store_true", help="re-label already labelled snapshots")
    args = p.parse_args()

    if not os.path.isdir(SNAPSHOTS_DIR):
        print(f"[ERR] snapshots dir not found: {SNAPSHOTS_DIR}")
        sys.exit(1)

    targets = []
    for name in sorted(os.listdir(SNAPSHOTS_DIR)):
        if not name.startswith("snapshot_") or not name.endswith(".json"):
            continue
        if args.symbol:
            sym = _symbol_from_filename(name)
            if sym != args.symbol:
                continue
        targets.append(os.path.join(SNAPSHOTS_DIR, name))

    if not targets:
        print("[INFO] no snapshots match the filter.")
        return

    updated = 0
    for path in targets:
        try:
            if label_snapshot_file(path, args.horizon, args.force):
                updated += 1
                print(f"[OK] labelled {os.path.basename(path)}")
        except Exception as e:
            print(f"[ERR] {os.path.basename(path)}: {e}")
    print(f"\nDone. Labelled {updated} / {len(targets)} snapshot(s).")


if __name__ == "__main__":
    main()
