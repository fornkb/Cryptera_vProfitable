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

- [ ] BUG-08 — regime detection rebuild: last 4–6 swings, 4H ADX ≥ 20 floor for
      "Trending", N-run hysteresis before rubric switch
- [ ] BUG-09 — dealing range must bracket price (else recompute/flag)
- [ ] BUG-10 — OF modifier: match OI change window to price-change window
- [ ] BUG-11 — keep nearest liquidity pools, not the 3 most extreme
- [ ] BUG-16 / BUG-17 — straddling FVGs visible; OB mitigation counts wicks
      (geometry realism, audit §3.5: OB-midpoint entries, unswept-entry filter)
- [ ] Prompt: drop duplicate top-level `engine_trade`; compact
      `json.dumps(..., separators=(",",":"))`; prune to 2 OBs/2 FVGs per side,
      drop filled FVGs and dead fields (audit §4)
- [ ] Prompt: add `dist_from_price_atr` on all candidates/levels, `bars_ago` on
      BOS/CHoCH, `live_vs_close_pct` drift flag, ATR-vs-horizon line, funding
      units note; resolve score_override-vs-action contradiction
- [ ] Add 1D timeframe: daily structure, weekly open, PWH/PWL, daily untested
      POCs wired into pools/target menu
- [ ] Session-aware volume gate (same-time-of-day baseline) + ICT kill-zone tag
- [ ] BUG-12 — calibration v2: logistic regression over `confluence_breakdown`,
      timeout credit via MFE, snapshot de-dup (one per symbol-hour or 1/overlap
      weights); keep equal-weights fallback
- [ ] Rubric decorrelation: C2/C5/C6 → location composite (~25 max), rebudget to
      orthogonal evidence; per-regime action thresholds once eval data supports it
- [ ] LLM-vs-engine divergence cut in `tools/eval_engine.py`
      (`selected_from_engine` true vs false hit-rate)
- [ ] Thinking-level + prompt A/B over 30–50 labeled snapshots; make
      `GEMINI_THINKING_LEVEL` and its comment agree with the measured winner
- [ ] Relabel + recalibrate; compare against Phase 1 baseline; record deltas
- [ ] Commit as v3.3; tick boxes + results note here

**Phase 2 vs baseline (fill in):** _pending_

## Phase 3 — Architecture, robustness, hygiene (v3.3.x)

**Model: Sonnet 5, effort xhigh.** Rationale: broad but mechanical work (config
extraction, logging, tests, parallelization, cleanup) with clear specs from the
audit; volume over depth. Use max effort only for the unit-test design pass.

Scope = audit §5 + §6 items 19–20 + remaining P2/P3:

- [ ] `config.py`: centralize LIMITS, WINDOW_SIZE, swing widths, gate/score
      thresholds, retention, model name
- [ ] Parallel external fetches (`asyncio.gather` + `to_thread` for orderbook /
      funding / OI / F&G / BTC.D) — BUG-24
- [ ] Structured `logging` (warn-once for dead fetches); remove `debug=True` and
      in-handler `asyncio.run` blocking pattern in `web/app.py` — BUG-23
- [ ] Unit tests (pytest, synthetic candles): `fractal_swing_mask`,
      `get_market_structure`, `get_value_area`, `_select_structural_tp`,
      `_label_one` (both-sides bars, gaps), OTE direction, SL selection
- [ ] Snapshot index for `/api/history` (SQLite or JSONL header index)
- [ ] Graceful TF degradation — BUG-15
- [ ] Remaining P2s: BUG-13 (events tz: require offsets / assume UTC), BUG-14
      (price-magnitude-scaled rounding), BUG-18 (gap-through-entry fills),
      BUG-19 (cache-hit `live_price` refresh), BUG-20 (star-pattern ratio),
      BUG-21 (retention by filename timestamp), BUG-22 (funding percentile
      window ≤1000)
- [ ] P3 hygiene: dead columns/branches, `datetime.utcnow()` → `now(timezone.utc)`,
      per-component calibration-weight renormalization, docstring "M1-M8" → M1-M6,
      pin `pandas-ta` (exact versions), README fixed (drop `improvements_report.md`
      ref or restore it)
- [ ] Full test run + one mocked-LLM smoke run; commit; tick boxes here

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
