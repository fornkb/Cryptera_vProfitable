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
from alerts.config import Cfg  # noqa: E402
from tools.corpus import snapshot_generation, CURRENT_GENERATION  # noqa: E402

HEARTBEAT_PATH = Cfg.heartbeat_path

_PREFIX = "snapshot_"
_MODE = {"trend": "Trend", "mean_revert": "MeanRev"}


def _parse(name):
    """snapshot_<SYM_WITH_UNDERSCORES>_<YYYYMMDD>_<HHMMSS>.json -> ('SYM/USDT', sortkey)."""
    stem = name[len(_PREFIX):-len(".json")] if name.endswith(".json") else name[len(_PREFIX):]
    parts = stem.rsplit("_", 2)
    if len(parts) != 3:
        return None, None
    return parts[0].replace("_", "/"), parts[1] + parts[2]  # sortkey = date+time, lexical == chrono


def _print_liveness():
    """The collector's own progress signal. `systemctl status` reporting `active
    (running)` proved insufficient on 2026-07-21 -- the process was alive and wedged
    for five days -- so the FIRST thing an operator sees is cycle progress, not
    process state."""
    try:
        with open(HEARTBEAT_PATH) as f:
            hb = json.load(f)
    except Exception:
        print("Collector: no heartbeat file yet "
              "(monitor not started since this build, or a pre-liveness build is running)")
        return
    try:
        ts = datetime.fromisoformat((hb.get("updated_at") or "").replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        age = f"{age_min:.0f}m ago"
    except Exception:
        age_min, age = None, "unknown age"
    ok = bool(hb.get("alive")) and (age_min is None or age_min < 45)
    flag = "OK " if ok else "STALE"
    print(f"Collector [{flag}] last cycle {age} · {hb.get('cycles_completed')} cycle(s) this run "
          f"· {hb.get('last_snapshots')} snapshot(s) last cycle · pid {hb.get('pid')}")
    if hb.get("last_error"):
        print(f"  last cycle error: {hb['last_error']}")
    if not ok:
        print("  ^ no recent completed cycle — check `journalctl -u cryptera-monitor -n 100`; "
              "the systemd watchdog should already be restarting it.")


def main():
    files = glob.glob(os.path.join(SNAPSHOTS_DIR, _PREFIX + "*.json"))
    counts = defaultdict(int)
    latest = {}          # sym -> (sortkey, path)
    generations = defaultdict(int)
    for path in files:
        sym, key = _parse(os.path.basename(path))
        if sym is None:
            continue
        counts[sym] += 1
        if sym not in latest or key > latest[sym][0]:
            latest[sym] = (key, path)

    total = len(files)
    _print_liveness()
    print(f"Cryptera corpus: {total} snapshot(s) across {len(latest)} symbol(s)")
    print(f"  ({SNAPSHOTS_DIR})")
    if not latest:
        print("  no snapshots yet — is the monitor running? (systemctl status cryptera-monitor)")
        return

    # Mixed generations in one directory is the deploy-day failure mode: the archived
    # pre-fix corpus and the clean one must never share a folder (invariant 5).
    for path in files:
        try:
            with open(path) as f:
                generations[snapshot_generation(json.load(f)) or "<none>"] += 1
        except Exception:
            continue
    if len(generations) > 1:
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(generations.items()))
        print(f"  !! MIXED SCHEMA GENERATIONS in snapshots/ — {detail} (engine is "
              f"{CURRENT_GENERATION}).\n"
              f"     calibrate/eval_engine will EXCLUDE the others; archive them out.")

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
