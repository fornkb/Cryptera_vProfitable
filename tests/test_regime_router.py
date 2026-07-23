"""
I3.0b: the 4-state regime router (trend / balanced range / transition / untradeable),
its stand-down-immediately hysteresis, and B8.0's counter-CHoCH veto + displacement
consumption. Directional components are asserted on both mirrored sides.
"""

import pandas as pd
import pytest

import strategies
from indicators import (compute_regime_with_hysteresis, detect_regime, is_tradable_bucket,
                        regime_bucket)
from strategies import _c1_trend, _counter_choch, _m6_range_confirmed, evaluate_strategies


def _adx_df(adx):
    return pd.DataFrame({"close": [100.0, 100.0], "ADX": [adx, adx]})


def _ctx(trend=None, state="trending", *, fresh_bos=False, fresh_choch=False,
         contained=True, choch_dir="BULLISH", displaced=False, bos_ago=None):
    return {
        "structure": {"bullish": "HH/HL", "bearish": "LH/LL"}.get(trend, "NEUTRAL"),
        "structure_state": {"state": state, "trend": trend},
        "bos": {"level": 100.0 if fresh_bos else None, "direction": "BULLISH",
                "fresh": fresh_bos, "displacement": displaced, "bars_ago": bos_ago},
        "choch": {"detected": fresh_choch, "direction": choch_dir, "fresh": fresh_choch,
                  "level": 99.0, "bars_ago": 2},
        "dealing_range": {"brackets_price": contained},
    }


def _smc(c4, c1=None):
    return {"4h": c4, "1h": c1 if c1 is not None else _ctx(state="neutral")}


# ---------------- the router ---------------- #

@pytest.mark.parametrize("trend,expected", [("bullish", "Trending Bullish"),
                                            ("bearish", "Trending Bearish")])
def test_trending_4h_over_the_adx_floor_is_a_trend(trend, expected):
    regime = detect_regime(_adx_df(30), _adx_df(30), None,
                           smc_context=_smc(_ctx(trend)), volatility={"regime": "normal"})
    assert regime == expected


def test_early_trend_is_a_transition_not_a_range():
    """
    The router's core failure: 'Ranging' was assigned on FAILURE of the trend test, so a
    directional 4H structure with sub-floor ADX routed straight into the fade rubric.
    """
    regime = detect_regime(_adx_df(19), _adx_df(19), None,
                           smc_context=_smc(_ctx("bullish", fresh_bos=True, contained=False)),
                           volatility={"regime": "normal"})
    assert regime == "Transition"
    assert regime_bucket(regime) == "transition"
    assert is_tradable_bucket(regime_bucket(regime)) is False


def test_balance_must_be_earned_to_get_the_fade_rubric():
    """Quiet on both HTFs, price contained by a bracketing range, nothing breaking."""
    regime = detect_regime(_adx_df(14), _adx_df(15), None,
                           smc_context=_smc(_ctx(state="neutral")),
                           volatility={"regime": "compressed"})
    assert regime == "Ranging / Sideways"
    assert regime_bucket(regime) == "ranging"


def test_a_fresh_break_disqualifies_the_range():
    regime = detect_regime(_adx_df(14), _adx_df(15), None,
                           smc_context=_smc(_ctx(state="neutral"),
                                            _ctx(state="neutral", fresh_bos=True)),
                           volatility={"regime": "compressed"})
    assert regime == "Transition"


def test_expanding_volatility_is_not_a_fadeable_range():
    smc = _smc(_ctx(state="neutral"))
    assert detect_regime(_adx_df(14), _adx_df(15), None, smc_context=smc,
                         volatility={"regime": "expanded"}) == "Untradeable"


def test_price_outside_the_range_is_not_a_range():
    regime = detect_regime(_adx_df(14), _adx_df(15), None,
                           smc_context=_smc(_ctx(state="transition", trend="bullish",
                                                 contained=False)),
                           volatility={"regime": "normal"})
    assert regime == "Transition"


def test_degraded_path_without_structure_never_defaults_to_a_fade():
    assert detect_regime(_adx_df(12), _adx_df(12), None) == "Transition"


# ---------------- hysteresis ---------------- #

def test_standing_down_into_transition_is_immediate():
    prior = {"effective": "Trending Bullish", "candidate": None, "candidate_runs": 0}
    eff, state = compute_regime_with_hysteresis("Transition", prior, runs=2, bar_id=1)
    assert eff == "Transition"
    assert state["candidate_runs"] == 0


def test_entering_a_tradable_rubric_still_needs_confirmation():
    state = {"effective": "Transition", "candidate": None, "candidate_runs": 0}
    eff, state = compute_regime_with_hysteresis("Trending Bullish", state, runs=2, bar_id=1)
    assert eff == "Transition"                      # first run: not yet
    eff, state = compute_regime_with_hysteresis("Trending Bullish", state, runs=2, bar_id=2)
    assert eff == "Trending Bullish"                # second consecutive run: accepted


def test_same_bar_replay_stays_idempotent_across_the_new_buckets():
    state = {"effective": "Ranging / Sideways", "candidate": None,
             "candidate_runs": 0, "last_bar_id": 7}
    eff, out = compute_regime_with_hysteresis("Trending Bullish", state, runs=2, bar_id=7)
    assert eff == "Ranging / Sideways" and out["candidate_runs"] == 0


