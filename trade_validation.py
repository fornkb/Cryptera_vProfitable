"""
B3.0 / I2.0 — deterministic post-LLM validation & normalization.

"Rules decide, the LLM narrates" was enforced only by the prompt: nothing checked the
model's output beyond JSON shape, so it could echo a price absent from the candidate menu,
set header.action against the engine's gates, emit BUY with the stop above entry, claim
selected_from_engine while its prices differ, or emit a live trade while engine_trade is
invalid / the volume HARD_GATE is active — and that plan would be persisted, labelled, and
alerted.

`validate_and_normalize_analysis` runs after every model response, before persistence/alerts:
  * recomputes header pair/price/regime/bias/volume_gate/score/action from ENGINE data —
    the model's copies are never trusted; action is derived from the engine gate in code;
  * forces every trade price to be a real engine candidate level (relative tol <= 5e-4),
    reverting an off-menu value to the engine primary;
  * enforces direction == engine and entry/stop/target ordering + a dead-on-arrival guard,
    reverting the whole geometry to the primary if broken;
  * recomputes R:R / sl_width engine-side;
  * nulls all trade fields on HOLD / invalid geometry / HARD_GATE;
  * derives selected_from_engine by comparison, never from the model's claim;
  * records every correction in analysis['contract_violations'] (an eval-able honesty metric).

Pure and dependency-light (engine_gate + config thresholds only) so it unit-tests without a
Gemini call. This is the load-bearing enforcement the Wave-2 adjudicator (I1.0) sits on.

I1.0 (schema v2) extends it into the adjudicator's substrate via `analysis.verdict`:
  * AGREE  — the trade IS the engine primary (the model may only re-manage/re-probability);
  * UPDATE — keep the engine direction, re-select entry/stop/targets from the candidate menu
    BY `level_id`; ids resolve to the engine's exact price (no float-tolerance guessing), an
    unresolvable id reverts that field to the primary;
  * REJECT — decline -> HOLD (overrides the engine gate) with a validated `reject_reason`.
An absent/unknown verdict (the deterministic NO_CALL skeleton, pre-v2 analyses) falls back to
the original tolerance-match path, so nothing regresses.
"""

from alerts.controller import engine_gate
from config import ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD

PRICE_TOL = 5e-4  # relative tolerance for "same price as an engine level"

# I1.0 adjudicator verdicts. UPDATE is the valued path: keep the engine's direction, re-select
# levels from the candidate menu BY level_id. REJECT declines the setup -> HOLD.
VERDICTS = ("AGREE", "UPDATE", "REJECT")
REJECT_REASONS = ("breakout_risk", "counter_htf_structure", "crowding", "data_quality",
                  "event_risk", "poor_rr", "other")


def _close(a, b, tol=PRICE_TOL) -> bool:
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    return abs(a - b) / max(abs(a), abs(b), 1e-9) <= tol


def _match_level(value, candidates):
    """The candidate {'price':...} nearest `value` within tolerance, or None. `candidates`
    may hold dicts (engine menu) or bare numbers."""
    if value is None:
        return None
    best = None
    for c in candidates or []:
        p = c.get("price") if isinstance(c, dict) else c
        if p is None or not _close(value, p):
            continue
        if best is None or abs(float(value) - float(p)) < abs(float(value) - float(best)):
            best = float(p)
    return best


def _norm_verdict(v):
    """Map a model verdict to AGREE/UPDATE/REJECT, or None for absent/unknown (the deterministic
    NO_CALL skeleton and pre-v2 analyses land here -> the legacy tolerance-match path)."""
    v = (v or "").strip().upper() if isinstance(v, str) else ""
    return v if v in VERDICTS else None


def _resolve_id(id_val, rows):
    """Resolve a candidate `level_id` (E*/S*/T*) to its EXACT (price, source) from the engine
    menu, or (None, None). This is the I1.0 selection contract: the model outputs an id, the
    engine owns the price -- so there is no float-tolerance guessing of which level it meant."""
    if not id_val:
        return None, None
    for r in rows or []:
        if isinstance(r, dict) and r.get("level_id") == id_val:
            p = r.get("price")
            return (float(p) if p is not None else None), r.get("source")
    return None, None


