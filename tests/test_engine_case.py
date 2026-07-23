"""
I1.0 engine_case: the engine's structured argument for the adjudicator, built from data it
already computes. for/against split scored components by contribution; vetoes_or_caps lists
the bounds already applied; uncertainty carries the win-rate and degraded inputs.
"""

from main import _build_engine_case, _data_degraded, _build_llm_view


def _snap(**over):
    strat = {
        "score_mode": "trend", "confluence_score": 68,
        "setup_direction": "bullish", "trend_bias": "bullish",
        "volume_gate": {"state": "CLEAR"}, "cvd_absorption_warning": False,
        "order_flow_modifier": -3, "order_flow_notes": ["crowded longs (funding p85, rising)"],
        "empirical_win_rate": 0.57, "win_rate_source": "logistic",
        "confluence_breakdown": {"c1_trend_alignment": 20, "c2_ob_proximity": 8,
                                 "c3_liquidity_sweep": 0, "c4_momentum": 12},
        "confluence_notes": {"c1_trend_alignment": "4H+1H HH/HL",
                             "c2_ob_proximity": "in 15m bull OB",
                             "c3_liquidity_sweep": "no sweep",
                             "c4_momentum": "1H RSI rising"},
        "engine_trade": {"valid": True, "direction": "BUY", "entry": 100.0,
                         "stop_loss": 96.0, "take_profit": 110.0, "rr": 2.5},
    }
    strat.update(over.pop("strategies", {}))
    snap = {
        "symbol": "SOL/USDT", "event_guard": {"active": False}, "strategies": strat,
        "orderbook": {"depth_bins": [{"imbalance": 0.1}]},
        "funding": {"percentile_window": 85}, "open_interest": {"value": 1.2e9},
        "windowed_indicators": {"15m": {"cvd_is_real": True}},
    }
    snap.update(over)
    return snap


def test_for_against_split_by_contribution():
    case = _build_engine_case(_snap())
    for_comps = {r["component"] for r in case["for"] if "component" in r}
    against_comps = {r["component"] for r in case["against"] if "component" in r}
    assert for_comps == {"c1_trend_alignment", "c2_ob_proximity", "c4_momentum"}
    assert against_comps == {"c3_liquidity_sweep"}          # 0 pts -> against


def test_order_flow_folded_by_sign():
    case = _build_engine_case(_snap())                      # of_mod = -3
    of = [r for r in case["against"] if r.get("source") == "order_flow"]
    assert of and of[0]["pts"] == -3
    # positive modifier folds into `for`
    case_pos = _build_engine_case(_snap(strategies={"order_flow_modifier": 4,
                                                    "order_flow_notes": ["shorts paying"]}))
    assert any(r.get("source") == "order_flow" for r in case_pos["for"])


def test_thesis_states_traded_geometry():
    t = _build_engine_case(_snap())["thesis"]
    assert "BUY" in t and "100.0" in t and "trend rubric" in t and "score 68" in t


def test_thesis_when_no_valid_trade():
    t = _build_engine_case(_snap(strategies={"engine_trade": {"valid": False},
                                             "setup_direction": "neutral"}))["thesis"]
    assert t.startswith("no valid structural trade")


def test_vetoes_event_guard_cvd_lowvol_counterstructure():
    snap = _snap(event_guard={"active": True},
                 strategies={"cvd_absorption_warning": True,
                             "volume_gate": {"state": "LOW_VOL_WARNING"},
                             "setup_direction": "bearish", "trend_bias": "bullish",
                             "engine_trade": {"valid": True, "direction": "SELL",
                                              "entry": 100, "stop_loss": 104, "take_profit": 90, "rr": 2.5}})
    v = " | ".join(_build_engine_case(snap)["vetoes_or_caps"])
    assert "event_guard" in v and "cvd_absorption_warning" in v
    assert "LOW_VOL_WARNING" in v and "counter_structure" in v


def test_no_vetoes_on_clean_aligned_setup():
    assert _build_engine_case(_snap())["vetoes_or_caps"] == []


def test_uncertainty_block():
    u = _build_engine_case(_snap())["uncertainty"]
    assert u["win_rate"] == 0.57 and u["win_rate_source"] == "logistic"
    assert u["sample_support"] is None and u["data_degraded"] == []


def test_data_degraded_flags_missing_inputs():
    snap = _snap(orderbook={"depth_bins": []}, funding={"percentile_window": None},
                 open_interest={"value": None},
                 windowed_indicators={"15m": {"cvd_is_real": False}})
    assert set(_data_degraded(snap)) == {"orderbook", "funding", "open_interest", "cvd_proxy"}


def test_llm_view_attaches_engine_case():
    view = _build_llm_view(_snap())
    assert "engine_case" in view and "thesis" in view["engine_case"]
