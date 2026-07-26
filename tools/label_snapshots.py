"""
Outcome-label saved snapshots by replaying the realized 15m / 1h candles that
followed each snapshot timestamp.

Chronological order-state machine (B1.0). For every snapshot with an
`analysis.trade_decision` / `engine_trade` that carries non-null
entry / stop_loss / take_profit, we fetch the next `horizon_hours` of candles
and replay them IN TIME ORDER through `PENDING_ENTRY -> OPEN -> TP | SL | TIMEOUT`:
the entry must fill before any exit is counted. Recorded per trade:
  * triggered:            did price reach the entry level (touch or gap-through)?
  * tp_first / sl_first:  which exit filled first, AFTER the entry filled
  * timeout:              filled but neither exit hit within the horizon
  * intrabar_unknown:     entry and an exit share one bar's range and the fill-vs-exit
                          order can't be reconstructed even at 1m -> a distinct class,
                          NEVER silently scored a win (this is the core B1.0 fix)
  * not_triggered:        entry never filled
  * mfe / mae (pct):      max favourable / adverse excursion FROM THE FILL onward
                          (no pre-fill credit on the entry bar)
  * mfe_r:                MFE expressed in R (an EXCURSION, not PnL -- named honestly so
                          it can never be mistaken for a win rate; B4.0)
  * end_rr:               close-at-horizon in R (terminal state; feeds B4.0/B15.0)
  * entry_time / exit_time / resolution_used: chronology + how the exit order was decided

Intrabar resolution: when one 15m bar's range straddles two decision levels the order
is unknowable at 15m, so we DRILL into that bar's 1m candles. Two cases:
  - a position already OPEN whose bar touches BOTH sl and tp -> `_resolve_1m` (the
    pessimistic `sl_first` tiebreak from f949ab9 survives when 1m can't separate them);
  - the ENTRY bar also touching an exit -> `_resolve_entry_1m`, which reconstructs the
    fill first and only counts an exit after it; unresolvable -> `intrabar_unknown`.

Horizon (B1.0): a bar is kept only if its CLOSE time is within the horizon. `timestamp`
is the bar OPEN, so the cut is strict `<` (the old `<=` kept the bar opening at +horizon,
which closes 15m past it).

Forward-scenario contingent setups are labelled the same way under `forward_outcome`.

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
sys.path.insert(0, ROOT_DIR)   # allow `python tools/label_snapshots.py` (no package context)

# Bump whenever _label_one's semantics change so corpora of mixed label meaning can
# never be pooled unnoticed. v1 = pre-B1.0 (order-of-events assumed); v2 = chronological
# state machine + intrabar_unknown + entry-bar excursion fix + strict horizon + end_rr.
# Lives in config.py now (single source of truth) so tools/corpus.py can enforce it
# without importing this module; re-exported here for the existing callers/tests.
from config import LABEL_VERSION  # noqa: E402,F401

try:
    from tools.corpus import (add_cli_flag, is_current_schema, snapshot_generation,
                              CURRENT_GENERATION)
except ImportError:  # pragma: no cover - direct-script invocation
    from corpus import (add_cli_flag, is_current_schema, snapshot_generation,
                        CURRENT_GENERATION)


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
    negligible pre-snapshot movement; the entry-bar order state machine in
    _label_one covers the snapshot bar itself (no exit counts before the fill).
    """
    return ms - ms % 900_000


def _ts_iso(ts) -> str | None:
    """A bar timestamp (pandas Timestamp / None) -> UTC ISO 'Z' string, or None."""
    if ts is None:
        return None
    try:
        if pd.isna(ts):
            return None
    except (TypeError, ValueError):
        pass
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def _truncate_to_horizon(df: pd.DataFrame, since_ms: int, horizon_hours: int) -> pd.DataFrame:
    """Keep only bars whose CLOSE time is within the horizon. Bars are 15m and
    `timestamp` is the bar OPEN, so a bar opening at exactly since+horizon closes
    15m later and must be dropped: strict `<` on open-time (B1.0 — the old `<=`
    kept one extra bar, letting outcomes be decided up to +15m past the horizon)."""
    cutoff = pd.Timestamp(since_ms, unit="ms", tz="UTC") + pd.Timedelta(hours=horizon_hours)
    return df[df["timestamp"] < cutoff].reset_index(drop=True)


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
    return _truncate_to_horizon(df, since_ms, horizon_hours)


