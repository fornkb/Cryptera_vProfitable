# Cryptera — Current Status & Remediation Plan

Source of truth: `audit_report.md` (2026-07-11 audit of v3.2.3). Reference bugs
by ID (BUG-01…BUG-24); do not restate them here. Each phase runs in its **own
Claude Code session**; the agent must read `CLAUDE.md` + this file first, work
only its phase's scope, tick checkboxes here, and commit at the end.

## Current state (2026-07-11)

- v3.2.3 on `main` (cb016d7). Architecture is sound ("rules decide, LLM
  narrates") but the audit found **7 P0 bugs**: spot/futures data mixing breaks
  CVD (BUG-01), EMA_200 permanently NaN so regime collapses to "Ranging"
  (BUG-02), timezone-shifted outcome labels corrupt all calibration/eval history
  (BUG-03), OI history silently dead (BUG-04), long-only OTE (BUG-05),
  widest-pool stop-losses (BUG-06), schema-forced C1-C8 hallucination in
  mean-revert mode (BUG-07).
- Consequence: **all existing labels, `data/calibration.json`, and eval numbers
  are untrustworthy** until Phase 1 completes and everything is relabeled.
- `CLAUDE.md` regenerated 2026-07-11 from README + audit. No unit tests exist yet.

## Ground rules (all phases)

- Phase order is strict: 1 → 2 → 3. Phase 2 measures against the Phase 1
  baseline; improving on corrupted labels optimizes the wrong thing.
- Stay within SMC/PA — no orderflow pivot.
- Never violate the invariants in `CLAUDE.md` (deterministic geometry, LLM
  selects-not-invents, closed candles, UTC everywhere).
- Verify deterministic code with synthetic candles / offline scripts, not live
  Gemini runs. One end-to-end smoke run per phase at most.

## Token-optimization guidelines (common to all phases)

Accuracy is never traded for tokens — these cut waste, not diligence.

1. **Locate before reading.** Grep for the symbol/bug's function first; Read only
   the relevant line range (audit gives `file:line` for every bug). Never read a
   whole file when a 60-line window suffices; never re-read a file just edited.
2. **Batch independent tool calls** (multiple Greps/Reads in one block); chain
   dependent shell steps in one command instead of many round-trips.
3. **Reference, don't restate.** Cite `BUG-xx` / audit sections in commits,
   checkboxes, and reports instead of re-describing them.
4. **No large dumps.** Never print snapshot JSONs, DataFrames, or candle arrays
   to the transcript. Test scripts should assert and print one PASS/FAIL line
   (plus the failing value only). Pipe verbose output to a scratchpad file and
   read only its tail.
5. **Mock the LLM.** Pipeline verification uses a stubbed Gemini response;
   deterministic layers (indicators/smc/strategies/tools) are tested without
   running `main.py` end-to-end.
6. **No subagents / exploration sweeps.** The file map in `CLAUDE.md` plus audit
   line refs make discovery unnecessary; work inline.
7. **Small, surgical edits.** Use Edit with minimal unique context; don't rewrite
   files wholesale. One commit per coherent group of fixes with a terse message.
8. **Terse progress.** Update this file by ticking boxes and adding ≤1-line
   notes; the final session summary is a short outcome list, not a narrative.

---

## Phase 1 — Correctness release (v3.2.4)

**Model: Fable 5, effort high.** Rationale: seven subtle P0s (unit mixing,
timezone semantics, sorted-order inversions, off-by-one bar counts) where a
wrong "fix" silently corrupts data again — maximum-precision session, small diff.

Scope = audit §6 items 1–7. Fix, in order:

- [x] BUG-01 — unify on USDT-perp: fapi klines carry OHLCV+takerBuyBase in one
      request (ccxt spot removed from main + labeler); fetch raises if symbol
      isn't a perp; snapshot records `market: "binance-usdm"`
- [x] BUG-02 — `LIMITS` 4h/1h → 300 (EMA_200 + ATR_Pct live on 299 closed bars)
- [x] BUG-03 — labeler prefers `generated_at` (filename fallback); filenames +
      `generated_at`/`labeled_at` now `datetime.now(timezone.utc)`
- [x] BUG-04 — OI hist `limit=500`; field renamed `percentile_20d` (dash label too)
- [x] BUG-05 — OTE mirrored for bearish legs; leg direction from most-recent
      swing; `leg_direction` in pd_zone; C6 pays only when leg matches bias
- [x] BUG-06 — SL = nearest protected pool beyond entry (max SSL below longs /
      min BSL above shorts), `SL_ATR_CAP=3.0`×15m-ATR width cap
- [x] BUG-07 — `score_breakdown` two-variant nullable (c1…c8 / m1…m6), keys match
      engine `confluence_breakdown`; prompt says echo active-mode keys verbatim
- [x] Reset & rebaseline: no-op — `snapshots/` empty and no `calibration.json`
      on this machine; labeler + eval verified to run clean post-change
- [x] Verification: 20 synthetic checks (OTE both directions, leg detection, C6
      gating, SL nearest/cap both sides, EMA_200/ATR_Pct non-NaN, CVD sign +
      |delta|≤volume, labeler time parse) ALL PASS + one mocked-LLM pipeline run
      on live perp data (real CVD all TFs, OI populated, UTC filename) ALL PASS
- [x] Commit as v3.2.4 (schema_version bumped to 3.2.4)

**Phase 1 baseline:** corpus empty on this machine (0 snapshots, no
calibration.json) — nothing to relabel; eval baseline will be established from
the first v3.2.4-labeled snapshots. Calibration falls back to equal weights.

## Phase 2 — Prediction quality (v3.3)

**Model: Opus 4.8, effort xhigh.** Rationale: design-heavy work (regime
hysteresis, logistic calibration, rubric decorrelation, prompt/schema redesign)
that needs breadth and judgment across many files; larger diff than Phase 1.
Escalate to Fable 5 medium only if the calibration-v2 statistics get stuck.

Scope = audit §6 items 8–18 (audit §3 has the design detail per item):

- [x] BUG-08 — regime rebuild: multi-swing structure (`smc._seq_state`/`_hl_label`,
      last 3–4 swings/side), 4H ADX ≥ 20 floor in `detect_regime`, 2-run rubric
      hysteresis (`indicators.compute_regime_with_hysteresis` + `apply_regime_hysteresis`
      persisting `data/regime_state.json`); `bars_ago` added to BOS/CHoCH
- [x] BUG-09 — `_current_dealing_range` recomputes to bracket price (nearest
      confirmed high above / low below) and sets `brackets_price=false` on breakout
- [x] BUG-10 — OF modifier compares `oi.change_4h_pct` against the last-closed 4H
      bar change (`bar_price_change_pct`), not the ~40h window
- [x] BUG-11 — `find_liquidity_levels(current_price=…)` keeps the 3 pools NEAREST
      price per side (extremes only as fallback)
- [x] BUG-16 / BUG-17 — straddling FVGs selected (`nearest_*` + `straddles_price`);
      OB mitigation is wick-aware (lows/highs, not closes) so swept OBs drop out =
      unswept-entry filter; entries now at OB **midpoint**
- [x] Prompt: `_build_llm_view` drops the duplicate top-level `engine_trade`,
      prunes OBs to 2/side, drops raw FVG lists + developing-swing arrays +
      `supertrend.level`; `json.dumps(separators=(",",":"))` (8k vs indented)
- [x] Prompt: `dist_from_price_atr` on every entry/stop/target candidate,
      `bars_ago` on BOS/CHoCH, `live_vs_close_pct` drift, `atr_context`
      (atr_15m/1h/4h + horizon), funding-units note; action follows the FINAL/
      overridden score (contradiction resolved in system instruction)
- [x] Add 1D timeframe: 1D fetch (`LIMITS`/`TF_MS`/`WINDOW_SIZE`/`CACHE`/`CHART_BARS`),
      daily SMC leg (`smc_context.1d`), `weekly_levels` (PWH/PWL/weekly_open),
      `untested_pocs_1d`; 1D BSL/SSL + PWH/PWL + daily POCs wired into the target
      menu; `mtf_context.d1` in the schema
- [x] Session-aware volume gate: `session_volume_context` (same UTC hour-of-day
      baseline) injected into 15m window; `_volume_gate` uses session rel-vol +
      `kill_zone` (LONDON/NEWYORK/ASIA/OFF) tag
- [x] BUG-12 — calibration v2: pure-numpy standardized logistic regression per
      rubric, timeout MFE partial credit (`realized_rr/target_rr`), 1/(symbol-hour
      cluster) de-dup weights; engine prefers `logistic` win-prob else score bucket;
      equal-weights fallback retained (`win_rate_source` recorded)
- [x] Rubric decorrelation: C2+C5+C6 share a LOCATION composite capped at 25
      (`_apply_location_cap`), freed budget rebudgeted to C3 sweep (→15) and C7 CVD
      (→15). Per-regime action thresholds DEFERRED (needs eval data — no corpus yet)
- [x] LLM-vs-engine divergence cut: `by_selected_from_engine` bucket in
      `tools/eval_engine.py` (engine_primary vs llm_alternative hit-rate)
- [x] Thinking-level A/B: harness `tools/ab_thinking.py` (replays snapshots at each
      level, replays outcomes). `GEMINI_THINKING_LEVEL` + comment made self-consistent
      on `high` (candidate-selection is now the high-stakes task); measured winner
      DEFERRED until ≥30 labeled snapshots exist
- [x] Relabel + recalibrate: **no-op** — corpus empty (0 snapshots, no
      `calibration.json`), same as Phase 1; labeler/eval/calibrate all run clean
      post-change. First trustworthy baseline comes from v3.3-labeled snapshots
- [x] Commit as v3.3 (schema_version → 3.3.0); boxes ticked + note here

**Phase 2 vs baseline:** no baseline exists on this machine (corpus empty), so
per-item deltas cannot be measured yet. Verification instead: 48 synthetic checks
(structure/hysteresis/ADX floor/dealing-range bracket/nearest-pools/straddle-FVG/
wick-OB/decorrelation/session-gate/weekly-levels/geometry/calibrate-v2/LLM-view)
ALL PASS + one mocked-LLM end-to-end run on live SOL perp (1D leg live, session
gate + kill zone, regime hysteresis persisted, compact prompt, geometry candidates
carry ATR distances) ALL PASS. Measurement-gated items (per-regime thresholds,
thinking-level A/B winner) are built as machinery and deferred to first corpus.

## Phase 3 — Architecture, robustness, hygiene (v3.3.x)

**Model: Sonnet 5, effort xhigh.** Rationale: broad but mechanical work (config
extraction, logging, tests, parallelization, cleanup) with clear specs from the
audit; volume over depth. Use max effort only for the unit-test design pass.

Scope = audit §5 + §6 items 19–20 + remaining P2/P3:

- [x] `config.py`: centralize LIMITS, WINDOW_SIZE, swing widths, gate/score
      thresholds, retention, model name — main.py/smc.py/indicators.py/
      strategies.py/price_action.py import from it (single source of truth);
      web/app.py imports the two gate thresholds for its fallback action logic
- [x] Parallel external fetches (`asyncio.gather` + `to_thread` for orderbook /
      funding / OI / F&G / BTC.D) — BUG-24
- [x] Structured `logging` (`logging.getLogger("cryptera")` / `"cryptera.web"`,
      per-attempt kline-fetch failures demoted to debug so only the final
      abort/degrade warns once); removed `debug=True` and the in-handler
      `asyncio.run` blocking pattern in `web/app.py` (native async view via
      `asgiref`, `threaded=True`) — BUG-23
- [x] Unit tests (pytest, synthetic candles) in `tests/` (26 tests, all pass):
      `fractal_swing_mask`, `get_market_structure` (BOS+CHoCH), `get_value_area`
      (incl. zero-range fallback), `_select_structural_tp`, `_label_one`
      (same-bar conservatism, gap-through-entry, timeout, no-data), OTE
      direction (BUG-05 regression guard), SL selection (BUG-06: nearest pool
      not widest, both sides, ATR cap)
- [x] Snapshot index for `/api/history` — self-healing `data/snapshot_index.json`
      (header cache keyed by filename; new files parsed incrementally, entries
      for retention-deleted files dropped automatically)
- [x] Graceful TF degradation — BUG-15: 4h/1h/15m still abort with a clear
      `RuntimeError`; the 1D leg degrades to `None` + one warning instead of
      aborting the whole run (every 1D consumer already tolerated `None`)
- [x] Remaining P2s: BUG-13 (naive event datetimes assumed UTC), BUG-14
      (`_price_decimals` scales rounding to price magnitude in `get_window_dict`),
      BUG-18 (gap-through-entry now triggers in `_label_one`), BUG-19 (15m
      cache-hit refetches live price via `fetch_live_price` instead of falling
      back to the last closed close), BUG-20 (star-pattern body ratio uses the
      middle candle's own range, not the first candle's), BUG-21 (retention
      sorts by the filename's embedded UTC timestamp, not mtime), BUG-22
      (funding history `limit` 24 → 1000)
- [x] P3 hygiene: dropped dead columns (`ATR_Z`, `VOL_Z`, `swing_high`/`swing_low`,
      `fvg_bull`/`fvg_bear` in indicators.py) and dead imports (`numpy` in
      strategies.py, unused `ROOT_DIR` import in main.py); no `datetime.utcnow()`
      calls existed (already clean); per-component calibration-weight
      renormalization (`_clamp_component_max`, clamps each weighted component to
      its own rubric max before the location cap / sum); docstring "M1-M8" →
      "M1-M6"; `pandas-ta` pinned to the exact installed `0.4.71b0`; removed the
      dead `ccxt` dependency (BUG-01 already dropped its only usage); README
      file map + roadmap no longer reference the missing `improvements_report.md`,
      and its stale `ccxt` mentions are fixed
- [x] Full test run (26/26 pytest pass) + one mocked-LLM smoke run on live SOL
      perp data (BUG-24 parallel fetches + BUG-19 live-price path + BUG-15
      degraded-1D path all exercised, ~2.5-3.4s end-to-end) + a Flask
      test-client smoke run of the async `/api/run` route and the self-healing
      history index (build, incremental pickup, self-heal on delete) — ALL
      PASS; commit; boxes ticked + note here

---

## Model assignment summary

| Phase | Model / effort | Why |
| --- | --- | --- |
| 1 — Correctness (P0s) | **Fable 5 high** | Subtle semantic bugs; a wrong fix re-corrupts data; small precise diff |
| 2 — Prediction quality | **Opus 4.8 xhigh** | Design + statistics breadth across engine, prompt, calibration |
| 3 — Arch & hygiene | **Sonnet 5 xhigh** | High-volume mechanical work with explicit specs; cost-efficient |

## Session log

- 2026-07-11 — Audit completed (`audit_report.md`); `CLAUDE.md` regenerated;
  this plan created. Phases 1–3 not started.
- 2026-07-11 — **Phase 1 complete (v3.2.4)**: BUG-01…07 fixed, verified
  offline (20 synthetic checks) + one mocked-LLM run. Corpus was empty →
  baseline deferred to first labeled v3.2.4 snapshots. Phase 2 next.
- 2026-07-11 — **Phase 2 complete (v3.3.0)**: audit §6 items 8–18 landed —
  regime rebuild (BUG-08), dealing-range bracket (BUG-09), OI-window match
  (BUG-10), nearest pools (BUG-11), straddle-FVG + wick-OB + OB-mid entries
  (BUG-16/17), 1D timeframe, session-aware gate + kill zones, rubric
  decorrelation, calibration v2 (BUG-12, logistic + timeout credit + de-dup),
  divergence eval cut, thinking A/B harness, and the prompt compaction/enrichment
  pass. 48 synthetic checks + one mocked-LLM live-perp run ALL PASS. Corpus still
  empty → relabel/recalibrate/A/B-winner/per-regime-thresholds deferred to first
  v3.3 snapshots. Phase 3 (Sonnet 5) next.
- 2026-07-11 — **Phase 3 complete**: `config.py` extraction, BUG-24 parallel
  external fetches, structured logging + web/app.py async-view/threaded fix
  (BUG-23), a real `tests/` pytest suite (26 tests), a self-healing
  `/api/history` index, graceful 1D degradation (BUG-15), and the remaining
  P2/P3 items (BUG-13/14/18/19/20/21/22 + hygiene). No snapshot-SHAPE change,
  so `schema_version` stays 3.3.0 and no relabel/recalibrate is needed (corpus
  still empty on this machine — same as Phase 1/2). All verification offline
  or mocked per the ground rules; one live-data mocked-LLM smoke run plus a
  Flask test-client smoke run, both cleaned up after (no snapshot/index left
  behind). Known drift for a future session: `CLAUDE.md`'s pipeline/scoring
  prose still describes the pre-Phase-2 pipeline (no 1D leg, session gate, or
  calibration v2 mentioned) — only the file map was updated here; a full
  CLAUDE.md refresh was out of Phase 3's explicit scope.
