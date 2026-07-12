"""
Unit tests for main.py: header.score_breakdown is engine data spliced in after
the Gemini call, not LLM output (removed from GEMINI_RESPONSE_SCHEMA -- see
_splice_score_breakdown).
"""

from main import _splice_score_breakdown


def test_splices_score_breakdown_from_strategies():
    analysis = {"header": {"pair": "SOL/USDT", "score": 72}}
    strategies = {"confluence_breakdown": {"c1_trend_alignment": 20, "c2_ob_proximity": 10}}
    result = _splice_score_breakdown(analysis, strategies)
    assert result["header"]["score_breakdown"] == strategies["confluence_breakdown"]
    assert result is analysis  # mutated in place, not copied


def test_noop_on_error_response():
    analysis = {"error": "Gemini call failed: no keys available"}
    strategies = {"confluence_breakdown": {"c1_trend_alignment": 20}}
    result = _splice_score_breakdown(analysis, strategies)
    assert result == {"error": "Gemini call failed: no keys available"}
    assert "header" not in result


# ---------------- mtf_context splice ---------------- #

def _mini_snapshot():
    return {
        "live_price": 100.0,
        "generated_at": "2026-07-11T12:00:00Z",
        "strategies": {"confluence_breakdown": {"c1_trend_alignment": 20}},
        "smc_context": {
            "1d": {"current_price": 100.0, "structure": "LH/LL", "bos": None,
                   "order_blocks": {"bullish": [], "bearish": []},
                   "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                   "premium_discount": {"zone": "premium", "in_ote": False}},
            "4h": {"current_price": 100.0, "structure": "HH/HL",
                   "bos": {"level": 99.0, "direction": "BULLISH", "bars_ago": 3},
                   "order_blocks": {"bullish": [{"top": 98.0, "bottom": 97.0}], "bearish": []},
                   "fvg": {"nearest_bullish_fvg": {"top": 96.0, "bottom": 95.0},
                           "nearest_bearish_fvg": None},
                   "premium_discount": {"zone": "discount", "in_ote": True}},
            "1h": {"current_price": 100.0, "structure": "HH/HL", "bos": None,
                   "order_blocks": {"bullish": [], "bearish": []},
                   "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                   "premium_discount": {"zone": "neutral", "in_ote": False}},
            "15m": {"current_price": 100.0, "structure": "HH/LL", "bos": None,
                    "order_blocks": {"bullish": [], "bearish": []},
                    "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                    "premium_discount": {"zone": "neutral", "in_ote": False}},
        },
        "windowed_indicators": {
            "1d": {"cvd_window_delta": 5.0, "window_price_change_pct": 1.0},
            "4h": {"cvd_window_delta": 4.0, "window_price_change_pct": 0.5, "adx": 30.0},
            "1h": {"cvd_window_delta": -3.0, "window_price_change_pct": -0.1, "adx": 18.0,
                   "rsi": 55.0, "rsi_series_last_5": [50.0, 51.0, 52.0, 54.0, 55.0],
                   "stochrsi_k": 70.0},
            "15m": {"cvd_window_delta": -1.0, "window_price_change_pct": 0.2,
                    "relative_volume": 1.4},
        },
        "price_action": {"last_candle_pattern_15m": "hammer"},
    }


def test_finalize_analysis_builds_mtf_table_and_keeps_llm_judgments():
    from main import _finalize_analysis
    analysis = {"header": {"score": 50},
                "mtf_context": {"cross_tf_momentum": "mixed", "htf_draw": "PWH 110 — draw above"}}
    result = _finalize_analysis(analysis, _mini_snapshot())
    mtf = result["mtf_context"]
    assert mtf["cross_tf_momentum"] == "mixed"
    assert mtf["d1"]["htf_draw"] == "PWH 110 — draw above"
    assert mtf["h4"]["adx_tier"] == "MEDIUM"           # 30 -> C1 medium tier
    assert mtf["h4"]["bos"] == "BULLISH BOS @ 99.0 (3 bars ago)"
    assert mtf["h4"]["nearest_ob"] == "97.0-98.0"
    assert mtf["h4"]["nearest_fvg"] == "95.0-96.0"
    assert mtf["h1"]["rsi_slope"] == 1.0               # 55.0 - 54.0
    assert mtf["m15"]["candle_pattern"] == "hammer"
    assert mtf["d1"]["nearest_ob"] == "NONE"
    assert result["header"]["score_breakdown"] == {"c1_trend_alignment": 20}


def test_finalize_analysis_noop_on_error_response():
    from main import _finalize_analysis
    analysis = {"error": "Gemini call failed"}
    assert _finalize_analysis(analysis, _mini_snapshot()) == {"error": "Gemini call failed"}


# ---------------- _build_llm_view trims ---------------- #

def test_llm_view_drops_duplicates_and_converts_timestamps():
    from main import _build_llm_view
    snap = _mini_snapshot()
    snap.update({
        "market_regime": "Ranging / Sideways",
        "raw_regime": "Ranging / Sideways",
        "previous_day": {"pdh": 1, "pdl": 2, "pdc": 3},
        "atr_context": {"atr_15m": 0.1, "note": "constant sentence"},
        "volatility_regime": {"4h": {"regime": "normal", "atr_percentile": 0.5, "bb_width": 0.03}},
        "engine_trade": {"valid": False},
    })
    snap["smc_context"]["15m"]["order_blocks"]["bullish"] = [
        {"top": 99.5, "bottom": 99.0, "timestamp": "2026-07-11T11:00:00"}]
    snap["windowed_indicators"]["15m"].update(
        {"session_relative_volume": 0.7, "kill_zone": "OFF", "hour_utc": 10, "slot_samples": 15})
    snap["price_action"]["untested_pocs_4h"] = [
        {"poc": 95.123450000000001, "session_end_ts": "2026-07-10T12:00:00", "distance_pct": 5.0}]

    view = _build_llm_view(snap)
    assert "previous_day" not in view
    assert "raw_regime" not in view                      # same as market_regime
    assert "note" not in view["atr_context"]
    assert "atr_percentile" not in view["volatility_regime"]["4h"]
    assert "engine_trade" not in view
    ob = view["smc_context"]["15m"]["order_blocks"]["bullish"][0]
    assert "timestamp" not in ob and isinstance(ob["bars_ago"], int)
    w15 = view["windowed_indicators"]["15m"]
    assert all(k not in w15 for k in
               ("session_relative_volume", "kill_zone", "hour_utc", "slot_samples"))
    poc = view["price_action"]["untested_pocs_4h"][0]
    assert "session_end_ts" not in poc and isinstance(poc["age_hours"], int)
    assert poc["poc"] == 95.12345                        # float-noise rounding


def test_llm_view_keeps_raw_regime_when_hysteresis_diverges():
    from main import _build_llm_view
    snap = _mini_snapshot()
    snap.update({"market_regime": "Ranging / Sideways", "raw_regime": "Trending Bullish"})
    assert _build_llm_view(snap)["raw_regime"] == "Trending Bullish"


# ---------------- FA-01: post-call / replay-only fields never reach the prompt ---------------- #

def test_llm_view_strips_post_call_and_calibration_fields():
    from main import _build_llm_view
    snap = _mini_snapshot()
    snap.update({
        "chart_series": {"15m": [[1, 2, 3, 4, 5]]},          # monitor attaches pre-narration
        "analysis": {"header": {"score": 70}},                # saved narrated snapshot (ab_thinking)
        "outcome": {"engine_trade_outcome": {"outcome": "tp_first"}},  # labelled snapshot
    })
    snap["strategies"]["confluence_breakdown_raw"] = {"c1_trend_alignment": 25}
    view = _build_llm_view(snap)
    assert "chart_series" not in view
    assert "analysis" not in view
    assert "outcome" not in view
    assert "confluence_breakdown_raw" not in view["strategies"]


# ---------------- FA-05: OI failure path must not fabricate data ---------------- #

def test_fetch_open_interest_failure_returns_nulls_and_skips_history(monkeypatch):
    import main

    def _boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(main.requests, "get", _boom)
    saved = []
    monkeypatch.setattr(main, "_save_oi_history", lambda hist: saved.append(hist))
    out = main.fetch_open_interest("SOL/USDT")
    assert out == {"value": None, "change_1h_pct": None,
                   "change_4h_pct": None, "percentile_20d": None}
    assert saved == []  # no {"oi": 0.0} row poisons the local percentile history


# ---------------- FA-08: BTC.D proxy computed on closed 4H bars only ---------------- #

def test_btcdom_proxy_drops_forming_candle(monkeypatch):
    import time
    import pytest
    import main

    tf_ms = 4 * 3600 * 1000
    now_ms = int(time.time() * 1000)
    forming_open = now_ms - (now_ms % tf_ms)
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108]  # closed bars, oldest first
    klines = [[forming_open - (len(closes) - i) * tf_ms, "0", "0", "0", str(c), "0"]
              for i, c in enumerate(closes)]
    klines.append([forming_open, "0", "0", "0", "999", "0"])  # forming bar — must be ignored

    class _Resp:
        def json(self):
            return klines

    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _Resp())
    out = main.fetch_btc_dominance_proxy("SOL/USDT")
    assert out["btcdom_close"] == 108                          # last CLOSED close, not 999
    assert out["change_4h_pct"] == pytest.approx((108 - 107) / 107 * 100, abs=1e-3)
    assert out["change_24h_pct"] == pytest.approx((108 - 102) / 102 * 100, abs=1e-3)


# ---------------- FA-03: concurrent OI-history writes must not lose updates ---------------- #

def test_fetch_open_interest_concurrent_threads_no_lost_updates(monkeypatch, tmp_path):
    import threading
    import json as _json
    import main

    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_get(url, params=None, timeout=None):
        if "openInterestHist" in url:
            return _Resp([])
        return _Resp({"openInterest": "123.45"})

    hist_path = str(tmp_path / "oi_history.json")
    monkeypatch.setattr(main, "OI_HISTORY_PATH", hist_path)
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main.requests, "get", fake_get)

    n_threads, calls_per_thread = 8, 15
    symbol = "TEST/USDT"

    def worker():
        for _ in range(calls_per_thread):
            main.fetch_open_interest(symbol)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(hist_path) as f:
        hist = _json.load(f)  # raises if the file is ever left truncated/corrupt
    assert len(hist[symbol]) == n_threads * calls_per_thread