def _fetch_1m_window(symbol: str, start_ms: int, minutes: int = 15):
    """Fetch the `minutes` one-minute candles of a single 15m bar (its open at
    `start_ms`) so an ambiguous bar can be resolved at 1m resolution instead of
    guessing. Same fapi namespace as everything else (BUG-01). Returns a
    high/low DataFrame, or None on failure."""
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


def _resolve_1m(df1m, side: str, sl: float, tp: float):
    """Exit-order resolution for a position that is ALREADY OPEN when a single 15m bar
    touches BOTH sl and tp. Walks the bar's 1m candles in order. Returns
    (outcome, resolution): ('tp_first'|'sl_first', '1m') when reconstructable; the
    pessimistic 'sl_first' tiebreak (f949ab9) survives with a '1m_unavailable' /
    '1m_unresolved' tag when 1m data is missing or a 1m bar itself straddles both."""
    if df1m is None or getattr(df1m, "empty", True):
        return "sl_first", "1m_unavailable"
    for _, r in df1m.iterrows():
        hi, lo = float(r["high"]), float(r["low"])
        if side == "long":
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            sl_hit, tp_hit = hi >= sl, lo <= tp
        if sl_hit and tp_hit:
            return "sl_first", "1m_unresolved"   # still ambiguous even at 1m -> worst case
        if tp_hit:
            return "tp_first", "1m"
        if sl_hit:
            return "sl_first", "1m"
    return "sl_first", "1m_unresolved"            # both seen at 15m, so unreachable; be safe


def _resolve_entry_1m(df1m, side: str, entry: float, sl: float, tp: float):
    """Entry-vs-exit order resolution: one 15m bar's range contains BOTH the entry and
    an exit level, so we cannot tell whether the entry filled before the exit was
    touched. Walk the bar's 1m candles reconstructing PENDING->OPEN and count an exit
    ONLY after the fill. Returns (outcome, resolution):
      ('tp_first'|'sl_first', '1m')   -> order reconstructed
      ('open_continue', '1m')         -> filled at 1m, no exit inside the bar (stay open)
      ('intrabar_unknown', ...)       -> 1m missing, never fills, or a single 1m bar
                                         straddles the fill and an exit
    Never returns a silent win when the order is truly unknowable (B1.0)."""
    if df1m is None or getattr(df1m, "empty", True):
        return "intrabar_unknown", "1m_unavailable"
    filled = False
    for _, r in df1m.iterrows():
        hi, lo = float(r["high"]), float(r["low"])
        if side == "long":
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            sl_hit, tp_hit = hi >= sl, lo <= tp
        if not filled:
            if lo <= entry <= hi:
                filled = True
                if sl_hit or tp_hit:
                    # the fill 1m bar also touches an exit -> order still unknowable
                    return "intrabar_unknown", "1m_unresolved"
            # not filled yet: any exit touch is pre-fill and does not count
            continue
        if sl_hit and tp_hit:
            return "intrabar_unknown", "1m_unresolved"
        if tp_hit:
            return "tp_first", "1m"
        if sl_hit:
            return "sl_first", "1m"
    return ("open_continue", "1m") if filled else ("intrabar_unknown", "1m_unresolved")


