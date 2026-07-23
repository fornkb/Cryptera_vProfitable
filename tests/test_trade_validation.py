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
            "entries": [{"price": entry, "source": "OB mid", "level_id": "E1"},
                        {"price": 99.0, "source": "FVG", "level_id": "E2"}],
            "stops": [{"price": sl, "source": "swing", "level_id": "S1"},
                      {"price": 94.0, "source": "DR low", "level_id": "S2"}],
            "targets": [{"price": tp, "source": "BSL", "level_id": "T1"},
                        {"price": 108.0, "source": "POC", "level_id": "T2"}],
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


# ---------------- I1.0 schema v2: verdict + level_id resolution ---------------- #

def _v2(verdict, entry_id=None, stop_loss_id=None, take_profit_id=None,
        reject_reason=None, entry=None, sl=None, tp=None):
    a = _analysis(entry=entry if entry is not None else 100.0,
                  sl=sl if sl is not None else 95.0,
                  tp=tp if tp is not None else 112.0)
    a["verdict"] = verdict
    if reject_reason is not None:
        a["reject_reason"] = reject_reason
    td = a["trade_decision"]
    td["entry_id"], td["stop_loss_id"], td["take_profit_id"] = entry_id, stop_loss_id, take_profit_id
    return a


def test_agree_forces_engine_primary_ignoring_model_prices():
    # model wrote off-menu prices but verdict AGREE -> engine primary wins, no price violation
    a = _v2("AGREE", entry=123.45, sl=1.0, tp=999.0)
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0, sl=95.0, tp=112.0))
    td = a["trade_decision"]
    assert (td["entry"], td["stop_loss"], td["take_profit"]) == (100.0, 95.0, 112.0)
    assert td["trade_management"]["selected_from_engine"] is True
    assert "trade_decision.entry" not in v          # AGREE path never tolerance-matches


def test_update_resolves_entry_id_to_candidate_price():
    a = _v2("UPDATE", entry_id="E2")                # E2 -> 99.0, others null -> keep primary
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0))
    td = a["trade_decision"]
    assert td["entry"] == 99.0 and td["stop_loss"] == 95.0 and td["take_profit"] == 112.0
    assert td["entry_source"] == "FVG"              # source pulled from the resolved candidate
    assert td["trade_management"]["selected_from_engine"] is False
    # the id resolution + geometry are clean (the selected_from_engine correction is expected:
    # the fixture claimed True, the UPDATE made it False).
    assert not [x for x in v if "_id" in x or x.endswith(".geometry")]


def test_update_null_ids_keep_primary_without_violation():
    a = _v2("UPDATE")                               # all ids null -> full primary
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0))
    td = a["trade_decision"]
    assert (td["entry"], td["stop_loss"], td["take_profit"]) == (100.0, 95.0, 112.0)
    assert td["trade_management"]["selected_from_engine"] is True
    assert not [x for x in v if x.startswith("trade_")]


def test_update_unresolvable_id_reverts_field_and_records():
    a = _v2("UPDATE", stop_loss_id="S9")            # not in the menu
    a, v = validate_and_normalize_analysis(a, _snap(sl=95.0))
    assert a["trade_decision"]["stop_loss"] == 95.0
    assert "trade_decision.stop_loss_id" in v


def test_update_tp_management_by_id():
    a = _v2("UPDATE")
    a["trade_decision"]["trade_management"]["tp1_id"] = "T2"     # -> 108.0
    a, v = validate_and_normalize_analysis(a, _snap(entry=100.0, sl=95.0))
    tm = a["trade_decision"]["trade_management"]
    assert tm["tp1"] == 108.0 and tm["tp1_source"] == "POC"
    assert tm["tp1_rr"] == round(abs(108.0 - 100.0) / 5.0, 2)


def test_reject_forces_hold_over_active_engine():
    a = _v2("REJECT", reject_reason={"code": "crowding", "note": "OI building against"})
    a, v = validate_and_normalize_analysis(a, _snap(score=80))   # engine would call ACTIVE
    assert a["header"]["action"] == "HOLD"
    td = a["trade_decision"]
    assert td["entry"] is None and td["primary"]["direction"] == "HOLD"
    assert a["reject_reason"] == {"code": "crowding", "note": "OI building against"}


def test_reject_invalid_reason_coerced_to_other():
    a = _v2("REJECT", reject_reason={"code": "vibes", "note": "n"})
    a, v = validate_and_normalize_analysis(a, _snap(score=80))
    assert a["reject_reason"]["code"] == "other" and "reject_reason" in v


def test_update_direction_still_forced_to_engine():
    a = _v2("UPDATE", entry_id="E2")
    a["trade_decision"]["primary"]["direction"] = "SELL"
    a, v = validate_and_normalize_analysis(a, _snap(setup_direction="bullish"))
    assert a["trade_decision"]["primary"]["direction"] == "BUY"
    assert "trade_decision.primary.direction" in v
