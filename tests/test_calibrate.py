"""
tools.calibrate: _load_labelled_episodes must collapse a persisting setup's
repeated 15m snapshots into one episode before anything is fitted on it
(pseudo-replication fix, tools.episodes).
"""

import json

from pytest import approx

import config
import tools.calibrate as cal


def _write(dir_, name, obj):
    # Every real snapshot carries its schema_version, and the corpus guard
    # (tools/corpus.py, invariant 5) now excludes rows from other generations —
    # so fixtures must be stamped like the real thing or they model nothing.
    obj.setdefault("schema_version", config.SCHEMA_VERSION)
    obj.setdefault("outcome", {}).setdefault("label_version", config.LABEL_VERSION)
    with open(dir_ / name, "w") as f:
        json.dump(obj, f)


# ---------------- B4.0: timeout soft label blends excursion + terminal ---------------- #

def test_soft_label_clean_fills():
    snap = {"engine_trade": {"rr": 2.0}}
    assert cal._soft_label(snap, {"outcome": "tp_first"}) == 1.0
    assert cal._soft_label(snap, {"outcome": "sl_first"}) == 0.0


def test_soft_label_timeout_penalizes_give_back():
    """A timeout that ran 1.5R toward a 2R target but ENDED flat gave it all back — the old
    MFE-only credit scored it 0.75; the blend scores it 0.375."""
    snap = {"engine_trade": {"rr": 2.0}}
    give_back = {"outcome": "timeout", "mfe_r": 1.5, "end_rr": 0.0}
    held = {"outcome": "timeout", "mfe_r": 1.5, "end_rr": 1.5}
    assert cal._soft_label(snap, give_back) == approx(0.375)   # was 0.75 MFE-only
    assert cal._soft_label(snap, held) == approx(0.75)
    assert cal._soft_label(snap, give_back) < 0.75


def test_soft_label_timeout_missing_mfe_is_zero():
    assert cal._soft_label({"engine_trade": {"rr": 2.0}},
                           {"outcome": "timeout", "mfe_r": None, "end_rr": 1.0}) == 0.0


def test_load_labelled_episodes_collapses_persisting_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(cal, "SNAPSHOTS_DIR", str(tmp_path))

    for i in range(6):
        hh, mm = divmod(i * 15, 60)
        _write(tmp_path, f"snapshot_BTC_USDT_20260101_{hh:02d}{mm:02d}00.json", {
            "symbol": "BTC/USDT",
            "generated_at": f"2026-01-01T{hh:02d}:{mm:02d}:00Z",
            "engine_trade": {"direction": "BUY", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
            "strategies": {"confluence_score": 65, "score_mode": "trending",
                            "confluence_breakdown_raw": {"c1_trend_alignment": 20}},
            "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first", "mfe_r": 2.0}},
        })

    # an untriggered snapshot must never enter the raw set (calibrate excludes it, unlike eval_engine)
    _write(tmp_path, "snapshot_BTC_USDT_20260101_020000.json", {
        "symbol": "BTC/USDT",
        "generated_at": "2026-01-01T02:00:00Z",
        "engine_trade": {"direction": "BUY", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
        "strategies": {"confluence_score": 65, "score_mode": "trending",
                        "confluence_breakdown_raw": {"c1_trend_alignment": 20}},
        "outcome": {"engine_trade_outcome": {"triggered": False, "outcome": "not_triggered"}},
    })

    episodes, n_raw = cal._load_labelled_episodes()
    assert n_raw == 6          # only the triggered rows
    assert len(episodes) == 1  # all 6 collapse to one BTC episode
    assert episodes[0][0] == "snapshot_BTC_USDT_20260101_000000.json"  # earliest representative


def test_load_labelled_episodes_distinct_symbols_stay_distinct(monkeypatch, tmp_path):
    monkeypatch.setattr(cal, "SNAPSHOTS_DIR", str(tmp_path))

    _write(tmp_path, "snapshot_BTC_USDT_20260101_000000.json", {
        "symbol": "BTC/USDT", "generated_at": "2026-01-01T00:00:00Z",
        "engine_trade": {"direction": "BUY", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
        "strategies": {"confluence_score": 65, "score_mode": "trending"},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first", "mfe_r": 2.0}},
    })
    _write(tmp_path, "snapshot_ETH_USDT_20260101_000000.json", {
        "symbol": "ETH/USDT", "generated_at": "2026-01-01T00:00:00Z",
        "engine_trade": {"direction": "SELL", "entry": 3000.0, "stop_loss": 3100.0, "take_profit": 2800.0},
        "strategies": {"confluence_score": 60, "score_mode": "trending"},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "sl_first"}},
    })

    episodes, n_raw = cal._load_labelled_episodes()
    assert n_raw == 2
    assert len(episodes) == 2
