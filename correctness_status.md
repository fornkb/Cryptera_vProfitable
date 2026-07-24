# Cryptera — Correctness → Quality Status (post-v3.3 audit)

Source of truth: `final_improvement_report.md` (2026-07-20, merged & verified). Reference
findings by ID (**B1.0–B28.0**, **I1.0–I15.0**); do not restate them here. Supersedes the
earlier remediation docs (`audit_report.md` / `current_status.md` / `final_audit.md`,
BUG-01…24 + FA-xx — all fully landed; removed from the tree, in git history if needed).
Each session: read `CLAUDE.md` + this file first, work only its scope, tick boxes, note
≤1-line outcomes.

> **Status (2026-07-25): Waves 1–3 COMPLETE, on branch `v3.2.4-correctness`, NOT deployed.**
> 275/275 pytest, `SCHEMA_VERSION` 3.9.0. The Azure VM is still collecting the pre-fix
> corpus (archive-only at deploy). A full pre-Azure-deploy audit is being commissioned
> before the DEPLOY EVENT; Wave 4 (I8.0–I15.0, measurement-gated) follows the clean corpus.

## Corpus discipline (the constraint that shapes everything)

- The Azure VM keeps collecting (~2.5K+ snapshots). Decision (2026-07-23): **keep collecting
  through local dev; archive + clean-restart only on the deploy day.** The pre-fix corpus is a
  valid reference / bug-repro set but **must never be fit on** after semantic fixes (invariant 5).
- Almost every Wave 1/3 fix changes snapshot or label semantics ⇒ on deploy day: bump
  `schema_version` + `label_version`, archive the VM `snapshots/`, redeploy, collect clean.
- **No tuning until Wave 4** (thresholds, weights, location cap, `MAX_TARGET_ATR_1H_MULTIPLE`,
  thinking level, sizing-from-probability) — both audits, independently.
- Verify deterministic code offline (synthetic candles + mocked Gemini). Mirror-property tests
  on every directional component.

---

## Wave 0 — corpus safeguard (deployable now, non-semantic)

- [x] **B14.0 / I15.0** — `_effective_retention()` in `main.py` honors `CRYPTERA_RETENTION`
      (explicit arg → env → default); systemd unit sets it 0; `.env` line + FAQ in
      `AZURE_DEPLOYMENT.md`. Protects the live corpus from manual CLI/web prune-to-1000.
      Verified: 7/7 offline checks + 72/72 pytest. **Deploy to VM immediately** (`.env` line).

## Wave 1 — measurement + decision correctness (P0s + directional P1s)

One release, schema bump, mirror-property tests introduced. Order flexible (scoring fixes are
independent of the labeler); B1.0 is the measurement ground truth.

- [x] **B1.0** — `_label_one` rewritten as PENDING→OPEN→TP|SL|TIMEOUT state machine;
      `_resolve_entry_1m` reconstructs fill-then-exit (unknowable → `intrabar_unknown`,
      never a silent win); `_resolve_1m` keeps the pessimistic SL tiebreak (returns
      `(outcome,resolution)`); strict horizon via `_truncate_to_horizon`; entry-bar
      favourable credit withheld; stamps `label_version=2`/`entry_time`/`exit_time`/
      `resolution_used`/`bars_to_fill`; emits `end_rr` (B4.0 hook). `eval_engine` given an
      `intrabar_unknown` bucket (kept out of timeouts). 24 labeler tests + 88/88 suite.
      Relabel note: label_version bumped → old labels not poolable (corpus archived anyway).
- [x] **B2.0** — `_is_naked_breakout` (OUT_TOL, unless sweep+reclaim) invalidates breakout
      fades in the MR orchestration; `_m1_edge_distance` sign-aware (0 outside the edge
      unless reclaim-confirmed); universal DOA guard in `compute_trade_geometry` (reject when
      live price has already breached the stop). Sequential MR (raid→MSS→return) stays I4.0.
