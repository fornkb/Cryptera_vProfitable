"""P0 regression tests for the 2026-07-21 five-day silent hang.

The failure: a Gemini request with no transport timeout never returned, so
`monitor.cycle()` awaited it forever. `except Exception` never fired (a hang is
not an exception) and `Restart=always` never fired (the process never died), so
the corpus collector -- which needs no Gemini at all -- stopped for 5 days while
systemd reported `active (running)`.

These tests pin the four defenses that make that specific failure impossible:
  1. the SDK client carries an explicit transport timeout,
  2. `query_gemini` is bounded by wall clock across keys/tiers/retries,
  3. a hung narration cannot cost a single snapshot, and the cycle still returns,
  4. the watchdog withholds systemd's heartbeat once cycles stop completing.
"""

import asyncio
import time

import pytest

import config
import main
from alerts import liveness as L


# --------------------------- 1. transport timeout --------------------------- #

def test_gemini_client_sets_an_explicit_request_timeout(monkeypatch):
    """No timeout on the SDK client == the 5-day hang. It must be set, in ms."""
    captured = {}

    class _FakeClient:
        def __init__(self, api_key=None, http_options=None):
            captured["http_options"] = http_options

    monkeypatch.setattr(main.genai, "Client", _FakeClient)
    monkeypatch.setattr(main, "_GEMINI_CLIENTS", {})

    main._gemini_client_for_key("k-test")

    ho = captured["http_options"]
    assert ho is not None, "genai.Client created with NO http_options — unbounded hang"
    assert ho.timeout == int(config.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000), \
        "HttpOptions.timeout is milliseconds"
    assert 0 < config.GEMINI_REQUEST_TIMEOUT_SECONDS <= 300


def test_gemini_clients_are_cached_per_key(monkeypatch):
    made = []

    class _FakeClient:
        def __init__(self, api_key=None, http_options=None):
            made.append(api_key)

    monkeypatch.setattr(main.genai, "Client", _FakeClient)
    monkeypatch.setattr(main, "_GEMINI_CLIENTS", {})
    main._gemini_client_for_key("a")
    main._gemini_client_for_key("a")
    main._gemini_client_for_key("b")
    assert made == ["a", "b"]


# --------------------------- 2. call-level deadline --------------------------- #

def _slow_client(seconds, exc=None):
    """A client that burns `seconds` then raises. Never calls time.sleep(0) — tests
    below patch time.sleep to record BACKOFF sleeps and must not see this one."""
    class _C:
        class models:
            @staticmethod
            def generate_content(**kw):
                if seconds:
                    time.sleep(seconds)
                raise (exc or RuntimeError("503 UNAVAILABLE: model overloaded"))
    return _C()


def test_call_gemini_respects_deadline_before_attempting(monkeypatch):
    """An already-blown deadline must abort WITHOUT issuing a request."""
    calls = {"n": 0}

    class _C:
        class models:
            @staticmethod
            def generate_content(**kw):
                calls["n"] += 1
                return object()

    result, error, kind = main._call_gemini_with_key(
        _C(), "prompt", deadline=time.monotonic() - 1)
    assert result is None and calls["n"] == 0
    assert "deadline" in error and kind == "response"


def test_call_gemini_deadline_skips_the_backoff_sleep(monkeypatch):
    """The backoff sleep must not be allowed to run PAST the deadline."""
    monkeypatch.setattr(config, "GEMINI_TRANSIENT_BACKOFF_SECONDS", 30)
    monkeypatch.setattr(main, "GEMINI_TRANSIENT_BACKOFF_SECONDS", 30)
    slept = []
    monkeypatch.setattr(main.time, "sleep", lambda s: slept.append(s))

    started = time.monotonic()
    result, error, kind = main._call_gemini_with_key(
        _slow_client(0), "prompt", deadline=started + 0.5)

    assert result is None and slept == [], "slept past the deadline"
    assert kind == "response" and "deadline" in error


def test_query_gemini_is_bounded_and_degrades_on_model_wide_503(monkeypatch):
    """A model-wide 503 must degrade on the FIRST key, not walk every key into the
    same overloaded model and bench all of them for the next cycle too."""
    monkeypatch.setattr(main, "_load_gemini_keys", lambda: ["k1", "k2", "k3"])
    monkeypatch.setattr(main, "_GEMINI_COOLDOWN_UNTIL", {})
    monkeypatch.setattr(main, "_GEMINI_RR_INDEX", 0, raising=False)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    used = []
    monkeypatch.setattr(main, "_gemini_client_for_key",
                        lambda k: used.append(k) or _slow_client(0))

    out = main.query_gemini({"symbol": "SOL/USDT"})

    assert "error" in out
    assert used == ["k1"], f"rotated into an overloaded model: {used}"
    assert main._GEMINI_COOLDOWN_UNTIL == {}, "benched keys for a model-wide outage"


