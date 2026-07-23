"""
I1.0: every engine candidate carries a stable, menu-scoped level_id (E*/S*/T*) so the
LLM adjudicator selects by ID rather than echoing a float. IDs follow the ranked order
the menu is emitted in and are unique within one snapshot's menu (all the resolver needs).
"""

from strategies import _assign_level_ids


def _geometry():
    return {
        "valid": True, "direction": "BUY",
        "entry": 100.0, "stop_loss": 95.0, "take_profit": 112.0,
        "candidates": {
            "entries": [{"price": 100.0, "source": "OB mid"}, {"price": 99.0, "source": "FVG"}],
            "stops": [{"price": 95.0, "source": "swing"}],
            "targets": [{"price": 108.0, "source": "POC"}, {"price": 112.0, "source": "BSL"},
                        {"price": 118.0, "source": "PWH"}],
        },
    }


def test_ids_stamped_in_ranked_order():
    g = _assign_level_ids(_geometry())
    c = g["candidates"]
    assert [e["level_id"] for e in c["entries"]] == ["E1", "E2"]
    assert [s["level_id"] for s in c["stops"]] == ["S1"]
    assert [t["level_id"] for t in c["targets"]] == ["T1", "T2", "T3"]


def test_ids_unique_within_menu():
    g = _assign_level_ids(_geometry())
    ids = [row["level_id"] for kind in ("entries", "stops", "targets")
           for row in g["candidates"][kind]]
    assert len(ids) == len(set(ids))


def test_ids_stable_for_same_menu():
    a = _assign_level_ids(_geometry())["candidates"]["targets"]
    b = _assign_level_ids(_geometry())["candidates"]["targets"]
    assert [t["level_id"] for t in a] == [t["level_id"] for t in b]


def test_no_candidates_is_safe():
    g = _assign_level_ids({"valid": False, "candidates": {"entries": [], "stops": [], "targets": []}})
    assert g["candidates"]["entries"] == []


def test_compute_trade_geometry_stamps_ids():
    # Integration: a real geometry build carries ids on whatever candidates it emits.
    from strategies import compute_trade_geometry
    smc = {
        "15m": {"order_blocks": {"bullish": [{"top": 99.5, "bottom": 99.0}], "bearish": []},
                "fvg": {}, "dealing_range": {"low": 96.0, "high": 104.0},
                "liquidity_levels": {"buy_side": [106.0, 110.0], "sell_side": [94.0]}},
        "1h": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
        "1d": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }
    pa = {"value_area_1h": {"vah": 108.0, "val": 92.0, "poc": 100.0}, "untested_pocs_4h": [],
          "untested_pocs_1d": [], "avwap": {}}
    g = compute_trade_geometry("bullish", 100.0, smc, pa, atr_abs=1.0, regime="Trending Bullish",
                               score_mode="trend", atr_1h=2.0)
    if g["valid"]:
        for kind in ("entries", "stops", "targets"):
            for row in g["candidates"][kind]:
                assert "level_id" in row and isinstance(row["level_id"], str)