def _select_field(verdict, td, id_key, price_key, src_key, rows, prim_price, prim_src,
                  violations):
    """Pick one geometry level (entry/stop/tp) per the verdict, returning (price, source):
      * UPDATE  -> a null id keeps the engine primary field (a partial update is normal);
        a non-null id resolves against the menu by level_id; a non-null off-menu id reverts
        to the primary and records a `<id_key>` violation;
      * AGREE   -> the engine primary, untouched (the model may only re-manage, not re-price);
      * legacy (verdict None) -> the pre-v2 behaviour: tolerance-match the model's numeric
        price, reverting off-menu values to the primary (records `<price_key>` violation)."""
    if verdict == "UPDATE":
        id_val = td.get(id_key)
        if not id_val:                              # null id -> keep the engine primary field
            return prim_price, prim_src
        price, src = _resolve_id(id_val, rows)
        if price is None:                           # non-null but off-menu -> revert + record
            violations.append(f"trade_decision.{id_key}")
            return prim_price, prim_src
        return price, (src if src is not None else td.get(src_key))
    if verdict == "AGREE":
        return prim_price, prim_src
    price = _match_level(td.get(price_key), rows)
    if price is None:
        violations.append(f"trade_decision.{price_key}")
        return prim_price, prim_src
    return price, td.get(src_key)


def _normalize_reject(analysis, violations):
    """Coerce a REJECT's reject_reason to {code, note} with a code from the enum; an
    absent/unknown code becomes `other` and is recorded."""
    rr = analysis.get("reject_reason")
    code = rr.get("code") if isinstance(rr, dict) else None
    note = rr.get("note", "") if isinstance(rr, dict) else ""
    if code not in REJECT_REASONS:
        violations.append("reject_reason")
        code = "other"
    analysis["reject_reason"] = {"code": code, "note": note}


def _engine_action(snapshot):
    g = engine_gate(snapshot, ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD)
    if g["active"]:
        return "ACTIVE_TRADE"
    if g["conditional"]:
        return "CONDITIONAL_ENTRY"
    return "HOLD"


def _geometry_ok(direction, entry, stop, tp, live_price) -> bool:
    if entry is None or stop is None or tp is None:
        return False
    if direction == "BUY":
        if not (stop < entry < tp):
            return False
        if live_price is not None and live_price <= stop:   # dead on arrival
            return False
    else:  # SELL
        if not (tp < entry < stop):
            return False
        if live_price is not None and live_price >= stop:   # dead on arrival
            return False
    return True


def _recompute_rr(td, entry, stop, tp):
    rr = td.get("rr")
    if entry is None or stop is None or tp is None:
        return
    risk, reward = abs(entry - stop), abs(tp - entry)
    ratio = round(reward / risk, 2) if risk > 0 else None
    if isinstance(rr, dict):
        rr["tp_distance"] = round(reward, 6)
        rr["sl_distance"] = round(risk, 6)
        rr["ratio"] = ratio
        rr["passed"] = bool(ratio is not None and ratio >= 2.0)
    if entry:
        td["sl_width_pct"] = round(risk / entry * 100, 3)


def _null_trade(td, violations):
    """Force a clean HOLD: every trade price/plan field null, direction HOLD."""
    had_trade = any(td.get(f) is not None for f in ("entry", "stop_loss", "take_profit"))
    for f in ("entry", "entry_source", "stop_loss", "stop_loss_source",
              "take_profit", "take_profit_source", "sl_width_pct"):
        td[f] = None
    rr = td.get("rr")
    if isinstance(rr, dict):
        rr["tp_distance"] = rr["sl_distance"] = rr["ratio"] = None
        rr["passed"] = False
    prim = td.get("primary")
    if isinstance(prim, dict):
        prim["direction"] = "HOLD"
    tm = td.get("trade_management")
    if isinstance(tm, dict):
        for f in ("tp1", "tp1_source", "tp1_rr", "tp2", "tp2_source", "tp2_rr",
                  "move_to_breakeven_at"):
            tm[f] = None
        tm["selected_from_engine"] = True
    if had_trade:
        violations.append("trade_fields_nulled_on_hold")


