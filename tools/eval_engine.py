"""
Aggregate labelled snapshots into per-bucket performance stats.

After running tools.label_snapshots, each snapshot file carries an `outcome`
block with two independent universes (FA-06):
  * `engine_trade_outcome` — the deterministic-geometry label, present on
    ANY valid-engine-trade snapshot (the ~99% no-LLM cheap-pass collector
    corpus included). This is the PRIMARY universe: overall hit rate,
    expectancy, avg MFE/MAE, and every `by_*` bucket below are computed on it.
  * `trade_decision_outcome` — the LLM's selected trade, present only on
    narrated snapshots. Kept as secondary cuts (`by_action`,
    `by_selected_from_engine`) so the LLM-vs-engine divergence question stays
    answerable, but it is NOT the loop's primary universe (it used to be,
    which reported `snapshots_labelled: 0` against a corpus that's almost
    entirely no-LLM snapshots).

Pseudo-replication fix (tools.episodes): the cheap pass scores every watched symbol
every 15m, so ONE persisting setup (same entry/SL/TP unchanged for hours) produces many
near-identical `engine_trade_outcome` rows. Before bucketing, these are collapsed to one
representative episode per `tools.episodes.episode_id` (symbol+side+rounded geometry) --
otherwise a single setup's outcome gets counted N times and every hit-rate/expectancy
number below is biased toward whatever that one setup happened to do. `engine_universe_raw_rows`
reports the pre-collapse count so the dedup ratio is visible. The LLM-secondary cuts are
NOT collapsed -- see the note at their loop below.

Buckets (engine-primary universe):
  * overall hit rate, expectancy, average MFE/MAE
  * stats by score bucket (45-59 conditional, 60-74 active, 75+ high-conviction)
  * stats by score_mode (trending / ranging rubric)
  * stats by trend bias (bullish / bearish)
  * stats by volatility regime
  * stats by event_guard.active state
  * stats by volume_gate

Usage:
    python -m tools.eval_engine
    python -m tools.eval_engine --symbol BTC/USDT
    python -m tools.eval_engine --out reports/eval_2026q2.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Iterable

try:
    from tools.episodes import collapse_to_episodes
except ImportError:  # allow running as `python tools/eval_engine.py` (no package context)
    from episodes import collapse_to_episodes

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(ROOT_DIR, "snapshots")


def _iter_snapshots(symbol: str | None) -> Iterable[tuple[str, dict]]:
    for name in sorted(os.listdir(SNAPSHOTS_DIR)):
        if not name.startswith("snapshot_") or not name.endswith(".json"):
            continue
        if symbol:
            parts = name.split("_")
            if len(parts) < 3 or f"{parts[1]}/{parts[2]}" != symbol:
                continue
        path = os.path.join(SNAPSHOTS_DIR, name)
        try:
            with open(path) as f:
                yield name, json.load(f)
        except Exception:
            continue


def _bucket_score(score: int) -> str:
    if score >= 75:
        return "75+"
    if score >= 60:
        return "60-74"
    if score >= 45:
        return "45-59"
    return "<45"


def _accumulator():
    return {
        "n": 0,
        "triggered": 0,
        "tp": 0,
        "sl": 0,
        "timeout": 0,
        "intrabar_unknown": 0,
        "no_data": 0,
        "sum_mfe": 0.0,
        "sum_mae": 0.0,
        "sum_rr": 0.0,
        "rr_n": 0,
    }


def _record(acc, outcome):
    acc["n"] += 1
    if not outcome:
        return
    if outcome.get("outcome") == "no_data":
        acc["no_data"] += 1
        return
    if outcome.get("triggered"):
        acc["triggered"] += 1
        o = outcome.get("outcome")
        if o == "tp_first":
            acc["tp"] += 1
        elif o == "sl_first":
            acc["sl"] += 1
        elif o == "intrabar_unknown":
            # B1.0: entry-vs-exit order unknowable even at 1m. A distinct class, kept
            # out of the timeout bucket; realized-R treatment lands with B15.0.
            acc["intrabar_unknown"] += 1
        else:
            acc["timeout"] += 1
        if outcome.get("mfe_pct") is not None:
            acc["sum_mfe"] += float(outcome["mfe_pct"])
        if outcome.get("mae_pct") is not None:
            acc["sum_mae"] += float(outcome["mae_pct"])
        if outcome.get("realized_rr") is not None:
            acc["sum_rr"] += float(outcome["realized_rr"])
            acc["rr_n"] += 1


def _finalize(acc):
    n = max(acc["n"], 1)
    triggered = max(acc["triggered"], 1)
    rr_n = max(acc["rr_n"], 1)
    return {
        "snapshots": acc["n"],
        "triggered": acc["triggered"],
        "tp_first": acc["tp"],
        "sl_first": acc["sl"],
        "timeout": acc["timeout"],
        "intrabar_unknown": acc["intrabar_unknown"],
        "no_data": acc["no_data"],
        "trigger_rate": round(acc["triggered"] / n, 3),
        "hit_rate": round(acc["tp"] / triggered, 3) if acc["triggered"] else None,
        "avg_mfe_pct": round(acc["sum_mfe"] / triggered, 3) if acc["triggered"] else None,
        "avg_mae_pct": round(acc["sum_mae"] / triggered, 3) if acc["triggered"] else None,
        "avg_realized_rr": round(acc["sum_rr"] / rr_n, 3) if acc["rr_n"] else None,
    }


def _expectancy(stats):
    """Naive expectancy in pct (avg_mfe * hit_rate + avg_mae * (1 - hit_rate))."""
    hr = stats.get("hit_rate")
    mfe = stats.get("avg_mfe_pct")
    mae = stats.get("avg_mae_pct")
    if hr is None or mfe is None or mae is None:
        return None
    return round(hr * mfe + (1 - hr) * mae, 3)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    buckets = {
        # Engine-primary universe (FA-06) — the collector corpus.
        "overall": _accumulator(),
        "by_score": defaultdict(_accumulator),
        "by_mode": defaultdict(_accumulator),
        "by_bias": defaultdict(_accumulator),
        "by_vol_regime_15m": defaultdict(_accumulator),
        "by_event_guard": defaultdict(_accumulator),
        "by_volume_gate": defaultdict(_accumulator),
        # LLM secondary cuts — narrated snapshots only.
        "by_action": defaultdict(_accumulator),
        # Does the LLM's candidate-picking add or subtract alpha vs the engine
        # primary? Hit-rate split on trade_management.selected_from_engine.
        "by_selected_from_engine": defaultdict(_accumulator),
    }

    seen = 0
    raw_engine_rows = []   # (name, snap, outcome) -- collapsed to episodes below
    llm_rows = []          # (name, snap, outcome) -- NOT collapsed, see note below
    for name, snap in _iter_snapshots(args.symbol):
        seen += 1
        outcome_block = snap.get("outcome") or {}
        engine_outcome = outcome_block.get("engine_trade_outcome")
        llm_outcome = outcome_block.get("trade_decision_outcome")
        if engine_outcome is not None:
            raw_engine_rows.append((name, snap, engine_outcome))
        if llm_outcome is not None:
            llm_rows.append((name, snap, llm_outcome))

    # Pseudo-replication fix: one persisting setup scored every 15m yields many
    # near-identical rows here -- collapse to one representative per episode BEFORE
    # any bucket counts them (see module docstring).
    episodes = collapse_to_episodes(raw_engine_rows)
    n_raw_rows = len(raw_engine_rows)

    for name, snap, engine_outcome in episodes:
        strategies = snap.get("strategies") or {}
        analysis = snap.get("analysis") or {}
        header = analysis.get("header") or {}

        # header.* is only populated on narrated snapshots; strategies.* always
        # is (cheap pass writes it too), so every field below tolerates a
        # no-LLM snapshot via the `or` fallback.
        score = int(header.get("score") or strategies.get("confluence_score") or 0)
        mode = strategies.get("score_mode") or "unknown"
        bias = (header.get("bias") or (strategies.get("trend_bias") or "neutral").upper())
        gate = (header.get("volume_gate") or (strategies.get("volume_gate") or {}).get("state") or "CLEAR")
        vr_15m = (snap.get("volatility_regime") or {}).get("15m", {}).get("regime", "unknown")
        event_active = "active" if (snap.get("event_guard") or {}).get("active") else "clear"

        _record(buckets["overall"], engine_outcome)
        _record(buckets["by_score"][_bucket_score(score)], engine_outcome)
        _record(buckets["by_mode"][mode], engine_outcome)
        _record(buckets["by_bias"][bias], engine_outcome)
        _record(buckets["by_vol_regime_15m"][vr_15m], engine_outcome)
        _record(buckets["by_event_guard"][event_active], engine_outcome)
        _record(buckets["by_volume_gate"][gate], engine_outcome)

    # LLM-secondary cuts: NOT episode-collapsed. tools.episodes.episode_id fingerprints
    # the ENGINE's primary geometry; when selected_from_engine is False the LLM chose a
    # DIFFERENT candidate (different entry/SL/TP), so collapsing on the engine's id would
    # wrongly merge distinct LLM decisions. This universe is also naturally far less
    # duplicated already: narration only fires on a gated ACTIVE transition plus a
    # per-symbol cooldown (alerts.controller / Cfg.min_realert_s, default 2h), not every
    # 15m cycle like the engine-primary universe.
    for name, snap, llm_outcome in llm_rows:
        analysis = snap.get("analysis") or {}
        header = analysis.get("header") or {}
        action = header.get("action") or "HOLD"
        tm = (analysis.get("trade_decision") or {}).get("trade_management") or {}
        sel = tm.get("selected_from_engine")
        sel_key = "engine_primary" if sel is True else ("llm_alternative" if sel is False else "unknown")
        _record(buckets["by_action"][action], llm_outcome)
        _record(buckets["by_selected_from_engine"][sel_key], llm_outcome)

    report = {
        "snapshots_scanned": seen,
        # FA-06: reported explicitly and separately — the engine universe is
        # the whole collector corpus, the LLM universe is narrated-only and
        # will be much smaller (budget-gated).
        "engine_universe_labelled": len(episodes),      # collapsed episode count
        "engine_universe_raw_rows": n_raw_rows,         # pre-collapse snapshot rows (dedup honesty metric)
        "llm_universe_labelled": len(llm_rows),
        "overall": _finalize(buckets["overall"]),
    }
    report["overall"]["expectancy_pct"] = _expectancy(report["overall"])

    for k, v in buckets.items():
        if k == "overall":
            continue
        bucket_report = {}
        for sub_k, acc in v.items():
            s = _finalize(acc)
            s["expectancy_pct"] = _expectancy(s)
            bucket_report[sub_k] = s
        report[k] = bucket_report

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Wrote {args.out}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
