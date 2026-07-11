# Cryptera v3.2.3 — Full Audit Report

Audit date: 2026-07-11. Scope: every `.py` file, the Gemini prompt/schema, the tools pipeline, and the web layer. Constraint honored: improvements stay within the SMC/PA paradigm — no orderflow pivot.

Severity legend: **P0** = corrupts signals or calibration data, fix before trusting any output · **P1** = materially degrades prediction quality · **P2** = quality/robustness · **P3** = hygiene.

---

## 1. Executive summary

The architecture ("rules decide, LLM narrates") is sound and ahead of most retail engines: closed-candle discipline, deterministic geometry, a candidate menu the LLM can't invent past, schema-enforced output, and a real labeling→calibration loop. However, the audit found **7 P0 bugs**. Three of them (spot/futures data mixing, EMA_200 permanently NaN, timezone-shifted outcome labels) mean that (a) the "real CVD" the engine is proud of is arithmetically wrong, (b) regime detection silently degrades to "Ranging / Sideways" far too often — so the mean-revert rubric fires in trends, and (c) every calibration/eval number ever produced on a non-UTC machine is measured against the wrong forward window. If this version felt profitable, it likely was profitable *despite* these — fixing them is the cheapest edge available.

---

## 2. Bug hunt

### P0 — Critical

**BUG-01 · Spot OHLCV mixed with futures taker flow → "real CVD" is wrong**
`main.py:207` creates `ccxt.binance()` (spot market) for OHLCV, but `fetch_taker_flow()` (`main.py:162-185`) pulls `taker_buy_base` from `fapi.binance.com` (USDT-perp). `indicators.py:98-102` then computes `cvd_delta_bar = 2·taker_buy(futures) − volume(spot)`. Futures volume is usually a large multiple of spot volume, so the delta magnitude and frequently the **sign** are wrong — while `cvd_is_real: true` tells you and the LLM to trust it. C7 (10 pts), M3 (15 pts), the absorption veto, and the LLM's sentiment reasoning all consume this.
Same inconsistency family: orderbook, funding, OI, liquidations context are all perp-side while price structure is spot.
**Fix:** pick one market (USDT-perp is the natural one — funding/OI/taker data live there) and use `ccxt.binanceusdm()` or raw `fapi` klines for OHLCV too. One-line class change plus symbol mapping.

**BUG-02 · EMA_200 is always NaN on 4H and 1H**
`LIMITS = {"4h": 200, "1h": 200, ...}` (`main.py:56`) then `_drop_forming_candle` leaves **199** closed bars. `ta.ema(length=200)` needs ≥200 bars → all-NaN column (verified empirically). Consequences:
- `derive_trend_bias()` (`strategies.py:79-83`) EMA fallback never fires → bias "neutral" whenever 4H structure is mixed → geometry `valid=false`, C2/C5/C7/C8 zeroed.
- `detect_regime()` fallback (`indicators.py:169-188`): the ±2 EMA term is dead for the two heaviest weights, leaving only RSI ±1 → weighted score almost never exceeds the 0.8 threshold → **defaults to "Ranging / Sideways"** → mean-revert rubric runs during trends.
**Fix:** raise limits to ≥ 260 (4h/1h). Also benefits `ATR_Pct` (rolling 100) and volume profile stability.

**BUG-03 · Snapshot filenames are local time; the labeler reads them as UTC**
`main.py:983` names files with `datetime.now()` (local clock), but `tools/label_snapshots.py:49` parses that timestamp with `.replace(tzinfo=timezone.utc)`. On an IST machine the labeler starts its forward-candle window **5 h 30 m after the true snapshot moment** — every `outcome` block, and therefore `calibrate.py` weights and `eval_engine.py` stats, are computed on the wrong window.
**Fix:** label from `snapshot["generated_at"]` (already UTC) instead of the filename; also switch the filename itself to UTC for consistency.

**BUG-04 · OI history request exceeds Binance's max limit → silently disabled**
`main.py:349`: `openInterestHist` is called with `limit = 30*24 = 720`; Binance caps this endpoint at **500**. The API returns an error object, `isinstance(r, list)` fails, and `oi_change_1h/4h` and `percentile_30d` come back `None` on almost every run — the OF modifier's OI term (±2) and the snapshot's OI context are effectively dead, silently.
**Fix:** `limit=500` (~20.8 days — rename the field) or paginate for a true 30d.

