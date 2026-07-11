"""
A/B the Gemini thinking level (and, by extension, prompt edits) over saved
snapshots — the "improve Gemini context" harness the audit §4 calls for.

For each labelled snapshot with enough elapsed forward window, this re-runs the
LLM at each thinking level, takes the trade_decision it produced, and replays the
realized 15m candles (same market namespace + labeller as the engine) to score
whether that decision would have hit TP before SL. It reports, per level:
  * decisions produced / active (non-HOLD) trades
  * triggered / tp_first / sl_first / timeout
  * hit-rate (tp / triggered) and how often the LLM echoed the engine primary

Because it issues one live Gemini call per snapshot per level, it is a manual,
opt-in tool — run it once a corpus of >= ~30 labelled snapshots exists to pick the
GEMINI_THINKING_LEVEL winner, then update the constant + comment in main.py.

Usage:
    python -m tools.ab_thinking --levels high medium --horizon 24
    python -m tools.ab_thinking --symbol BTC/USDT --limit 40
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

# Import the engine's prompt view + LLM caller and the labeller's replay helpers.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as engine  # noqa: E402
from tools.label_snapshots import (  # noqa: E402
    _snapshot_time, _fetch_future_candles, _label_one, _direction_to_side,
    _symbol_from_filename,
)

SNAPSHOTS_DIR = engine.SNAPSHOTS_DIR


def _acc():
    return {"decisions": 0, "active": 0, "triggered": 0, "tp": 0, "sl": 0,
            "timeout": 0, "no_data": 0, "from_engine": 0}


def _score_decision(td: dict, df) -> dict | None:
    side = _direction_to_side((td.get("primary") or {}).get("direction"))
    if not side or td.get("entry") is None or td.get("stop_loss") is None or td.get("take_profit") is None:
        return None
    return _label_one(side, float(td["entry"]), float(td["stop_loss"]), float(td["take_profit"]), df)


def _run_level(level: str, llm_view: dict) -> dict:
    engine.GEMINI_THINKING_LEVEL = level  # patched per call; _build_generate_config reads it
    return engine.query_gemini(llm_view)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--levels", nargs="+", default=["high", "medium"])
    p.add_argument("--symbol", default=None)
    p.add_argument("--horizon", type=int, default=24)
    p.add_argument("--limit", type=int, default=None, help="cap number of snapshots evaluated")
    args = p.parse_args()

    if not os.path.isdir(SNAPSHOTS_DIR):
        print(f"[ERR] snapshots dir not found: {SNAPSHOTS_DIR}")
        sys.exit(1)

    names = [n for n in sorted(os.listdir(SNAPSHOTS_DIR))
             if n.startswith("snapshot_") and n.endswith(".json")]
    if args.symbol:
        names = [n for n in names if _symbol_from_filename(n) == args.symbol]

    stats = {lvl: _acc() for lvl in args.levels}
    evaluated = 0
    for name in names:
        if args.limit and evaluated >= args.limit:
            break
        path = os.path.join(SNAPSHOTS_DIR, name)
        try:
            with open(path) as f:
                snap = json.load(f)
        except Exception:
            continue
        snap_time = _snapshot_time(snap, name)
        symbol = snap.get("symbol") or _symbol_from_filename(name)
        if snap_time is None or symbol is None:
            continue
        import pandas as pd
        from datetime import datetime, timezone
        if snap_time + pd.Timedelta(hours=args.horizon) > datetime.now(timezone.utc):
            continue  # horizon not elapsed
        df = _fetch_future_candles(symbol, int(snap_time.timestamp() * 1000), args.horizon)
        if df is None or df.empty:
            continue

        llm_view = engine._build_llm_view(snap)
        evaluated += 1
        for lvl in args.levels:
            resp = _run_level(lvl, llm_view)
            td = (resp or {}).get("trade_decision") or {}
            acc = stats[lvl]
            acc["decisions"] += 1
            if (td.get("trade_management") or {}).get("selected_from_engine") is True:
                acc["from_engine"] += 1
            res = _score_decision(td, df)
            if res is None:
                continue
            acc["active"] += 1
            if res.get("outcome") == "no_data":
                acc["no_data"] += 1
            elif res.get("triggered"):
                acc["triggered"] += 1
                o = res.get("outcome")
                acc["tp" if o == "tp_first" else ("sl" if o == "sl_first" else "timeout")] += 1

    print(f"\nEvaluated {evaluated} snapshot(s) across levels {args.levels}\n")
    hdr = f"{'level':<8} {'active':>6} {'trig':>5} {'tp':>4} {'sl':>4} {'to':>4} {'hit_rate':>9} {'from_engine':>12}"
    print(hdr)
    print("-" * len(hdr))
    for lvl in args.levels:
        a = stats[lvl]
        hit = round(a["tp"] / a["triggered"], 3) if a["triggered"] else None
        fe = round(a["from_engine"] / a["decisions"], 3) if a["decisions"] else None
        print(f"{lvl:<8} {a['active']:>6} {a['triggered']:>5} {a['tp']:>4} {a['sl']:>4} "
              f"{a['timeout']:>4} {str(hit):>9} {str(fe):>12}")
    print("\nPick the higher hit_rate (tie-break: fewer tokens/latency -> lower level), "
          "then set GEMINI_THINKING_LEVEL + its comment in main.py to the winner.")


if __name__ == "__main__":
    main()
