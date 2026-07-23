"""
FA-06: tools.eval_engine must treat engine_trade_outcome as the primary
universe (the ~99% no-LLM collector corpus) and keep trade_decision_outcome
as a secondary, LLM-only cut, reporting both universes' counts explicitly.
"""

import io
import json
import contextlib

import tools.eval_engine as ee


def _write(dir_, name, obj):
    with open(dir_ / name, "w") as f:
        json.dump(obj, f)


def _run(monkeypatch, dir_):
    class Args:
        symbol = None
        out = None

    class FakeParser:
        def add_argument(self, *a, **k):
            pass

        def parse_args(self):
            return Args()

    monkeypatch.setattr(ee, "SNAPSHOTS_DIR", str(dir_))
    monkeypatch.setattr(ee.argparse, "ArgumentParser", lambda *a, **k: FakeParser())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ee.main()
    return json.loads(buf.getvalue())


def test_engine_only_narrated_and_unlabelled_snapshots(monkeypatch, tmp_path):
    # 1. engine-only: no-LLM cheap-pass snapshot, no `analysis` block at all.
    _write(tmp_path, "snapshot_BTC_USDT_20260101_000000.json", {
        "symbol": "BTC/USDT",
        "strategies": {"confluence_score": 62, "score_mode": "trending",
                        "trend_bias": "bullish", "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "normal"}},
        "event_guard": {"active": False},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first",
                                              "mfe_pct": 1.2, "mae_pct": -0.3, "mfe_r": 2.1}},
    })

    # 2. narrated: carries both engine_trade_outcome and trade_decision_outcome.
    _write(tmp_path, "snapshot_ETH_USDT_20260101_000000.json", {
        "symbol": "ETH/USDT",
        "strategies": {"confluence_score": 70, "score_mode": "ranging",
                        "trend_bias": "bearish", "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "high"}},
        "event_guard": {"active": False},
        "analysis": {"header": {"score": 70, "bias": "BEARISH", "volume_gate": "CLEAR",
                                 "action": "ACTIVE_TRADE"},
                     "trade_decision": {"trade_management": {"selected_from_engine": True}}},
        "outcome": {
            "engine_trade_outcome": {"triggered": True, "outcome": "tp_first",
                                      "mfe_pct": 2.0, "mae_pct": -0.5, "mfe_r": 2.5},
            "trade_decision_outcome": {"triggered": True, "outcome": "sl_first",
                                        "mfe_pct": 0.5, "mae_pct": -1.0, "mfe_r": None},
        },
    })

    # 3. unlabelled: no `outcome` block at all -- must be scanned but not counted.
    _write(tmp_path, "snapshot_SOL_USDT_20260101_000000.json", {
        "symbol": "SOL/USDT", "strategies": {"confluence_score": 40},
    })

    report = _run(monkeypatch, tmp_path)

    assert report["snapshots_scanned"] == 3
    assert report["engine_universe_labelled"] == 2
    assert report["llm_universe_labelled"] == 1
    assert report["overall"]["snapshots"] == 2               # engine-primary universe
    assert set(report["by_mode"]) == {"trending", "ranging"}  # engine universe, both snaps
    assert report["by_action"]["ACTIVE_TRADE"]["snapshots"] == 1   # LLM cut, narrated only
    assert report["by_selected_from_engine"]["engine_primary"]["snapshots"] == 1


def test_persisting_setup_collapses_to_one_episode(monkeypatch, tmp_path):
    """Pseudo-replication fix: a BTC setup scored every 15m for 2h (same entry/SL/TP each
    cycle) must count as ONE trade in the engine-primary universe, not 8."""
    for i in range(8):
        hh, mm = divmod(i * 15, 60)
        _write(tmp_path, f"snapshot_BTC_USDT_20260101_{hh:02d}{mm:02d}00.json", {
            "symbol": "BTC/USDT",
            "generated_at": f"2026-01-01T{hh:02d}:{mm:02d}:00Z",
            "engine_trade": {"direction": "BUY", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
            "strategies": {"confluence_score": 65, "score_mode": "trending", "trend_bias": "bullish",
                            "volume_gate": {"state": "CLEAR"}},
            "volatility_regime": {"15m": {"regime": "normal"}},
            "event_guard": {"active": False},
            "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first",
                                                  "mfe_pct": 3.0, "mae_pct": -0.2, "mfe_r": 2.0}},
        })
    # a genuinely distinct SOL setup, one snapshot only
    _write(tmp_path, "snapshot_SOL_USDT_20260101_000000.json", {
        "symbol": "SOL/USDT",
        "generated_at": "2026-01-01T00:00:00Z",
        "engine_trade": {"direction": "SELL", "entry": 80.0, "stop_loss": 84.0, "take_profit": 72.0},
        "strategies": {"confluence_score": 60, "score_mode": "trending", "trend_bias": "bearish",
                        "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "normal"}},
        "event_guard": {"active": False},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "sl_first",
                                              "mfe_pct": 0.1, "mae_pct": -1.0, "mfe_r": None}},
    })

    report = _run(monkeypatch, tmp_path)

    assert report["snapshots_scanned"] == 9
    assert report["engine_universe_raw_rows"] == 9      # pre-collapse count
    assert report["engine_universe_labelled"] == 2       # 1 BTC episode + 1 SOL episode
    assert report["overall"]["snapshots"] == 2
    assert report["overall"]["tp_first"] == 1             # the BTC episode's single outcome
    assert report["overall"]["sl_first"] == 1


# ---------------- B15.0: realized-R economics ---------------- #