**BUG-05 · OTE is long-only; shorts get credited in the wrong zone**
`get_premium_discount()` (`smc.py:360-373`) computes OTE as `[high − 0.786·range, high − 0.618·range]` — the discount-side retracement zone for **longs**. For a bearish leg, the short OTE is the mirrored upper zone (`low + 0.618..0.786·range`). As written, `in_ote=true` for a short means price is deep in *discount* — the worst place to sell — yet C6 pays +10 and the LLM is told "in_ote" as a fact.
**Fix:** pass leg direction (from structure/last swing order) and mirror the zone for bearish setups.

**BUG-06 · Stop-loss is anchored to the widest liquidity pool, not the nearest protected swing**
`find_liquidity_levels()` returns `sell_side` sorted **ascending** and `buy_side` **descending** (`smc.py:357`). Geometry then takes `sell_side[0]` for a long SL (`strategies.py:682`) — i.e., the *lowest* swing low in the lookback — and `buy_side[0]` for a short SL — the *highest* high. Stops are systematically far wider than structure requires → `risk` inflated → `_select_structural_tp` rarely finds a ≥2R target → frequent "farthest structural level (<2R, honest)" primaries and depressed `rr_passed`. This single bug degrades nearly every emitted trade geometry.
**Fix:** select the nearest pool *beyond entry in the adverse direction* (min BSL above entry for shorts, max SSL below entry for longs), with an ATR cap on SL width.

**BUG-07 · Response schema forces C1-C8 even in mean-revert mode**
`GEMINI_RESPONSE_SCHEMA` hard-requires `score_breakdown.c1_trend … c8_stoch` (`main.py:610-619`), but when `score_mode == "mean_revert"` the snapshot contains only M1-M6. The model is *forced by the schema* to fabricate eight C-values on every ranging run — institutionalized hallucination in an engine whose core rule is "never invent."
**Fix:** make `score_breakdown` a nullable free-form object (or two variants), and align key names with the engine's (`c1_trend_alignment` vs schema's `c1_trend`).

### P1 — Materially degrades quality

**BUG-08 · Regime detection is structurally biased toward "Ranging"**
Beyond BUG-02: `detect_regime()` decides 4H trend from just the **last two** confirmed swing highs/lows (`get_market_structure`, `smc.py:137-142`) — one equal-high or deep pullback flips it to mixed; the "ranging" early-exit checks `bos_1h.level is None`, which is almost never true (some historical break nearly always exists), so real ranges frequently fall through to the crippled fallback too. Net effect: regime (and with it the entire rubric selection) is noisy. This is the single highest-leverage prediction-quality item after the P0s.

**BUG-09 · Dealing range doesn't verify it brackets price**
`_current_dealing_range()` (`smc.py:376-395`) claims (docstring) the range brackets current price but never checks. After a breakout above the last swing high, price sits *outside* the range → `pd_zone` says "premium" and OTE is computed on a stale leg → C6 and the LLM's premium/discount reasoning mislead exactly when trends extend.

**BUG-10 · OF modifier compares 4h OI change against a 40-hour price change**
`strategies.py:448`: `window_price_change_pct` for 4h is measured over `WINDOW_SIZE["4h"]=10` bars ≈ 40 h, but is matched with `oi.change_4h_pct` (4 h). "OI building with trend" can fire on a 40-hour-old move that already reversed within the last 4 hours.

**BUG-11 · Liquidity pools keep the 3 most extreme levels, dropping the nearest ones**
`smc.py:357` keeps top-3 highest BSL / lowest SSL. The pools nearest to price — the ones that actually get swept and targeted — can be truncated away, so sweep detection (C3/M2) and target menus miss the most relevant levels.

**BUG-12 · Calibration samples are pseudo-replicated and survivorship-filtered**
Snapshots taken minutes apart share nearly identical forward windows, so `calibrate.py` counts the same market outcome many times (no de-duplication / time-decay). Timeouts are excluded entirely (`calibrate.py:53`) — a rubric that produces slow winners gets no credit; MFE data is available but unused.

**BUG-13 · Naive event datetimes are parsed in machine-local time**
`main.py:487`: `datetime.fromisoformat(...).timestamp()` treats offset-less strings as *local* time. If `data/events.json` is maintained in UTC (as the examples suggest), the ±2h guard window is shifted by the machine's UTC offset. Require explicit offsets (`2026-12-31T13:30:00+00:00`) or assume UTC in code.

**BUG-14 · Sub-cent asset destruction by fixed rounding**
`get_window_dict` rounds close series to 2 decimals and prices to 4 (`main.py:92-96`); geometry rounds to 6. For a custom low-priced pair (user can enter any symbol), `close_series_last_5` collapses to `[0.0, …]`. Scale rounding by price magnitude.

