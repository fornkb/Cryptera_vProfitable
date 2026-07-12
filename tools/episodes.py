"""Trade-episode identity + grouping — pseudo-replication fix for calibrate/eval_engine.

monitor.py's cheap pass scores every watched symbol every 15m, so ONE persisting setup
(same structural entry/SL/TP, unchanged for hours) produces many near-identical snapshot
rows. Counting each as an independent trade inflates sample sizes cluster-fold and biases
every win-rate/expectancy number downstream (tools.calibrate's weights/logistic AND
tools.eval_engine's reported stats) toward whatever that one persisting setup happened to
do, N times over. This module is the single source of truth for:

  * episode_id(snapshot)         — a stable fingerprint of the same PHYSICAL trade
  * collapse_to_episodes(items)  — one representative row per episode (the EARLIEST
                                   snapshot, i.e. the honest "setup first appeared" decision
                                   point), with a time-gap rule so a geometry that recurs
                                   after a long silence is a NEW episode.

The tolerances live HERE so they are tuned in one place once the corpus shows real episode
structure. (Ported from the sibling v4.x engine's identical M1 pseudo-replication fix —
this repo's snapshot shape already matches: `strategies.engine_trade` mirrored to a
top-level `engine_trade` key, `direction` "BUY"/"SELL", `generated_at` ISO-8601 UTC.)
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

# ~0.1-0.5% geometry tolerance: absorbs the small ATR-driven drift of SL/TP between
# consecutive 15m bars of one setup, while still separating genuinely distinct setups.
# First default -- validate against real within-episode drift once the corpus matures (raw
# entry/SL/TP stay on each snapshot precisely so this can be re-tuned WITHOUT re-labelling).
PRICE_SIG_FIGS = 3
# Same episode_id after a silence longer than this = a new episode (a level that recurs
# days later is a new trade, not the same one). Conservative-large by design: for the
# pseudo-replication bug, under-counting (merging) is far less harmful than the many-fold
# over-counting being removed.
EPISODE_GAP_HOURS = 24.0


def _sig(p, figs: int = PRICE_SIG_FIGS):
    """Round a price to `figs` significant figures -- a scale-invariant bucket, so BTC
    (~60k) and SOL (~80) group with the same relative tolerance."""
    if not isinstance(p, (int, float)) or p <= 0:
        return "na"
    return round(p, (figs - 1) - math.floor(math.log10(abs(p))))


def _side(direction) -> str:
    d = str(direction or "").lower()
    if d in ("buy", "long", "bullish", "bull"):
        return "long"
    if d in ("sell", "short", "bearish", "bear"):
        return "short"
    return "?"


def episode_id(snapshot: dict) -> str:
    """Same symbol + side + rounded engine geometry (entry/SL/TP) => the same physical trade."""
    s = snapshot.get("strategies") or {}
    et = snapshot.get("engine_trade") or s.get("engine_trade") or {}
    sym = snapshot.get("symbol") or "?"
    side = _side(et.get("direction") or s.get("setup_direction"))
    return "|".join(str(x) for x in
                    (sym, side, _sig(et.get("entry")), _sig(et.get("stop_loss")), _sig(et.get("take_profit"))))


def row_time(snapshot: dict, name: str = "") -> float:
    """Epoch seconds from the snapshot's UTC `generated_at` (fallback: the filename timestamp)."""
    g = snapshot.get("generated_at") if isinstance(snapshot, dict) else None
    if isinstance(g, str) and g.strip():
        s = g.strip()
        if s.endswith("Z"):
            s = s[:-1]  # datetime.fromisoformat (<3.11) rejects a trailing 'Z'
        try:
            dt = datetime.fromisoformat(s)
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    parts = name.replace(".json", "").split("_")
    if len(parts) >= 5:
        try:
            return datetime.strptime(f"{parts[-2]}{parts[-1]}", "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


def collapse_to_episodes(items, gap_hours: float = EPISODE_GAP_HOURS):
    """items: iterable of (name, snapshot, outcome). Returns ONE representative per episode --
    the EARLIEST snapshot of each contiguous same-id run (the honest 'setup first appeared'
    moment). A gap between consecutive same-id snapshots longer than `gap_hours` starts a NEW
    episode. Output is time-then-name sorted for stable, reproducible downstream ordering."""
    buckets: dict = defaultdict(list)
    for name, snap, outcome in items:
        buckets[episode_id(snap)].append((row_time(snap, name), name, snap, outcome))
    reps = []
    gap_s = gap_hours * 3600.0
    for rows in buckets.values():
        rows.sort(key=lambda r: r[0])
        prev_t = None
        for t, name, snap, outcome in rows:
            if prev_t is None or (t - prev_t) > gap_s:
                reps.append((name, snap, outcome))   # first snapshot of a new run = the episode
            prev_t = t
    reps.sort(key=lambda r: (row_time(r[1], r[0]), r[0]))
    return reps
