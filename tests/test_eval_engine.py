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
                                              "mfe_pct": 1.2, "mae_pct": -0.3, "realized_rr": 2.1}},
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
                                      "mfe_pct": 2.0, "mae_pct": -0.5, "realized_rr": 2.5},
            "trade_decision_outcome": {"triggered": True, "outcome": "sl_first",
                                        "mfe_pct": 0.5, "mae_pct": -1.0, "realized_rr": None},
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