### P2 — Robustness / correctness edges

- **BUG-15** `main.py:924`: `data["4h"]` raises a bare `KeyError` if one timeframe fetch fails entirely — no graceful degradation or clear message.
- **BUG-16** FVG "nearest" selection (`smc.py:315-321`) only considers gaps entirely below (bull) / above (bear) price; a gap straddling current price — the most actionable one — is invisible to C2/C5 and entries.
- **BUG-17** OB mitigation uses closes only (`smc.py:272-282`): an OB whose level was wicked through (stops taken) still shows as unmitigated and can be offered as an entry candidate.
- **BUG-18** `_label_one` (`tools/label_snapshots.py`): checks SL/TP on the *entry bar itself* using its full range — conservatively OK (documented), but entry "trigger" requires the bar to contain the entry price; a gap through a limit level counts as not-triggered. Slight systematic under-count of fills.
- **BUG-19** `fetch_ohlcv` cache-hit path never refreshes `live_price` (`main.py:216-221` skips the fresh fetch where `live_price` is set) → falls back to the last **closed** 15m close; distance/trigger context can be one bar stale.
- **BUG-20** Morning/evening star uses `b1 / r2` (middle-candle body ÷ *first* candle's range, `price_action.py:53-55`) — unconventional; a large first candle masks a fat middle body.
- **BUG-21** Snapshot retention sorts by `mtime` (`main.py:889`), but `label_snapshots` rewrites files in place, resetting mtime — retention can delete newer snapshots before older relabeled ones. Sort by filename timestamp instead.
- **BUG-22** Funding percentile uses only 24 samples (8 days) yet drives a ±3/-4 modifier; widen the window (`limit` up to 1000 on this endpoint) for a stable percentile.
- **BUG-23** `web/app.py`: `asyncio.run()` inside a request handler blocks the worker for the entire pipeline+LLM call, and `debug=True` (auto-reloader + werkzeug debugger) in the run block. Fine locally; do not expose.
- **BUG-24** Sequential blocking REST calls (`main.py:913-918`) — orderbook, funding, sentiment, OI, BTC.D, events run serially inside an async pipeline; ~3-6 s of avoidable latency. `asyncio.gather` + `to_thread` them.

### P3 — Hygiene / dead code

- Dead columns computed every run, never consumed: `ATR_Z`, `VOL_Z`, `swing_high`/`swing_low`, `fvg_bull`/`fvg_bear` (`indicators.py`).
- Dead branch `main.py:246-247` (`elif tf == "15m" and cache_key in OHLCV_CACHE: pass`).
- `GEMINI_THINKING_LEVEL = "high"` while the comment above it argues `medium` is the sweet spot (`main.py:563-566`) — decide and make them agree (see §4).
- `strategies.py` docstring says "M1-M8"; there are six.
- `datetime.utcnow()` is deprecated in 3.12+; use `datetime.now(timezone.utc)`.
- `improvements_report.md` referenced by README is missing from the repo.
- Calibration weights multiply then round per component with only a global 0-100 clamp — a 1.5-weighted C1 can print 38/25 in the breakdown shown to the LLM; renormalize per-component maxes.

---

## 3. Prediction-quality improvements (staying SMC/PA)

Ordered by expected impact:

1. **Fix the P0s first.** Correct CVD (BUG-01), regime (BUG-02/08), labels (BUG-03), stops (BUG-06), OTE (BUG-05). These are not "improvements" so much as reclaiming the design's intended edge — everything downstream (calibration, thresholds, LLM judgment) is currently fitted to corrupted inputs.
2. **Rebuild regime detection with hysteresis.** Use the last 4-6 swings (not 2) for structure, add an ADX floor (e.g., 4H ADX ≥ 20 required for "Trending"), and require N consecutive runs before switching rubric. Rubric flapping between C-mode and M-mode on the same market is the most expensive kind of noise because it flips trade *direction* logic.
3. **Add a daily (1D) timeframe.** ~60 daily candles is a few KB: daily structure, weekly open, previous-week high/low, daily untested POCs. SMC entries keyed off 4H/1H but *targeted* at daily pools is the classic upgrade, and it slots into the existing pool/target-menu machinery unchanged.
4. **Session-aware volume gate (your Tier-2 item — do it first among them).** Compare 15m volume to the same-time-of-day average over the past N days instead of the last 30 bars; the current gate systematically flags Asia hours as LOW_VOL and halves size on perfectly normal sessions. Add ICT kill-zone tagging (London/NY opens) as a scoring input — cheap and native to the SMC vocabulary.
5. **Geometry realism.** Nearest-protected-swing stops with an ATR-band cap (BUG-06); enter at OB midpoint rather than the edge; require entry candidates to be *unswept* since formation (BUG-17); optionally emit expected-value (`win_rate × reward − loss_rate × risk`) per target using the calibration table, not just R:R.
6. **Decorrelate the rubric.** C2 (OB proximity), C5 (FVG magnet), C6 (OTE) are all "price is at a good location" — a single location can bank 40 pts, which is why scores cluster. Group into a location composite (max ~25) and free budget for orthogonal evidence: sweep quality, momentum, CVD, HTF alignment. Recalibrate thresholds afterwards.
7. **Upgrade calibration from ratio-weights to logistic regression.** You already persist `confluence_breakdown` + outcomes; a tiny logistic model (scikit-learn or even closed-form) yields calibrated win probabilities per component mix, replaces both `weights` and `score_to_winrate`, and handles correlated components properly. Keep the equal-weights fallback. Include timeouts with MFE-based partial credit (BUG-12), de-duplicate overlapping snapshots (keep one per symbol per hour, or weight by 1/overlap).
8. **Sweep detection window & scaling.** 5 closed 15m bars (~75 min) is a narrow memory for an HTF sweep; scan ~12-16 bars for HTF pools and require ATR-scaled minimum penetration (currently any tick through the level counts if the wick is big).
9. **Per-regime action thresholds.** A 60 in mean-revert mode (max ≈ 90, different component distribution) is not the same evidence as a 60 in trend mode; once eval data is trustworthy, fit thresholds per rubric from `score_to_winrate` rather than sharing 45/60.
10. **BTC-beta gate for alts (Tier-2).** Rolling 1h-return correlation + BTC 4H structure direction; veto alt longs when BTC is breaking down regardless of the alt's own chart. More predictive than the BTCDOM proxy alone.
11. **Track LLM-vs-engine divergence.** You label `trade_decision`, `forward_scenario`, and `engine_trade` separately but never compare them. Add an eval cut: hit-rate when `selected_from_engine=true` vs `false`. This directly answers "is the LLM's candidate-picking adding alpha or subtracting it?" — the central bet of v3.2.2/3.2.3.

---

## 4. Gemini context & prompt improvements

**Cut waste (token budget → more signal per token):**
- **Duplicate `engine_trade`** — it appears both at snapshot top level and inside `strategies` (`main.py:969-970`); the biggest object in the prompt is sent twice. Drop the top-level copy (keep it in the saved file if the dashboard needs it — attach after the LLM call like `chart_series`).
- `json.dumps(snapshot, indent=2)` → use `separators=(",", ":")`; indentation alone is roughly 15-20% of prompt tokens.
- Prune per-TF lists to what's decision-relevant: last 2 OBs / 2 FVGs per side instead of 3/5; drop `filled: true` FVGs; drop dead fields (`supertrend.level`, developing-swing raw arrays if unused by the rubric).

**Fix contradictions the model currently has to paper over:**
- Schema/mode mismatch (BUG-07) — this is the most important one; in ranging regimes today the model *must* invent C-values.
- Key-name mismatch (`c1_trend` schema vs `c1_trend_alignment` snapshot).
- The prompt says thresholds decide `action`, but also lets `score_override` change score — state explicitly whether action follows the original or overridden score.

**Add cheap high-value context:**
- **Precomputed ATR distances everywhere.** LLMs are unreliable at arithmetic; you already rank by confluence — add `dist_from_price_atr` to every entry/target candidate and key level so "which target is realistic within the horizon" requires zero math.
- **Event ages.** BOS/CHoCH carry `fresh` booleans; add `bars_ago` so the model can weigh a 2-bar-old break vs an 11-bar-old one (both currently just "fresh").
- **Volatility-to-target sanity.** One line: `atr_15m`, `atr_1h`, and the horizon assumption (24h), so the model can veto targets that are > X·ATR away.
- **`live_price` drift flag**: `live_vs_close_pct` — if the forming bar has already moved 1×ATR from the last close, entry-trigger phrasing should account for it.
- Units note for funding (decimal per 8h) in the system prompt; models often misread 0.0001 as 0.01%.

**Model config:**
- `thinking_level`: the code comment argues `medium`; the constant says `high`. Don't guess — run 30-50 labeled snapshots through both and compare selection quality (the eval loop already exists). Same harness can A/B prompt edits; that's the real "improve Gemini context" engine and it's nearly free given the tooling you already built.
- Keep sampling knobs omitted (correct per 3.x guidance).

---

## 5. Architectural improvements

- **Single market source of truth** (BUG-01's fix, elevated to principle): one exchange class, one symbol namespace, asserted at startup; snapshot records `market: "binance-usdm"`.
- **Parallel external fetches**: `asyncio.gather` over orderbook/funding/OI/F&G/BTC.D (all independent) — cuts wall time materially per run.
- **Config module**: `LIMITS`, `WINDOW_SIZE`, swing widths, gate thresholds, score thresholds, retention, model name are scattered constants across four files; one `config.py` (or TOML) makes tuning + future calibration sweeps sane.
- **Logging over prints**: structured `logging` with levels; warnings like the silently-dead OI fetch (BUG-04) should be loud once, not `print` noise per run.
- **Tests**: zero unit tests exist. Highest ROI: synthetic-candle tests for `fractal_swing_mask`, `get_market_structure` (BOS/CHoCH events), `get_value_area`, `_select_structural_tp`, and `_label_one` (both-sides-touched bars, gaps). These functions are pure and trivially testable — they're also where the P0 bugs lived.
- **Snapshot index**: `/api/history` re-reads every JSON on each call; at 1000 snapshots × N symbols that's seconds per request. Maintain a small SQLite (or one JSONL index) of header rows.
- **Graceful TF degradation** (BUG-15): if a TF is missing, either abort with a clear message or run in a declared degraded mode — never `KeyError`.
- **Pin `pandas-ta`**: it's effectively unmaintained; column-name contracts (`SUPERT_10_3.0` etc.) are fragile across versions. Pin exact versions in requirements or vendor the 3-4 indicators you use.
- **Reproduce CLAUDE.md**: this repo lost it; the README §Architecture + §Scoring is 90% of what it needs. (Happy to generate one from this audit if you want.)

---

## 6. Consolidated priority list

| # | Item | Type | Effort | Impact |
|---|------|------|--------|--------|
| 1 | Unify on USDT-perp data (fix CVD) — BUG-01 | fix | S | Critical |
| 2 | Raise candle limits ≥260 (EMA_200 alive) — BUG-02 | fix | XS | Critical |
| 3 | Label from `generated_at` (UTC) — BUG-03; then **relabel + recalibrate everything** | fix | XS | Critical |
| 4 | Nearest-protected-swing SL + ATR cap — BUG-06 | fix | S | Critical |
| 5 | Direction-aware OTE — BUG-05 | fix | S | High |
| 6 | Regime-conditional response schema + key alignment — BUG-07 | fix | S | High |
| 7 | OI history `limit=500` — BUG-04 | fix | XS | Medium |
| 8 | Regime detection: multi-swing structure + ADX floor + hysteresis — BUG-08 | improvement | M | High |
| 9 | De-duplicate `engine_trade` in prompt + compact JSON | prompt | XS | Medium |
| 10 | ATR distances + `bars_ago` on candidates/levels for the LLM | prompt | S | Medium |
| 11 | Dealing-range price-bracket check — BUG-09 | fix | S | Medium |
| 12 | Nearest liquidity pools kept (not extremes) — BUG-11 | fix | XS | Medium |
| 13 | Session-aware volume gate + kill zones | improvement | M | Medium |
| 14 | Add 1D timeframe (daily structure, PWH/PWL, daily POCs) | improvement | M | High |
| 15 | Calibration v2: logistic regression, timeout credit, snapshot de-dup — BUG-12 | improvement | M | High |
| 16 | Rubric decorrelation (location composite) + per-regime thresholds | improvement | M | Medium |
| 17 | LLM-vs-engine divergence eval cut | improvement | S | Medium |
| 18 | Thinking-level / prompt A/B via existing label loop | process | S | Medium |
| 19 | Parallel fetches, config module, logging, tests, snapshot index | arch | M | Medium |
| 20 | Remaining P2/P3s (events tz, rounding, retention mtime, dead code) | fix | S | Low |

**Suggested sequence:** items 1-7 in one "correctness release" (v3.2.4), then delete `data/calibration.json`, relabel all snapshots, and re-run eval to establish a *trustworthy* baseline — only then start 8-18, measuring each against that baseline. Improving on top of corrupted labels would optimize the wrong thing.

---

*Line references are to this repo state (v3.2.3, 2026-06-08). This is research tooling analysis, not financial advice.*