- [x] **B3.0 / I2.0** — `trade_validation.validate_and_normalize_analysis` runs in
      `_finalize_analysis` (both CLI/web + monitor paths). Recomputes header
      pair/price/regime/bias/volume_gate/score/action from engine (engine_gate); forces
      entry/stop/tp to be real candidate levels (rel tol 5e-4, revert to primary); enforces
      direction==engine + ordering + DOA (revert geometry); recomputes R:R/sl_width; nulls
      trade fields on HOLD/HARD_GATE/invalid; derives `selected_from_engine` by comparison;
      stamps `analysis.contract_violations[]`. 10 tests. **Candidate-ID selection is Wave 2 (I1.0).**
- [x] **B4.0** — `_soft_label` timeout credit blends `mfe_r` + `end_rr` (0.5/0.5 vs target_rr),
      so give-back timeouts stop scoring 0.75; labeler field `realized_rr`→`mfe_r` (honest
      excursion name); `ENTRY_ATR_CAP=1.5` on entry candidates (stale far OB no longer becomes
      the primary). Deferred to I9.0/W4: 3-quantity model, per-component trigger-rate
      diagnostics, shadow market-entry label, Brier/reliability.
- [x] **B5.0** — `_c4_momentum` reads slope in bias-relative space (`rel_slope`); mirror
      test proves bearish+falling==bullish+rising. Was inverted for every short.
- [x] **B6.0** — shared `_zone_distance` (0 inside zone, else nearest boundary) for both OB
      and FVG in `_c2_ob_prox`; parity test on an identical zone (was 0 vs 15).
- [x] **B10.0** — `calculate_previous_day`/`weekly_levels` select by UTC calendar identity
      vs explicit `now` (prev-day = date==now-1d; prev-week = ISO week of now-7d, rollover-safe),
      not `iloc[-2]`/`groups[-1]`. Fixes PDH two-days-ago 00:00-01:00 UTC and PWH two-weeks-old
      all Monday; weekly_open honestly None on Monday. 6 tests in `test_calendar_levels.py`.
- [x] **B13.0** — `_c1_trend`: full conflict → 0 (+`tf_conflict` note), one-TF-directional → 5,
      both-neutral → 0, aligned → ADX tiering. Prompt already frames C1 on "agreement".
- [x] **B15.0** — eval engine universe reads `strategies.*` exclusively (never `header.*`);
      `by_bias` buckets by `setup_direction` (fallback trend_bias); `_realized_r` +
      `expectancy_r` (realized R: +target_rr/−1R/end_rr, net `ROUND_TRIP_COST_R`) replaces the
      excursion `expectancy_pct` (kept, deprecated); `avg_realized_rr`→`avg_mfe_r`. Deferred to
      W2/W4: policy-universe separation + paired `by_verdict` (rides with I1.0).
- [x] **B16.0** — `compute_regime_with_hysteresis` takes `bar_id`; re-processing one closed
      15m bar is idempotent (`last_bar_id`), so restarts/`--once`/CLI-alongside-monitor can't
      advance the flip counter with no new data. `apply_regime_hysteresis` + call site pass it.
      (4-state transition router stays I3.0.)
- [x] Rider **B25.0** — labeler now writes compact (`separators=(",",":")`) instead of
      `indent=2`, done as part of B1.0's `label_snapshot_file`.
- [x] Rider **B26.0** — `ab_thinking` now uses the canonical replay path: floored window,
      1m intrabar drill, episode collapse before any LLM call, net-R (realized) instead of raw
      hit-rate. Paired per-episode net-R + bootstrap CI rides with I8.0.
- [x] Rider **B27.0** — `fetch_funding` infers `interval_hours` from `fundingTime` deltas and
      windows the 24h average by hours (4h/1h perps no longer mislabel a 12h/3h span as 24h);
      `interval_hours` added to the block. 2 tests in `test_funding.py`.
- [ ] Relabel + recalibrate discipline; schema/label_version bump; full pytest + regression seeds.

