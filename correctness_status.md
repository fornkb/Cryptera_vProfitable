# Cryptera — Correctness → Quality Status (post-v3.3 audit)

Source of truth: `final_improvement_report.md` (2026-07-20, merged & verified). Reference
findings by ID (**B1.0–B28.0**, **I1.0–I15.0**); do not restate them here. This supersedes
`current_status.md` (the audit_report.md / BUG-01…24 plan, fully done). Each session: read
`CLAUDE.md` + this file first, work only its scope, tick boxes, note ≤1-line outcomes.

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

- [ ] **B1.0** — chronological labeler state machine (PENDING_ENTRY→OPEN→TP|SL|TIMEOUT),
      extend 1m drill to entry+any-exit, horizon close-time ≤ end, entry-bar excursion fix,
      `label_version`/`entry_time`/`exit_time`/`resolution_used`. Relabel + bump label version.
- [ ] **B2.0** — MR outside-value invalidation + sign-aware `_m1_edge_distance` + universal
      DOA guard in `compute_trade_geometry`.
- [ ] **B3.0 / I2.0** — deterministic `validate_and_normalize_analysis` after every model
      response (price/ID membership, gates recomputed engine-side, DOA, HOLD-null,
      `selected_from_engine` derived, `contract_violations[]`).
- [ ] **B4.0** — win-rate semantics: blend excursion+terminal (`end_rr`), rename MFE fraction
      (no `win_rate`/`realized_rr` may be an excursion), `ENTRY_ATR_CAP`.
- [ ] **B5.0** — RSI-slope bias-relative (`rel_slope`) + mirror test.
- [ ] **B6.0** — one distance rule for OB and FVG (0 inside zone, else nearest boundary) + parity test.
- [ ] **B10.0** — PDH/PDL/PWH/PWL/weekly_open by calendar identity vs explicit UTC now.
- [ ] **B13.0** — C1 conflict → 0 (+`tf_conflict`), 4H-dir/1H-neutral → 5.
- [ ] **B15.0** — eval integrity: engine universe reads `strategies.*`; bucket by
      `setup_direction`; realized-R expectancy.
- [ ] **B16.0** — hysteresis keyed by `(symbol, last_closed_15m_bar_open_ms)` (bar-idempotent).
- [ ] Riders: **B25.0** (labeler compact write), **B26.0** (ab_thinking canonical replay),
      **B27.0** (funding interval inference).
- [ ] Relabel + recalibrate discipline; schema/label_version bump; full pytest + regression seeds.

## Wave 2 — I1.0 adjudicator (on the I2.0 validator)

- [ ] Trigger predicate `should_call_llm`; `engine_case` block; schema v2 AGREE/UPDATE/REJECT;
      `level_id` candidate-ID selection; `by_verdict` eval; shadow rollout.

## Wave 3 — structure & menus (semantics-changing; land before collection restarts)

- [ ] **I3.0** structural event state machine (absorbs B7.0/B8.0/B16.0 full).
- [ ] **I4.0** sequential setup detectors + expiry (absorbs rest of B2.0/B18.0).
- [ ] **I5.0** typed liquidity & sweep quality (B12.0).
- [ ] **I6.0** OTE anchored to displacement leg (B9.0).
- [ ] **I7.0** candidate-first geometry & menu enrichment (B11.0/B19.0).
- [ ] **B17.0** flat-tape swing artifacts; **B21.0** DST kill zones; **B22.0** stable POC windows;
      **B23.0** distance-based episode identity.

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
  created. Next: Wave 1, starting with B1.0 (chronological labeler).