def validate_and_normalize_analysis(analysis, snapshot):
    """Returns (analysis, violations). Mutates `analysis` in place; never raises on
    malformed input. Also stamps analysis['contract_violations']."""
    violations: list = []
    if not isinstance(analysis, dict) or analysis.get("error"):
        return analysis, violations

    strat = snapshot.get("strategies") or {}
    et = snapshot.get("engine_trade") or strat.get("engine_trade") or {}
    symbol = snapshot.get("symbol")
    live_price = snapshot.get("live_price")
    regime = snapshot.get("market_regime")
    score = strat.get("confluence_score", 0)
    trend_bias = (strat.get("trend_bias") or "neutral")
    gate_state = (strat.get("volume_gate") or {}).get("state") or "CLEAR"
    # I1.0: header.action is engine-derived post-call. REJECT forces HOLD even on a setup the
    # engine gate would call live -- the adjudicator declined it.
    verdict = _norm_verdict(analysis.get("verdict"))
    action = "HOLD" if verdict == "REJECT" else _engine_action(snapshot)

    # ---- header: engine truth, never the model's copies ----
    header = analysis.get("header")
    if isinstance(header, dict):
        def _fix(field, engine_val):
            if header.get(field) != engine_val:
                violations.append(f"header.{field}")
            header[field] = engine_val
        if symbol is not None:
            _fix("pair", symbol)
        if live_price is not None:
            _fix("price", live_price)
        if regime is not None:
            _fix("regime", regime)
        _fix("bias", trend_bias.upper())
        _fix("volume_gate", gate_state)
        _fix("score", score)
        _fix("action", action)

    td = analysis.get("trade_decision")
    if not isinstance(td, dict):
        analysis["contract_violations"] = violations
        return analysis, violations

    eng_dir = et.get("direction")  # BUY / SELL
    # verdict == REJECT already forced action HOLD above, so it drops here to a nulled trade.
    is_live = (action != "HOLD" and bool(et.get("valid"))
               and gate_state != "HARD_GATE" and eng_dir in ("BUY", "SELL"))

    if not is_live:
        _null_trade(td, violations)
        if verdict == "REJECT":
            _normalize_reject(analysis, violations)
        analysis["contract_violations"] = violations
        return analysis, violations

    cands = et.get("candidates") or {}
    entries, stops, targets = (cands.get("entries") or [], cands.get("stops") or [],
                               cands.get("targets") or [])

    # direction must match the engine (it emits exactly one direction)
    prim = td.get("primary")
    if isinstance(prim, dict) and prim.get("direction") != eng_dir:
        violations.append("trade_decision.primary.direction")
        prim["direction"] = eng_dir

    # entry / stop / take_profit selection: UPDATE resolves candidate level_ids, AGREE takes
    # the engine primary, legacy (no verdict) tolerance-matches the model's numeric prices.
    entry, entry_src = _select_field(verdict, td, "entry_id", "entry", "entry_source",
                                     entries, et.get("entry"), et.get("entry_source"), violations)
    stop, stop_src = _select_field(verdict, td, "stop_loss_id", "stop_loss", "stop_loss_source",
                                   stops, et.get("stop_loss"), et.get("stop_loss_source"), violations)
    tp, tp_src = _select_field(verdict, td, "take_profit_id", "take_profit", "take_profit_source",
                               targets, et.get("take_profit"), et.get("take_profit_source"), violations)

    # ordering + dead-on-arrival; if broken, revert the WHOLE geometry to the primary
    if not _geometry_ok(eng_dir, entry, stop, tp, live_price):
        violations.append("trade_decision.geometry")
        entry, stop, tp = et.get("entry"), et.get("stop_loss"), et.get("take_profit")
        entry_src, stop_src, tp_src = (et.get("entry_source"), et.get("stop_loss_source"),
                                       et.get("take_profit_source"))

    td["entry"], td["stop_loss"], td["take_profit"] = entry, stop, tp
    td["entry_source"], td["stop_loss_source"], td["take_profit_source"] = entry_src, stop_src, tp_src
    _recompute_rr(td, entry, stop, tp)   # engine-side R:R, never the model's arithmetic

    tm = td.get("trade_management")
    if isinstance(tm, dict):
        sel = (_close(entry, et.get("entry")) and _close(stop, et.get("stop_loss"))
               and _close(tp, et.get("take_profit")))
        if tm.get("selected_from_engine") != sel:
            violations.append("trade_management.selected_from_engine")
        tm["selected_from_engine"] = sel
        risk = abs(entry - stop) if (entry is not None and stop is not None) else None
        for k, kid, ksrc, krr in (("tp1", "tp1_id", "tp1_source", "tp1_rr"),
                                  ("tp2", "tp2_id", "tp2_source", "tp2_rr")):
            if tm.get(kid):                                  # id-based selection (AGREE/UPDATE)
                lvl, src = _resolve_id(tm.get(kid), targets)
                if lvl is None:
                    violations.append(f"trade_management.{kid}")
                    tm[k] = tm[ksrc] = tm[krr] = None
                else:
                    tm[k], tm[ksrc] = lvl, (src if src is not None else tm.get(ksrc))
                    tm[krr] = round(abs(lvl - entry) / risk, 2) if risk else None
            else:                                            # legacy numeric tp
                lvl = _match_level(tm.get(k), targets)
                if tm.get(k) is not None and lvl is None:
                    violations.append(f"trade_management.{k}")
                    tm[k], tm[ksrc], tm[krr] = None, None, None
                elif lvl is not None:
                    tm[k] = lvl
                    tm[krr] = round(abs(lvl - entry) / risk, 2) if risk else None

    analysis["contract_violations"] = violations
    return analysis, violations
