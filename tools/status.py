"""cryptera-status: at-a-glance VM health — snapshot count + latest score/gate per symbol.

Reads snapshots/ only (no network, no Gemini). Classifies each symbol's MOST RECENT
snapshot with the exact gate the monitor uses (alerts.controller.engine_gate), so the
line you see here is the same ACTIVE/CONDITIONAL/HOLD decision the alert path makes.

Run directly (`python -m tools.status`) or via the `cryptera-status` command that
deploy/setup_vm.sh installs into /usr/local/bin.
"""

import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (SNAPSHOTS_DIR, ACTIVE_TRADE_THRESHOLD,  # noqa: E402
                    CONDITIONAL_ENTRY_THRESHOLD)
from alerts.controller import engine_gate  # noqa: E402

_PREFIX = "snapshot_"
_MODE = {"trend": "Trend", "mean_revert": "MeanRev"}


def _parse(name):
    """snapshot_<SYM_WITH_UNDERSCORES>_<YYYYMMDD>_<HHMMSS>.json -> ('SYM/USDT', sortkey)."""
    stem = name[len(_PREFIX):-len(".json")] if name.endswith(".json") else name[len(_PREFIX):]
    parts = stem.rsplit("_", 2)
    if len(parts) != 3:
        return None, None
    return parts[0].replace("_", "/"), parts[1] + parts[2]  # sortkey = date+time, lexical == chrono


def main():
    files = glob.glob(os.path.join(SNAPSHOTS_DIR, _PREFIX + "*.json"))
    counts = defaultdict(int)
    latest = {}          # sym -> (sortkey, path)
    for path in files:
        sym, key = _parse(os.path.basename(path))
        if sym is None:
            continue
        counts[sym] += 1
        if sym not in latest or key > latest[sym][0]:
            latest[sym] = (key, path)

    total = len(files)
    print(f"Cryptera corpus: {total} snapshot(s) across {len(latest)} symbol(s)")
    print(f"  ({SNAPSHOTS_DIR})")
    if not latest:
        print("  no snapshots yet — is the monitor running? (systemctl status cryptera-monitor)")
        return

    now = datetime.now(timezone.utc)
    for sym in sorted(latest):
        try:
            with open(latest[sym][1]) as f:
                snap = json.load(f)
        except Exception as e:
            print(f"  {sym:<10} (unreadable latest snapshot: {e})")
            continue
        d = engine_gate(snap, ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD)
        state = "ACTIVE" if d["active"] else ("CONDITIONAL" if d["conditional"] else "HOLD")
        mode = _MODE.get(d["mode"], d["mode"] or "?")
        direction = d["direction"] or "neutral"
        wr = d.get("win_rate")
        wr_txt = f"  wr {round(wr * 100)}%" if isinstance(wr, (int, float)) else ""
        age = ""
        try:
            ts = datetime.fromisoformat((snap.get("generated_at") or "").replace("Z", "+00:00"))
            age = f"  {(now - ts).total_seconds() / 60:.0f}m ago"
        except Exception:
            pass
        print(f"  {sym:<10} {d['score']:>3}/{d['threshold']}  {mode:<8} {state:<11} "
              f"{direction:<8} {counts[sym]:>5} snaps{wr_txt}{age}")
        et = d.get("et") or {}
        if d["active"] and et.get("entry") is not None:
            rr = et.get("rr")
            print(f"             -> entry {et.get('entry')}  stop {et.get('stop_loss')}  "
                  f"tp {et.get('take_profit')}" + (f"  R:R {rr}" if rr is not None else ""))


if __name__ == "__main__":
    main()
