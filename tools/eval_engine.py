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
    `by_selected_from_engine`, and the I1.0 `by_verdict`) so the LLM-vs-engine
    divergence question stays answerable, but it is NOT the loop's primary
    universe (it used to be, which reported `snapshots_labelled: 0` against a
    corpus that's almost entirely no-LLM snapshots).

`by_verdict` (I1.0) pairs the adjudicated plan against the engine primary on the same
narrated snapshots: AGREE/UPDATE carry a `paired_delta_r` (does adjudication add alpha?),
REJECT carries the engine primary's outcome had the declined trade been taken (rejection
quality). Promotion of the adjudicated plan to headline is Wave-4 measurement-gated.

Pseudo-replication fix (tools.episodes): the cheap pass scores every watched symbol
every 15m, so ONE persisting setup (same entry/SL/TP unchanged for hours) produces many
near-identical `engine_trade_outcome` rows. Before bucketing, these are collapsed to one
representative episode by `tools.episodes.collapse_to_episodes` (symbol+side, geometry
within a relative tolerance of the open episode's representative -- B23.0 distance-based) --
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
    from tools.corpus import GenerationFilter, add_cli_flag, CURRENT_GENERATION
except ImportError:  # allow running as `python tools/eval_engine.py` (no package context)
    from corpus import GenerationFilter, add_cli_flag, CURRENT_GENERATION

try:
    from tools.episodes import collapse_to_episodes
except ImportError:  # allow running as `python tools/eval_engine.py` (no package context)
    from episodes import collapse_to_episodes

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(ROOT_DIR, "snapshots")

# Round-trip fees + slippage as a fraction of 1R, subtracted from every triggered trade's
# realized R (B15.0). A conservative placeholder for liquid USDT-perps; a fitted cost model
# is Wave-4 (I9.0). Keep small so it documents costs without dominating the economics.
ROUND_TRIP_COST_R = 0.02


def _realized_r(outcome: dict, target_rr):
    """Realized economics in R for one triggered outcome (B15.0), NOT an excursion:
    +target_rr on tp_first, -1R on sl_first, the terminal end_rr on a timeout. Returns None
    for intrabar_unknown / missing data (excluded from the realized-R average, never a win).
    Fees+slippage are charged once here."""
    o = outcome.get("outcome")
    if o == "tp_first":
        r = float(target_rr) if target_rr is not None else None
    elif o == "sl_first":
        r = -1.0
    elif o == "timeout":
        er = outcome.get("end_rr")
        r = float(er) if er is not None else None
    else:  # intrabar_unknown / not_triggered / no_data
        r = None
    return None if r is None else (r - ROUND_TRIP_COST_R)


def _iter_snapshots(symbol: str | None, gen_filter=None) -> Iterable[tuple[str, dict]]:
    """Yield (name, snap) for the current corpus generation only.

    Pooling generations here does not corrupt a fit, but it silently corrupts every
    metric this tool reports -- and those metrics are what Wave 4 promotion decisions
    are made on. Same guard as calibrate (invariant 5), defaults ON.
    """
    if gen_filter is None:
        gen_filter = GenerationFilter()
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
                snap = json.load(f)
        except Exception:
            continue
        if not gen_filter.accept(snap):
            continue
        yield name, snap


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
        "sum_mfe_r": 0.0,     # sum of the MFE excursion in R (informational, not PnL)
        "mfe_r_n": 0,
        "sum_realized_r": 0.0,  # sum of realized economics in R (B15.0 -- the headline)
        "realized_r_n": 0,
    }


def _record(acc, outcome, target_rr=None):
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
            # B1.0: entry-vs-exit order unknowable even at 1m. A distinct class, kept out of
            # the timeout bucket and out of realized-R (excluded by _realized_r).
            acc["intrabar_unknown"] += 1
        else:
            acc["timeout"] += 1
        if outcome.get("mfe_pct") is not None:
            acc["sum_mfe"] += float(outcome["mfe_pct"])
        if outcome.get("mae_pct") is not None:
            acc["sum_mae"] += float(outcome["mae_pct"])
        if outcome.get("mfe_r") is not None:
            acc["sum_mfe_r"] += float(outcome["mfe_r"])
            acc["mfe_r_n"] += 1
        r = _realized_r(outcome, target_rr)
        if r is not None:
            acc["sum_realized_r"] += r
            acc["realized_r_n"] += 1


