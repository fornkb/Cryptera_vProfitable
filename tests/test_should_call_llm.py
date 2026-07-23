"""
I1.0 trigger gate: should_call_llm is the single predicate deciding whether a snapshot
earns a Gemini adjudication call. Below the gate, run_analysis emits a deterministic engine
HOLD with ZERO Gemini spend -- narrate() must never be called.
"""

import asyncio

import main
from config import LLM_CALL_THRESHOLD


def _snap(score=70, gate="CLEAR", valid=True):
    return {
        "symbol": "SOL/USDT", "live_price": 100.0, "market_regime": "Trending Bullish",
        "strategies": {
            "confluence_score": score, "volume_gate": {"state": gate},
            "engine_trade": {"valid": valid, "direction": "BUY", "candidates": {}},
        },
    }


# ---------------- the pure predicate ---------------- #

def test_gate_true_at_threshold():
    assert main.should_call_llm(_snap(score=LLM_CALL_THRESHOLD)) is True


def test_gate_false_below_threshold():
    assert main.should_call_llm(_snap(score=LLM_CALL_THRESHOLD - 1)) is False


def test_gate_false_when_engine_trade_invalid():
    assert main.should_call_llm(_snap(score=90, valid=False)) is False


def test_gate_false_on_hard_gate():
    assert main.should_call_llm(_snap(score=90, gate="HARD_GATE")) is False


def test_gate_true_low_vol_warning_still_calls():
    # LOW_VOL_WARNING halves size but is still a tradeable setup -> adjudicate.
    assert main.should_call_llm(_snap(score=70, gate="LOW_VOL_WARNING")) is True


def test_gate_safe_on_empty_snapshot():
    assert main.should_call_llm({}) is False
    assert main.should_call_llm(None) is False


# ---------------- deterministic HOLD skeleton ---------------- #

def test_deterministic_analysis_is_a_clean_hold():
    a = main._finalize_analysis(main._deterministic_analysis(_snap(score=30)), _snap(score=30))
    assert a["verdict"] == "NO_CALL"
    assert a["header"]["action"] == "HOLD"
    td = a["trade_decision"]
    assert td["entry"] is None and td["stop_loss"] is None and td["take_profit"] is None
    assert td["primary"]["direction"] == "HOLD"


# ---------------- run_analysis wiring: narrate only above the gate ---------------- #

def _run_analysis_with(monkeypatch, snap):
    calls = {"narrate": 0}

    async def fake_compute(symbol):
        return snap, {}

    monkeypatch.setattr(main, "compute_snapshot_no_llm", fake_compute)
    monkeypatch.setattr(main, "narrate",
                        lambda s: (calls.__setitem__("narrate", calls["narrate"] + 1)
                                   or {"header": {}, "trade_decision": {}}))
    monkeypatch.setattr(main, "build_chart_series", lambda data: {})
    monkeypatch.setattr(main, "_persist_snapshot", lambda s, *a, **k: "path")
    _, analysis = asyncio.run(main.run_analysis("SOL/USDT"))
    return calls, analysis


def test_run_analysis_skips_gemini_below_gate(monkeypatch):
    calls, analysis = _run_analysis_with(monkeypatch, _snap(score=20))
    assert calls["narrate"] == 0                        # zero Gemini spend
    assert analysis["verdict"] == "NO_CALL"
    assert analysis["header"]["action"] == "HOLD"


def test_run_analysis_calls_gemini_above_gate(monkeypatch):
    calls, _ = _run_analysis_with(monkeypatch, _snap(score=70))
    assert calls["narrate"] == 1