def test_realized_r_economics():
    c = ee.ROUND_TRIP_COST_R
    assert ee._realized_r({"outcome": "tp_first"}, 2.0) == 2.0 - c
    assert ee._realized_r({"outcome": "sl_first"}, 2.0) == -1.0 - c
    assert ee._realized_r({"outcome": "timeout", "end_rr": 0.5}, 2.0) == 0.5 - c
    assert ee._realized_r({"outcome": "intrabar_unknown"}, 2.0) is None   # never counted
    assert ee._realized_r({"outcome": "tp_first"}, None) is None          # no target -> excluded


def test_engine_universe_ignores_header_and_buckets_by_setup_direction(monkeypatch, tmp_path):
    """B15.0: the engine bucket must read strategies.* (score 50, MR fade SELL), never the
    LLM header (score 90/BULLISH/HARD_GATE). And direction buckets by setup_direction."""
    _write(tmp_path, "snapshot_BTC_USDT_20260101_000000.json", {
        "symbol": "BTC/USDT",
        "engine_trade": {"rr": 2.0, "direction": "SELL"},
        "strategies": {"confluence_score": 50, "score_mode": "ranging", "trend_bias": "bullish",
                        "setup_direction": "bearish", "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "normal"}},
        "event_guard": {"active": False},
        "analysis": {"header": {"score": 90, "bias": "BULLISH", "volume_gate": "HARD_GATE"}},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first",
                                             "mfe_pct": 2.0, "mae_pct": -0.3, "mfe_r": 2.0, "end_rr": 1.8}},
    })
    report = _run(monkeypatch, tmp_path)
    assert "45-59" in report["by_score"] and "75+" not in report["by_score"]   # strategies, not header
    assert "BEARISH" in report["by_bias"]                                       # setup_direction, not trend_bias
    assert report["by_volume_gate"]["CLEAR"]["snapshots"] == 1                  # strategies gate, not HARD_GATE
    assert report["overall"]["expectancy_r"] == round(2.0 - ee.ROUND_TRIP_COST_R, 3)  # realized R, not excursion


# ---------------- I1.0 by_verdict cut ---------------- #

def _narrated(verdict, engine_outcome, llm_outcome=None, engine_rr=2.0, plan_ratio=2.0):
    td = {"trade_management": {"selected_from_engine": verdict == "AGREE"},
          "rr": {"ratio": plan_ratio}}
    outcome = {"engine_trade_outcome": engine_outcome}
    if llm_outcome is not None:
        outcome["trade_decision_outcome"] = llm_outcome
    return {
        "symbol": "BTC/USDT",
        "engine_trade": {"rr": engine_rr, "direction": "BUY"},
        "strategies": {"confluence_score": 70, "score_mode": "trending", "trend_bias": "bullish",
                       "setup_direction": "bullish", "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "normal"}},
        "event_guard": {"active": False},
        "analysis": {"verdict": verdict, "header": {"action": "ACTIVE_TRADE"},
                     "trade_decision": td},
        "outcome": outcome,
    }


def test_by_verdict_pairs_update_against_engine_primary(monkeypatch, tmp_path):
    c = ee.ROUND_TRIP_COST_R
    # UPDATE: the adjudicated plan won (tp_first) where the engine primary lost (sl_first).
    _write(tmp_path, "snapshot_BTC_USDT_20260101_000000.json",
           _narrated("UPDATE",
                     engine_outcome={"triggered": True, "outcome": "sl_first", "mfe_r": 0.3},
                     llm_outcome={"triggered": True, "outcome": "tp_first", "mfe_r": 2.4}))
    # AGREE: both are the same trade -> identical outcome, delta 0.
    _write(tmp_path, "snapshot_BTC_USDT_20260101_001500.json",
           _narrated("AGREE",
                     engine_outcome={"triggered": True, "outcome": "tp_first", "mfe_r": 2.2},
                     llm_outcome={"triggered": True, "outcome": "tp_first", "mfe_r": 2.2}))
    # REJECT: declined; the engine primary would have lost -> good rejection.
    _write(tmp_path, "snapshot_BTC_USDT_20260101_003000.json",
           _narrated("REJECT",
                     engine_outcome={"triggered": True, "outcome": "sl_first", "mfe_r": 0.1}))

    report = _run(monkeypatch, tmp_path)
    bv = report["by_verdict"]

    # UPDATE: llm +2.0R vs engine -1.0R (each net of cost) -> positive paired delta.
    assert bv["UPDATE"]["decisions"] == 1
    assert bv["UPDATE"]["llm"]["expectancy_r"] == round(2.0 - c, 3)
    assert bv["UPDATE"]["engine_primary"]["expectancy_r"] == round(-1.0 - c, 3)
    assert bv["UPDATE"]["paired_delta_r"] == round((2.0 - c) - (-1.0 - c), 3)   # == 3.0

    assert bv["AGREE"]["paired_delta_r"] == 0.0

    # REJECT reports the avoided trade's outcome (a loss -> rejection was correct).
    assert bv["REJECT"]["decisions"] == 1
    assert bv["REJECT"]["engine_primary_if_taken"]["expectancy_r"] == round(-1.0 - c, 3)
    assert "llm" not in bv["REJECT"]


def test_by_verdict_absent_when_no_narration(monkeypatch, tmp_path):
    _write(tmp_path, "snapshot_BTC_USDT_20260101_000000.json", {
        "symbol": "BTC/USDT",
        "strategies": {"confluence_score": 62, "score_mode": "trending", "trend_bias": "bullish",
                       "volume_gate": {"state": "CLEAR"}},
        "volatility_regime": {"15m": {"regime": "normal"}}, "event_guard": {"active": False},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first", "mfe_r": 2.0}},
    })
    report = _run(monkeypatch, tmp_path)
    assert report["by_verdict"] == {}
