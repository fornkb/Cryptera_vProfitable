# Cryptera — Final Pre-Deployment Audit (v3.3.0 → Azure)

Audit date: 2026-07-12. Scope: every `.py` file post-Phase-1/2/3 remediation
(all of BUG-01…BUG-24 verified fixed), plus the monitor/alerts collector path,
the label→eval→calibrate pipeline, and the deploy scripts — i.e. everything the
Azure box will run unattended. New findings use **FA-xx** IDs (BUG-xx = the
2026-07-11 audit, all closed).

Severity: **P0** = corrupts the corpus/calibration or burns money, fix before
deploy · **P1** = materially degrades prediction/eval quality · **P2** =
robustness · **P3** = hygiene/docs.

**Context that shapes priorities:** after deployment the system is frozen; the
VM collects snapshots 24/7 and calibration is later fitted on that corpus.
Therefore anything that changes *snapshot semantics, labels, or the
calibration features* MUST land before the first VM snapshot is written
(CLAUDE.md invariant 5). The local `snapshots/` contains only 7 dev-test files
— delete them before deploy; there is nothing to relabel.

---

## 1. Executive summary

The engine is in good shape: all 24 prior bugs are genuinely fixed (verified in
code, not just checkboxes), the geometry/scoring layer is internally
consistent, and the monitor's budget/cooldown math is sound. The audit found
**3 P0s**, all in the seams between components rather than inside them:

1. **FA-01** — the monitor's alert path sends `chart_series` (72% of the
   snapshot, measured: 38.7KB of 53.7KB) inside the Gemini prompt, and the A/B
   harness replays saved snapshots with `analysis` + `outcome` still attached
   (answer leakage).
2. **FA-02** — the calibration loop is self-contaminating: snapshots persist
   only the *weighted* confluence breakdown, so the first `calibration.json`
   deployed to the VM poisons every subsequent snapshot as a training row, and
   the logistic model is fed weighted features at inference while fitted on
   raw ones.
3. **FA-03** — concurrent per-symbol threads do unlocked, non-atomic
   read-modify-write on `data/oi_history.json` every cycle → lost updates now,
   file corruption + silent full reset eventually (guaranteed on a 24/7 box).

None of these require design work — all are small, surgical fixes. Phase 1
fixes everything that touches snapshot/label semantics (schema bump 3.3.1),
Phase 2 hardens the collector and the eval tooling, Phase 3 adds three
high-confidence prediction-quality improvements plus the deploy checklist.

---

## 2. Findings

### P0 — fix before any corpus exists

**FA-01 · LLM prompt contamination: `chart_series` in monitor narrations; `analysis`/`outcome` in A/B replays**
`monitor.cycle` attaches `chart_series` in the persist loop (`monitor.py:135-136`)
*before* the alert path narrates the same dict (`monitor.py:158`), and
`_build_llm_view` (`main.py:986-1064`) never strips it — measured 38.7KB of raw
OHLC arrays (72% of the snapshot) entering every monitor-fired Gemini prompt:
~3-4× token cost per narration, buried signal, and a direct violation of the
"attached AFTER the LLM call" invariant. The CLI path (`run_analysis`) is
unaffected only by ordering luck. Same root cause hits `tools/ab_thinking.py`,
which calls `_build_llm_view` on *saved* snapshots — narrated ones carry
`analysis` and labelled ones carry `outcome`, so the A/B replay shows the model
the previous answer and the realized result. Any A/B verdict produced that way
would be garbage.
**Fix:** in `_build_llm_view`, alongside `view.pop("engine_trade", None)`, add
`view.pop("chart_series", None)`, `view.pop("analysis", None)`,
`view.pop("outcome", None)` (defense in depth — fixes monitor, CLI, and
ab_thinking in one line each). Do NOT reorder monitor.cycle; the pop is the
robust fix.