def _label_one(side: str, entry: float, sl: float, tp: float, df: pd.DataFrame,
               intrabar_resolver=None) -> dict:
    """
    side: 'long' or 'short'. Replay `df` (15m bars, time-ordered) through a
    PENDING_ENTRY -> OPEN -> TP|SL|TIMEOUT state machine. The entry fills on the first
    bar whose range contains it OR that price gapped THROUGH between two closes (BUG-18);
    no exit is counted before the fill. A bar that makes the fill-vs-exit or the
    sl-vs-tp order ambiguous at 15m is drilled to 1m (`_resolve_entry_1m` /
    `_resolve_1m`); truly unknowable entry-vs-exit orders become `intrabar_unknown`
    (never a silent win). Excursions (MFE/MAE) accrue from the fill onward — the entry
    bar contributes no favourable credit (pre-fill contamination fix, B1.0) but keeps
    its adverse move (pessimistic).
    """
    if df is None or df.empty:
        return {"triggered": False, "outcome": "no_data"}

    state = "PENDING"            # PENDING -> OPEN -> (terminal via break)
    entry_idx = None
    entry_time = None
    exit_time = None
    outcome = "timeout"         # stays 'timeout' if filled but no exit within horizon
    resolution = "none"
    ambiguous = False
    bars_to_close = None
    mfe = 0.0
    mae = 0.0
    prev_close = None

    for i, row in df.iterrows():
        lo, hi = float(row["low"]), float(row["high"])
        close = float(row["close"])
        ts = row.get("timestamp") if hasattr(row, "get") else None

        if side == "long":
            fav = (hi - entry) / entry * 100
            adv = (lo - entry) / entry * 100
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            fav = (entry - lo) / entry * 100
            adv = (entry - hi) / entry * 100
            sl_hit, tp_hit = hi >= sl, lo <= tp

        if state == "PENDING":
            range_fill = lo <= entry <= hi
            gap_fill = prev_close is not None and (
                (prev_close < entry < lo) or (prev_close > entry > hi))
            if not (range_fill or gap_fill):
                prev_close = close
                continue

            entry_idx = i
            entry_time = ts

            if gap_fill:
                # Fill at the bar open (price crossed entry between closes), so the whole
                # bar is post-fill: count its excursion and evaluate exits as OPEN. A gap
                # bar can reach at most one exit level (price is on one side of entry).
                mfe, mae = max(mfe, fav), min(mae, adv)
                if sl_hit and tp_hit:                       # defensive; not reachable on a gap bar
                    ambiguous = True
                    outcome, resolution = _drill_open(intrabar_resolver, ts, side, sl, tp)
                    exit_time, bars_to_close = ts, 0
                    break
                if sl_hit:
                    outcome, resolution, exit_time, bars_to_close = "sl_first", "15m", ts, 0
                    break
                if tp_hit:
                    outcome, resolution, exit_time, bars_to_close = "tp_first", "15m", ts, 0
                    break
                state = "OPEN"                              # opened, no exit this bar
                prev_close = close
                continue

            # range_fill: the entry filled somewhere inside this bar -> intra-bar order is
            # unknown. If the same bar also reaches an exit, reconstruct fill-then-exit at 1m.
            if sl_hit or tp_hit:
                ambiguous = True
                res, tag = _drill_entry(intrabar_resolver, ts, side, entry, sl, tp)
                if res == "open_continue":
                    state = "OPEN"
                    mae = min(mae, adv)                     # entry-bar fav withheld; adverse kept
                    resolution = tag
                    prev_close = close
                    continue
                outcome, resolution = res, tag
                exit_time, bars_to_close = ts, 0
                mae = min(mae, adv)
                break

            # clean fill, no exit on the entry bar -> open from the next bar
            state = "OPEN"
            mae = min(mae, adv)                             # no favourable credit on the entry bar
            prev_close = close
            continue

        # state == OPEN
        mfe, mae = max(mfe, fav), min(mae, adv)
        if sl_hit and tp_hit:
            ambiguous = True
            outcome, resolution = _drill_open(intrabar_resolver, ts, side, sl, tp)
            exit_time, bars_to_close = ts, i - entry_idx
            break
        if sl_hit:
            outcome, resolution, exit_time, bars_to_close = "sl_first", "15m", ts, i - entry_idx
            break
        if tp_hit:
            outcome, resolution, exit_time, bars_to_close = "tp_first", "15m", ts, i - entry_idx
            break

    if entry_idx is None:
        return {"triggered": False, "outcome": "not_triggered", "bars_observed": int(len(df))}

    risk = abs(entry - sl)
    tp_dist_pct = abs(tp - entry) / entry * 100
    sl_dist_pct = abs(entry - sl) / entry * 100
    # An exit reached at least its level: keep excursions consistent with the outcome
    # (covers the entry-bar case where per-bar favourable credit was withheld).
    if outcome == "tp_first":
        mfe = max(mfe, tp_dist_pct)
    elif outcome == "sl_first":
        mae = min(mae, -sl_dist_pct)

    mfe_r = round((mfe / 100) * entry / risk, 3) if risk > 0 else None
    last_close = float(df.iloc[-1]["close"])
    end_move = (last_close - entry) if side == "long" else (entry - last_close)
    end_rr = round(end_move / risk, 3) if risk > 0 else None

    return {
        "triggered": True,
        "outcome": outcome,
        "resolution_used": resolution,
        "ambiguous_15m": ambiguous,
        "bars_to_fill": int(entry_idx),
        "bars_to_close": (int(bars_to_close) if bars_to_close is not None else None),
        "mfe_pct": round(mfe, 3),
        "mae_pct": round(mae, 3),
        "mfe_r": mfe_r,
        "end_rr": end_rr,
        "entry_time": _ts_iso(entry_time),
        "exit_time": _ts_iso(exit_time),
        "bars_observed": int(len(df)),
    }