def _finalize(acc):
    n = max(acc["n"], 1)
    triggered = max(acc["triggered"], 1)
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
        # informational excursion (renamed from the misleading avg_realized_rr, B4.0)
        "avg_mfe_r": round(acc["sum_mfe_r"] / acc["mfe_r_n"], 3) if acc["mfe_r_n"] else None,
        # B15.0 headline: realized economics in R over decisive trades (tp/sl/timeout),
        # net of fees+slippage. Replaces the excursion-based "expectancy".
        "decisive": acc["realized_r_n"],
        "expectancy_r": round(acc["sum_realized_r"] / acc["realized_r_n"], 3) if acc["realized_r_n"] else None,
    }


def _plan_rr(td: dict):
    """The LLM plan's own R:R ratio (validator-recomputed) — the realized-R target for the
    geometry the model actually chose. In the analysis schema `trade_decision.rr` is a dict
    {ratio, ...}; fall back to a bare number for older shapes."""
    rr = (td or {}).get("rr")
    return rr.get("ratio") if isinstance(rr, dict) else rr


def _by_verdict(verdict_rows):
    """I1.0 by_verdict cut (NOT episode-collapsed -- narration is already gated + cooldown'd):
      * AGREE / UPDATE — the adjudicated plan's realized R (trade_decision_outcome) PAIRED
        against the engine primary's (engine_trade_outcome) on the SAME narrated snapshots.
        `paired_delta_r` = llm expectancy_r - engine primary expectancy_r is the direct measure
        of whether adjudication adds alpha (UPDATE is the verdict that should).
      * REJECT — the engine primary's outcome had the declined trade been TAKEN, i.e. rejection
        quality: how often the avoided trade would have lost.
    Promotion of the adjudicated plan to headline is measurement-gated (Wave 4, needs the clean
    corpus + >=50 UPDATE episodes with a non-negative paired delta); this only builds the readout."""
    llm_acc = defaultdict(_accumulator)
    eng_acc = defaultdict(_accumulator)
    counts = defaultdict(int)
    for _name, snap, eng_o, llm_o, verdict in verdict_rows:
        counts[verdict] += 1
        strategies = snap.get("strategies") or {}
        eng = snap.get("engine_trade") or strategies.get("engine_trade") or {}
        td = (snap.get("analysis") or {}).get("trade_decision") or {}
        if verdict in ("AGREE", "UPDATE"):
            if llm_o is not None:
                _record(llm_acc[verdict], llm_o, _plan_rr(td))
            if eng_o is not None:
                _record(eng_acc[verdict], eng_o, eng.get("rr"))
        elif verdict == "REJECT" and eng_o is not None:
            _record(eng_acc[verdict], eng_o, eng.get("rr"))

    out = {}
    for verdict in ("AGREE", "UPDATE"):
        if not counts.get(verdict):
            continue
        llm_s, eng_s = _finalize(llm_acc[verdict]), _finalize(eng_acc[verdict])
        delta = (round(llm_s["expectancy_r"] - eng_s["expectancy_r"], 3)
                 if llm_s["expectancy_r"] is not None and eng_s["expectancy_r"] is not None
                 else None)
        out[verdict] = {"decisions": counts[verdict], "llm": llm_s,
                        "engine_primary": eng_s, "paired_delta_r": delta}
    if counts.get("REJECT"):
        out["REJECT"] = {"decisions": counts["REJECT"],
                         "engine_primary_if_taken": _finalize(eng_acc["REJECT"])}
    return out