def test_query_gemini_still_rotates_on_a_per_key_error(monkeypatch):
    """A 429/auth failure IS key-specific — rotation must still happen."""
    monkeypatch.setattr(main, "_load_gemini_keys", lambda: ["k1", "k2"])
    monkeypatch.setattr(main, "_GEMINI_COOLDOWN_UNTIL", {})
    monkeypatch.setattr(main, "_GEMINI_RR_INDEX", 0, raising=False)
    used = []
    monkeypatch.setattr(
        main, "_gemini_client_for_key",
        lambda k: used.append(k) or _slow_client(0, exc=RuntimeError("429 RESOURCE_EXHAUSTED")))

    out = main.query_gemini({"symbol": "SOL/USDT"})
    assert "error" in out
    assert used == ["k1", "k2"], "did not fail over on a per-key error"
    assert set(main._GEMINI_COOLDOWN_UNTIL) == {"k1", "k2"}


def test_call_deadline_constant_is_sane():
    assert config.GEMINI_CALL_DEADLINE_SECONDS >= config.GEMINI_REQUEST_TIMEOUT_SECONDS
    # Must leave room inside one 15m tick for the rest of the cycle.
    assert config.GEMINI_CALL_DEADLINE_SECONDS < 900


# --------------------------- 3. a hang cannot cost the corpus --------------------------- #

class _RT:
    symbols = ["SOL/USDT"]
    retention = 0
    alert_mode = True
    skip_chart_series = True
    once = True


def test_hung_narration_never_blocks_snapshot_collection(monkeypatch, tmp_path):
    """THE regression test. narrate() hangs forever; the cycle must still persist the
    snapshot, still return, and record progress."""
    import monitor as M
    from alerts.config import Cfg

    persisted = []

    async def _fake_snapshot(sym):
        return ({"symbol": sym, "strategies": {"confluence_score": 99}}, {})

    monkeypatch.setattr(M, "compute_snapshot_no_llm", _fake_snapshot)
    monkeypatch.setattr(M, "_persist_snapshot",
                        lambda snap, ret: persisted.append(snap["symbol"]) or "/tmp/x.json")
    monkeypatch.setattr(M.C, "engine_gate",
                        lambda s, t, c: {"active": True, "conditional": False,
                                         "score": 99, "threshold": t, "mode": "trend",
                                         "direction": "buy", "et": {}})
    monkeypatch.setattr(M.C, "should_narrate", lambda prev, d, cfg: True)
    monkeypatch.setattr(M.C, "rank", lambda rows: rows)
    monkeypatch.setattr(M.C, "new_state", lambda d: {"active": True})

    # narrate() blocks far past its deadline — exactly the 2026-07-21 failure.
    # (Bounded at 8s rather than forever ONLY because asyncio.run() joins the default
    # executor at teardown; the production loop never closes, and the SDK request
    # timeout is what actually reaps the orphaned thread there.)
    def _hang(snap):
        time.sleep(8)

    monkeypatch.setattr(M, "narrate", _hang)
    monkeypatch.setattr(M.T, "send_message", lambda cfg, msg: True)
    monkeypatch.setattr(Cfg, "narration_deadline_s", 1)
    monkeypatch.setattr(Cfg, "alert_phase_deadline_s", 2)
    monkeypatch.setattr(Cfg, "min_call_spacing_s", 0)
    monkeypatch.setattr(Cfg, "heartbeat_path", str(tmp_path / "hb.json"))

    class _St:
        call_lock = asyncio.Lock()
        def sym(self, s): return {}
        def set_sym(self, s, d): pass
        def can_spend(self, r, c): return True
        def spend(self): pass
        def budget_used(self): return 0

    health = L.Health(max_stall_s=999)
    started = time.monotonic()
    n = asyncio.run(M.cycle(_St(), _RT(), health))
    elapsed = time.monotonic() - started

    assert persisted == ["SOL/USDT"], "the hang cost us the corpus — the 5-day bug"
    assert n == 1
    assert elapsed < 30, f"cycle did not return promptly ({elapsed:.0f}s)"
    assert health.cycles == 1, "progress not recorded — watchdog would restart us"


def test_narrate_timeout_returns_an_error_dict_not_an_exception(monkeypatch):
    """The caller branches on `'error' in analysis`; a timeout must land in that
    branch (deterministic Telegram fallback), never propagate."""
    import monitor as M
    from alerts.config import Cfg

    monkeypatch.setattr(M, "narrate", lambda snap: time.sleep(6))
    monkeypatch.setattr(Cfg, "narration_deadline_s", 1)

    out = asyncio.run(M._narrate_with_retry({"symbol": "X"}))
    assert isinstance(out, dict) and "error" in out and "timeout" in out["error"]