## Wave 2 — I1.0 adjudicator (on the I2.0 validator)

- [x] **level_id menu** — `strategies._assign_level_ids` stamps stable menu-scoped
      `level_id` (E*/S*/T*) on every entry/stop/target candidate in `compute_trade_geometry`,
      in ranked order. `SCHEMA_VERSION` bumped 3.3.1→3.4.0 (snapshot shape now carries
      `level_id`). Foundation for ID-based UPDATE selection. `test_level_ids.py` (5). 141/141.
- [x] **`should_call_llm` trigger gate** — `config.LLM_CALL_THRESHOLD` (=45);
      `main.should_call_llm(snapshot)` pure predicate (`engine_trade.valid AND gate !=
      HARD_GATE AND score >= threshold`). `run_analysis` (CLI/web) newly acquires it —
      below the gate it emits `_deterministic_analysis` (a clean engine HOLD, `verdict:
      "NO_CALL"`) with ZERO Gemini spend. Monitor left unchanged (its active-transition
      gate strictly implies the predicate). `test_should_call_llm.py` (9). 150/150.
- [x] **`engine_case` block** — `main._build_engine_case(snapshot)` assembles the engine's
      structured argument (thesis / for[] / against[] / vetoes_or_caps / uncertainty) from
      `confluence_breakdown`+`notes`, `order_flow_notes`, gate flags, and win-rate;
      `_data_degraded` flags missing orderbook/funding/OI/cvd-proxy. Injected into
      `_build_llm_view`. The system-instruction rewrite that consumes it rides with schema v2.
      `test_engine_case.py` (9). 159/159.
- [x] **Schema v2 verdict + `level_id` resolution** — `GEMINI_RESPONSE_SCHEMA` gains
      `verdict` (AGREE|UPDATE|REJECT, required), `reject_reason{code,note}`, trade_decision
      `entry_id/stop_loss_id/take_profit_id`+`update_rationale[]`, and tm
      `tp1_id/tp2_id/move_to_breakeven_at_id`; system instruction rewritten as an
      adjudicator contract that consumes `engine_case` and selects by id. `trade_validation`
      now branches on verdict: AGREE→engine primary; UPDATE→resolve ids→exact price (null id
      keeps primary, off-menu id reverts+records); REJECT→HOLD (overrides the engine gate) +
      validated `reject_reason`. `header.action` is engine-derived (REJECT→HOLD). Absent
      verdict = legacy tolerance path (nothing regresses). `test_trade_validation.py` +8 (18).
      167/167.
- [x] **`by_verdict` eval cut** — `tools/eval_engine._by_verdict` pairs the adjudicated plan
      (trade_decision_outcome) against the engine primary (engine_trade_outcome) on the SAME
      narrated snapshots: AGREE/UPDATE carry a `paired_delta_r` (does adjudication add alpha?),
      REJECT carries the engine primary's outcome had the declined trade been taken (rejection
      quality). NOT episode-collapsed. Also fixed a latent `by_action` crash (raw `td.rr` dict
      → `float(dict)`) via a shared `_plan_rr` unwrap. Promotion decision is Wave-4
      measurement-gated (needs clean corpus + ≥50 UPDATE episodes). `test_eval_engine.py` +2.
      169/169.
- [x] **Shadow-rollout machinery + docs** — the adjudicated plan is persisted
      (`analysis.verdict` + normalized `final_plan` + untouched `engine_trade`) and read out
      paired via `by_verdict`; the **promotion decision defers to Wave 4** (needs the clean
      corpus + ≥50 UPDATE episodes with a non-negative paired delta). CLAUDE.md invariant #1
      relaxed to "engine proposes, LLM adjudicates" (bounded UPDATE-by-`level_id`); pipeline
      note + file map + `compute_trade_geometry` geometry note updated.

