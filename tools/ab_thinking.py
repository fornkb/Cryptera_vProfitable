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
GEMINI_THINKING_LEVEL winner, then update the constant + comment in config.py
(FA-13: the constant lives there, not main.py — main.py just imports it).

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

# Import the engine's prompt view + LLM caller and the CANONICAL labeller replay path
# (B26.0: the harness must replay the SAME window the labeller/eval do -- floored start,
# 1m ambiguity drill, episode collapse, realized-R -- or its verdict is noise).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as engine  # noqa: E402
from tools.label_snapshots import (  # noqa: E402
    _snapshot_time, _fetch_future_candles, _fetch_1m_window, _floor_to_15m,
    _label_one, _direction_to_side, _symbol_from_filename,
)
from tools.episodes import collapse_to_episodes  # noqa: E402
from tools.eval_engine import _realized_r  # noqa: E402
from tools.corpus import GenerationFilter, add_cli_flag  # noqa: E402

SNAPSHOTS_DIR = engine.SNAPSHOTS_DIR


def _acc():
    return {"decisions": 0, "active": 0, "triggered": 0, "tp": 0, "sl": 0,
            "timeout": 0, "intrabar_unknown": 0, "no_data": 0, "from_engine": 0,
            "sum_net_r": 0.0, "net_r_n": 0}


def _score_decision(td: dict, df, resolver) -> dict | None:
    side = _direction_to_side((td.get("primary") or {}).get("direction"))
    if not side or td.get("entry") is None or td.get("stop_loss") is None or td.get("take_profit") is None:
        return None
    # same 1m intrabar drill as the labeller, so ambiguous bars aren't guessed worst-case
    return _label_one(side, float(td["entry"]), float(td["stop_loss"]),
                      float(td["take_profit"]), df, intrabar_resolver=resolver)


def _run_level(level: str, llm_view: dict) -> dict:
    # Patched on the `main` MODULE's re-bound name, not config.py's: main.py does
    # `from config import ... GEMINI_THINKING_LEVEL`, which copies the value into
    # main's own namespace at import time, and _build_generate_config reads that
    # module-global (not config.GEMINI_THINKING_LEVEL) on every call -- so
    # rebinding `engine.GEMINI_THINKING_LEVEL` here works without touching config.py.
    engine.GEMINI_THINKING_LEVEL = level
    return engine.query_gemini(llm_view)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--levels", nargs="+", default=["high", "medium"])
    p.add_argument("--symbol", default=None)
    p.add_argument("--horizon", type=int, default=24)
    p.add_argument("--limit", type=int, default=None, help="cap number of snapshots evaluated")
    add_cli_flag(p)
    args = p.parse_args()

    if not os.path.isdir(SNAPSHOTS_DIR):
        print(f"[ERR] snapshots dir not found: {SNAPSHOTS_DIR}")
        sys.exit(1)

    import pandas as pd
    from datetime import datetime, timezone

    names = [n for n in sorted(os.listdir(SNAPSHOTS_DIR))
             if n.startswith("snapshot_") and n.endswith(".json")]
    if args.symbol:
        names = [n for n in names if _symbol_from_filename(n) == args.symbol]

    # B26.0: collapse a persisting setup's repeated 15m snapshots to ONE representative
    # BEFORE issuing any (paid) LLM calls -- else the same decision is A/B'd many times and
    # counted as independent trials, exactly the pseudo-replication eval/calibrate fix kills.
    # Invariant 5 + budget: an old-generation snapshot describes a SHAPE the current
    # system instruction no longer matches (no structure_state / typed pools /
    # setup_sequence / size_class), so replaying one buys a garbage answer with a real
    # paid Gemini call. Filter before spending anything.
    gen_filter = GenerationFilter(allow_mixed=args.allow_mixed_schema)
    rows = []
    for name in names:
        try:
            with open(os.path.join(SNAPSHOTS_DIR, name)) as f:
                snap = json.load(f)
        except Exception:
            continue
        if gen_filter.accept(snap):
            rows.append((name, snap, None))
    gen_filter.print_report()
    episodes = collapse_to_episodes(rows)

    stats = {lvl: _acc() for lvl in args.levels}
    evaluated = 0
    for name, snap, _ in episodes:
        if args.limit and evaluated >= args.limit:
            break
        snap_time = _snapshot_time(snap, name)
        symbol = snap.get("symbol") or _symbol_from_filename(name)
        if snap_time is None or symbol is None:
            continue
        if snap_time + pd.Timedelta(hours=args.horizon) > datetime.now(timezone.utc):
            continue  # horizon not elapsed
        # Floored start (B26.0) so the replay window matches the labeller's exactly.
        since_ms = _floor_to_15m(int(snap_time.timestamp() * 1000))
        df = _fetch_future_candles(symbol, since_ms, args.horizon)
        if df is None or df.empty:
            continue
        resolver = lambda ms, s=symbol: _fetch_1m_window(s, ms)  # noqa: E731

        llm_view = engine._build_llm_view(snap)
        evaluated += 1
        for lvl in args.levels:
            resp = _run_level(lvl, llm_view)
            td = (resp or {}).get("trade_decision") or {}
            acc = stats[lvl]
            acc["decisions"] += 1
            if (td.get("trade_management") or {}).get("selected_from_engine") is True:
                acc["from_engine"] += 1
            res = _score_decision(td, df, resolver)
            if res is None:
                continue
            acc["active"] += 1
            if res.get("outcome") == "no_data":
                acc["no_data"] += 1
            elif res.get("triggered"):
                acc["triggered"] += 1
                o = res.get("outcome")
                acc[{"tp_first": "tp", "sl_first": "sl", "intrabar_unknown": "intrabar_unknown"}
                    .get(o, "timeout")] += 1
                target_rr = (td.get("primary") or {}).get("rr") or td.get("rr")
                net_r = _realized_r(res, target_rr)   # canonical realized R (net of fees)
                if net_r is not None:
                    acc["sum_net_r"] += net_r
                    acc["net_r_n"] += 1

    print(f"\nEvaluated {evaluated} episode(s) across levels {args.levels}\n")
    hdr = (f"{'level':<8} {'active':>6} {'trig':>5} {'tp':>4} {'sl':>4} {'to':>4} {'unk':>4} "
           f"{'hit_rate':>9} {'net_R':>7} {'from_engine':>12}")
    print(hdr)
    print("-" * len(hdr))
    for lvl in args.levels:
        a = stats[lvl]
        hit = round(a["tp"] / a["triggered"], 3) if a["triggered"] else None
        net = round(a["sum_net_r"] / a["net_r_n"], 3) if a["net_r_n"] else None
        fe = round(a["from_engine"] / a["decisions"], 3) if a["decisions"] else None
        print(f"{lvl:<8} {a['active']:>6} {a['triggered']:>5} {a['tp']:>4} {a['sl']:>4} "
              f"{a['timeout']:>4} {a['intrabar_unknown']:>4} {str(hit):>9} {str(net):>7} {str(fe):>12}")
    print("\nPick the higher net_R (realized economics, not raw hit-rate), tie-break fewer "
          "tokens/latency -> lower level; then set GEMINI_THINKING_LEVEL + comment in config.py.")
    print("NOTE (B26.0): paired per-episode net-R deltas with a bootstrap CI land with I8.0's "
          "canonical replay stack -- until then treat close net_R as a tie.")


if __name__ == "__main__":
    main()
