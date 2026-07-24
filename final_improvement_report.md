# Cryptera — FINAL Prediction-Engine Improvement Report (merged & verified)

Consolidation date: 2026-07-20. This report merges two independent audits of the
same tree (branch `v3.2.4-correctness`):

- **[A1]** `improvements_report_agent1.md` (2026-07-19, findings B1.0–B18.0 + plan I1.0–I12.0)
- **[A2]** `improvements_report_agent2.md` (2026-07-18, findings PE-01…PE-16)

**Merge policy:** every claim from either report was re-verified against the
current code before inclusion — *[verified]* means reproduced with a synthetic
offline probe (two scratchpad scripts, **21/21 checks confirmed**, zero Gemini
calls); *[code-verified]* means confirmed by direct code reading at the cited
lines. Claims that did not survive verification were corrected or dropped —
see §5 (Cross-audit verification notes). Overlapping findings are merged into
one entry with both sources credited. Prior closed IDs (BUG-01…24, FA-01…18,
IMP-1…3) are referenced, never restated.

Severity legend: **P0** = corrupts live decisions or the corpus — fix before
collecting another snapshot · **P1** = materially degrades prediction quality ·
**P2** = quality/robustness · **P3** = hygiene.

**Operational context:** the v3 Azure VM is collecting live snapshots. Plan:
**stop the collector first** (`sudo systemctl stop cryptera-monitor && sudo
systemctl disable --now cryptera-label.timer`), land the fixes, then resume.
Many fixes change snapshot/label semantics — invariant 5 applies throughout
(schema bump; archive the pre-fix corpus; never fit calibration on it).

---

## 1. Executive summary

Both audits independently converge on the same diagnosis: the architecture is
sound, all 45 previously-audited findings are genuinely fixed, but the engine
currently cannot make a defensible claim about prediction accuracy because
**the measurement layer lies before the signal layer even gets a chance**, and
several core SMC/ICT concepts are additive approximations rather than event
sequences. The merged list contains **28 findings**. Four are P0:

1. **B1.0 — the labeler cannot order entry vs exit.** A short labeled
   `tp_first` with certainty when the TP could have been hit *before the entry
   ever filled* (verified). Every downstream number — win rate, weights,
   logistic probabilities, A/B verdicts — inherits this.
2. **B2.0 — ranging-regime breakouts are scored as strong fades** and the MR
   geometry can emit a short whose stop is already below live price
   (verified) — the engine's biggest live-decision hole.
3. **B3.0 — "rules decide, LLM narrates" is enforced only by the prompt.**
   No post-call validator checks price membership, stop side, action gates, or
   `selected_from_engine` honesty; an invalid plan can be persisted, labeled,
   and alerted.
4. **B4.0 — the calibrated "win rate" is not a win probability.** Timeouts get
   MFE-only credit (a full round-trip scores 0.75–0.95, verified), the
   universe is conditioned on fill, and `realized_rr` is an excursion, not
   PnL.

The deepest *new* signal-layer bugs found across the two audits: bearish
momentum scored backwards on RSI slope (B5.0, verified — every short setup),
the same structural break emitted as both BOS and CHoCH (B7.0, verified),
OB proximity measured to the far edge while FVG uses the near edge — the same
zone scores 0 vs 15 (B6.0, verified), and PDH/PDL/PWH/PWL wrong at UTC day/week
boundaries — PWH is wrong **all of Monday** (B10.0, verified).

The improvement plan is led by the operator-mandated **I1.0 — "engine
proposes, LLM adjudicates"**: the LLM is called only above a confluence
threshold, receives all precomputed data plus the engine's structured
arguments, and returns AGREE / UPDATE / REJECT with UPDATE as the first-class,
most-valued outcome — bounded by the deterministic validation layer (I2.0)
that also fixes B3.0.

---

## 2. Bugs (severity-sorted)

### P0

---

**B1.0 · The labeler does not reconstruct entry-before-exit chronology; the
horizon includes one extra bar; entry-bar excursions are contaminated**
*[verified]* — statistical/technical — sources: **A2 PE-01** (primary) + **A1
B9.0** (excursion aspect)

*Where:* `tools/label_snapshots.py:182-262` (`_label_one`),
`tools/label_snapshots.py:119-120` (horizon cutoff),
`tools/label_snapshots.py:81-91` (`_floor_to_15m`).

*The bug — three layers, worst first:*

1. **Order-of-events is assumed, not reconstructed.** Entry is detected by "a
   bar's range contains the entry price"; the *same full bar* is then reused
   for SL/TP checks. Verified: a short with entry 100 / SL 105 / TP 90
   replayed against one bar `open=95, low=89, high=101` labels
   **`tp_first`, `bars_to_close=0`, `ambiguous_15m=false`** — but the bar
   opened *below* the entry, so a perfectly ordinary intrabar path hits 90
   before price ever rallies to fill the 100 sell. The result is unknowable
   at 15m resolution, yet it is recorded as a confident win. The 1-minute
   drill (the `f949ab9` fix) only fires when **SL and TP** coexist in one
   bar — it never resolves **entry vs a single exit level**, which is the far
   more common ambiguity.
2. **The horizon keeps the bar that OPENS at +24h.** `timestamp <= cutoff`
   retains 97 bar-opens for a 24h horizon (verified) — outcomes can be
   decided by price action up to +24h15m. Small, systematic, and it
   disagrees with `HORIZON_HOURS`' meaning everywhere else.
3. **Entry-bar excursions are contaminated** (A1 B9.0): MFE/MAE are computed
   from the entry bar's *full* range, including movement that happened before
   the limit filled — for a long limit below price the pre-fill upside is
   credited as MFE, inflating `realized_rr` by up to a full bar range
   (routinely 0.3–0.8R on 15m), which then feeds B4.0's timeout credit and
   eval's `avg_realized_rr`.

*Why it matters:* this is the ground truth for *everything* — component
weights, logistic win probabilities, score→win-rate buckets, eval hit rates,
A/B verdicts, and eventually the I1.0 by-verdict readout. False wins and false
losses at the label layer are unrecoverable downstream.

*Proposed fix (canonical chronological labeler — one shared implementation):*
- Rewrite `_label_one` as an order-state machine: `PENDING_ENTRY → OPEN →
  TP | SL | TIMEOUT`, walking bars in time order.
- Extend the 1m drill: resolve at 1-minute resolution **whenever the entry and
  any exit level coexist in a 15m bar's range** (not only SL+TP). When 1m data
  is unavailable or a 1m bar itself still straddles entry+exit, emit
  `intrabar_unknown` — a separate class, never silently a win, and
  conservatively a loss only for the SL-side tiebreak as today.