**Wave 2 COMPLETE** (6 commits): level_id menu · `should_call_llm` gate · `engine_case` ·
schema v2 verdict + id resolution · `by_verdict` eval · docs. 169/169 pytest. Nothing
deployed; VM still collecting pre-fix corpus. Promotion + first calibration fit are Wave 4.

## Wave 3 — structure & menus (semantics-changing; land before collection restarts)

- [x] **I3.0 (machine)** — `smc.structural_state_machine` walks closed bars holding
      (state, trend, protected swings) and emits MUTUALLY EXCLUSIVE trend-conditioned
      events (B7.0): in a bullish trend a break of the last swing high is a BOS, a break
      of the protected low is a bearish CHoCH — never both from one bar. A CHoCH opens a
      `transition` state; the label only flips on a confirming BOS with displacement (or a
      2nd confirming break), a BOS in the old direction cancels it, and it decays to
      neutral after `TRANSITION_EXPIRY_BARS`. Displacement certified inside the breaking
      impulse only (`_impulse_origin`), events carry `origin_idx`/`origin_price`/
      `break_idx` (I6.0's OTE anchor). `structure` label now sourced from the machine
      (HH/HL · LH/LL · TRANSITION · progression) so it no longer lags a CHoCH by several
      swings (B8.0); per-TF `structure_state` in the snapshot; `get_market_structure`
      returns a 4-tuple. `SCHEMA_VERSION` 3.4.0→3.5.0. New `test_structure_machine.py`
      (11 golden scenarios, **every one asserted on both mirrored sides**) + 24-check
      offline pipeline smoke. 183/183. *Counter-CHoCH veto + 4-state router = I3.0b.*
- [x] **I3.0b (router + B8.0)** — `detect_regime` is a 4-state router: **trend** /
      **Ranging / Sideways** (now EARNED by evidence of balance — sub-floor ADX on 4H *and*
      1H, price contained by a bracketing 4H dealing range, no fresh break, volatility not
      expanding) / **Transition** / **Untradeable**, with `detect_volatility_regime` finally
      wired in as a routing input. Everything that merely fails the trend test is a
      Transition, not a fade. `regime_bucket` gains the two no-rubric buckets +
      `is_tradable_bucket`; hysteresis now accepts a stand-down IMMEDIATELY (only entering a
      tradable rubric needs confirmation runs). `evaluate_strategies` routes those buckets to
      `score_mode="no_trade"` (canonical zeroed keys, score 0, neutral direction, invalid
      geometry → HOLD, and below `LLM_CALL_THRESHOLD` so zero Gemini spend). **B8.0**:
      `_counter_choch` caps the score at `CONDITIONAL_ENTRY_THRESHOLD - 1` on a fresh
      opposing 1H/4H CHoCH (+ `counter_choch_warning` → engine_case veto), `bos.displacement`
      is finally consumed (bounded +5 to C1 in the bias direction; a displaced break keeps
      disqualifying M6 after `fresh` ages out). New `test_regime_router.py` (21). 204/204.
- [x] **I4.0** — setups are ORDERED sequences with an expiry, in `strategies.setup_sequence`.
      MR: `_count_rotations` (≥`BALANCE_MIN_ROTATIONS` traversals = established balance) →
      `_detect_raid` (ATR-scaled penetration of the edge, closing back inside) →
      `_detect_mss` (15m I3.0 event in the fade direction AFTER the raid) → expiry
      (`MR_SETUP_EXPIRY_BARS`). **No sweep/MSS ⇒ no MR trade regardless of additive score** —
      `mr_dir` is killed exactly like a naked breakout. `stage` names what the tape still
      owes (`no_balance`/`awaiting_raid`/`awaiting_mss`/`expired`/`ready`), so a HOLD can say
      why. Continuation: displacement break → pullback (off I6.0's leg bounds) →
      return-to-zone, recorded as evidence, not gated (a second unmeasured frequency cut is
      a Wave 4 call). **B18.0**: `_m3_cvd_absorption` now separates true absorption (flow
      AGAINST the fade while the edge holds → 15) from initiative flow (with the fade → 10),
      which is what it used to pay 15 for while asserting "absorption" to the LLM. Sequence
      surfaced in `engine_case` for/against + the prompt. New `test_setup_sequences.py` (18).
      SCHEMA 3.7.0. 242/242.
- [x] **I5.0** — `find_liquidity_levels` rebuilt as a typed layer (B12.0): `pools` require
      ≥`POOL_MIN_TOUCHES` DISTINCT touches (≥`POOL_MIN_SEPARATION_BARS` apart, so two bars of
      one pivot aren't two tests) and carry `{price, side, touches, first_idx, last_idx,
      swept_idx, tf, strength}`; wick-consumed pools are retired as draws but KEPT as sweep
      history; single-touch extremes move to `swing_levels` (valid structure/stop anchors,
      explicitly not liquidity) and only fill the draw menu while unswept. Sweep quality:
      `detect_mtf_sweep` reads the TYPED pools (a lone pivot can no longer manufacture sweep
      evidence), requires ≥`SWEEP_MIN_PENETRATION_ATR`×ATR15m penetration, scans
      `SWEEP_SCAN_BARS`=16 (was 5) and decays with age (HTF demotes past
      `SWEEP_FRESH_BARS`, a stale 15m-only sweep expires). New `test_liquidity_pools.py`
      (12). SCHEMA 3.6.0. 224/224. *Menu still singleton-filled for geometry until I7.0.*
- [x] **I6.0** — `smc.impulse_leg_ote` draws OTE on the impulse leg of the LAST structural
      break (`_latest_event` → origin swing → the extreme that leg reached), records
      `leg_source` (`bos_impulse`/`choch_impulse`/`dealing_range`), and returns nothing when
      the leg has been retraced past 100% (void, not inverted). Premium/discount stays on the
      bracketing dealing range. **B9.0**: `_current_dealing_range` re-derives `leg` from the
      swings the re-bracket walk actually chose (it was pinned to the latest confirmed swing
      and survived re-bracketing, so a breakdown kept a bullish leg label) and reports
      `leg=None` when price is beyond all confirmed structure → no OTE rather than a guess.
      New `test_ote_leg.py` (8, incl. the audit's verified 100/105/95/98 case + a mirrored
      bearish leg). 212/212.
- [x] **I7.0** — geometry is candidate-FIRST: build typed rows → merge → select the primary
      **from the merged menu** (`_select_primary_entry`/`_select_primary_stop`/
      `_select_primary_target`), so menu membership and one canonical R:R are invariants,
      not coincidences (B11.0 defect 3). **The ATR cap no longer clips the stop inside
      invalidation** — the honest wide stop is kept with `size_class:"reduced"` +
      `sl_atr_multiple` (defect 1). Stops anchor on the **protected swing** (I3.0
      `structure_state`) / `swing_levels`, never on liquidity pools — and the primary is the
      tightest candidate strictly BEYOND the invalidation anchor (raid extreme for a fade,
      protected swing for continuation) (defect 2). `_tf_of_source("current price")→"live"`
      (rank 0), unknown→None (defect 4). Tick quantization (`_tick_decimals`, magnitude-
      scaled) on every emitted level. **B19.0** menu enrichment: PDH/PDL, S/R zones, 1H OB
      entries, and an MR menu that reaches POC → opposite edge → external liquidity with the
      stop beyond the raid extreme. Superseded `_select_structural_tp`/`_nearest_level`
      deleted (their tests ported onto the live selector). New `test_geometry_menu.py` (17);
      the two BUG-06 SL regression tests rewritten for the new contract. SCHEMA 3.8.0. 260/260.
- [x] **B17.0** — `_dedup_plateau` collapses a run of equal extremes inside one fractal
      half-window to its LAST bar (a flat stretch is one pivot, not N); `fractal_swing_mask`
      gains a relative `tol` fed by `_equal_tol(df)` (ATR%·0.25, ≤20bps) so "equal" is the
      cluster tolerance, not float equality; `_hl_label` tie → neutral `EQ` token (flat tape
      no longer reads LH/LL → no more shorting chop). 3 tests. 172/172.
- [x] **B21.0** — `KILL_ZONES_LOCAL` resolves London/NY/Asia in their own tz via stdlib
      `zoneinfo`, so the highest-liquidity windows stop being ~1h wrong for months at a time
      (fixed-UTC kept as the hour-only fallback). `session_volume_context` baselines the
      exact MINUTE-of-day slot (not the pooled hour), splits weekday/weekend, and uses the
      MEDIAN so one news spike can't depress today's ratio; `slot_minute`/`slot_is_weekend`
      reported alongside `slot_samples`.
- [x] **B22.0** — `find_untested_pocs` anchors windows to CALENDAR periods (`period="D"` for
      4H bars, `"W"` for daily) instead of tail-anchored N-bar blocks, so a naked POC keeps
      its identity across cycles (it shifted every boundary on every new candle); retest band
      scales with the window's own range instead of a flat 0.5%; each POC carries `period`.
- [x] **B23.0** — episode identity is DISTANCE-based (`same_episode`, `EPISODE_PRICE_TOL`
      =0.4%): rows group per (symbol, side) and are walked chronologically against the open
      episode's representative geometry, so the audit's 104.4-vs-104.6 drift (0.19%) no
      longer splits one physical trade across a rounding boundary. `episode_id` kept for
      diagnostics. Consumers re-checked: `eval_engine`, `calibrate`, `ab_thinking` all route
      through `collapse_to_episodes`. *The 24h-gap merge direction is left alone on purpose
      — under-counting is the safe direction; tune on the clean corpus in Wave 4.*
      New `test_sessions_and_episodes.py` (15). SCHEMA 3.9.0. 275/275.

## DEPLOY EVENT

- [ ] Stop VM monitor + label timer → archive pre-fix `snapshots/` → deploy branch → restart
      clean under new schema + label_version.

## Wave 4 — measurement-gated (needs the NEW clean corpus)

- [ ] **I9.0** calibration v3 (walk-forward, embargo, Brier/log-loss gates) — first fit.
- [ ] **I1.0** `by_verdict` readout + promotion decision.
- [ ] **I8.0** measurement stack v2 paired A/Bs.
- [ ] **I10.0** order-flow completeness, **I11.0** session realism, **I13.0** BTC structure gate,
      **I14.0** horizon-aware reachability — each evaluated for out-of-sample value, never in-sample.

---

## Session log

- 2026-07-23 — Plan devised from the merged report; decision: keep collecting + archive at
  deploy. **Wave 0 (B14.0/I15.0) landed & verified offline** (7/7 + 72/72). Tracking doc
  created.
- 2026-07-23 — **Wave 1 B1.0 + B25.0 landed** (`tools/label_snapshots.py` rewrite,
  `tools/eval_engine.py` intrabar bucket). Chronological state machine, `intrabar_unknown`
  class, strict horizon, entry-bar excursion fix, `label_version=2`, `end_rr`. 88/88 tests.
- 2026-07-23 — **Wave 1 B5.0 + B6.0 + B13.0 landed** (`strategies.py` scoring). Bias-relative
  RSI slope, shared OB/FVG `_zone_distance`, C1 conflict→0/partial→5. New
  `tests/test_scoring_directional.py` establishes the mirror-property pattern. 98/98 tests.
- 2026-07-23 — **Wave 1 B2.0 + B16.0 landed** (`strategies.py` MR + geometry, `indicators.py`
  /`main.py` hysteresis). `_is_naked_breakout` + sign-aware M1 + universal DOA guard;
  bar-keyed idempotent hysteresis. New `tests/test_mr_and_regime.py`. 111/111 tests.
- 2026-07-23 — **Wave 1 B4.0 + B15.0 landed** (`calibrate.py`, `eval_engine.py`,
  `label_snapshots.py`, `strategies.py`/`config.py`). Timeout soft-label blend, `mfe_r`
  rename, `ENTRY_ATR_CAP`; eval reads strategies.* only, `setup_direction` bucket, realized-R
  `expectancy_r`. 118/118 tests. P0s B1.0 + B2.0 + B4.0 done; **B3.0/I2.0 (post-LLM
  validator) is the last remaining P0.** Remaining Wave 1: B10.0, B3.0/I2.0, riders B26/B27.
- 2026-07-23 — **Wave 1 B10.0 + B26.0 + B27.0 landed** (`price_action.py`, `tools/ab_thinking.py`,
  `main.py` funding). Calendar-correct PDH/PDL/PWH/PWL; ab_thinking canonical replay + net-R;
  funding interval inference. New `test_calendar_levels.py` (6) + `test_funding.py` (2). 126/126.
- 2026-07-23 — **Wave 1 B3.0/I2.0 landed → WAVE 1 COMPLETE.** New `trade_validation.py`
  (post-LLM validator) hooked into `_finalize_analysis`; 10 tests; 136/136 suite. All four P0s
  (B1/B2/B3/B4) + directional (B5/B6/B13) + MR/regime (B2/B16) + measurement (B4/B15/B25) +
  levels/ops (B10/B14/B26/B27) done. **Next: Wave 2 — I1.0 adjudicator on the I2.0 validator.**
  Reminder: nothing has been deployed; VM still collecting the pre-fix corpus (archive at deploy).
- 2026-07-23 — **Wave 2 level_id menu landed** (`strategies.py`, `config.py`). Every
  entry/stop/target candidate now carries a stable menu-scoped `level_id`; SCHEMA_VERSION
  3.3.1→3.4.0. New `test_level_ids.py` (5). 141/141. Foundation for schema-v2 UPDATE-by-ID.
- 2026-07-23 — **Wave 2 `should_call_llm` gate landed** (`config.py`, `main.py`).
  `LLM_CALL_THRESHOLD`=45; CLI/web `run_analysis` now calls Gemini only above the gate,
  else a deterministic engine HOLD (`verdict:"NO_CALL"`, zero spend). Monitor unchanged.
  New `test_should_call_llm.py` (9). 150/150.
- 2026-07-23 — **Wave 2 `engine_case` block landed** (`main.py`). `_build_engine_case`
  builds thesis/for/against/vetoes_or_caps/uncertainty from data the engine already computes;
  attached to the LLM view. New `test_engine_case.py` (9). 159/159.
- 2026-07-23 — **Wave 2 schema v2 verdict + level_id resolution landed** (`main.py`,
  `trade_validation.py`). AGREE/UPDATE/REJECT contract; UPDATE re-selects candidates by
  level_id (engine resolves id→exact price, killing float-tolerance guessing); REJECT→HOLD
  overrides the engine gate; system-instruction adjudicator rewrite consuming engine_case.
  `test_trade_validation.py` +8 (18 total). 167/167.
- 2026-07-23 — **Wave 2 `by_verdict` eval cut landed** (`tools/eval_engine.py`). Paired
  AGREE/UPDATE vs engine primary (`paired_delta_r`) + REJECT rejection-quality readout;
  shared `_plan_rr` unwrap also fixes a latent `by_action` `float(dict)` crash.
  `test_eval_engine.py` +2. 169/169. Promotion decision deferred to Wave 4.
- 2026-07-23 — **Wave 2 docs close-out → WAVE 2 COMPLETE** (`CLAUDE.md`, `strategies.py`
  docstring). Invariant #1 relaxed to "engine proposes, LLM adjudicates" (bounded
  UPDATE-by-`level_id`); pipeline note + file map (`trade_validation.py`, `should_call_llm`,
  `LLM_CALL_THRESHOLD`) + geometry note updated. Full I1.0 adjudicator landed on the I2.0
  validator. **Next: Wave 3 (structure & menus) — all before the collection restart.**
- 2026-07-24 — **Wave 3 B17.0 landed** (`smc.py`). Plateau dedup in the shared swing mask +
  ATR-scaled equality tolerance + neutral `EQ` structure token on a tie. `test_smc.py` +3.
  172/172. Swing layer cleaned before the I3.0 state machine reads it.
- 2026-07-24 — **Wave 3 I3.0 (state machine) landed** (`smc.py`, `config.py`, `main.py`).
  Mutually-exclusive trend-conditioned BOS/CHoCH + transition state + impulse-anchored
  displacement; `structure` label sourced from the machine; `structure_state` per TF.
  SCHEMA 3.5.0. New `test_structure_machine.py` (11, all mirrored). 183/183.
- 2026-07-24 — **Wave 3 I3.0b landed** (`indicators.py`, `strategies.py`, `main.py`, `app.js`).
  4-state router (range must be earned; volatility wired in), immediate stand-down hysteresis,
  `no_trade` score mode, B8.0 counter-CHoCH cap + displacement consumption.
  New `test_regime_router.py` (21). 204/204. **Expect a large drop in trade frequency —
  that is the intended effect; measure quality-per-trade on the clean corpus in Wave 4.**
- 2026-07-24 — **Wave 3 I6.0 + B9.0 landed** (`smc.py`, `main.py`). OTE moved onto the
  impulse leg of the last break with `leg_source`; re-bracketed dealing range re-derives its
  leg and reports none when price is outside all structure. New `test_ote_leg.py` (8). 212/212.
- 2026-07-24 — **Wave 3 I5.0 landed** (`smc.py`, `strategies.py`, `config.py`, `main.py`).
  Typed pools with touches/age/swept state + `swing_levels`; sweep needs typed pools,
  ATR-scaled penetration, 16-bar scan with age decay. SCHEMA 3.6.0.
  New `test_liquidity_pools.py` (12). 224/224.
- 2026-07-24 — **Wave 3 I4.0 + B18.0 landed** (`strategies.py`, `config.py`, `main.py`).
  Ordered MR sequence (balance→raid→MSS) as a HARD gate + continuation sequence as evidence,
  both with expiry; absorption vs initiative relabel. SCHEMA 3.7.0.
  New `test_setup_sequences.py` (18). 242/242.
- 2026-07-24 — **Wave 3 I7.0 landed** (`strategies.py`, `config.py`, `main.py`).
  Candidate-first geometry (primary selected from the merged menu), stop anchored on the
  protected swing and never clipped inside invalidation (`size_class`), live price demoted
  to `tf:"live"`, tick quantization, B19.0 menu enrichment. SCHEMA 3.8.0.
  New `test_geometry_menu.py` (17). 260/260.
- 2026-07-24 — **Wave 3 riders B21.0 + B22.0 + B23.0 landed → WAVE 3 COMPLETE**
  (`indicators.py`, `config.py`, `price_action.py`, `tools/episodes.py`,
  `tools/eval_engine.py`). DST-correct kill zones + minute-slot median volume baseline;
  calendar-anchored POC windows; distance-based episode identity. SCHEMA 3.9.0.
  New `test_sessions_and_episodes.py` (15). 275/275.

**Wave 3 COMPLETE** (8 commits): B17.0 · I3.0 · I3.0b/B8.0 · I6.0/B9.0 · I5.0/B12.0 ·
I4.0/B18.0 · I7.0/B11.0/B19.0 · riders B21/B22/B23. 275/275 pytest, SCHEMA 3.4.0→3.9.0.
Nothing deployed; the VM is still collecting the pre-fix corpus. **Next: the DEPLOY EVENT**
— stop the monitor + label timer, archive `snapshots/`, deploy this branch, restart clean
under the new schema + label_version. Then Wave 4 (measurement-gated, needs the new corpus).
