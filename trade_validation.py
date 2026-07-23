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
"""

from alerts.controller import engine_gate
from config import ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD

PRICE_TOL = 5e-4  # relative tolerance for "same price as an engine level"


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
    action = _engine_action(snapshot)

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
    is_live = (action != "HOLD" and bool(et.get("valid"))
               and gate_state != "HARD_GATE" and eng_dir in ("BUY", "SELL"))

    if not is_live:
        _null_trade(td, violations)
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

    # entry / stop / take_profit must each be a real engine candidate level
    entry = _match_level(td.get("entry"), entries)
    if entry is None:
        violations.append("trade_decision.entry")
        entry, td["entry_source"] = et.get("entry"), et.get("entry_source")
    stop = _match_level(td.get("stop_loss"), stops)
    if stop is None:
        violations.append("trade_decision.stop_loss")
        stop, td["stop_loss_source"] = et.get("stop_loss"), et.get("stop_loss_source")
    tp = _match_level(td.get("take_profit"), targets)
    if tp is None:
        violations.append("trade_decision.take_profit")
        tp, td["take_profit_source"] = et.get("take_profit"), et.get("take_profit_source")

    # ordering + dead-on-arrival; if broken, revert the WHOLE geometry to the primary
    if not _geometry_ok(eng_dir, entry, stop, tp, live_price):
        violations.append("trade_decision.geometry")
        entry, stop, tp = et.get("entry"), et.get("stop_loss"), et.get("take_profit")
        td["entry_source"], td["stop_loss_source"], td["take_profit_source"] = (
            et.get("entry_source"), et.get("stop_loss_source"), et.get("take_profit_source"))

    td["entry"], td["stop_loss"], td["take_profit"] = entry, stop, tp
    _recompute_rr(td, entry, stop, tp)   # engine-side R:R, never the model's arithmetic

    tm = td.get("trade_management")
    if isinstance(tm, dict):
        sel = (_close(entry, et.get("entry")) and _close(stop, et.get("stop_loss"))
               and _close(tp, et.get("take_profit")))
        if tm.get("selected_from_engine") != sel:
            violations.append("trade_management.selected_from_engine")
        tm["selected_from_engine"] = sel
        risk = abs(entry - stop) if (entry is not None and stop is not None) else None
        for k, ksrc, krr in (("tp1", "tp1_source", "tp1_rr"), ("tp2", "tp2_source", "tp2_rr")):
            lvl = _match_level(tm.get(k), targets)
            if tm.get(k) is not None and lvl is None:
                violations.append(f"trade_management.{k}")
                tm[k], tm[ksrc], tm[krr] = None, None, None
            elif lvl is not None:
                tm[k] = lvl
                tm[krr] = round(abs(lvl - entry) / risk, 2) if risk else None

    analysis["contract_violations"] = violations
    return analysis, violations
