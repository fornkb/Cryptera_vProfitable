"""Gate + transition/cooldown logic: decides which symbols are ACTIVE and which deserve
a (budget-spending) Gemini narration this cycle. Pure functions -- easy to unit-test with
saved snapshots and a fake clock."""

import time


def engine_gate(snap, threshold, conditional_threshold):
    """Replicate the engine's OWN gate logic so we never spend a Gemini call on a
    setup the engine itself would not call ACTIVE_TRADE.

    This version scores against a single GLOBAL threshold pair (config.py's
    ACTIVE_TRADE_THRESHOLD / CONDITIONAL_ENTRY_THRESHOLD) -- there is no per-module
    router, no active_module/mr_submode, and no liquidation-based gate to replicate
    (those are v4.x-only concepts). `setup_direction == "neutral"` (the ranging-regime
    no-fade-setup case) already carries the same "nothing to trade" meaning a v4 grey-zone
    module would.
    """
    s = snap.get("strategies", {}) or {}
    et = s.get("engine_trade") or {}
    gate = (s.get("volume_gate") or {}).get("state")
    score = s.get("confluence_score", 0)
    # Structural eligibility shared by ACTIVE and CONDITIONAL (direction/store/gate/geometry).
    base_ok = (
        s.get("setup_direction") in ("bullish", "bearish")  # "neutral" (no fade setup) never alerts
        and gate != "HARD_GATE"                             # forced HOLD
        and bool(et.get("valid"))                           # no structural trade -> HOLD
    )
    active = (
        base_ok
        and score >= threshold
        and not (snap.get("event_guard") or {}).get("active")  # event guard caps at CONDITIONAL
    )
    # CONDITIONAL band = [conditional_threshold, threshold), or an event-guard-capped high score.
    conditional = bool(base_ok and not active and score >= conditional_threshold)
    return {
        "active": active,
        "conditional": conditional,
        "score": score,
        "threshold": threshold,
        "direction": s.get("setup_direction"),
        "win_rate": s.get("empirical_win_rate"),
        "mode": s.get("score_mode"),
        "gate": gate,
        "et": et,
    }


def should_narrate(prev, d, cfg, now=None):
    """Fire a narration only on a state TRANSITION, never every cycle while a setup
    persists: rising-edge cross into ACTIVE, or a material change (direction flip /
    score jump >= material_delta) while already active -- and only after the per-symbol
    cooldown has elapsed."""
    now = now if now is not None else time.time()
    was_active = prev.get("active", False)
    crossed_up = d["active"] and not was_active
    material = d["active"] and was_active and (
        d["direction"] != prev.get("last_direction")
        or (d["score"] - prev.get("last_score", 0)) >= cfg.material_delta
    )
    if not (crossed_up or material):
        return False
    return (now - prev.get("last_alert_ts", 0)) >= cfg.min_realert_s


def rank(eligible):
    """Best-first by confluence score, then empirical win-rate. `eligible` is a
    list of (symbol, snap, data, decision) tuples."""
    return sorted(eligible, key=lambda t: (t[3]["score"], t[3]["win_rate"] or 0.0), reverse=True)


def new_state(d, now=None):
    now = now if now is not None else time.time()
    return {
        "active": d["active"],
        "last_score": d["score"],
        "last_direction": d["direction"],
        "last_action": "ACTIVE_TRADE" if d["active"] else "—",
        "last_alert_ts": now,
    }