**FA-02 · Calibration features are the weighted breakdown → feedback loop + train/inference mismatch**
`evaluate_strategies` persists only the post-weight, post-cap
`confluence_breakdown` (`strategies.py:950-951, 972-974`); `tools/calibrate.py:217`
fits component weights AND the logistic model on it, and
`_logistic_winrate` (`strategies.py:55-76, 998`) consumes it at inference. Two
consequences: (a) the moment a `calibration.json` with non-1.0 weights is
deployed, every new snapshot's breakdown is weight-scaled, so the next
calibration run fits weights on already-weighted features — compounding drift
with each refit cycle; (b) even on the very first refit, the logistic model is
fitted on raw-corpus features but fed weighted features at inference — its
win-probabilities are biased the moment weights exist. This breaks the exact
workflow planned for Azure (collect → calibrate → keep collecting → recalibrate).
**Fix:** persist the pre-weight, pre-cap component dict as
`strategies.confluence_breakdown_raw` (both rubrics; the `raw` dict already
exists in both branches). `_logistic_winrate` must consume the RAW breakdown.
`calibrate.py` prefers `confluence_breakdown_raw` (falls back to
`confluence_breakdown` for any legacy file). Pop `confluence_breakdown_raw`
from the LLM view (token-dead field). Schema bump → 3.3.1.

**FA-03 · `data/oi_history.json`: unlocked concurrent read-modify-write + non-atomic writes**
`monitor.cycle` runs all watchlist symbols concurrently (`monitor.py:124`);
each calls `fetch_open_interest` on a worker thread (`main.py:1339-1345`),
which does `_load_oi_history()` → append → `_save_oi_history()`
(`main.py:461-467`) with no lock — concurrent threads overwrite each other's
appends every single cycle (lost updates), and `_save_oi_history`/`_write_json_file`
(`main.py:373-379, 408-414`) write in place, so a crash/OOM mid-write leaves
truncated JSON that the next load silently replaces with `{}` (full history +
regime-state reset, no error). Guaranteed to bite on a 1GB VM running 24/7.
**Fix:** (a) atomic writes everywhere state is persisted: write to `path + ".tmp"`
then `os.replace` (pattern already correct in `alerts/budget.py:24-29` — copy it)
for `_save_oi_history`, `_write_json_file`, and `web/app.py:_save_index`;
(b) a module-level `threading.Lock` in `main.py` wrapping the whole
load-append-save critical section inside `fetch_open_interest` (and the same
lock — or the loop-thread guarantee, documented — for `apply_regime_hysteresis`,
which is currently safe only because it never awaits).

### P1 — materially degrades prediction/eval quality

**FA-04 · Engine-primary TP bypasses the `MAX_TARGET_ATR_1H_MULTIPLE` gate**
`_rank_targets` drops candidates farther than 10×ATR-1h from price
(`strategies.py:729`), but the PRIMARY TP is selected from the *unfiltered*
`target_labeled` list (`strategies.py:870-875`). With 1d pools / PWH / PWL in
the menu, `_select_structural_tp`'s "farthest available (<2R, honest)" branch
can pick a target that is unreachable within the 24h horizon — and the primary
is exactly what `label_snapshots` scores and `calibrate` learns from, so the
corpus systematically over-collects timeouts. Also inconsistent: the primary
can be a price absent from its own candidates menu.
**Fix:** apply the same atr_1h gate to `tp_prices` before
`_select_structural_tp`/`_nearest_level` (fail open when atr_1h is None, same
as `_rank_targets`). Keep the honest-sub-2R behaviour on the *filtered* list.

**FA-05 · `fetch_open_interest` failure path fabricates data**
When the `openInterest` call fails, `oi_now` stays `0.0` (`main.py:420-429`)
and the function proceeds: `change_1h/4h` compute against real history → e.g.
−100%, percentile computes rank-of-zero → 0.0, and `{"oi": 0.0}` is APPENDED to
the local history file, permanently poisoning the percentile fallback. The
snapshot then shows the LLM a fake OI collapse.
**Fix:** on failure set `oi_now = None`; skip deltas, percentile, and the
history append; return `{"value": None, ...}` (all consumers —
`_order_flow_modifier`, the LLM view — already tolerate None fields).

**FA-06 · `tools/eval_engine.py` cannot evaluate the collector corpus**
Eval keys the entire loop on `outcome.trade_decision_outcome`
(`eval_engine.py:149-151`), which only exists for *narrated* snapshots. The
Azure corpus is ~99% no-LLM cheap-pass snapshots whose labels live in
`outcome.engine_trade_outcome` — eval will report `snapshots_labelled: 0`
against thousands of labelled files, so the baseline the whole
calibrate-later plan depends on can't be measured.
**Fix:** make `engine_trade_outcome` the primary universe (score from
`strategies.confluence_score`, buckets by score/mode/regime/gate as today);
keep the existing LLM cuts (`by_action`, `by_selected_from_engine`) computed
from `trade_decision_outcome` where analysis exists. Report both universes'
counts explicitly.