# ---------------- no-rubric routing ---------------- #

def _eval(monkeypatch, regime, smc=None):
    monkeypatch.setattr(strategies, "_CALIBRATION_CACHE", {
        "weights": {}, "mr_weights": {}, "score_to_winrate": [], "logistic": {}})
    df = pd.DataFrame({"open": [100.0] * 7, "high": [101.0] * 7,
                       "low": [99.0] * 7, "close": [100.0] * 7, "volume": [1.0] * 7})
    return evaluate_strategies(
        df_4h=None, df_1h=None, df_15m=df, orderbook=None, funding=None, sentiment=None,
        open_interest=None,
        smc_context=smc or {"4h": _ctx("bullish"), "1h": _ctx("bullish"),
                            "15m": {"order_blocks": {"bullish": [], "bearish": []},
                                    "fvg": {"nearest_bullish_fvg": None,
                                            "nearest_bearish_fvg": None},
                                    "liquidity_levels": {"buy_side": [], "sell_side": []},
                                    "dealing_range": {}}},
        windowed_indicators={}, pa_context={}, market_regime=regime)


@pytest.mark.parametrize("regime", ["Transition", "Untradeable"])
def test_no_rubric_states_score_zero_and_propose_nothing(monkeypatch, regime):
    out = _eval(monkeypatch, regime)
    assert out["score_mode"] == "no_trade"
    assert out["confluence_score"] == 0
    assert out["setup_direction"] == "neutral"
    assert out["engine_trade"]["valid"] is False


def test_no_rubric_states_keep_canonical_breakdown_keys(monkeypatch):
    out = _eval(monkeypatch, "Transition")
    assert set(out["confluence_breakdown"]) == {
        "c1_trend_alignment", "c2_ob_proximity", "c3_liquidity_sweep", "c4_momentum",
        "c5_fvg_magnet", "c6_ote_bonus", "c7_cvd_alignment", "c8_stochrsi"}
    assert set(out["confluence_breakdown"].values()) == {0}


# ---------------- B8.0: counter-CHoCH veto ---------------- #

@pytest.mark.parametrize("setup,opposing", [("bullish", "BEARISH"), ("bearish", "BULLISH")])
def test_counter_choch_is_detected_on_both_sides(setup, opposing):
    smc = {"1h": _ctx("bullish", fresh_choch=True, choch_dir=opposing), "4h": _ctx("bullish")}
    assert _counter_choch(smc, setup) != ""
    assert _counter_choch(smc, {"bullish": "bearish", "bearish": "bullish"}[setup]) == ""


def test_counter_choch_caps_the_score_below_the_conditional_threshold(monkeypatch):
    from config import CONDITIONAL_ENTRY_THRESHOLD
    smc = {"4h": _ctx("bullish"), "1h": _ctx("bullish", fresh_choch=True, choch_dir="BEARISH"),
           "15m": {"order_blocks": {"bullish": [], "bearish": []},
                   "fvg": {"nearest_bullish_fvg": None, "nearest_bearish_fvg": None},
                   "liquidity_levels": {"buy_side": [], "sell_side": []},
                   "dealing_range": {}}}
    out = _eval(monkeypatch, "Trending Bullish", smc)
    assert out["counter_choch_warning"] is True
    assert out["confluence_score"] <= CONDITIONAL_ENTRY_THRESHOLD - 1


# ---------------- B8.0: displacement is finally consumed ---------------- #

@pytest.mark.parametrize("trend,tokens", [("bullish", ("HH/HL", "BULLISH")),
                                          ("bearish", ("LH/LL", "BEARISH"))])
def test_displaced_bos_in_the_bias_direction_pays_a_bounded_bonus(trend, tokens):
    label, direction = tokens
    base_ctx = {"4h": {"structure": label}, "1h": {"structure": label,
                "bos": {"direction": direction, "displacement": False, "bars_ago": 3}}}
    disp_ctx = {"4h": {"structure": label}, "1h": {"structure": label,
                "bos": {"direction": direction, "displacement": True, "bars_ago": 3}}}
    windowed = {"4h": {"adx": 30.0}}
    plain, _ = _c1_trend(base_ctx, windowed, trend)
    boosted, note = _c1_trend(disp_ctx, windowed, trend)
    assert boosted - plain == 5
    assert "displaced" in note


def test_displaced_bos_bonus_ignores_the_wrong_direction():
    ctx = {"4h": {"structure": "HH/HL"}, "1h": {"structure": "HH/HL",
           "bos": {"direction": "BEARISH", "displacement": True, "bars_ago": 3}}}
    pts, _ = _c1_trend(ctx, {"4h": {"adx": 30.0}}, "bullish")
    assert pts == 15


def test_displaced_break_keeps_disqualifying_the_range_after_freshness_ages_out():
    stale = {"1h": {"bos": {"fresh": False, "displacement": True, "bars_ago": 20},
                    "choch": {"fresh": False}}}
    drift = {"1h": {"bos": {"fresh": False, "displacement": False, "bars_ago": 20},
                    "choch": {"fresh": False}}}
    assert _m6_range_confirmed(stale)[0] == 0
    assert _m6_range_confirmed(drift)[0] == 10
