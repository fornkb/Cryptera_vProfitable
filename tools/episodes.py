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
# B23.0: episode identity is DISTANCE-based, not rounded-bucket. Significant-figure
# buckets have hard edges — verified: entries 104.4 and 104.6 (0.19% apart, one setup
# drifting with ATR) fell in different buckets and double-counted one physical trade,
# which re-inflates exactly the pseudo-replication this module exists to remove. A
# snapshot joins the open episode when symbol+side match and entry/SL/TP are ALL within
# this relative tolerance of that episode's representative geometry.
EPISODE_PRICE_TOL = 0.004
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


def _geometry(snapshot: dict):
    """(symbol, side, entry, stop, target) — the identity inputs, unrounded."""
    s = snapshot.get("strategies") or {}
    et = snapshot.get("engine_trade") or s.get("engine_trade") or {}
    return (snapshot.get("symbol") or "?",
            _side(et.get("direction") or s.get("setup_direction")),
            et.get("entry"), et.get("stop_loss"), et.get("take_profit"))


def _close(a, b, tol: float = EPISODE_PRICE_TOL) -> bool:
    """Relative closeness that treats a missing price as matching only another missing one."""
    if a is None or b is None:
        return a is None and b is None
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    return abs(a - b) <= max(abs(b), 1e-9) * tol


def same_episode(snap_a: dict, snap_b: dict, tol: float = EPISODE_PRICE_TOL) -> bool:
    """Same symbol+side and all three of entry/SL/TP within `tol` of each other (B23.0)."""
    sym_a, side_a, *geo_a = _geometry(snap_a)
    sym_b, side_b, *geo_b = _geometry(snap_b)
    if (sym_a, side_a) != (sym_b, side_b):
        return False
    return all(_close(x, y, tol) for x, y in zip(geo_a, geo_b))


def episode_id(snapshot: dict) -> str:
    """
    Coarse bucket key: symbol + side + rounded geometry. Kept for diagnostics and as the
    pre-filter for grouping; membership itself is decided by `same_episode` distance, so a
    setup drifting across a rounding boundary is no longer split in two.
    """
    sym, side, entry, sl, tp = _geometry(snapshot)
    return "|".join(str(x) for x in (sym, side, _sig(entry), _sig(sl), _sig(tp)))


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


def collapse_to_episodes(items, gap_hours: float = EPISODE_GAP_HOURS,
                         tol: float = EPISODE_PRICE_TOL):
    """items: iterable of (name, snapshot, outcome). Returns ONE representative per episode --
    the EARLIEST snapshot of each run (the honest 'setup first appeared' decision point).

    B23.0: rows are grouped per (symbol, side) and walked chronologically against the OPEN
    episode's representative geometry rather than bucketed by a rounded key, so a setup
    whose levels drift a few tenths of a percent with ATR stays ONE trade instead of
    splitting at a rounding boundary. A snapshot starts a new episode when its geometry is
    outside `tol` of the representative, or when it arrives more than `gap_hours` after the
    previous snapshot of that episode (a level recurring days later is a new trade).

    Output is time-then-name sorted for stable, reproducible downstream ordering."""
    buckets: dict = defaultdict(list)
    for name, snap, outcome in items:
        sym, side, *_ = _geometry(snap)
        buckets[(sym, side)].append((row_time(snap, name), name, snap, outcome))

    reps = []
    gap_s = gap_hours * 3600.0
    for rows in buckets.values():
        rows.sort(key=lambda r: r[0])
        open_rep, prev_t = None, None
        for t, name, snap, outcome in rows:
            if (open_rep is None or (t - prev_t) > gap_s
                    or not same_episode(snap, open_rep, tol)):
                reps.append((name, snap, outcome))
                open_rep = snap
            prev_t = t
    reps.sort(key=lambda r: (row_time(r[1], r[0]), r[0]))
    return reps
