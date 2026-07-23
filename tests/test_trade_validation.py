"""
B3.0 / I2.0: deterministic post-LLM validation. The engine's gates/prices/geometry are
authoritative; the model's copies are recomputed, off-menu prices reverted, HOLD nulled,
selected_from_engine derived, and every correction recorded in contract_violations.
"""

from trade_validation import validate_and_normalize_analysis


def _snap(score=70, gate="CLEAR", setup_direction="bullish", valid=True, event=False,
          entry=100.0, sl=95.0, tp=112.0, live_price=100.5):
    et = {
        "valid": valid,
        "direction": "BUY" if setup_direction == "bullish" else "SELL",
        "entry": entry, "stop_loss": sl, "take_profit": tp, "rr": 2.4,
        "entry_source": "OB mid", "stop_loss_source": "swing", "take_profit_source": "BSL",
        "candidates": {
            "entries": [{"price": entry, "source": "OB mid"}, {"price": 99.0, "source": "FVG"}],
            "stops": [{"price": sl, "source": "swing"}],
            "targets": [{"price": tp, "source": "BSL"}, {"price": 108.0, "source": "POC"}],
        },
    }
    return {
        "symbol": "BTC/USDT", "live_price": live_price, "market_regime": "Trending Bullish",
        "event_guard": {"active": event},
        "strategies": {
            "confluence_score": score, "score_mode": "trend", "trend_bias": "bullish",
            "setup_direction": setup_direction, "volume_gate": {"state": gate},
            "engine_trade": et,
        },
        "engine_trade": et,
    }


def _analysis(entry=100.0, sl=95.0, tp=112.0, direction="BUY", sel=True):
    return {
        "header": {"pair": "WRONG", "price": 0.0, "bias": "BEARISH", "regime": "X",
                   "score": 99, "action": "HOLD", "volume_gate": "HARD_GATE"},
        "trade_decision": {
            "primary": {"direction": direction, "probability": 60},
            "entry": entry, "entry_source": "x", "stop_loss": sl, "stop_loss_source": "x",
            "take_profit": tp, "take_profit_source": "x",
            "rr": {"tp_distance": 0, "sl_distance": 0, "ratio": 0, "passed": False},
            "sl_width_pct": 0,
            "trade_management": {"selected_from_engine": sel, "selection_rationale": "",
                                 "tp1": None, "tp1_source": "", "tp1_rr": None,
                                 "tp2": None, "tp2_source": "", "tp2_rr": None,
                                 "move_to_breakeven_at": None, "scale_out": ""},
        },
    }


def test_header_recomputed_from_engine_not_model():
    a, v = validate_and_normalize_analysis(_analysis(), _snap(score=70))
    h = a["header"]
    assert h["pair"] == "BTC/USDT" and h["price"] == 100.5 and h["bias"] == "BULLISH"
    assert h["score"] == 70 and h["action"] == "ACTIVE_TRADE" and h["volume_gate"] == "CLEAR"
    for f in ("header.pair", "header.bias", "header.score", "header.action", "header.volume_gate"):
        assert f in v


def test_hold_nulls_all_trade_fields():
    a, v = validate_and_normalize_analysis(_analysis(), _snap(score=40))   # <45 -> HOLD
    td = a["trade_decision"]
    assert a["header"]["action"] == "HOLD"
    assert td["entry"] is None and td["stop_loss"] is None and td["take_profit"] is None
    assert td["primary"]["direction"] == "HOLD"
    assert td["rr"]["passed"] is False
    assert "trade_fields_nulled_on_hold" in v


def test_hard_gate_forces_hold():
    a, _ = validate_and_normalize_analysis(_analysis(), _snap(score=70, gate="HARD_GATE"))
    assert a["header"]["action"] == "HOLD"
    assert a["trade_decision"]["entry"] is None


def test_off_menu_entry_reverts_to_engine_primary():
    a = _analysis()
    a["trade_decision"]["entry"] = 123.45                    # not a candidate
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0))
    assert a["trade_decision"]["entry"] == 100.0
    assert "trade_decision.entry" in v


def test_direction_must_match_engine():
    a = _analysis(direction="SELL")
    a, v = validate_and_normalize_analysis(a, _snap(setup_direction="bullish"))
    assert a["trade_decision"]["primary"]["direction"] == "BUY"
    assert "trade_decision.primary.direction" in v


def test_selected_from_engine_derived_by_comparison():
    a = _analysis(entry=99.0, sel=True)                      # 99 is a valid candidate, != primary 100
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0))
    assert a["trade_decision"]["entry"] == 99.0             # kept (real level)
    assert a["trade_decision"]["trade_management"]["selected_from_engine"] is False
    assert "trade_management.selected_from_engine" in v


def test_rr_recomputed_engine_side():
    a = _analysis()
    a["trade_decision"]["rr"] = {"tp_distance": 999, "sl_distance": 999, "ratio": 99, "passed": True}
    a, _ = validate_and_normalize_analysis(a, _snap(entry=100.0, sl=95.0, tp=112.0))
    rr = a["trade_decision"]["rr"]
    assert rr["ratio"] == 2.4 and rr["passed"] is True       # 12 / 5
    assert a["trade_decision"]["sl_width_pct"] == 5.0


def test_broken_geometry_reverts_to_primary():
    snap = _snap()
    snap["engine_trade"]["candidates"]["targets"].append({"price": 90.0, "source": "bad"})
    a = _analysis()
    a["trade_decision"]["take_profit"] = 90.0               # valid level but below entry for a BUY
    a, v = validate_and_normalize_analysis(a, snap)
    assert a["trade_decision"]["take_profit"] == 112.0      # reverted
    assert "trade_decision.geometry" in v


def test_clean_echo_has_no_violations():
    snap = _snap(score=70)
    a = {
        "header": {"pair": "BTC/USDT", "price": 100.5, "bias": "BULLISH",
                   "regime": "Trending Bullish", "score": 70, "action": "ACTIVE_TRADE",
                   "volume_gate": "CLEAR"},
        "trade_decision": {
            "primary": {"direction": "BUY", "probability": 60},
            "entry": 100.0, "entry_source": "", "stop_loss": 95.0, "stop_loss_source": "",
            "take_profit": 112.0, "take_profit_source": "",
            "rr": {"tp_distance": 12, "sl_distance": 5, "ratio": 2.4, "passed": True},
            "sl_width_pct": 5.0,
            "trade_management": {"selected_from_engine": True, "selection_rationale": "",
                                 "tp1": None, "tp1_source": "", "tp1_rr": None,
                                 "tp2": None, "tp2_source": "", "tp2_rr": None,
                                 "move_to_breakeven_at": None, "scale_out": ""},
        },
    }
    a, v = validate_and_normalize_analysis(a, snap)
    assert v == []
    assert a["trade_decision"]["trade_management"]["selected_from_engine"] is True


def test_error_analysis_passes_through_untouched():
    a, v = validate_and_normalize_analysis({"error": "gemini failed"}, _snap())
    assert a == {"error": "gemini failed"} and v == []