def test_alert_phase_deadlines_fit_inside_one_tick():
    """The deadline chain must NEST and the total must fit one 15m tick, or bounded
    cycles simply pile up at a slower rate instead of hanging."""
    from alerts.config import Cfg
    assert config.GEMINI_REQUEST_TIMEOUT_SECONDS <= config.GEMINI_CALL_DEADLINE_SECONDS
    assert config.GEMINI_CALL_DEADLINE_SECONDS <= Cfg.narration_deadline_s
    assert Cfg.narration_deadline_s <= Cfg.alert_phase_deadline_s
    # cycle() grants the collect phase a 30s grace over the per-symbol bound.
    assert (Cfg.collect_deadline_s + 30) + Cfg.alert_phase_deadline_s < 900, \
        "a full cycle must fit inside the 15m tick or cycles pile up"
    assert Cfg.max_stall_s > 900, "a normal quiet tick must not read as a stall"


# --------------------------- 4. watchdog sees a hang --------------------------- #

def test_health_goes_stale_when_cycles_stop_completing():
    clock = {"t": 1000.0}
    h = L.Health(max_stall_s=100, clock=lambda: clock["t"])
    assert h.is_alive()
    clock["t"] += 99
    assert h.is_alive()
    clock["t"] += 2
    assert not h.is_alive(), "a stalled collector still reports healthy"
    h.cycle_ok(3)
    assert h.is_alive() and h.cycles == 1


def test_a_raised_cycle_does_not_refresh_liveness():
    """A cycle that raises proves the loop turns but produced NO corpus. It must not
    reset the stall clock, or a permanently-failing collector looks healthy forever."""
    clock = {"t": 0.0}
    h = L.Health(max_stall_s=10, clock=lambda: clock["t"])
    clock["t"] = 50
    h.cycle_failed(RuntimeError("boom"))
    assert not h.is_alive()
    assert h.last_error == "boom"


def test_watchdog_withholds_the_heartbeat_once_stalled(monkeypatch):
    """The mechanism: systemd restarts us because we STOP petting, not because we
    tell it anything. A naive timer heartbeat would have petted through the 5-day
    hang (it lived in an await, which never blocks the event loop)."""
    sent = []
    monkeypatch.setattr(L, "sd_notify", lambda m: sent.append(m) or True)

    clock = {"t": 0.0}
    h = L.Health(max_stall_s=10, clock=lambda: clock["t"])

    async def _run():
        task = asyncio.create_task(L.watchdog_task(h, interval_s=0.01))
        await asyncio.sleep(0.05)          # healthy: pets
        clock["t"] = 999                   # wedged
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert "WATCHDOG=1" in sent, "never petted while healthy"
    tail = sent[sent.index("WATCHDOG=1"):]
    assert any(m.startswith("STATUS=stalled") for m in tail)
    # After the stall began, no further pets may be sent.
    stall_at = next(i for i, m in enumerate(sent) if m.startswith("STATUS=stalled"))
    assert "WATCHDOG=1" not in sent[stall_at:], "kept petting through a stall"


def test_watchdog_noop_without_systemd(monkeypatch):
    """Local/dev runs have no WatchdogSec — the task must return immediately."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    h = L.Health(max_stall_s=10)
    asyncio.run(asyncio.wait_for(L.watchdog_task(h), 2))


def test_watchdog_interval_is_half_of_watchdog_sec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", str(60 * 1_000_000))
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert L.watchdog_interval_s() == 30.0
    monkeypatch.setenv("WATCHDOG_USEC", "0")
    assert L.watchdog_interval_s() == 0.0
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert L.watchdog_interval_s() == 0.0


def test_watchdog_interval_ignores_an_inherited_pid(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", str(60 * 1_000_000))
    monkeypatch.setenv("WATCHDOG_PID", "999999")
    assert L.watchdog_interval_s() == 0.0


def test_sd_notify_is_a_safe_noop_without_a_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert L.sd_notify("READY=1") is False


def test_heartbeat_file_is_written_atomically_and_never_raises(tmp_path):
    h = L.Health(max_stall_s=100)
    h.cycle_ok(4)
    p = tmp_path / "sub" / "hb.json"
    assert L.write_heartbeat(str(p), h, ["SOL/USDT"]) is True
    import json
    d = json.loads(p.read_text())
    assert d["cycles_completed"] == 1 and d["alive"] is True
    assert d["last_snapshots"] == 4 and d["symbols"] == ["SOL/USDT"]
    assert not (tmp_path / "sub" / "hb.json.tmp").exists()
    # An unwritable path must degrade, not take the collector down.
    assert L.write_heartbeat("\0bad", h) is False
