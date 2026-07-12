"""
tools.episodes -- trade-episode identity + collapsing (pseudo-replication fix).
Ported from the sibling v4.x engine's equivalent test coverage (tests/unit_labeller.py).
"""

import tools.episodes as E


def _snap(sym, side, e, s, t, gen):
    return {"symbol": sym, "generated_at": gen,
            "engine_trade": {"direction": side, "entry": e, "stop_loss": s, "take_profit": t}}


def test_episode_id_same_geometry_same_id():
    a = _snap("BTC/USDT", "long", 100.4, 95.2, 110.6, "2026-01-01T00:00:00Z")
    b = _snap("BTC/USDT", "long", 100.3, 95.24, 110.8, "2026-01-01T00:15:00Z")  # within 3-sig-fig
    assert E.episode_id(a) == E.episode_id(b)


def test_episode_id_opposite_side_differs():
    a = _snap("BTC/USDT", "long", 100.4, 95.2, 110.6, "2026-01-01T00:00:00Z")
    c = _snap("BTC/USDT", "short", 100.4, 95.2, 110.6, "2026-01-01T00:00:00Z")
    assert E.episode_id(a) != E.episode_id(c)


def test_episode_id_other_symbol_differs():
    a = _snap("BTC/USDT", "long", 100.4, 95.2, 110.6, "2026-01-01T00:00:00Z")
    d = _snap("ETH/USDT", "long", 100.4, 95.2, 110.6, "2026-01-01T00:00:00Z")
    assert E.episode_id(a) != E.episode_id(d)


def test_episode_id_v3_direction_values():
    """v3's engine_trade.direction is 'BUY'/'SELL' (strategies.py), not 'long'/'short'."""
    buy = _snap("SOL/USDT", "BUY", 80.0, 78.0, 84.0, "2026-01-01T00:00:00Z")
    sell = _snap("SOL/USDT", "SELL", 80.0, 78.0, 84.0, "2026-01-01T00:00:00Z")
    assert E.episode_id(buy) != E.episode_id(sell)
    buy2 = _snap("SOL/USDT", "buy", 80.0, 78.0, 84.0, "2026-01-01T00:15:00Z")
    assert E.episode_id(buy) == E.episode_id(buy2)


def test_collapse_contiguous_run_is_one_episode():
    g = "2026-01-01T%02d:00:00Z"
    run = [(f"f{i}", _snap("BTC/USDT", "long", 100.0, 95.0, 110.0, g % h), {})
           for i, h in enumerate((0, 1, 2))]
    reps = E.collapse_to_episodes(run)
    assert len(reps) == 1
    assert reps[0][0] == "f0"  # earliest snapshot is the representative


def test_collapse_gap_over_24h_splits_episode():
    g = "2026-01-01T%02d:00:00Z"
    run = [(f"f{i}", _snap("BTC/USDT", "long", 100.0, 95.0, 110.0, g % h), {})
           for i, h in enumerate((0, 1, 2))]
    run2 = run + [("f_late", _snap("BTC/USDT", "long", 100.0, 95.0, 110.0, "2026-01-02T06:00:00Z"), {})]
    assert len(E.collapse_to_episodes(run2)) == 2


def test_collapse_distinct_setups_stay_distinct():
    g = "2026-01-01T00:00:00Z"
    mixed = [("a", _snap("BTC/USDT", "long", 100.0, 95.0, 110.0, g), {}),
             ("b", _snap("BTC/USDT", "short", 200.0, 210.0, 180.0, g), {})]
    assert len(E.collapse_to_episodes(mixed)) == 2
