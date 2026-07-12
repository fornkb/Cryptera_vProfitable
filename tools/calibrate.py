"""
Calibrate the confluence engine from labelled snapshots (T1-8 + BUG-12 v2).

Reads every snapshot that has an `outcome.engine_trade_outcome` (produced by
tools.label_snapshots) and emits `data/calibration.json` containing:

  * weights      : per-component multipliers for the trend (C1-C8) rubric, derived
                   from each component's edge (how much more of its points land on
                   winners than on the average trade), normalised to mean 1.0 so
                   the score range stays ~0-100.
  * mr_weights   : same for the mean-revert (M1-M6) rubric.
  * score_to_winrate : empirical TP-before-SL hit-rate per score bucket.
  * logistic     : {trend|mean_revert: {intercept, coef, mean, std}} — a
                   standardized logistic model over the breakdown components that
                   yields a calibrated win probability per component mix (BUG-12).
                   The engine (strategies._logistic_winrate) prefers this over the
                   coarse score bucket when present.

Calibration v2 upgrades (BUG-12):
  * Timeouts are no longer discarded — a trade that timed out gets partial MFE
    credit (how far toward its target it travelled), so rubrics that produce slow
    winners are not penalised as losses.
  * Overlapping snapshots (same symbol within the same hour) share nearly identical
    forward windows, so each is weighted by 1/(cluster size) — one market outcome
    is counted once, not many times.

The engine loads this file when present and otherwise runs at equal weights. Safe
with few samples: it refuses to emit weights/logistic below a minimum support and
only writes score buckets that have enough trades.

Usage:
    python -m tools.calibrate                 # write data/calibration.json
    python -m tools.calibrate --min-samples 200 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(ROOT_DIR, "snapshots")
OUT_PATH = os.path.join(ROOT_DIR, "data", "calibration.json")

TREND_KEYS = ["c1_trend_alignment", "c2_ob_proximity", "c3_liquidity_sweep", "c4_momentum",
              "c5_fvg_magnet", "c6_ote_bonus", "c7_cvd_alignment", "c8_stochrsi"]
MR_KEYS = ["m1_edge_distance", "m2_edge_sweep", "m3_cvd_absorption",
           "m4_stoch_extreme", "m5_rejection", "m6_range_intact"]

DEFAULT_TARGET_RR = 2.0  # timeout MFE credit is realized_rr / target_rr, capped at 1.0


def _iter_labelled():
    if not os.path.isdir(SNAPSHOTS_DIR):
        return
    for name in sorted(os.listdir(SNAPSHOTS_DIR)):
        if not (name.startswith("snapshot_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(SNAPSHOTS_DIR, name)) as f:
                snap = json.load(f)
        except Exception:
            continue
        outcome = (snap.get("outcome") or {}).get("engine_trade_outcome")
        # Include timeouts (BUG-12): they get partial credit rather than exclusion.
        if outcome and outcome.get("triggered") and outcome.get("outcome") in ("tp_first", "sl_first", "timeout"):
            yield name, snap, outcome


def _soft_label(snap: dict, outcome: dict) -> float:
    """
    Win-ness in [0,1]. Clean fills are 1/0; a timeout gets MFE-based partial credit
    = min(1, realized_rr / target_rr) — how far toward its target it ran before the
    horizon closed (BUG-12).
    """
    o = outcome.get("outcome")
    if o == "tp_first":
        return 1.0
    if o == "sl_first":
        return 0.0
    # timeout
    eng = snap.get("engine_trade") or (snap.get("strategies") or {}).get("engine_trade") or {}
    target_rr = float(eng.get("rr") or DEFAULT_TARGET_RR) or DEFAULT_TARGET_RR
    rrr = outcome.get("realized_rr")
    if rrr is None:
        return 0.0
    return max(0.0, min(1.0, float(rrr) / target_rr))


def _snap_hour_key(name: str, snap: dict):
    """(symbol, UTC-hour) key for de-dup weighting of overlapping forward windows."""
    symbol = snap.get("symbol") or "?"
    gen = snap.get("generated_at")
    hour = None
    if gen:
        try:
            dt = datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(timezone.utc)
            hour = dt.strftime("%Y%m%d%H")
        except ValueError:
            hour = None
    if hour is None:
        parts = name.replace(".json", "").split("_")
        hour = (parts[-2] + parts[-1][:2]) if len(parts) >= 5 else name
    return (symbol, hour)


def _component_weights(rows, keys):
    """
    rows: list of (breakdown, y_soft, w). Weight each component by the ratio of its
    (weighted) mean points on winners vs its mean points on all trades, normalised
    to mean 1.0. `y_soft` and sample weight `w` both feed the winner aggregate so
    timeouts contribute proportional credit. Non-discriminating components stay ~1.
    """
    win_sum = defaultdict(float)
    all_sum = defaultdict(float)
    win_w = 0.0
    all_w = 0.0
    for bd, y, w in rows:
        all_w += w
        win_w += w * y
        for k in keys:
            v = float(bd.get(k, 0) or 0)
            all_sum[k] += w * v
            win_sum[k] += w * y * v
    if all_w <= 0 or win_w <= 0:
        return {}
    raw = {}
    for k in keys:
        mean_all = all_sum[k] / all_w
        mean_win = win_sum[k] / win_w
        raw[k] = (mean_win + 1e-6) / (mean_all + 1e-6)
    mean_w = sum(raw.values()) / len(raw)
    if mean_w <= 0:
        return {}
    return {k: round(max(0.5, min(1.5, v / mean_w)), 4) for k, v in raw.items()}


def _fit_logistic(rows, keys, l2=1.0, iters=800, lr=0.3):
    """
    Standardized weighted logistic regression (pure numpy, no sklearn dependency).
    rows: (breakdown, y_soft, w). Returns {intercept, coef{key:w}, mean{}, std{}} or
    None if the sample is degenerate. Features are z-scored so coefficients are
    comparable and the stored mean/std let the engine standardize at inference.
    """
    if len(rows) < len(keys) + 2:
        return None
    X = np.array([[float(bd.get(k, 0) or 0) for k in keys] for bd, _, _ in rows], dtype=float)
    y = np.array([y for _, y, _ in rows], dtype=float)
    w = np.array([w for _, _, w in rows], dtype=float)
    if w.sum() <= 0:
        return None
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std_safe = np.where(std > 1e-9, std, 1.0)
    Z = (X - mean) / std_safe
    n, d = Z.shape
    beta = np.zeros(d)
    b0 = float(np.average(y, weights=w))
    b0 = np.log((b0 + 1e-6) / (1 - b0 + 1e-6))  # init at base-rate logit
    wsum = w.sum()
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(b0 + Z @ beta)))
        err = w * (p - y)
        grad_beta = (Z.T @ err) / wsum + (l2 / n) * beta
        grad_b0 = err.sum() / wsum
        beta -= lr * grad_beta
        b0 -= lr * grad_b0
    if not (np.all(np.isfinite(beta)) and np.isfinite(b0)):
        return None
    return {
        "intercept": round(float(b0), 6),
        "coef": {k: round(float(v), 6) for k, v in zip(keys, beta)},
        "mean": {k: round(float(v), 6) for k, v in zip(keys, mean)},
        "std": {k: round(float(v), 6) for k, v in zip(keys, std_safe)},
    }


def _score_buckets(rows, edges=(0, 45, 60, 75, 101)):
    """rows: (score, y_soft, w). Weighted expected win-rate per bucket."""
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = [(y, w) for sc, y, w in rows if lo <= sc < hi]
        if not sub:
            continue
        tot_w = sum(w for _, w in sub)
        if tot_w <= 0:
            continue
        wr = sum(y * w for y, w in sub) / tot_w
        out.append({"min": lo, "max": hi - 1, "n": len(sub), "win_rate": round(wr, 3)})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-samples", type=int, default=150,
                   help="minimum trades per rubric before emitting weights/logistic")
    p.add_argument("--min-bucket", type=int, default=20,
                   help="minimum trades per score bucket before emitting its win-rate")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # First pass: collect, then compute de-dup weights per (symbol, hour) cluster.
    collected = list(_iter_labelled())
    cluster_counts = defaultdict(int)
    for name, snap, _ in collected:
        cluster_counts[_snap_hour_key(name, snap)] += 1

    trend_rows, mr_rows = [], []      # (breakdown, y_soft, w)
    score_rows = []                   # (score, y_soft, w)
    for name, snap, outcome in collected:
        strat = snap.get("strategies") or {}
        # FA-02: fit on the RAW (pre-weight, pre-cap) components — fitting on the
        # weighted breakdown feeds each refit its own previous output (compounding
        # drift) and mismatches _logistic_winrate's raw-feature inference. The
        # plain-breakdown fallback covers legacy (< 3.3.1) snapshots only, where
        # the two are identical as long as no calibration weights were active.
        bd = strat.get("confluence_breakdown_raw") or strat.get("confluence_breakdown") or {}
        y = _soft_label(snap, outcome)
        w = 1.0 / cluster_counts[_snap_hour_key(name, snap)]
        score = int(strat.get("confluence_score") or 0)
        score_rows.append((score, y, w))
        if strat.get("score_mode") == "mean_revert":
            mr_rows.append((bd, y, w))
        else:
            trend_rows.append((bd, y, w))

    n_total = len(score_rows)
    print(f"Labelled engine trades found: {n_total}  (trend={len(trend_rows)}, mean_revert={len(mr_rows)})")

    calib = {"weights": {}, "mr_weights": {}, "score_to_winrate": [], "logistic": {}}
    if len(trend_rows) >= args.min_samples:
        calib["weights"] = _component_weights(trend_rows, TREND_KEYS)
        lg = _fit_logistic(trend_rows, TREND_KEYS)
        if lg:
            calib["logistic"]["trend"] = lg
        print(f"  trend weights + logistic fitted on {len(trend_rows)} trades")
    else:
        print(f"  trend weights skipped (need {args.min_samples}, have {len(trend_rows)})")
    if len(mr_rows) >= args.min_samples:
        calib["mr_weights"] = _component_weights(mr_rows, MR_KEYS)
        lg = _fit_logistic(mr_rows, MR_KEYS)
        if lg:
            calib["logistic"]["mean_revert"] = lg
        print(f"  mean-revert weights + logistic fitted on {len(mr_rows)} trades")
    else:
        print(f"  mean-revert weights skipped (need {args.min_samples}, have {len(mr_rows)})")

    buckets = [b for b in _score_buckets(score_rows) if b["n"] >= args.min_bucket]
    calib["score_to_winrate"] = buckets
    if buckets:
        print("  score->winrate:")
        for b in buckets:
            print(f"    {b['min']:>3}-{b['max']:<3}  n={b['n']:<4} win_rate={b['win_rate']}")
    else:
        print(f"  no score buckets with >= {args.min_bucket} trades yet")

    if args.dry_run:
        print("\n[dry-run] would write:")
        print(json.dumps(calib, indent=2))
        return

    if not (calib["weights"] or calib["mr_weights"] or calib["score_to_winrate"] or calib["logistic"]):
        print("\nNothing to calibrate yet — not writing the file (engine stays at equal weights).")
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(calib, f, indent=2)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