def test_write_json_file_atomic_no_leftover_tmp(tmp_path):
    import os
    import json as _json
    from main import _write_json_file

    path = str(tmp_path / "state.json")
    _write_json_file(path, {"a": 1})
    _write_json_file(path, {"a": 2})  # second write exercises the replace path
    assert not os.path.exists(path + ".tmp")
    with open(path) as f:
        assert _json.load(f) == {"a": 2}


# ---------------- FA-11: health report flags a placeholder/stale/empty calendar ---------------- #

def test_calendar_health_states(monkeypatch, tmp_path):
    import json as _json
    import time
    import main

    events_path = tmp_path / "events.json"
    monkeypatch.setattr(main, "EVENTS_PATH", str(events_path))

    events_path.write_text(_json.dumps([
        {"datetime": "2026-12-31T00:00:00", "name": "EXAMPLE — CPI", "impact": "high"}]))
    assert main._calendar_health().startswith("PLACEHOLDER")

    events_path.write_text(_json.dumps([]))
    assert main._calendar_health().startswith("EMPTY")

    far_future = "2099-01-01T00:00:00"
    events_path.write_text(_json.dumps([{"datetime": far_future, "name": "CPI", "impact": "high"}]))
    assert main._calendar_health().startswith("STALE")

    soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 3 * 86400))
    events_path.write_text(_json.dumps([{"datetime": soon, "name": "CPI", "impact": "high"}]))
    assert main._calendar_health() == "OK"


# ---------------- plain-json fallback skeleton ---------------- #

def test_schema_skeleton_covers_all_top_level_fields():
    from main import _schema_skeleton, GEMINI_RESPONSE_SCHEMA
    skeleton = _schema_skeleton(GEMINI_RESPONSE_SCHEMA)
    assert set(skeleton) == set(GEMINI_RESPONSE_SCHEMA["properties"])
    assert skeleton["header"]["action"] == "ACTIVE_TRADE|CONDITIONAL_ENTRY|HOLD"
    assert skeleton["trade_decision"]["entry"] == "<number>|null"
    assert set(skeleton["mtf_context"]) == {"cross_tf_momentum", "htf_draw"}