- Horizon: require the bar's **close time** ≤ horizon end (`timestamp <
  cutoff` on open-times), dropping the +24h bar.
- Entry bar excursions: start MFE from the fill (conservatively `fav=0` on the
  entry bar; keep the entry bar's MAE — pessimistic is safe), or use the 1m
  window when it was fetched anyway.
- Record `label_version`, `entry_time`, `exit_time`, `resolution_used` in each
  outcome block so corpora of mixed label semantics can never be pooled
  unnoticed (pairs with B25.0's versioning).
- Tests: target-before-entry, stop-before-entry, entry+one-exit same bar,
  exact horizon boundary, gap-through-entry, and long/short mirrors of each.
- Labeler change ⇒ relabel everything; bump label version.

---

**B2.0 · Ranging-regime breakouts are scored as strong fades and the geometry
can emit trades that are dead on arrival (price already beyond the SL)**
*[verified]* — trader-logic + technical — sources: **A1 B1.0** (primary) +
**A2 PE-12** (setup-sequence aspect)

*Where:* `strategies.py:346-363` (`_mr_setup`), `strategies.py:366-377`
(`_m1_edge_distance`), `strategies.py:793-807` + missing guard before
`strategies.py:900` (`compute_trade_geometry`, MR branch).

*The bug — three compounding holes:*

1. `_mr_setup` returns a fade direction whenever price is at/beyond the outer
   15% band of the value area with **no outer bound and no prerequisite**:
   price 5% above VAH — a clean, accepted breakout — still returns
   `("bearish", vah)` (verified). There is no requirement of an established
   balanced range, no sweep of the edge, no market-structure shift — the fade
   fires on *location alone* (A2's sequencing critique).
2. `_m1_edge_distance` uses `abs(price - edge)`, blind to which side of the
   edge price is on: 10% of the VA span *above* VAH scores **18/25 "near
   edge"**; just above VAH scores the full 25 (verified). Maximum location
   credit is paid at the single worst place to fade.
3. The MR geometry anchors entry at the edge and SL at `edge ± 2*buf` with no
   check against live price. Verified: price 105, VAH 100, ATR 0.8 → emitted
   `SELL entry=100.0 sl=100.8 tp=95.0` — the stop is 4.2 below the market.
   `engine_gate` treats `valid=true` + score ≥ 60 as ACTIVE and can alert on
   it; the labeler replays it into a near-guaranteed `sl_first`, poisoning MR
   calibration rows with structurally impossible trades.

*Proposed fix:*
- `_mr_setup`: outside-value invalidation — if `price > vah + OUT_TOL` (or
  `< val - OUT_TOL`, `OUT_TOL = max(0.25*ATR_1h, 0.1*span)`), return no-fade
  **unless** the M2 sweep+reclaim fired on that edge (a failed breakout is the
  *legitimate* fade trigger — the ICT-correct trade survives, the naked
  breakout fade dies).
- `_m1_edge_distance`: sign-aware — inside the band → current scoring;
  outside the edge → 0 unless reclaim-confirmed (then score reclaim quality).
- `compute_trade_geometry`: universal DOA guard before the final
  `out.update(...)` — SELL: `valid=false` if `price >= stop_loss`; BUY:
  `valid=false` if `price <= stop_loss`. Mode-agnostic, one unit test per side.
- Longer-term the full sequential MR definition (raid → absorption → MSS →
  return) lands as I4.0. Scoring change ⇒ invariant 5.

---

**B3.0 · "Rules decide, LLM narrates" is a prompt rule, not an enforced
invariant — no post-LLM semantic validation exists** *[code-verified]* —
technical/architectural — sources: **A2 PE-02** (primary; A1 had raised the
same gap as improvement I2.0)

*Where:* prompt rules at `main.py:673-745`; response handling only parses JSON
(`main.py:1000-1032`, `main.py:1173-1260`); finalization only splices
breakdown + mtf table (`main.py:1263-1378`). No semantic validator anywhere.

*The bug:* nothing checks the model's output beyond JSON shape. The model can
today: output a price absent from the candidate menu; set `header.score` /
`header.action` contrary to the engine's gates; emit BUY with the stop above
entry; claim `selected_from_engine=true` while its prices differ from the
primary; and emit a live trade while `engine_trade.valid=false` or the volume
HARD_GATE is active. The plain-JSON fallback tier (schema-rejected path) has
no local schema validation at all. Any such output is persisted, labeled by
the labeler (it reads `analysis.trade_decision` prices verbatim), pushed to
Telegram, and later used to judge LLM-vs-engine value — so a hallucinated
plan doesn't just slip through, it contaminates the divergence eval that is
supposed to detect exactly this.

*Why it is P0 now:* under the I1.0 redesign the LLM gains *more* freedom
(UPDATE verdicts). That freedom is only safe on top of deterministic
enforcement; this validator is the load-bearing wall of the new design.

*Proposed fix:* a deterministic `validate_and_normalize_analysis` step after
every model response, before persistence/alerts (detailed as I2.0):
- recompute score/action/bias/volume/event gates from engine data — never
  trust the model's copies; enforce action-vs-threshold consistency in code;
- validate every output price against the set of price levels actually
  present in the LLM view (relative tolerance ≤ 5e-4) — better, move to
  **candidate-ID selection** (the model outputs IDs, not floats — A2's
  refinement, adopted);
- enforce direction/entry/stop/target ordering, tick precision, and re-apply
  the B2.0 DOA guard to the model's chosen geometry;
- recompute all R:R / distance fields engine-side;
- force all trade fields null on HOLD or invalid geometry;
- derive `selected_from_engine` by comparison, never from the model's claim;
- on any violation: revert the offending field(s) to the engine primary and
  record `analysis.contract_violations[]` (an eval-able honesty metric).

---

**B4.0 · The calibrated "win rate" is not a win probability: MFE-only timeout
credit, fill-conditioned universe, misnamed metrics** *[verified]* —
statistical — sources: **A1 B7.0 + B8.0** and **A2 PE-03** (independently
found; merged)

*Where:* `tools/calibrate.py:94-111` (`_soft_label`),
`tools/label_snapshots.py:244-260` (`realized_rr` = MFE/risk),
`tools/calibrate.py:65-91` (triggered-only universe),
`strategies.py:815-821/844-851` (no entry-distance cap → manufactured
`not_triggered` rows).

*The bug — three related distortions:*

1. **Timeout credit ignores where price ended.** `min(1, realized_rr /
   target_rr)` where `realized_rr` is max favourable excursion: a timeout that
   ran 1.5R toward a 2R target and gave *all of it back* (or ended at a loss)
   scores 0.75 (verified; A2's 1.9R example scores 0.95). The logistic model
   fitted on this target estimates "fraction of target MFE achieved", not
   `P(TP before SL)` — yet it is surfaced as `empirical_win_rate`, anchors the
   LLM's `probability`, and would drive sizing. Rubrics that produce
   spike-and-retrace setups (exactly what bad fades do) get systematically
   inflated weights.
2. **The universe is conditioned on fill, silently.** Only `triggered` rows
   are fitted; the engine's *best* setups — momentum leaves without the
   pullback, the OB never tags — vanish from the sample, while the setups
   that filled (price came in deep = weaker) dominate. Classic limit-order
   survivorship; the reported probability is `P(win | filled)` presented as
   `P(win)`.
3. **No entry-distance cap compounds it.** A stale OB mid 4-5 ATR from price
   becomes the primary entry, manufacturing `not_triggered` rows and pushing
   real signal out of the corpus.

*Proposed fix (calibration v3 target semantics — full design in I9.0):*
- Model three distinct quantities: `P(entry triggers before expiry)`,
  `P(TP before SL | triggered)` fitted on **true binary** TP/SL outcomes with
  timeouts as their own class (or competing-risk treatment), and
  `E[net R | signal]` including timeout terminal value, fees, slippage.
- Immediate cheap patch while v3 is built: blend excursion with terminal
  state — label `end_rr` (close-at-horizon in R) in `_label_one`, then
  `y_timeout = clamp01(0.5*realized_rr/target_rr + 0.5*max(0,end_rr)/target_rr)`.
- Rename the MFE fraction (`target_progress_ratio`) — nothing named
  `win_rate`/`realized_rr` may be an excursion.
- Geometry: `ENTRY_ATR_CAP` (≈1.5×ATR15m) on entry candidates, falling back to
  market entry as today; per-component trigger-rate diagnostics in calibrate;
  a shadow market-entry label per snapshot for fill-bias validation.
- Evaluate probability quality with Brier/log-loss/reliability curves, not
  bucket monotonicity. Labeler change ⇒ relabel.

---

### P1

---

**B5.0 · Bearish momentum is scored backwards: the RSI-slope adjustment is not
direction-aware** *[verified]* — trader-logic — source: **A2 PE-06**

*Where:* `strategies.py:258-280` (`_c4_momentum`).

*The bug:* the slope bonus is applied with the same sign for both directions:
`slope > 5 → +5, slope < -5 → -5`. For a **bearish** setup, falling RSI
(momentum *with* the short) is penalized and rising RSI (momentum *against*
it) is rewarded. Verified: bearish base 10, slope −6 → **5/15**; slope +8 →
**15/15**. Exactly inverted. Every short setup since this component existed
has had its momentum evidence scored backwards by up to ±10 of 15 points —
one of the cleanest direct hits on prediction accuracy in either audit, and
it biases the engine structurally toward late shorts (entering when RSI is
already bouncing) and away from shorts with genuine downside momentum.

*Proposed fix:* express the slope in bias-relative space:
`rel_slope = slope * (+1 if bias == "bullish" else -1)`, then
`rel_slope > 5 → +5`, `rel_slope < -5 → −5`. Add **mirror property tests**
(A2's recommendation, adopted for every directional component): invert a
synthetic price/momentum/CVD series and the direction, and require identical
component scores — this class of asymmetry bug can't survive such a test
suite. Scoring change ⇒ invariant 5.

---

**B6.0 · C2 measures OB distance to the FAR edge but FVG distance to the NEAR
edge — the same zone scores 0 vs 15** *[verified]* — trader-logic — source:
**A1 B2.0**

*Where:* `strategies.py:224` (OB: `bottom` for bullish / `top` for bearish —
the far boundary) vs `strategies.py:232` (FVG: near boundary).

*The bug:* verified with an identical zone `[97.0, 99.8]`, price 100, ATR 1.0:
as an OB it scores **0** ("3.00 ATR away"), as an FVG **15** ("0.20 ATR
away"). Order blocks — the engine's primary entry vehicle — are systematically
under-credited by the full height of the zone: C2 fires only after price
trades deep into the OB, scores skew across regimes, and the OB-vs-FVG balance
any future calibration fits is distorted from the start.

*Proposed fix:* one distance rule for both zone types — `d = 0` when price is
inside the zone, else distance to the nearest boundary. Keep the ATR tiering.
Regression test asserting OB/FVG parity on an identical zone. Scoring change
⇒ invariant 5.

---

**B7.0 · BOS and CHoCH are not mutually exclusive — the same break is emitted
as both; displacement certification is unanchored** *[verified]* —
trader-logic — source: **A2 PE-05** (core)

*Where:* `smc.py:183-215` (BOS = any close beyond the latest confirmed
high/low, unconditioned on trend) and `smc.py:217-234` (CHoCH from the same
break); `smc.py:103-114` (`_has_displacement_fvg`).

*The bug:* in a `LH/LL` (bearish) structure, the first close above the last
confirmed lower-high is simultaneously reported as **bullish BOS** and
**bullish CHoCH** — verified synthetically (same `bars_ago` on both), and A2
observed it live in an ETH snapshot that was *still classified Trending
Bearish*. Semantically these are different objects: in a downtrend, a break
of a lower-high is a change of character (potential reversal), and "BOS" is
reserved for continuation breaks in the trend direction. Consumers disagree
accordingly: M6 treats either fresh event as "range at risk" (accidentally
right), but the LLM receives contradictory structure narrative, and any
future BOS-continuation logic (I6.0's displacement bonus, regime routing)
would double-count one event. Secondary: displacement is certified by *any*
same-direction FVG anywhere in the span from swing formation to break — an
unrelated gap dozens of bars before the actual breaking impulse qualifies.

*Proposed fix:* make structural events exclusive and trend-conditioned (full
state machine in I3.0):
- in bullish structure: close above the continuation high = BOS; close below
  the protected low = bearish CHoCH — never both from one event; mirror for
  bearish structure;
- after a CHoCH, enter a `transition` state; require displacement + a
  confirming BOS in the new direction before the trend label flips;
- displacement: only count an FVG inside the *breaking impulse* (between the
  origin swing of the final leg and the break candle), not the whole span.

---

**B8.0 · A fresh counter-trend CHoCH never vetoes continuation; displacement
is computed but consumed nowhere; CHoCH is invisible in mixed structures**
*[code-verified]* — trader-logic — sources: **A1 B4.0** + **A2 PE-05** (sub)

*Where:* `strategies.py:199-212` (C1 reads only structure strings + ADX);
`smc.py:206,214` (displacement produced; grep confirms zero consumers in
`strategies.py`); `smc.py:219-234` (CHoCH only for exact `HH/HL`/`LH/LL`).

*The bug:* the single most important SMC "stand down" signal has no effect on
the trend rubric. Structure labels are a last-4-swing majority, so they lag a
CHoCH by several swings — the engine keeps proposing continuation longs into
a broken market with C1 paying 15-25. `bos.displacement` — the ICT-native
quality marker — is dead weight in the snapshot. And because CHoCH requires an
exact HH/HL or LH/LL label, the transition structures (`HH/LL`, `LH/HL`) where
character changes actually happen can never report one.

*Proposed fix:*
- **Counter-CHoCH veto:** if `choch.fresh` on 1H (or 4H) opposes `trend_bias`,
  cap the final score at `CONDITIONAL_ENTRY_THRESHOLD - 1` (same mechanism as
  the event guard) and set `counter_choch_warning` for the LLM. An explicit
  capped state beats silently zeroing C1 — easier to calibrate and explain.
- Displacement as evidence: displaced BOS in the bias direction → small C1
  bonus; in MR mode a *displaced* break of the range edge counts against M6
  even after `fresh` ages out.
- Track the protected swing (the low that produced the last HH / high that
  produced the last LL) independently of the majority label so CHoCH works in
  mixed structures — this is the same machinery B7.0's state machine needs.

---

**B9.0 · OTE / premium-discount is computed on a stale, non-impulse leg; the
re-bracketed dealing range keeps the pre-breakout leg direction** *[verified]*
— trader-logic — source: **A1 B3.0**

*Where:* `smc.py:447-481` (`_current_dealing_range` — `leg` fixed at line 462
from the latest confirmed swings, never recomputed after the re-bracket walk
at 473-480); consumed by `get_premium_discount` (`smc.py:420-444`) and
`_c6_ote` (`strategies.py:306-312`).

*The bug:* after a breakdown below the last confirmed low, the range is
correctly re-bracketed to older swings (BUG-09 fix) but `leg_direction` still
says `bullish` because the most recent confirmed swing happens to be a high —
verified: highs=[100,105], lows=[95,98], price 96 → range [95,100],
`leg=bullish`. The OTE band is then drawn as a pullback-to-buy zone inside
what is now a bearish leg; C6 can pay the wrong side and the `pd_zone` shown
to the LLM describes a leg that no longer exists. More fundamentally, OTE
belongs to the *impulse leg that produced the last BOS*, not to "whichever
confirmed swings happen to bracket price".

*Proposed fix:* (1) minimal — recompute `leg` after re-bracketing (from the
BOS direction when one exists, else the chronological order of the chosen
bracket swings); when `brackets_price` is false emit `leg_direction=None` and
have `_c6_ote` treat it as no-OTE. (2) correct — I6.0: anchor OTE to the
swing pair of the most recent BOS/CHoCH event per TF (impulse origin → break
extreme) with `leg_source` recorded.

---

**B10.0 · PDH/PDL are wrong for the first hour of every UTC day; PWH/PWL and
weekly_open are wrong for ALL of Monday** *[verified]* — trader-logic /
technical — source: **A2 PE-10**

*Where:* `price_action.py:184-192` (`calculate_previous_day` — always
`daily.iloc[-2]`), `price_action.py:235-256` (`weekly_levels` — last closed
daily group treated as "current week").

*The bug:* both functions index by position over groups of **closed** bars,
not by calendar position relative to now:
- Between 00:00 and 01:00 UTC no 1H bar of the new day has closed yet, so the
  last date-group *is* yesterday and `iloc[-2]` returns **two days ago**.
  Verified: with yesterday's true high 300 and the prior day's 200, the
  function returns PDH=200 during that hour.
- On Monday, before Monday's daily candle closes (i.e. the **entire day**),
  the last closed daily group is last week — so `weekly_levels` reports last
  week as the "current" week (weekly_open = *last* week's open) and the week
  before last as "previous" (PWH/PWL two weeks old). Verified: true prev-week
  high 600, returned 500.

*Why it matters:* PDH/PDL/PWH/PWL are the highest-value ICT liquidity
references in the engine — sweep detection (C3/M2 scans them directly),
target menus (PWH/PWL), and the LLM's HTF-draw reasoning. Monday — the
weekly-open session where these levels matter most — runs on levels from the
wrong week all day; the midnight-UTC hour (late NY afternoon) runs on the
wrong day.

*Proposed fix:* select periods by calendar identity relative to an explicit
UTC `now`, not by position: previous day = the group whose date ==
`(now - 1 day).date()`; previous week = the ISO week `now`'s week − 1; the
current daily/weekly *open* comes from the first 1H/1D bar of the current
period when present, else from the raw (forming) candle's open captured as a
non-signal context field. Tests: 00:00-01:00 UTC, Monday, ISO-year rollover,
missing bars.

---

**B11.0 · Geometry is not tied to structural invalidation: the ATR cap clips
the stop INSIDE invalidation; stops come from generic pools; the primary TP
can be absent from its own candidate menu; "current price" masquerades as a 1H
level** *[verified]* — trader-logic + technical — sources: **A2 PE-09**
(primary) + **A1** (menu/consistency aspects)

*Where:* `strategies.py:823-863` (stop selection + `SL_ATR_CAP` clip),
`strategies.py:879-895` + `strategies.py:627-643` (primary TP from raw
prices) vs `strategies.py:674-748` (menu merge), `strategies.py:651-662`
(`_tf_of_source` default "1h"), `strategies.py:793-807` (MR stop).

*The bug — four defects:*

1. **The ATR cap moves the stop inside invalidation.** When the structural
   stop (beyond the protected swing) is wider than 3×ATR15m, the code slides
   the SL to `entry ± sl_cap` — a stop *in front of* the level whose breach
   would actually invalidate the thesis. That converts a structural stop into
   a volatility stop placed in the noise band: the trade gets stopped on
   ordinary rotation while the setup is still valid. A trader would reject or
   downsize the setup, never tighten the stop inside the swing.
2. **Stops reference generic liquidity pools, not the protected swing.** The
   "nearest protected 15m swing" is sourced from the clustered pool list — an
   arbitrary nearby pivot (see B12.0: singletons qualify), not the specific
   swing whose violation flips structure.
3. **The primary TP can be missing from the merged candidate menu.**
   `_select_structural_tp` picks from raw prices; `_rank_targets` then merges
   confluent prices into a representative. Verified: short from 1804, risk
   2.0, targets [1800.27, 1799.757] → primary = 1799.757 (the ≥2R pick) but
   the menu contains only representative 1800.27 with rr 1.87 vs the
   primary's claimed 2.06+ — the LLM is told to select from a menu that does
   not contain the engine's own primary, and rr figures disagree for what is
   "the same" level. (This is the merge-side gap FA-04's ATR gate didn't
   cover.)
4. **`_tf_of_source` defaults unknown sources to "1h"** — verified: the
   `"current price"` entry candidate carries `tf: "1h"`, and the system
   instruction explicitly tells the model higher-TF candidates are stronger.
   A live market price is being ranked as a structural 1H level.

Also: the MR stop is a fixed VA-edge ± ATR buffer rather than beyond the raid
extreme / range invalidation (folded into I4.0's sequential MR).

*Proposed fix:*
- Cap behavior: if the structural stop exceeds `SL_ATR_CAP`, **invalidate the
  setup** (or emit `size_class: reduced` with the honest wide stop) — never
  clip inside the invalidation level. Update the BUG-06 regression tests
  accordingly.
- Stop anchor: once B12.0's typed swings land, reference the protected
  structural swing by ID; pools remain targets, not stop anchors.
- Menu consistency: select the primary **from the merged candidate list**
  (build typed candidates → merge → select), so the primary is by
  construction a menu member with one canonical rr. Add an invariant test:
  `engine_trade.take_profit ∈ candidates.targets[].price`.
- `_tf_of_source("current price")` → `tf: "live"`, rank 0; unknown sources →
  null tf, never a default that outranks real levels.

---

**B12.0 · Liquidity "pools" are ordinary swings: singletons qualify, no
touch/age/swept metadata, wick-consumed pools persist as future targets**
*[verified]* — trader-logic — sources: **A2 PE-07** (primary) + **A1**
(pool-strength note)

*Where:* `smc.py:378-417` (`find_liquidity_levels`), consumers:
`strategies.py:128-194` (sweep), `strategies.py:823-863` (stops),
`strategies.py:835-870` (targets).

*The bug:* `_cluster` emits a level for **every** swing including
single-touch pivots — verified: one high at 105 / one low at 95 produce
`BSL [105]` / `SSL [95]`. Pools carry no touch count, no separation, no age,
no swept/unswept state; the only filter is which side of the current close
they sit on. Consequences: a single noisy pivot simultaneously becomes sweep
evidence (C3/M2), a "protected" stop anchor (B11.0), and a liquidity target;
a pool whose liquidity was already consumed by a wick (price closed back
inside) remains on the menu as a future draw — the engine targets liquidity
that no longer exists. Resting liquidity in the SMC sense is *engineered
equal highs/lows or untested extremes* — plural touches or HTF prominence,
not any fractal pivot.

*Proposed fix:* typed pools: require ≥2 distinct touches within the cluster
tolerance (ATR/tick-scaled) separated by ≥N bars for an EQH/EQL pool —
single-touch swing extremes stay available as *swing* levels (stops/structure)
but not as "liquidity pools"; store
`{price, touches, first_idx, last_idx, swept_idx|null, tf, strength}`;
retire a pool as a draw once a wick takes it (keep the sweep as an event for
C3/M2 history). Surface `touches` in candidate metadata — it slots directly
into the LLM ranking rules and I1.0's UPDATE menu.

---

**B13.0 · C1 pays a 10/25 floor even on full 4H-vs-1H structure conflict**
*[verified]* — trader-logic — source: **A1 B5.0**

*Where:* `strategies.py:204-207`.

*The bug:* `_c1_trend` returns 10 both for "structures conflict" and for
"aligned but weak ADX" — verified (`HH/HL` vs `LH/LL`, ADX 45 → 10/25). C1
can never emit less than 10 in trend mode, so a fully-conflicted chop starts
every snapshot ten points up the 45/60 ladder; with location (≤25) and
momentum (≤15) a structurally contradictory market reaches the CONDITIONAL
band far too easily. Alignment is the *premise* of a continuation trade, not
a graded bonus.

*Proposed fix:* conflict (both TFs directional, opposed) → **0** + a
`tf_conflict` note; 4H directional + 1H neutral/mixed → 5; aligned → current
ADX tiering. Update the rubric text in the system instruction. Scoring change
⇒ invariant 5.

---

**B14.0 · One manual CLI/web run on the collector box silently prunes the
corpus to 1000 snapshots/symbol** *[code-verified]* — technical/ops — source:
**A1 B6.0**

*Where:* `main.py:1526-1548` (`_persist_snapshot`, default
`SNAPSHOT_RETENTION=1000`) reached from `run_analysis` (`main.py:1585`) and
`web/app.py:48`; the VM service runs `monitor.py --retention 0`
(`deploy/setup_vm.sh:100`) but `run_analysis` has no retention override.

*The bug:* the corpus VM accumulates ~96 snapshots/symbol/day and crosses
1000 in ~10 days. From then on a single debugging `python main.py SOL/USDT`
(or dashboard `/api/run`) on that box deletes every older SOL snapshot beyond
the newest 1000 — irreversible corpus loss, no warning. The same run also
spends the shared Gemini key outside the monitor's budget accounting and
advances shared regime-hysteresis state (the latter FA-18-documented; the
deletion is not).

*Proposed fix:* honor a `CRYPTERA_RETENTION` env var in `_persist_snapshot`
for **all** callers (systemd unit sets `CRYPTERA_RETENTION=0`), and/or route
manual runs to `snapshots_manual/` when a collector marker env is present.
One ops paragraph in AZURE_DEPLOYMENT.md.

---

**B15.0 · Evaluation integrity: narrated header fields override engine fields
in the engine universe; MR trades are bucketed by trend bias, not fade
direction; "expectancy" is excursion-based, not economic** *[code-verified]*
— statistical — source: **A2 PE-04**

*Where:* `tools/eval_engine.py:202-205` (header preference),
`tools/eval_engine.py:204` (`by_bias` from `trend_bias`),
`tools/eval_engine.py:143-150` (`_expectancy`).

*The bug — three measurement distortions:*

1. Engine-universe rows prefer `header.score` / `header.bias` /
   `header.volume_gate` (the LLM's copies, including any `score_override`)
   over `strategies.*` — an LLM override moves an *engine* outcome into the
   wrong score bucket, corrupting exactly the score→win-rate curve that
   thresholds will be tuned from.
2. `by_bias` uses `trend_bias` — but an MR fade's traded direction is
   `setup_direction`, frequently the *opposite*. Long/short performance cuts
   are mislabeled for the entire mean-revert universe.
3. `expectancy_pct = hit_rate*avg_MFE + (1-hit_rate)*avg_MAE` averages
   excursions over **all** triggered trades (not winners'/losers' realized
   outcomes) — a strategy can show positive "expectancy" while losing money.
   It is labeled naive in code, but it is the headline number of the report.

*Proposed fix:* engine universe reads `strategies.*` exclusively; bucket
direction by `setup_direction` (fallback `trend_bias` only when neutral);
replace expectancy with realized R (`+target_rr` on TP, `−1R` on SL, terminal
marked R on timeout — available once B1.0's labeler emits `end_rr`), with
fees/slippage constants; report the policy universes separately (all valid
setups / conditional band / actual ACTIVE alerts / triggered) and add paired
LLM-vs-primary deltas on the same narrated snapshots (feeds I1.0's
`by_verdict`).

---

**B16.0 · Regime router: failing the trend test force-routes into the fade
rubric (no transition/no-trade state), and hysteresis advances per CALL, not
per closed bar** *[verified — mechanism corrected from A2's description]* —
trader-logic + technical — source: **A2 PE-08** (corrected, see §5)

*Where:* `indicators.py:158-166` (ADX-floor demotion → "Ranging"),
`strategies.py:948-979` (every non-trend enters MR scoring),
`indicators.py:196-225` + `main.py:388-401` (hysteresis state, no bar key).

*The bug:*
1. "Ranging / Sideways" is assigned on *failure of the trend test* (e.g.
   directional 4H structure with ADX 19), not on evidence of balance — and
   every non-trend routes into the VAH/VAL fade rubric. Early trends,
   post-CHoCH transitions, and low-ADX directional drifts get faded. There is
   no transition/no-trade state (B2.0 kills the worst instances; this is the
   structural cause).
2. Hysteresis counts *invocations*, not closed bars. Verified: two calls with
   the same raw regime within one 15m bar advance `candidate_runs` twice and
   flip the rubric — no new market information required. Triggers: service
   restarts, `--once` smoke runs, or a CLI run alongside the monitor.
   (A2's specific claim that a normal startup + next boundary tick reprocesses
   the same candle is **incorrect** — those are consecutive different bars —
   but the underlying non-idempotency is real and verified.)

*Proposed fix:* key hysteresis by `(symbol, last_closed_15m_bar_open_ms)` —
identical bar IDs are idempotent (store the bar id in
`data/regime_state.json`). Add a `transition` outcome to the router: 4H
directional + sub-floor ADX (or fresh counter-CHoCH per B8.0) → `transition`
→ **no fade rubric, no continuation rubric** — HOLD with a named reason,
which is what a discretionary trader does in that tape. Full 4-state router
(trend / balanced_range / transition / untradeable) is I3.0's scope; the
bar-key fix is a 5-line patch to do now.

---

### P2

---

**B17.0 · Flat-tape structure artifacts: plateau bars all flagged as swings
AND equal-swing tie-break defaults bearish** *[verified]* — technical →
trader-visible — sources: **A1 B10.0 + B15.0** + **A2 PE-05 sub** (equal
levels as forced structure)

*Where:* `smc.py:30-42` (`fractal_swing_mask` — `high == roll_high`, no
strictness/dedup), `smc.py:136-143` (`_hl_label` tie → down-token).

*The bug:* any bar whose high equals the rolling-window max is a "swing
high" — in a flat stretch every bar qualifies on both sides (verified: a
plateau series produced 3 swing highs). Downstream: junk pools (compounding
B12.0's singleton problem), inflated S/R touch counts, degenerate dealing
ranges. Then `_hl_label` breaks exact ties to `LH`/`LL` — so a flat tape
reads `LH/LL`, `is_bear` passes, and with passable ADX the router calls it
"Trending Bearish" and shorts chop. Equal highs/lows are *liquidity*
(EQH/EQL), not directional structure — both audits converged on this.

*Proposed fix:* plateau dedup in the mask (count only the last bar of an
equal-extreme run, or require strict inequality vs one neighbour) + neutral
token on tie in `_hl_label` (structure → "mixed" → ranging path), with
"equal" defined by the cluster tolerance, not float equality. The equal
extremes should instead surface as EQH/EQL pools via B12.0's typed pools.
Flat-series unit tests for both.

---

**B18.0 · `_m3_cvd_absorption` rewards initiative flow and calls it
absorption — mislabeled evidence asserted to the LLM as fact**
*[code-verified]* — trader-logic — sources: **A1 B11.0** + **A2 PE-12** (sub)

*Where:* `strategies.py:385-393`.

*The bug:* at a range low, true absorption is *negative* delta (aggressive
selling) while price holds — passive buyers absorbing. The component pays 15
for **positive** delta (initiative buying — also bullish, but a different
mechanism) and labels it "CVD absorption supports fade"; genuine absorption
(price holding the edge on opposing delta) scores 0. Snapshot, dashboard, and
Gemini prompt all assert absorption that never happened.

*Proposed fix:* score both patterns explicitly with honest notes — initiative
(delta with the fade) → 10; absorption (delta *against* the fade while the
last 2-3 closes hold the edge, no new extreme) → 15; else 0. Keep the
component key (relabel-cheaper), fix the notes; fold the full
absorption-with-price-progress divergence test into I4.0's sequential MR.

---

**B19.0 · Candidate menus are missing the classic intraday draws: no PDH/PDL
targets anywhere, no S/R zones, no 1H OB entries (score evidence the menu
can't execute), sparse MR menu** *[verified: PDH/PDL absence]* — trader-logic
— sources: **A1 B12.0** + **A2 PE-09** (sub: score/menu mismatch)

*Where:* `strategies.py:835-841/864-870` (trend targets),
`strategies.py:793-807` (MR menus), `strategies.py:809-851` (15m-only
entries), `price_action.py:315` (`sr_levels` computed, never consumed).

*The bug:* previous-day high/low — the most-targeted intraday liquidity in
the ICT playbook — is fetched, shown to the LLM, and scanned for sweeps, but
**never offered as a target candidate**, so the deterministic TP (what the
labeler scores) can pick a 5th-ranked AVWAP while PDH sits between entry and
it. The MR menu is POC/VAH/VAL-only. Touch-weighted `sr_levels` never enter
any menu. And C2/C5 award points for 1H OB/FVG proximity while the entry menu
only contains 15m zones — the score cites evidence the geometry cannot
execute (A2's sharp framing).

*Proposed fix:* extend the labeled tuples: PDH/PDL targets per side, top-2
S/R zones (`"S/R zone (n touches)"`), 1H OB mid entries (tagged `tf:"1h"`),
MR targets += untested POCs inside the VA + opposite-edge pool cluster.
`_merge_confluent` automatically converts density into `confluence_count`
stacks; the wider menu is exactly what I1.0's UPDATE verdict selects from.
Geometry semantics change ⇒ invariant 5.

---

**B20.0 · External-data honesty: funding "current" is the last settled rate
(hours old), and failed fetches fabricate neutral values indistinguishable
from real data** *[code-verified]* — technical — sources: **A1 B13.0** + **A2
PE-11** (merged)

*Where:* `main.py:511-557` (`fetch_funding` — `/fapi/v1/fundingRate` history;
last settlement presented as `current`; zeros on failure), `main.py:316-327`
(orderbook zeros), `main.py:560-565` (F&G literal 50).

*The bug:* two flavors of dishonest data. (1) `funding.current` is the last
*settled* rate — up to 8h stale — while the OF modifier and the LLM treat it
as live positioning; Binance's premium-index endpoint carries the live
last/predicted funding. The "trend" is `last_settlement − mean(last 3)`, a
step function, not a trajectory. (2) On fetch failure, orderbook returns
`skew: 0.0` ("book is balanced" — a tradeable statement), funding returns
`current: 0.0` ("funding is flat" — a crowding signal), F&G returns 50.
FA-05 fixed exactly this for OI; the other three feeds still fabricate.

*Proposed fix:* fetch `/fapi/v1/premiumIndex` for live
`lastFundingRate`/`nextFundingTime` (keep the history endpoint for the
percentile series); return None-shaped defaults on every failure (the OF
modifier's existing guards already tolerate None); stamp each external block
with `fetched_at`/`age_s`; add the snapshot-level `data_quality` list
(I12.0) so the LLM knows *missing* from *neutral*.

---

**B21.0 · Sessions: fixed-UTC kill zones are wrong by ~1h around DST, and the
volume baseline compares the wrong bars** *[code-verified]* — trader-logic —
sources: **A1 B14.0** + **A2 PE-13** (merged)

*Where:* `config.py:46-51` (`KILL_ZONES`), `indicators.py:238-263`
(`session_volume_context`).

*The bug:* kill zones are defined by local session opens; fixed UTC windows
mislabel London under BST and New York for months at a time — the
highest-liquidity windows of the day, feeding both the gate context and the
LLM. The session-relative volume baseline pools all four 15m bars sharing the
UTC hour — the `:00` bar (heavy, post-close flows) against `:45` — with a
plain mean, no weekday/weekend split, and no minimum-support guard, for a
gate that can halve position size.

*Proposed fix:* `zoneinfo`-derived windows (`Europe/London`,
`America/New_York` — stdlib) computed per date; baseline the exact
minute-of-day slot with a robust statistic (median or trimmed mean),
weekday/weekend split, and `slot_samples` minimum before the gate may
hard-trigger. Tests with a January and a July date.

---

**B22.0 · Untested-POC "sessions" are tail-anchored rolling blocks — level
identity shifts every bar; fixed 0.5% retest band; uniform volume spread**
*[code-verified]* — technical → trader-visible — source: **A2 PE-14**

*Where:* `price_action.py:195-232` (`find_untested_pocs` — `end = n -
s*session_bars`, anchored to the newest bar), `price_action.py:218-220`
(fixed band), `price_action.py:65-99` (uniform spread — a documented
approximation).

*The bug:* "sessions" are blocks of 50×4H (or 5×1D) bars counted backwards
from *now* — every new candle shifts every window boundary, so a "naked POC"
is a different level from one cycle to the next. Consequences: unstable
targets in the menu (which also perturbs episode identity, B23.0), phantom
appear/disappear of magnets on the dashboard, and a retest band (0.5% flat)
that ignores volatility and bin width. The uniform intra-bar volume spread is
acceptable as an approximation but should be labeled as such.

*Proposed fix:* anchor profile windows to stable calendar IDs (UTC days /
ISO weeks / fixed session blocks with persisted `period_start`), so a POC has
identity across cycles; retest tolerance = max(bin width, 0.25×ATR of the
profile TF); note the approximation in the snapshot field name or docs.

---

**B23.0 · Episode identity is brittle: a 0.2% drift across a sig-fig boundary
splits one setup; the same geometry recurring within 24h merges**
*[verified]* — statistical — source: **A2 PE-15** (sub)

*Where:* `tools/episodes.py:28-64` (`_sig`, 3 significant figures;
`EPISODE_GAP_HOURS = 24`).

*The bug:* rounding-bucket identity has hard edges — verified: entries 104.4
vs 104.6 (0.19% apart, same setup drifting with ATR) produce different
episode IDs (104 vs 105), double-counting one physical trade; conversely the
same rounded geometry recurring 23h later merges into the prior episode. The
module's own docstring anticipated re-tuning; the boundary-split direction is
the harmful one (re-inflates the pseudo-replication the fix exists to kill).

*Proposed fix:* distance-based identity instead of rounded equality — same
symbol+side and all three of entry/SL/TP within a relative tolerance
(~0.3-0.5%, ATR-aware later) of the episode's *representative* geometry joins
the episode; else new. Longer-term (I8.0): fingerprint by structural setup ID
(entry-zone ID + invalidation swing ID + expiry) once typed candidates exist.
Re-run the existing episodes tests plus a boundary-drift case.

---

**B24.0 · Calibration lifecycle: malformed files silently swallowed while
health reports "loaded"; score buckets pool both rubrics; no version/hash
anywhere** *[code-verified]* — technical/statistical — source: **A2 PE-16**
(bug subset; process items merged into I9.0)

*Where:* `strategies.py:31-45` (`load_calibration` — bare `except: pass`,
process-lifetime cache), `main.py` `data_health_report` (existence check
only), `tools/calibrate.py:200-257` (pooled `score_rows`; no metadata).

*The bug:* a corrupt/incompatible `calibration.json` silently degrades to
equal weights while the health banner says "calibration: loaded" — the exact
silent-degradation class the health report exists to prevent. Score→win-rate
buckets pool trend and MR rows even though the two rubrics have different
maxima (100 vs 90) and distributions, so the shared curve is a blend of two
different meanings of "a 60". Snapshots record no calibration ID, so a corpus
spanning a refit mixes scores generated under different weights with no way
to separate them.

*Proposed fix:* validate on load (schema/keys/finite values) and log+report
the actual state ("loaded id=… fitted 2026-07-30 on 412 episodes" vs "INVALID
— equal weights"); write `calibration_id` (content hash) into the file and
stamp every snapshot with the active id + `SCHEMA_VERSION` + label version;
fit and store score buckets per rubric. Walk-forward/embargo process goes
with I9.0.

---

### P3

---

**B25.0 · The labeler rewrites snapshots with `indent=2`, re-bloating the
compact corpus** — technical — source: **A1 B16.0**
*Where:* `tools/label_snapshots.py:337-338`. FA-14 made persistence compact
for the 24/7 corpus; the daily label timer then rewrites every labeled file
indented (~25-30% disk growth, forever). *Fix:* `separators=(",", ":")`.

**B26.0 · `ab_thinking` is not measurement-grade: unfloored replay window, no
1m resolver, no episode collapse, unadjusted hit-rate winner** —
technical — sources: **A1 B17.0** + **A2 PE-15** (merged)
*Where:* `tools/ab_thinking.py:47-51,76-135,100`. The A/B harness evaluates a
different forward window than the labeler (no `_floor_to_15m`), labels
without the 1m ambiguity drill, counts duplicate snapshots of one persisting
setup as independent trials, and picks the thinking-level winner on raw hit
rate with no pairing or confidence interval. Any verdict it produced today
would be noise. *Fix:* reuse the canonical replay path (B1.0/I8.0), collapse
episodes (fingerprint the LLM geometry separately), compare **paired** net-R
with a bootstrap CI, persist each response + config hash.

**B27.0 · Funding trend/average assumes an 8h interval for every symbol** —
technical — source: **A1 B18.0**
*Where:* `main.py:533-546`. Binance runs 4h/1h funding on a growing set of
USDT-perps; there `avg_24h` is mislabeled and the trajectory mixes regimes.
*Fix:* infer the interval from consecutive `fundingTime` deltas; window the
average by hours, not row count; record `interval_hours`.

**B28.0 · Cheap hygiene riders** — sources: both
- `_floor_to_15m` admits ~30s of pre-decision movement (A2 PE-01 note) —
  already FA-09-documented; keep, but record `decision_time` in outcomes so
  the tolerance is auditable (rides along with B1.0).
- `fetch_open_interest` 1h/4h deltas use `series[-2]`/`series[-5]` of the
  history endpoint — actual spans drift 1-2h/4-5h depending on the partial
  hour (A1) — acceptable; document, or index by timestamp.
- `_merge_confluent` tolerance is a flat 0.15% — make it ATR-scaled when
  B19.0 widens the menus (A1).

---

## 3. Proposed improvement plan

### I1.0 · "Engine proposes, LLM adjudicates" — gated LLM co-pilot (the new
core interaction model) — **do first after the P0/P1 correctness wave**

*(Operator-mandated redesign. Both audits independently concluded the LLM
boundary needs enforcement — A2 via PE-02, A1 via I2.0; this design absorbs
both, and A2's candidate-ID contract is adopted as the selection mechanism.)*

*What changes conceptually:* v3's contract is "rules decide, the LLM narrates
and selects". The new contract: the engine still computes everything
deterministically, but above a confluence threshold the LLM becomes an
**adjudicator** — it receives all precomputed data *plus the engine's explicit
arguments*, and may **agree, disagree, or update** the setup, with *update*
treated as the highest-value outcome. Rationale: an LLM reads a rich evidence
table better than it invents numbers; bounded adjustment turns contextual
judgment into measurable alpha instead of prose.

**1) Trigger policy (LLM call only above threshold):**
- One pure predicate shared by every path (CLI, web, monitor):
  `should_call_llm(snapshot) = engine_trade.valid AND volume_gate !=
  HARD_GATE AND confluence_score >= LLM_CALL_THRESHOLD`.
- `LLM_CALL_THRESHOLD` in `config.py`, default `CONDITIONAL_ENTRY_THRESHOLD`
  (45): the 45-59 band is included deliberately — borderline setups are where
  adjudication has the most room to add value (upgrades and rejections both).
  Below threshold: deterministic output, zero Gemini spend. The monitor keeps
  its stricter transition/cooldown/budget gating on top (unchanged); CLI/web
  newly acquire the gate.

**2) Input packet — the engine's arguments (`engine_case` block):**
Everything `_build_llm_view` sends today, plus a compact structured argument
assembled from data the engine already computes (~300 tokens, near-zero new
computation):
```json
"engine_case": {
  "thesis": "SELL fade at VAH 100.0: ranging regime (ADX 14), swept 1h BSL
             and reclaimed, stoch 1h overbought, absorption at edge",
  "for":     [{"component": "m1_edge_distance", "pts": 25, "note": "..."},
              {"component": "m2_edge_sweep",    "pts": 15, "note": "..."}],
  "against": [{"component": "m6_range_intact",  "pts": 0,  "note": "fresh 1H BOS - range at risk"},
              {"source": "order_flow", "note": "OI building against (+2.1% 4h)"}],
  "vetoes_or_caps": ["event_guard", "counter_choch_warning"],
  "uncertainty": {"win_rate": 0.54, "win_rate_source": "logistic",
                  "sample_support": 210, "data_degraded": ["orderbook"]}
}
```
Built from `confluence_notes`, `order_flow_notes`, gate flags, and the I12.0
`data_quality` block — the engine finally *argues its case* instead of dumping
numbers, which is what a model adjudicates well.

**3) Output contract (schema v2):**
- `verdict`: `"AGREE" | "UPDATE" | "REJECT"` (required).
  - **AGREE** — echo the engine primary; may adjust only
    probability/confidence/position-size and management fields.
  - **UPDATE** (the valued path) — keep the direction; may replace
    entry / stop_loss / take_profit / tp1 / tp2 / move_to_breakeven_at by
    **selecting candidate IDs**: every level in the enriched snapshot
    (candidates, pools, POCs, AVWAP, PDH/PDL/PWH/PWL, VA edges, OTE bounds,
    S/R zones — B19.0/B12.0 make the snapshot itself the menu) carries a
    stable `level_id`, and the model outputs IDs, never floats (A2's
    contract, adopted — it removes the float-tolerance matching problem
    entirely). Each change requires a one-line `update_rationale[]` entry.
    Free-price invention is structurally impossible: "update" means
    re-selecting real structure.
  - **REJECT** — decline with `reject_reason` enum (`breakout_risk |
    counter_htf_structure | crowding | data_quality | event_risk | poor_rr |
    other`) + free text → HOLD. (Counter-proposals deferred: the engine emits
    no opposite-direction geometry today.)
- `score_override` semantics kept. `header.action` becomes **engine-derived
  post-call** (final score + verdict; REJECT → HOLD; event guard caps at
  CONDITIONAL) — never trusted from the model.

**4) Deterministic validation layer (I2.0 — hard dependency):** resolves IDs
to prices engine-side; ordering/DOA sanity re-applied to updated geometry;
R:R recomputed by the engine; `selected_from_engine` derived by comparison;
violations revert per-field to the engine primary and are recorded in
`analysis.contract_violations[]`.

**5) Persistence, labeling, measurement (the loop that judges the bet):**
- Persist `analysis.verdict`, the post-validation `final_plan`, and the
  untouched `engine_trade`. The labeler already labels both universes
  (`engine_trade_outcome` / `trade_decision_outcome`) — the adjudicated plan
  slots into the existing path with zero labeler changes.
- `eval_engine`: `by_verdict` cut — AGREE/UPDATE compared **paired** against
  the engine primary on the same snapshots (the direct measure of "does
  UPDATE add alpha"); for REJECT, report the engine primary's outcome
  (rejection quality = how often the declined trade would have lost).
- Rollout: **shadow first** — 2-4 weeks with alerts showing both plans;
  promote the adjudicated plan to headline once `by_verdict` shows a
  non-negative paired delta over ≥50 UPDATE episodes.

**6) Files:** `config.py` (threshold), `main.py` (schema v2, system
instruction rewrite, `engine_case` builder, `level_id` assignment, validator,
`should_call_llm`), `monitor.py` (pass-through), `tools/eval_engine.py`
(`by_verdict`), dashboard verdict badge. Schema bump; prompt A/B via the
repaired harness (B26.0 first).

### I2.0 · Deterministic post-LLM validation & candidate-ID contract
The fix-side of B3.0 and the enforcement substrate of I1.0 — valuable
standalone *today* even before the verdict redesign: price/ID membership,
ordering + DOA sanity, engine-side R:R, threshold-consistent action,
HOLD-null enforcement, `contract_violations` recorded and surfaced in eval.
Small, pure, unit-testable (tests: candidate membership, gates, HOLD-null,
R:R recompute — A2's list adopted).

### I3.0 · Structural event state machine
(B7.0/B8.0/B16.0 fix substrate; sources: A2 PE-05/PE-08 recs + A1 I6.)
Mutually-exclusive BOS/CHoCH conditioned on trend state; `transition` state
after CHoCH requiring displacement + confirming BOS before the trend label
flips; protected-swing IDs tracked explicitly; EQH/EQL represented as
liquidity, not forced structure; 4-state router (trend / balanced_range /
transition / untradeable) with bar-keyed idempotent hysteresis; volatility
expansion/compression as a routing input (computed today, unused). Golden
synthetic scenarios on both mirrored sides as the exit criterion.

### I4.0 · Sequential setup detectors with expiry
(B2.0/B18.0 completion; sources: A2 PE-12 rec + A1.)
Encode *order*, not just presence: range fade = established balance (≥2
rotations) → raid of VAH/VAL/external liquidity with ATR-scaled penetration →
failure-to-progress / absorption divergence → 15m MSS + displacement → entry
on return to the displacement FVG/OB, stop beyond the raid extreme, target
POC then opposite external liquidity. Continuation = pullback → displacement
BOS → return-to-zone. Each setup carries an explicit **expiry** (a London
sweep setup is not the same pending order next session — also stabilizes
episode identity). No sweep/MSS ⇒ no MR trade, regardless of additive score.

### I5.0 · Typed liquidity & sweep quality (audit §3.8 — still open)
(B12.0 completion; sources: A1 I3/I5 + A2 PE-07 rec.)
Two-touch EQH/EQL pools with `{touches, age, swept_idx, tf, strength}`;
swept pools retired as draws; sweep detection scans 12-16 bars for HTF pools
(vs today's 5), requires ATR-scaled minimum penetration (≥0.15×ATR15m), and
decays score with sweep age; protected swings kept separate from pools
(stop anchors per B11.0).

### I6.0 · OTE anchored to the displacement leg
(B9.0 completion; source: A1 I4.)
OTE leg per TF = origin swing → break extreme of the most recent BOS/CHoCH
event (both already computed); `leg_source` recorded; premium/discount stays
on the bracketing dealing range. Makes C6 meaningful exactly when trends
extend — where the current implementation is weakest.

### I7.0 · Candidate-first geometry & menu enrichment
(B11.0/B19.0 completion; sources: A1 I5 + A2 PE-09 rec.)
Build typed candidate objects (with `level_id`, tf, touches, confluence,
zone) → merge → select the primary **from the merged list** (menu-membership
by construction); rank by setup quality/EV rather than "nearest ≥2R" alone (a
strong 1.8R external-liquidity target beats a weak singleton at 2.05R —
surface both honestly and let calibrated EV decide once I9.0 exists); stops
beyond the named invalidation swing or the setup is rejected/downsized —
never clipped inside; PDH/PDL + S/R + 1H OB + MR-menu additions; tick-size
quantization of all levels; `ENTRY_ATR_CAP`.

### I8.0 · Measurement stack v2 — one canonical replay path
(B1.0/B15.0/B23.0/B26.0 completion; sources: A2 PE-01/04/15 recs + A1.)
One chronological replay/labeling implementation shared by labeler, eval, and
A/B (same flooring, same 1m drill, same horizon rule); realized-R economics
everywhere (`+target_rr` / `−1R` / terminal marked R, minus fees/slippage);
policy universes reported separately (valid / conditional / ACTIVE-alert /
triggered); paired comparisons with bootstrap CIs for every A-vs-B question
(thinking level, prompt variants, I1.0 verdicts); label/episode versioning on
every outcome row; stable setup-based episode IDs.

### I9.0 · Calibration v3 — probabilities that mean what they say
(B4.0/B24.0 completion; sources: A1 I7 + A2 PE-03/16 recs.)
Three quantities (P(trigger), P(TP|fill) on binary outcomes, E[net R]);
versioned artifacts (`calibration_id` + fit metadata) stamped into snapshots;
purged walk-forward folds with ≥24h embargo, clustered by symbol/day;
per-rubric thresholds and buckets; loud load validation; Brier/log-loss/
reliability as acceptance gates; equal-weights fallback retained. Refuse to
write a calibration that underperforms the score-bucket baseline
out-of-sample.

### I10.0 · Order-flow completeness
(B20.0/B27.0 completion; sources: A1 I8 + A2 PE-11.)
Premium-index live funding + settled history with timestamps/age; ~30d
crowding percentile (a 333-day window answers a different question);
funding-interval awareness; full price×OI quadrant (OI falling = covering /
liquidation states, currently unscored); C7 CVD windows harmonized to
comparable wall-clock horizons (~8h per TF); persist raw OF components so
their incremental value is testable out-of-sample.

### I11.0 · Session realism
(B21.0 completion; sources: A1 I9 + A2.)
DST-aware kill zones; slot-matched robust volume baseline with minimum
support; Asia-range tracking (Asia high/low as raid targets for the London
model); midnight-open / daily-open / weekly-open tagged as bias references
(`weekly_open` is fetched today and consumed by nothing).

### I12.0 · Data-quality honesty block
(B20.0 completion; source: A1 I10.) Null-on-failure everywhere + snapshot
`data_quality: {degraded: [...], ages}` consumed by the prompt and by
`engine_case.uncertainty` — an adjudicator must know *missing* from
*neutral*.

### I13.0 · BTC structure gate for alts (audit §3.10 — still open)
(Sources: A1 I11 + A2.) Rolling 1h-return correlation/beta + BTC's own 4H/1H
structure as a veto/cap for alt trades against a breaking BTC — BTC taking
external sell-side liquidity vetoes marginal alt longs more reliably than the
BTCDOM proxy (which stays as the macro-rotation term). Implement behind a
flag; measurement-gated.

### I14.0 · Horizon-aware target reachability
(Source: A2.) Replace the flat `MAX_TARGET_ATR_1H_MULTIPLE = 10` with
empirical time-to-hit by distance-in-ATR × mode × volatility regime ×
session, fitted from the corpus once I8.0 exists — a valid 1D pool is not
automatically a valid 24h target, and 10×ATR is the right *placeholder*, not
the right *model*. Until the corpus supports it, keep the constant.

### I15.0 · Collector-box safety rails
(B14.0 completion; source: A1 I12.) `CRYPTERA_RETENTION` honored by all
callers; manual-run snapshot redirection; ops paragraph in
AZURE_DEPLOYMENT.md. Protects the asset everything else depends on.

---

## 4. Sequencing & corpus discipline

1. **Stop collection** (operator, on the VM): `sudo systemctl stop
   cryptera-monitor && sudo systemctl disable --now cryptera-label.timer`.
   Archive the VM `snapshots/` collected since 2026-07-12 — pre-fix
   semantics; inspect freely, **never fit on it** (invariant 5).
2. **Wave 1 — measurement + decision correctness (P0s + directional P1s):**
   B1.0 (chronological labeler + label version), B2.0, B3.0/I2.0 (validator
   now, verdict schema later), B4.0 (cheap patch + renames), B5.0, B6.0,
   B10.0, B13.0, B14.0, B15.0, B16.0 (bar-key patch) + P3 riders (B25-B27).
   One release, schema bump, mirror-property tests introduced for every
   directional component. Full pytest + the two verification scripts as
   regression seeds.
3. **Wave 2 — I1.0 adjudicator** on top of the validator: schema v2,
   `engine_case`, `level_id` selection, `by_verdict` eval, shadow rollout.
4. **Wave 3 — structure & menus:** I3.0 (state machine absorbing B7/B8),
   I4.0 (sequential MR absorbing the rest of B2/B18), I5.0, I6.0, I7.0
   (absorbing B11/B12/B19), B17.0, B21.0-B23.0. All semantics-changing ⇒
   land **before** collection restarts.
5. **Redeploy & collect** — restart monitor + label timer; corpus starts
   clean under the new schema + label version.
6. **Wave 4 — measurement-gated (needs corpus):** I9.0 first fit
   (walk-forward), I1.0 `by_verdict` readout and promotion decision, I8.0
   paired A/Bs, then I10/I11/I13/I14 evaluated for incremental
   out-of-sample value — never tuned on in-sample score alone.

**Do not tune until Wave 4:** the 45/60 thresholds, component weights, the
location cap, `MAX_TARGET_ATR_1H_MULTIPLE`, Gemini thinking level, or sizing
from displayed probability (both audits, independently — A2 §"What should not
be tuned yet").

---

## 5. Cross-audit verification notes

- **21/21 synthetic probes confirmed** across both audits' claims (A1 script:
  10/10 — MR DOA, `_mr_setup` breakout fade, m1 outside-edge credit, C2
  OB/FVG asymmetry, dealing-range stale leg, C1 conflict floor, fractal
  plateau, PDH/PDL menu absence, timeout round-trip credit; A2 script: 11/11 —
  labeler entry/exit chronology, +24h bar inclusion, BOS==CHoCH duplication,
  C4 slope reversal, singleton pools, primary-TP-not-in-menu,
  current-price-as-1H-tf, PDH two-days-ago at 00:30 UTC, PWH
  two-weeks-ago on Monday, episode sig-fig split, hysteresis per-call
  advance). All remaining claims verified by code reading at the cited lines.
- **One A2 mechanism corrected — PE-08:** the claim that a normal monitor
  startup followed by the next boundary tick reprocesses the *same* closed
  candle is incorrect — the immediate cycle scores the last closed bar and
  the boundary tick scores the next one. The underlying defect is real and
  verified in corrected form: hysteresis advances per *invocation* with no
  closed-bar key, so two invocations within one bar (service restarts,
  `--once` smoke runs, CLI runs alongside the monitor) double-count
  (B16.0).
- **A2's PE-01 supersedes A1's B9.0:** A1 caught the entry-bar excursion
  contamination; A2's chronology analysis showed the same root cause also
  corrupts the *outcome itself* — merged as B1.0 at P0.
- **Both audits independently found** (strong mutual confirmation): the
  post-LLM enforcement gap (PE-02 / A1-I2.0), timeout MFE credit (PE-03 /
  A1-B7.0), fill-conditioning (PE-03 / A1-B8.0), M3 absorption mislabel
  (PE-12 / A1-B11.0), equal-level bearish bias (PE-05 / A1-B15.0), DST kill
  zones (PE-13 / A1-B14.0), fabricated-neutral fallbacks (PE-11 / A1-B13.0),
  OI-falling gap, and the ab_thinking floor inconsistency (PE-15 / A1-B17.0).
- **A2 exec-summary environment notes** ("3 snapshots, 0 labeled",
  55-test partial run) were point-in-time observations of its sandbox, not
  code claims — 72/72 tests pass in the current tree.

### Traceability (final ID ← source IDs)

| Final | Sources | | Final | Sources |
|---|---|---|---|---|
| B1.0 | A2 PE-01 + A1 B9.0 | | B15.0 | A2 PE-04 |
| B2.0 | A1 B1.0 + A2 PE-12 | | B16.0 | A2 PE-08 (corrected) |
| B3.0 | A2 PE-02 (+ A1 I2.0) | | B17.0 | A1 B10.0+B15.0 + A2 PE-05 sub |
| B4.0 | A1 B7.0+B8.0 + A2 PE-03 | | B18.0 | A1 B11.0 + A2 PE-12 sub |
| B5.0 | A2 PE-06 | | B19.0 | A1 B12.0 + A2 PE-09 sub |
| B6.0 | A1 B2.0 | | B20.0 | A1 B13.0 + A2 PE-11 |
| B7.0 | A2 PE-05 | | B21.0 | A1 B14.0 + A2 PE-13 |
| B8.0 | A1 B4.0 + A2 PE-05 sub | | B22.0 | A2 PE-14 |
| B9.0 | A1 B3.0 | | B23.0 | A2 PE-15 sub |
| B10.0 | A2 PE-10 | | B24.0 | A2 PE-16 subset |
| B11.0 | A2 PE-09 + A1 | | B25.0 | A1 B16.0 |
| B12.0 | A2 PE-07 + A1 | | B26.0 | A1 B17.0 + A2 PE-15 |
| B13.0 | A1 B5.0 | | B27.0 | A1 B18.0 |
| B14.0 | A1 B6.0 | | B28.0 | both (riders) |

*Line references are to this working tree (branch `v3.2.4-correctness`,
2026-07-20). Research tooling analysis — not financial advice.*