def _expectancy(stats):
    """DEPRECATED excursion expectancy (avg_mfe*hit + avg_mae*(1-hit)) — kept for reference
    only. The trustworthy figure is stats['expectancy_r'] (realized R, B15.0)."""
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
    add_cli_flag(p)
    args = p.parse_args()
    gen_filter = GenerationFilter(allow_mixed=args.allow_mixed_schema)

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
    verdict_rows = []      # (name, snap, engine_outcome, llm_outcome, verdict) -- I1.0 by_verdict
    for name, snap in _iter_snapshots(args.symbol, gen_filter):
        seen += 1
        outcome_block = snap.get("outcome") or {}
        engine_outcome = outcome_block.get("engine_trade_outcome")
        llm_outcome = outcome_block.get("trade_decision_outcome")
        if engine_outcome is not None:
            raw_engine_rows.append((name, snap, engine_outcome))
        if llm_outcome is not None:
            llm_rows.append((name, snap, llm_outcome))
        # by_verdict spans narrated snapshots regardless of which universe got labelled: a
        # REJECT is a HOLD (no trade_decision_outcome) but its engine_trade_outcome measures
        # rejection quality.
        verdict = ((snap.get("analysis") or {}).get("verdict") or "").upper()
        if verdict in ("AGREE", "UPDATE", "REJECT"):
            verdict_rows.append((name, snap, engine_outcome, llm_outcome, verdict))

    # Pseudo-replication fix: one persisting setup scored every 15m yields many
    # near-identical rows here -- collapse to one representative per episode BEFORE
    # any bucket counts them (see module docstring).
    episodes = collapse_to_episodes(raw_engine_rows)
    n_raw_rows = len(raw_engine_rows)

    for name, snap, engine_outcome in episodes:
        strategies = snap.get("strategies") or {}

        # B15.0: the ENGINE universe reads strategies.* EXCLUSIVELY — never header.*. The
        # header carries the LLM's copies (incl. any score_override), so preferring it moved
        # engine outcomes into the wrong score/bias/gate bucket and corrupted the very
        # score->win-rate curve thresholds are tuned from.
        score = int(strategies.get("confluence_score") or 0)
        mode = strategies.get("score_mode") or "unknown"
        # Bucket by the TRADED direction (setup_direction): an MR fade's direction is often
        # the opposite of trend_bias, so by_bias on trend_bias mislabelled the whole
        # mean-revert universe's long/short cut. Fall back to trend_bias when neutral.
        direction = (strategies.get("setup_direction")
                     or strategies.get("trend_bias") or "neutral").upper()
        gate = (strategies.get("volume_gate") or {}).get("state") or "CLEAR"
        vr_15m = (snap.get("volatility_regime") or {}).get("15m", {}).get("regime", "unknown")
        event_active = "active" if (snap.get("event_guard") or {}).get("active") else "clear"
        eng = snap.get("engine_trade") or strategies.get("engine_trade") or {}
        target_rr = eng.get("rr")

        _record(buckets["overall"], engine_outcome, target_rr)
        _record(buckets["by_score"][_bucket_score(score)], engine_outcome, target_rr)
        _record(buckets["by_mode"][mode], engine_outcome, target_rr)
        _record(buckets["by_bias"][direction], engine_outcome, target_rr)
        _record(buckets["by_vol_regime_15m"][vr_15m], engine_outcome, target_rr)
        _record(buckets["by_event_guard"][event_active], engine_outcome, target_rr)
        _record(buckets["by_volume_gate"][gate], engine_outcome, target_rr)

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
        td = analysis.get("trade_decision") or {}
        tm = td.get("trade_management") or {}
        sel = tm.get("selected_from_engine")
        sel_key = "engine_primary" if sel is True else ("llm_alternative" if sel is False else "unknown")
        # realized R for the LLM trade uses the LLM plan's own rr ratio (its geometry may
        # differ from the engine primary when it selected an alternative candidate).
        # trade_decision.rr is a {ratio,...} dict in the analysis schema -- _plan_rr unwraps it
        # (the old `td.get("rr")` fallback passed the raw dict to _realized_r -> float(dict) crash).
        llm_rr = _plan_rr(td)
        _record(buckets["by_action"][action], llm_outcome, llm_rr)
        _record(buckets["by_selected_from_engine"][sel_key], llm_outcome, llm_rr)

    report = {
        "snapshots_scanned": seen,
        # Invariant 5 provenance: which engine generation these numbers describe, and
        # how many rows were held out of them. A metric with no generation stamp is
        # not interpretable once a schema bump has happened.
        "schema_generation": CURRENT_GENERATION,
        "excluded_other_generations": gen_filter.rejected,
        "excluded_detail": {"schema": gen_filter.by_schema, "label": gen_filter.by_label},
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

    # I1.0: paired AGREE/UPDATE vs engine primary + REJECT rejection quality.
    report["by_verdict"] = _by_verdict(verdict_rows)

    import sys as _sys
    gen_filter.print_report(_sys.stderr)   # keep stdout pure JSON for piping

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Wrote {args.out}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
