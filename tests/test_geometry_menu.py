"""
I7.0 / B11.0 / B19.0: candidate-first geometry. The primary entry/stop/target must be
members of the menus the adjudicator selects from, the live price must not masquerade as
a structural level, levels are tick-quantized, and the menus carry the classic intraday
draws the score already reasons about.
"""

import pytest

from strategies import _tf_of_source, _tick_decimals, compute_trade_geometry

PA = {
    "value_area_1h": {"vah": 104.0, "val": 96.0, "poc": 100.0},
    "pdh": 106.5, "pdl": 93.5, "pwh": 112.0, "pwl": 88.0,
    "sr_levels": {"resistance": [{"price": 108.0, "touches": 3, "high_confidence": True}],
                  "support": [{"price": 92.0, "touches": 3, "high_confidence": True}]},
    "untested_pocs_4h": [], "untested_pocs_1d": [], "avwap": {},
}


def _ctx(direction="bullish"):
    ob = {"top": 99.3, "bottom": 98.7, "high_volume_node": False}
    return {
        "15m": {
            "order_blocks": {"bullish": [ob], "bearish": [{"top": 102.5, "bottom": 101.5}]},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"buy_side": [107.0, 110.0], "sell_side": [94.0, 91.0],
                                 "swing_levels": {"lows": [95.5], "highs": [104.5]}},
            "dealing_range": {"low": 90.0, "high": 112.0},
            "structure_state": {"protected_low": 95.0, "protected_high": 105.0},
        },
        "1h": {
            "order_blocks": {"bullish": [{"top": 97.0, "bottom": 96.0}], "bearish": []},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"buy_side": [109.0], "sell_side": [92.5]},
            "structure_state": {"protected_low": 93.0, "protected_high": 107.0},
        },
        "1d": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }


def _geo(direction="bullish", **kw):
    kwargs = dict(price=100.0, smc_context=_ctx(direction), pa_context=PA, atr_abs=1.0,
                  regime="Trending Bullish", score_mode="trend", atr_1h=8.0)
    kwargs.update(kw)
    return compute_trade_geometry(direction, **kwargs)


# ---------------- menu membership is an invariant, not a coincidence ---------------- #

@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_primary_levels_are_menu_members(direction):
    g = _geo(direction)
    assert g["valid"] is True
    for key, bucket in (("entry", "entries"), ("stop_loss", "stops"),
                        ("take_profit", "targets")):
        assert g[key] in [c["price"] for c in g["candidates"][bucket]], key


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_the_primary_rr_is_the_menus_rr_for_the_same_level(direction):
    """
    B11.0: the primary was picked from raw prices and the menu from merged ones, so the
    engine could quote rr 2.06 for a level the menu listed at 1.87 — or omit it entirely.
    """
    g = _geo(direction)
    row = next(c for c in g["candidates"]["targets"] if c["price"] == g["take_profit"])
    assert row["rr"] == g["rr"]


def test_every_candidate_keeps_its_level_id():
    """Wave 2 contract: trade_validation resolves the model's chosen id to a price."""
    g = _geo()
    for bucket, tag in (("entries", "E"), ("stops", "S"), ("targets", "T")):
        ids = [c["level_id"] for c in g["candidates"][bucket]]
        assert ids == [f"{tag}{i}" for i in range(1, len(ids) + 1)]


# ---------------- the live price is not a structural level ---------------- #

def test_current_price_is_tagged_live_and_outranked():
    assert _tf_of_source("current price") == "live"
    assert _tf_of_source("some unknown source") is None
    g = _geo()
    live = [c for c in g["candidates"]["entries"] if c["tf"] == "live"]
    assert live, "the market entry should still be offered"
    assert all(c["tf"] != "1h" or "OB" in c["source"] or "FVG" in c["source"]
               for c in live) is True


def test_a_structural_pullback_entry_beats_the_market_entry():
    g = _geo("bullish")
    row = next(c for c in g["candidates"]["entries"] if c["price"] == g["entry"])
    assert row["tf"] != "live"


# ---------------- quantization ---------------- #

@pytest.mark.parametrize("price,dp", [(65000.0, 1), (1800.0, 2), (140.0, 3), (2.5, 4), (0.04, 6)])
def test_tick_grid_scales_with_price_magnitude(price, dp):
    assert _tick_decimals(price) == dp