def _drill_open(resolver, ts, side, sl, tp):
    """Resolve an already-open bar that touches both sl and tp; (outcome, resolution)."""
    if resolver is not None and ts is not None:
        bar_open_ms = int(pd.Timestamp(ts).timestamp() * 1000)
        return _resolve_1m(resolver(bar_open_ms), side, sl, tp)
    return "sl_first", "1m_unavailable"


def _drill_entry(resolver, ts, side, entry, sl, tp):
    """Resolve an entry bar that also touches an exit; (outcome, resolution)."""
    if resolver is not None and ts is not None:
        bar_open_ms = int(pd.Timestamp(ts).timestamp() * 1000)
        return _resolve_entry_1m(resolver(bar_open_ms), side, entry, sl, tp)
    return "intrabar_unknown", "1m_unavailable"


def _direction_to_side(direction: str) -> str | None:
    if not direction:
        return None
    d = direction.upper()
    if d in ("BUY", "LONG"):
        return "long"
    if d in ("SELL", "SHORT"):
        return "short"
    return None


def label_snapshot_file(path: str, horizon_hours: int, force: bool, allow_mixed: bool = False):
    with open(path, "r") as f:
        snapshot = json.load(f)

    filename = os.path.basename(path)

    # A label is only meaningful against the geometry that produced it. Stamping the
    # CURRENT label_version onto a snapshot from an older schema generation would make
    # a pre-fix row look current to calibrate/eval_engine -- the exact pooling this
    # guard exists to prevent (invariant 5). Skip it and say so.
    if not allow_mixed and not is_current_schema(snapshot):
        return "skipped_schema"

    if not force and snapshot.get("outcome"):
        return False

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
        "label_version": LABEL_VERSION,
        # un-floored snapshot moment: makes the ~30s + up-to-14.5m flooring tolerance
        # auditable against the fill/exit times (FA-09 / B28.0).
        "decision_time": snap_time.isoformat().replace("+00:00", "Z"),
        "horizon_hours": horizon_hours,
        "bars_observed": int(len(df)),
        "trade_decision_outcome": trade_outcome,
        "forward_scenario_outcome": forward_outcome,
        "engine_trade_outcome": engine_outcome,
    }

    with open(path, "w") as f:
        # compact: the daily label pass must not re-bloat the machine-read corpus (B25.0)
        json.dump(snapshot, f, separators=(",", ":"))
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=None, help="restrict to one symbol (e.g. BTC/USDT)")
    p.add_argument("--horizon", type=int, default=24, help="hours of look-forward (default 24)")
    p.add_argument("--force", action="store_true", help="re-label already labelled snapshots")
    add_cli_flag(p)
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
    skipped_schema = {}
    for path in targets:
        try:
            res = label_snapshot_file(path, args.horizon, args.force, args.allow_mixed_schema)
            if res == "skipped_schema":
                with open(path) as f:
                    gen = snapshot_generation(json.load(f)) or "<none>"
                skipped_schema[gen] = skipped_schema.get(gen, 0) + 1
            elif res:
                updated += 1
                print(f"[OK] labelled {os.path.basename(path)}")
        except Exception as e:
            print(f"[ERR] {os.path.basename(path)}: {e}")
    print(f"\nDone. Labelled {updated} / {len(targets)} snapshot(s).")
    if skipped_schema:
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(skipped_schema.items()))
        print(f"[corpus] SKIPPED {sum(skipped_schema.values())} snapshot(s) from other schema "
              f"generations (current {CURRENT_GENERATION}) — {detail}.\n"
              f"         Labelling them would stamp a current label_version onto pre-fix "
              f"geometry and let them pool (invariant 5). Archive them out of snapshots/.")
    if args.allow_mixed_schema:
        print("[corpus] WARNING: --allow-mixed-schema was ON — labels were written across "
              "schema generations. Never fit calibration on the result.")


if __name__ == "__main__":
    main()