**FA-07 · Ranging no-fade snapshots emit wrong breakdown keys (`m1`…`m6`)**
`evaluate_strategies` mid-range branch builds `{f"m{i}": 0}` (`strategies.py:937-939`)
instead of the full component names — confirmed live in
`snapshot_SOL_USDT_20260711_203024.json` (`breakdown keys: m1..m6`), and the
short keys were spliced into `header.score_breakdown` of a real narrated
output. Harmless to calibration (these rows have `engine_trade.valid=false` so
they're never labelled) but it breaks the dashboard's breakdown panel and any
future by-component analysis over HOLD snapshots.
**Fix:** use the canonical `m1_edge_distance … m6_range_intact` keys (values 0)
and full-name notes in that branch.

### P2 — robustness / correctness edges

**FA-08 · BTC.D proxy uses the forming 4H candle**
`fetch_btc_dominance_proxy` (`main.py:544-555`) takes `closes[-1]` from raw
klines — the still-forming bar. `change_4h_pct` is really "0-to-4h so far",
`change_24h_pct` is ~20h+partial, and it violates closed-candle discipline for
a ±4 modifier input. **Fix:** drop the last kline if unclosed (compare its open
time + 4h vs now, same logic as `_drop_forming_candle`), compute changes on
closed bars.

**FA-09 · Labeler skips the remainder of the snapshot bar**
`_fetch_future_candles` uses `startTime = snapshot ts` (`label_snapshots.py:84-87`);
Binance returns bars with openTime ≥ startTime, so the bar forming at snapshot
time (up to 14.5 min of price action the engine's levels were quoted against)
is excluded from the replay. **Fix:** floor `since_ms` to the 15m boundary
(`since_ms -= since_ms % 900_000`) so the snapshot bar is the first replay bar;
the existing same-bar SL-first conservatism already covers it. Document the
~30s of pre-snapshot movement this admits (POLL_SECOND_OFFSET) as negligible.

**FA-10 · monitor.py configures no logging handlers**
`monitor.py` runs the whole engine but never calls `logging.basicConfig`
(`monitor.py:305-309`), so every `logger.info` from main.py is dropped and
warnings surface only via Python's bare lastResort handler (no timestamps) in
journald. CLAUDE.md's own rule says handlers are configured at entry points.
**Fix:** add the same `logging.basicConfig(...)` used by `main.py.__main__` to
monitor's `__main__` (level INFO, or WARNING if journal noise is a concern).

**FA-11 · Health report can't see a placeholder event calendar**
`data/events.json` still contains the two `EXAMPLE` entries dated 2026-12-31;
`data_health_report` (`main.py:1465-1482`) prints "event guard: calendar OK",
so the guard is silently blind until New Year's Eve. **Fix:** warn when the
calendar has no event within the next N days (e.g. 14) or any name contains
"EXAMPLE" — "event guard: calendar EMPTY/PLACEHOLDER — populate data/events.json".
Operator task (deploy checklist): fill in real CPI/FOMC/NFP dates.

**FA-12 · Daily Gemini budget counts successes, not attempts**
`_narrate_with_retry` (`monitor.py:102-109`) can fire 2 narrations per spend,
and each `narrate` internally walks up to 3 keys × transient retries × 2
schema tiers — on an error-heavy day real API calls can exceed
`DAILY_BUDGET` while `state.spend()` records few. Not a correctness bug (the
cap targets successful narrations) but the "hard bound" comment overpromises.
**Fix (cheap):** also `state.spend()` on a failed-then-retried attempt (charge
the retry), and correct the comment; or explicitly document attempts ≠ spends
in `alerts/config.py`. Either is acceptable — pick one, keep it simple.

### P3 — hygiene / docs

- **FA-13** `tools/ab_thinking.py:15,129` tells the operator to update
  `GEMINI_THINKING_LEVEL` "in main.py" — it lives in `config.py` now. The
  monkey-patch `engine.GEMINI_THINKING_LEVEL = level` works (main re-binds the
  name at import) but deserves a comment saying why.
- **FA-14** Snapshots persist with `indent=2` (`main.py:1454`, `monitor.py:170`)
  — ~25-30% corpus bloat for a machine-read file. Switch to
  `separators=(",", ":")` on the VM path (keep whatever the dashboard needs —
  it parses JSON either way). Combined with `CRYPTERA_NO_CHART_SERIES=1` the
  ~340KB/cycle in AZURE_DEPLOYMENT.md drops to ~45KB/cycle.
- **FA-15** `deploy/setup_vm.sh:88-106` systemd unit doesn't set
  `Environment=CRYPTERA_NO_CHART_SERIES=1` — the headless corpus VM will store
  72%-dead-weight chart arrays forever. Add the line (and document the choice).
- **FA-16** `load_calibration` caches at first call (`strategies.py:31-45`) and
  the monitor runs 24/7 — after refreshing `calibration.json` on the VM you
  MUST `systemctl restart cryptera-monitor`. Document in AZURE_DEPLOYMENT.md §6.
- **FA-17** pandas-ta version skew: local dev runs `pandas-ta==0.4.71b0`,
  the VM shim installs `pandas-ta-classic==0.3.14b2`. Column contracts are the
  known fragile point. One-time parity check required (Phase 3 verification):
  run `monitor.py --once --collect-only` on the VM and diff the
  `windowed_indicators` block for the same closed bars against a local run.
- **FA-18** Manual CLI runs (`python main.py`) on a box where the monitor also
  runs share `data/regime_state.json` and advance the hysteresis run-counter
  out-of-cadence. Not an Azure concern (monitor-only box); add one README line.

---

## 3. Remediation plan — 3 phases

Ground rules (all phases — same as current_status.md, which this file
supersedes for planning):

- Phase order strict 1 → 2 → 3. Each phase = its own session; the agent reads
  `CLAUDE.md` + this file first, works ONLY its phase scope, ticks boxes here,
  commits at the end.
- Reference findings by `FA-xx`; do not restate them in commits/notes.
- Verify with synthetic candles / mocked LLM offline; **never** spend Gemini
  calls on deterministic verification. One live smoke run per phase max.
- Locate-before-read: this file gives `file:line` for every item; read only
  the needed window. Batch independent edits; small surgical diffs.
- UTC everywhere; never violate the CLAUDE.md invariants.
- Local `snapshots/*.json` are pre-fix dev artifacts — Phase 1 deletes them.

### Phase 1 — Corpus-semantics correctness (v3.3.1) — DO FIRST, blocks deploy

**Model: Fable 5, effort high.** Small, precision-critical diff: every item
changes what gets persisted/labelled/fitted; a wrong fix contaminates the
corpus the whole plan depends on.

Scope — all findings that touch snapshot/label/calibration semantics:

- [x] FA-01 — `_build_llm_view`: pop `chart_series`, `analysis`, `outcome`
      (and `confluence_breakdown_raw` once FA-02 adds it). Add a unit test
      asserting none of these keys survive into the view, using a dict that
      contains all of them.
- [x] FA-02 — persist `confluence_breakdown_raw` (pre-weight, pre-cap, both
      rubrics incl. the FA-07-fixed no-fade branch); `_logistic_winrate`
      consumes raw; `calibrate.py` prefers raw with legacy fallback. Test: with
      a synthetic calibration file (weights ≠ 1), assert breakdown ≠ raw,
      logistic input == raw.
- [x] FA-04 — gate `tp_prices` by `MAX_TARGET_ATR_1H_MULTIPLE × atr_1h` before
      primary TP selection (fail-open on missing atr_1h). Test: far-only
      targets ⇒ `valid=false`; near+far ⇒ far one excluded from primary AND
      candidates agree with primary.
- [x] FA-05 — `fetch_open_interest` failure path: `oi_now=None` ⇒ no deltas, no
      percentile, no history append, `value: null`. Test the function with a
      mocked failing request.
- [x] FA-07 — canonical `m1_edge_distance…m6_range_intact` keys in the no-fade
      branch (notes too).
- [x] FA-08 — BTC.D proxy computed on closed 4H bars only.
- [x] FA-09 — labeler `since_ms` floored to the 15m boundary; docstring note.
- [x] Bump `SCHEMA_VERSION` → `"3.3.1"`. Delete the 7 dev snapshots in
      `snapshots/` (pre-fix semantics; corpus starts clean on the VM).
- [x] Verification: full pytest suite (existing 40 + new tests above) PASS;
      one mocked-LLM `compute_snapshot_no_llm` run on live perp data asserting
      `confluence_breakdown_raw` present, OI block sane, LLM view free of the
      FA-01 keys and < ~20KB compact.
- [x] Commit `v3.3.1: FA-01/02/04/05/07/08/09 — corpus semantics` + tick boxes.

### Phase 2 — Collector & tooling robustness (v3.3.2)

**Model: Sonnet 5, effort high.** Mechanical, well-specified hardening; no
snapshot-shape changes (no schema bump, no relabel).

- [x] FA-03 — atomic writes (`.tmp` + `os.replace`, copy `alerts/budget.py`
      pattern) for `_save_oi_history`, `_write_json_file`, `web/app.py`
      `_save_index`; module-level `threading.Lock` around the OI-history
      read-modify-write in `fetch_open_interest`. Test: hammer
      `fetch_open_interest`'s history section from N threads with a mocked
      API; assert no lost appends and file always parses.
- [x] FA-06 — `eval_engine` engine-outcome universe as primary + existing LLM
      cuts retained; explicit counts per universe. Test with 3 synthetic
      snapshot files: engine-only, narrated, unlabelled.
- [x] FA-10 — logging.basicConfig in monitor `__main__`.
- [x] FA-11 — health-report placeholder/stale-calendar warning.
- [x] FA-12 — budget: charge retries (or document attempts≠spends) — pick one,
      one-line diff plus comment fix.
- [x] FA-13 — ab_thinking docstring/print path correction + monkey-patch
      comment.
- [x] FA-14 — compact `json.dump` for snapshot persistence (both call sites).
- [x] FA-15 — `Environment=CRYPTERA_NO_CHART_SERIES=1` in the systemd unit.
- [x] FA-16 — AZURE_DEPLOYMENT.md §6: restart-after-recalibrate note.
      FA-18 — README one-liner re CLI runs sharing hysteresis state.
- [x] Verification: pytest PASS; `python monitor.py --once --collect-only`
      (live data, no keys needed) writes a compact snapshot, console shows
      health lines incl. the new calendar warning; corrupt `oi_history.json`
      by hand → next run recovers without wiping (atomic + lock in place).
- [x] Commit `v3.3.2: FA-03/06/10/11/12/13/14/15/16/18 — collector hardening`.

### Phase 3 — Pre-deploy prediction-quality improvements + deploy readiness

**Model: Opus 4.8, effort high.** Three improvements only — each is
low-risk, stays inside SMC/PA, and needs no eval data to justify. Explicitly
out of scope: rubric re-weights, threshold changes, new data sources (all
measurement-gated; the corpus doesn't exist yet). Don't over-engineer.

- [ ] IMP-1 — **Deeper 15m history for the session-volume baseline.**
      `LIMITS["15m"] = 300` gives ~3 days ⇒ only ~12 same-hour samples behind
      the session gate (`indicators.session_volume_context`) — a noisy basis
      for a gate that can halve position size. Raise `LIMITS["15m"]` to 1000
      (~10 days ⇒ ~40 samples/slot). Verified safe: all 15m consumers use
      `tail(...)` windows or last-5-swings; only EMA/ATR warmup and the session
      baseline see the extra bars. Confirm memory/latency is a non-issue in the
      smoke run.
- [ ] IMP-2 — **1D pools + PWH/PWL in sweep detection.** `detect_mtf_sweep`
      (`strategies.py:136-146`) scans 4h/1h/15m pools + PDH/PDL but ignores the
      1d liquidity pools and weekly levels Phase 2 (v3.3) added — the exact
      levels whose sweeps matter most. Add `"1d"` to the pool loop (tier_rank
      4) and pass `pwh`/`pwl` (tier "pd"-equivalent) via new optional params
      from both call sites (`evaluate_strategies` trend C3 + `_m2_edge_sweep`).
      Synthetic test: sweep of a 1d pool scores the HTF 10 (→15 rebudgeted).
- [ ] IMP-3 — **Automated labelling on the VM.** The plan is "calibration runs
      on the collected snapshots" — make labels accumulate without operator
      discipline: a `cryptera-label.timer` + oneshot service running
      `python -m tools.label_snapshots --horizon 24` daily (off-peak, e.g.
      03:10 UTC). Labels only complete-horizon snapshots by design, so daily is
      exactly right. `calibrate` stays MANUAL (deliberate: refits should be a
      reviewed decision + service restart, FA-16). Add both units to
      `deploy/setup_vm.sh` (registered, enabled with the monitor).
- [ ] Deploy-readiness checklist (tick each on the actual VM):
      - [ ] FA-17 parity check: `monitor.py --once --collect-only` on the VM;
            diff `windowed_indicators`/`smc_context` for the same closed bars
            vs a local run (small tolerance). Any column-name NaN ⇒ stop, fix
            the shim before going 24/7.
      - [ ] `data/events.json` populated with real upcoming high-impact events
            (health line must say calendar OK for the right reason).
      - [ ] `.env` on VM: GEMINI key(s) + Telegram creds; `--once` full smoke
            (heartbeat + one narrated alert path if a setup is active).
      - [ ] `snapshots/` empty at service start; `systemctl enable --now
            cryptera-monitor` + label timer; journal clean after 2 cycles.
- [ ] Verification: pytest PASS; one mocked-LLM end-to-end run (IMP-1/2 live);
      timer fires in a `systemd-run --on-active` dry test or equivalent.
- [ ] Commit `v3.3.3: IMP-1/2/3 + deploy readiness` + tick boxes; update the
      Session log below.

---

## 4. Model assignment summary

| Phase | Model / effort | Why |
| --- | --- | --- |
| 1 — Corpus semantics (P0/P1) | **Fable 5 high** | Every line changes persisted/fitted data; wrong fix = contaminated corpus |
| 2 — Collector hardening | **Sonnet 5 high** | Mechanical, fully specified, volume over depth |
| 3 — Improvements + deploy | **Opus 4.8 high** | Small design judgment (sweep tiers, timer), plus live-VM verification |

## 5. Session log

- 2026-07-12 — Final audit completed (this file). FA-01…FA-18 identified;
  3-phase plan created. Phases not started. current_status.md phases 1-3
  (BUG-01…24) all complete and verified during this audit.
- 2026-07-12 — **Phase 1 complete (v3.3.1)**: FA-01/02/04/05/07/08/09 fixed;
  schema bumped 3.3.1; the 7 pre-fix dev snapshots removed from `snapshots/`
  (archived to session scratchpad, not deleted). 9 new unit tests added
  (49/49 pytest PASS) + one live-perp `compute_snapshot_no_llm` smoke run:
  raw breakdown present + canonical keys, OI sane, primary TP ∈ candidates,
  LLM view free of FA-01 keys at 8.1KB compact — ALL PASS, zero Gemini calls.
  Phase 2 (Sonnet 5) next.
- 2026-07-12 — **Phase 2 complete (v3.3.2)**: FA-03/06/10/11/12/13/14/15/16/18
  fixed. Atomic `.tmp`+`os.replace` writes (`_write_json_file`,
  `_save_oi_history`, `web/app.py._save_index`) + a module-level
  `threading.Lock` around `fetch_open_interest`'s OI-history critical section
  (apply_regime_hysteresis documented as already-safe: event-loop-thread-only,
  no `await`). `eval_engine` now treats `engine_trade_outcome` as the primary
  universe (`by_mode` bucket added) with `trade_decision_outcome` kept as
  secondary LLM-only cuts (`by_action`/`by_selected_from_engine`); both
  universes' counts reported explicitly. `monitor.py` configures
  `logging.basicConfig` in `__main__`. `data_health_report` gained
  `_calendar_health` (EMPTY/PLACEHOLDER/STALE/OK,
  `config.EVENT_CALENDAR_STALE_DAYS=14`). Budget attempts≠spends documented
  in three places (monitor.py docstring, `_narrate_with_retry`,
  `alerts/config.py`) rather than charging retries — avoids penalizing
  transient network blips. `ab_thinking.py` corrected to point at
  `config.py` for `GEMINI_THINKING_LEVEL` + explained the monkey-patch.
  Snapshot persistence (`main.py._persist_snapshot`, monitor's narrated
  re-write) switched to `separators=(",", ":")`. `deploy/setup_vm.sh`
  systemd unit sets `CRYPTERA_NO_CHART_SERIES=1`. AZURE_DEPLOYMENT.md §6 +
  README got the FA-16/FA-18 notes. No snapshot-shape change — schema stays
  3.3.1, no relabel needed. 4 new unit tests (concurrent OI-history hammer:
  8 threads × 15 calls, 0 lost updates; atomic-write no-leftover-tmp;
  4-state calendar health; eval_engine 3-file dual-universe) — 53/53 pytest
  PASS. Live smoke: `monitor.py --once --collect-only` twice against real
  SOL/BTC/ETH data — compact snapshots written, health line showed the new
  PLACEHOLDER calendar warning, OI history incremented by exactly +1/symbol
  (no lost updates); hand-corrupted `oi_history.json` between runs — next
  cycle recovered cleanly (valid JSON, no crash, no permanent wipe) — ALL
  PASS, zero Gemini calls. Test artifacts (6 snapshot files, temp state)
  cleaned up after; pre-existing `data/*.json` restored from backup.
  Phase 3 (Opus 4.8) next.