def test_emitted_levels_are_quantized():
    g = _geo()
    for bucket in ("entries", "stops", "targets"):
        for c in g["candidates"][bucket]:
            assert c["price"] == round(c["price"], _tick_decimals(100.0))


# The fixture above uses round numbers (104.0, 107.0), so it cannot tell a quantized
# menu from an unquantized one. This one is built from off-grid inputs at a price
# magnitude with a COARSER grid, which is what exposed targets still emitting raw
# 6dp floats (`take_profit` 1878.246813) after entries and stops were fixed.
_OFF_GRID_PA = {
    "value_area_1h": {"vah": 1840.123456, "val": 1760.987654, "poc": 1800.0},
    "pdh": 1856.234567, "pdl": 1735.876543, "pwh": 1912.345678, "pwl": 1688.135791,
    "sr_levels": {"resistance": [{"price": 1878.246813, "touches": 3, "high_confidence": True}],
                  "support": [{"price": 1722.864213, "touches": 3, "high_confidence": True}]},
    "untested_pocs_4h": [], "untested_pocs_1d": [], "avwap": {},
}


def _off_grid_ctx():
    return {
        "15m": {
            "order_blocks": {"bullish": [{"top": 1793.456789, "bottom": 1787.123456,
                                          "high_volume_node": False}],
                             "bearish": [{"top": 1812.345678, "bottom": 1806.987654}]},
            "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
            "liquidity_levels": {"buy_side": [1871.357913, 1900.246802],
                                 "sell_side": [1740.135792, 1710.864197],
                                 "swing_levels": {"lows": [1755.864209], "highs": [1845.913578]}},
            "dealing_range": {"low": 1700.0, "high": 1920.0},
            "structure_state": {"protected_low": 1750.0, "protected_high": 1850.0},
        },
        "1h": {"order_blocks": {"bullish": [], "bearish": []},
               "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
               "liquidity_levels": {"buy_side": [1889.024680], "sell_side": [1725.135790]}},
        "1d": {"liquidity_levels": {"buy_side": [], "sell_side": []}},
    }


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_off_grid_inputs_are_still_quantized_in_every_menu(direction):
    g = compute_trade_geometry(direction, price=1800.0, smc_context=_off_grid_ctx(),
                               pa_context=_OFF_GRID_PA, atr_abs=15.0,
                               regime="Trending Bullish", score_mode="trend", atr_1h=40.0)
    assert g["valid"] is True
    dp = _tick_decimals(1800.0)
    assert dp == 2
    for bucket in ("entries", "stops", "targets"):
        off = [c["price"] for c in g["candidates"][bucket]
               if c["price"] != round(c["price"], dp)]
        assert not off, f"{bucket} emitted off-grid prices: {off[:3]}"
    for key in ("entry", "stop_loss", "take_profit"):
        assert g[key] == round(g[key], dp), f"primary {key} is off-grid: {g[key]}"


# ---------------- B19.0: the menu can execute what the score reasons about ---------------- #

def test_menu_carries_the_classic_intraday_draws():
    sources = " ".join(c["source"] + " " + " ".join(c["confluence"])
                       for c in _geo("bullish")["candidates"]["targets"])
    assert "PDH" in sources
    assert "resistance zone" in sources


def test_short_menu_mirrors_the_draws():
    sources = " ".join(c["source"] + " " + " ".join(c["confluence"])
                       for c in _geo("bearish")["candidates"]["targets"])
    assert "PDL" in sources
    assert "support zone" in sources


def test_1h_order_blocks_are_entry_candidates():
    """B19.0: C2 scores 1H OB proximity, so the menu has to be able to execute one."""
    sources = " ".join(c["source"] + " " + " ".join(c["confluence"])
                       for c in _geo("bullish")["candidates"]["entries"])
    assert "1h" in sources


def test_mean_reversion_menu_reaches_past_the_opposite_edge():
    g = compute_trade_geometry("bearish", price=103.5, smc_context=_ctx(), pa_context=PA,
                               atr_abs=1.0, regime="Ranging / Sideways",
                               score_mode="mean_revert", mr_edge=104.0, atr_1h=8.0,
                               mr_raid_extreme=105.2)
    assert g["valid"] is True
    sources = " ".join(c["source"] for c in g["candidates"]["targets"])
    assert "POC" in sources and "VAL" in sources
    # the fade's invalidation is the raid extreme, not a fixed buffer off the edge
    assert "raid extreme" in g["stop_loss_source"]
    assert g["stop_loss"] == pytest.approx(105.7)
