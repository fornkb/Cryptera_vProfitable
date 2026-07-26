"""Cryptera -- unified collector + Telegram monitor (ONE process).

Every 15m candle close, the engine's CHEAP pass (fetch + score, NO Gemini) runs across the
watchlist. EVERY resulting snapshot is persisted (the calibration corpus). When Telegram +
Gemini are configured (and --collect-only is not set), a single Gemini narration fires on a
gated ACTIVE state transition and pushes a Telegram alert; otherwise a deterministic-only
alert is sent.

Modes:
    python monitor.py                 # collect corpus + Telegram alerts  (needs TELEGRAM_* + a GEMINI key)
    python monitor.py --collect-only  # corpus only: no Gemini, no Telegram (keyless -- e.g. an Azure box)
    python monitor.py --once          # one immediate cycle then exit (handy for a smoke check)
    python monitor.py --symbols SOL/USDT,BTC/USDT --retention 0 --no-color

With no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID configured it auto-degrades to collect-only.
The hard bound -- SUCCESSFUL narrations/day <= DAILY_BUDGET - BUDGET_RESERVE -- is enforced
by budget.can_spend (the reserve is never spent). This bounds successful spends, not raw
Gemini API call attempts (FA-12): `narrate` can retry across keys/schema tiers internally,
and `_narrate_with_retry` adds one more outer attempt, all before a single `state.spend()`
-- on an error-heavy day, real API calls can exceed DAILY_BUDGET while spend() stays low.

LIVENESS (P0, 2026-07-21 -- five days of silent hang behind `active (running)`):
every phase of a cycle is bounded by WALL CLOCK, not by hope.
  * The COLLECT phase (cheap pass + persist) is bounded per symbol and produces the
    corpus. It runs FIRST and is completely independent of Gemini.
  * The ALERT phase (narrate + Telegram) is bounded per symbol AND as a whole. It can
    time out, fail, or be skipped entirely without costing a single snapshot.
  * A cycle that overruns everything still returns, so the 15m cadence loop survives.
  * `alerts.liveness.Health` + `watchdog_task` convert a hang into a systemd restart,
    which `Restart=always` alone provably cannot do (the process never died).

Run:  python monitor.py   (24/7 via systemd -- see deploy/setup_vm.sh)
"""

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import time
from datetime import timezone

from main import (compute_snapshot_no_llm, narrate, build_chart_series,
                  _persist_snapshot, finalize_analysis, data_health_report)
from alerts.config import Cfg
from alerts.budget import State
from alerts import controller as C
from alerts import telegram as T
from alerts import liveness as L

logger = logging.getLogger("cryptera.monitor")


# --------------------------- console output --------------------------- #

class _Clr:
    on = True
    R = "\033[0m"; B = "\033[1m"
    GREEN = "\033[32m"; RED = "\033[31m"; YEL = "\033[33m"; GREY = "\033[90m"

    @classmethod
    def w(cls, code, s):
        return f"{code}{s}{cls.R}" if cls.on else str(s)


def _enable_win_ansi():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            _Clr.on = False


def _price(v):
    if not isinstance(v, (int, float)):
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    return (f"{v:.4f}".rstrip("0").rstrip(".")) or "0"


def _print_status(sym, d):
    """One concise console line per symbol; ACTIVE setups also print entry/stop/target."""
    w = _Clr.w
    score, thr, mode = d["score"], d["threshold"], (d.get("mode") or "?")
    if d["active"]:
        et = d.get("et") or {}
        dcol = _Clr.GREEN if (et.get("direction") == "BUY") else _Clr.RED
        rr = et.get("rr")
        rr_txt = f"  R:R {rr}{'' if et.get('rr_passed') else ' ⚠<2R'}" if rr is not None else ""
        print(f"  {w(_Clr.GREEN, '● ACTIVE')} {w(_Clr.B, sym)}  {mode}  "
              f"score {w(_Clr.B, str(score))}/{thr}  "
              f"{w(dcol, (et.get('direction') or '').upper())}")
        print(f"      entry {_price(et.get('entry'))}  stop {_price(et.get('stop_loss'))}  "
              f"tp {_price(et.get('take_profit'))}{rr_txt}")
    else:
        tag = w(_Clr.YEL, "conditional") if d.get("conditional") else w(_Clr.GREY, "hold")
        print(f"  {w(_Clr.GREY, '·')} {sym}  {mode}  score {score}/{thr}  {tag}  "
              f"dir={d.get('direction') or '—'}")


# --------------------------- Gemini / cadence helpers --------------------------- #

def _seconds_to_next_tick(offset):
    """Seconds until the next 15m boundary + `offset` seconds (lets the just-closed bar
    settle on the exchange before the cheap pass runs)."""
    now = dt.datetime.now(timezone.utc)
    base = now.replace(second=0, microsecond=0)
    mins = 15 - (now.minute % 15)
    nxt = base + dt.timedelta(minutes=mins, seconds=offset)
    if nxt <= now:
        nxt += dt.timedelta(minutes=15)
    return max(1.0, (nxt - now).total_seconds())


async def _narrate_with_retry(snap):
    """One `narrate` call + at most one outer retry (still <=5/min via min_call_spacing).
    Never raises -- returns an error dict on failure so the caller can fall back. Each
    `narrate` call is itself not one API call -- it walks up to 3 keys x transient
    retries x 2 schema tiers internally (FA-12) -- so a failed-then-retried attempt here
    can cost several real Gemini calls while `state.spend()` (module docstring) only
    ever charges 1, on success.

    Bounded by `Cfg.narration_deadline_s` of WALL CLOCK (P0). `asyncio.wait_for` cannot
    kill the worker thread, but it does return control to the cycle -- and the SDK's own
    request timeout (config.GEMINI_REQUEST_TIMEOUT_SECONDS) guarantees the orphaned
    thread dies on its own shortly after, so threads cannot accumulate across cycles.
    """
    async def _attempt():
        analysis = await asyncio.to_thread(narrate, snap)
        if isinstance(analysis, dict) and "error" in analysis:
            await asyncio.sleep(Cfg.min_call_spacing_s)
            analysis = await asyncio.to_thread(narrate, snap)
        return analysis

    try:
        return await asyncio.wait_for(_attempt(), Cfg.narration_deadline_s)
    except asyncio.TimeoutError:
        logger.error(f"narrate timed out after {Cfg.narration_deadline_s}s — "
                     f"degrading to engine-only for this symbol.")
        return {"error": f"narrate timeout ({Cfg.narration_deadline_s}s)"}


# --------------------------- cycle --------------------------- #

async def _collect_phase(rt, out):
    """Cheap pass + persist. The corpus is the top-priority output, so this phase runs
    FIRST, touches no Gemini, and writes its results into `out` INCREMENTALLY -- if the
    phase is cancelled on deadline, everything already persisted still counts.

    Each symbol is bounded independently: one wedged symbol must not cost the others."""
    async def _one(sym):
        try:
            snap, data = await asyncio.wait_for(
                compute_snapshot_no_llm(sym), Cfg.collect_deadline_s)
            return (sym, snap, data, C.engine_gate(snap, Cfg.threshold, Cfg.conditional_threshold))
        except asyncio.TimeoutError:
            logger.error(f"[cheap-pass] {sym} exceeded {Cfg.collect_deadline_s}s (skipping this cycle)")
            return None
        except Exception as e:
            print(f"[cheap-pass] {sym} failed (skipping this cycle): {e}")
            return None
    results = [r for r in await asyncio.gather(*(_one(s) for s in rt.symbols)) if r]
    out["results"] = results

    # --- persist EVERY snapshot FIRST: the corpus (top-priority output) must never depend on
    # the alert path. chart_series is attached here for all (added after any LLM call, never in
    # the prompt). Narrated snaps are re-written in place below so they additionally carry
    # `analysis`. retention <=0 (collector mode) = keep everything. ---
    n_active = 0
    paths = out["paths"]
    for sym, snap, data, d in results:
        # Per-symbol isolation: one bad chart_series/serialization must not skip the rest of the corpus.
        try:
            if not rt.skip_chart_series and "chart_series" not in snap:
                snap["chart_series"] = build_chart_series(data)
            paths[sym] = _persist_snapshot(snap, rt.retention)
            if d["active"]:
                n_active += 1
            _print_status(sym, d)
        except Exception as e:
            print(f"[persist] {sym} failed (skipping): {e}")
    print(_Clr.w(_Clr.GREY, f"  cycle: {len(results)} snapshot(s) saved · {n_active} ACTIVE"))


async def _alert_phase(state, rt, results, paths):
    """narrate + Telegram on gated transitions (budget-guarded). Everything here is
    OPTIONAL: the corpus is already on disk before this is entered, and the caller bounds
    the whole phase, so any failure/timeout costs at most one cycle's alerts."""
    eligible = C.rank([t for t in results if C.should_narrate(state.sym(t[0]), t[3], Cfg)])
    if eligible:
        print(f"[cycle] {len(eligible)} eligible: " +
              ", ".join(f"{s}({d['score']})" for s, _, _, d in eligible))
    async with state.call_lock:                    # <=1 Gemini call in flight
        for sym, snap, data, d in eligible:
            # Per-symbol isolation: a finalize/serialization/Telegram failure on ONE symbol
            # must not unwind the alert path and skip the remaining symbols.
            try:
                if state.can_spend(Cfg.budget_reserve, Cfg.daily_budget):
                    analysis = await _narrate_with_retry(snap)
                    if isinstance(analysis, dict) and "error" not in analysis:
                        state.spend()              # only successful calls consume budget
                        analysis = finalize_analysis(snap, analysis)
                        snap["analysis"] = analysis
                        # re-write the SAME corpus file in place so narrated snaps carry
                        # `analysis` (needed by eval_engine's LLM-vs-engine A/B). Same
                        # filename = same labeller row (dedupe is by filename), no
                        # duplicate corpus entry.
                        if paths.get(sym):
                            try:
                                with open(paths[sym], "w") as f:
                                    json.dump(snap, f, separators=(",", ":"))  # FA-14: compact
                            except Exception as e:
                                print(f"[persist] {sym} re-write with analysis failed: {e}")
                        # Telegram is a synchronous requests call (retries w/ backoff) -- run
                        # it off the event loop so a slow/down Telegram can't stall the cycle.
                        sent = await asyncio.to_thread(T.send_message, Cfg, T.format_active(sym, snap, analysis))
                        if sent:
                            print(f"[narrate] {sym} sent (budget {state.budget_used()}/{Cfg.usable_budget()})")
                        else:
                            print(f"[telegram] send FAILED for {sym} — alert lost, cooldown consumed (budget spent)")
                    else:
                        await asyncio.to_thread(T.send_message, Cfg, T.format_deterministic(sym, snap, "LLM narration failed — engine geometry only"))
                else:
                    await asyncio.to_thread(T.send_message, Cfg, T.format_deterministic(
                        sym, snap, f"LLM narration skipped — daily Gemini quota exhausted "
                                   f"({state.budget_used()}/{Cfg.usable_budget()})"))
                state.set_sym(sym, C.new_state(d))
            except Exception as e:
                print(f"[narrate] {sym} failed (skipping, cycle continues): {e}")
            await asyncio.sleep(Cfg.min_call_spacing_s)

    # optional CONDITIONAL heads-up: deterministic-only (no Gemini spend), rising edge
    # into the conditional band, same per-symbol cooldown. Per-symbol isolation: a
    # State.save() disk error on ONE symbol must not unwind the alert path.
    if Cfg.alert_on_conditional:
        for sym, snap, data, d in results:
            try:
                prev = state.sym(sym)
                now_t = time.time()
                if (d.get("conditional") and not d["active"] and not prev.get("conditional")
                        and (now_t - prev.get("last_alert_ts", 0)) >= Cfg.min_realert_s):
                    await asyncio.to_thread(T.send_message, Cfg, T.format_deterministic(
                        sym, snap, f"CONDITIONAL setup (score {d['score']}/{d['threshold']}) — "
                                   f"engine-only heads-up, no LLM spent"))
                    prev.update({"conditional": True, "last_alert_ts": now_t})
                    state.set_sym(sym, prev)
                elif not d.get("conditional") and prev.get("conditional"):
                    prev["conditional"] = False
                    state.set_sym(sym, prev)
            except Exception as e:
                print(f"[conditional] {sym} failed (skipping): {e}")

    # re-arm symbols that dropped out of ACTIVE so a future cross fires again
    for sym, snap, data, d in results:
        try:
            if not d["active"]:
                prev = state.sym(sym)
                if prev.get("active"):
                    prev["active"] = False
                    state.set_sym(sym, prev)
        except Exception as e:
            print(f"[re-arm] {sym} failed (skipping): {e}")


async def cycle(state, rt, health=None):
    """One 15m cycle: bounded COLLECT, then bounded ALERT.

    The two phases are separated so the failure that cost 5 days of corpus is
    structurally impossible: narration lives entirely downstream of persistence and
    is bounded independently, so it can hang, fail, or time out without touching the
    snapshot count. Returns the number of snapshots persisted."""
    out = {"results": [], "paths": {}}
    try:
        await asyncio.wait_for(_collect_phase(rt, out), Cfg.collect_deadline_s + 30)
    except asyncio.TimeoutError:
        logger.error(f"[collect] phase exceeded {Cfg.collect_deadline_s + 30}s — "
                     f"{len(out['paths'])} snapshot(s) persisted before the cut.")
    except Exception as e:
        logger.error(f"[collect] phase failed: {e}")

    results, paths = out["results"], out["paths"]
    if rt.alert_mode and results:
        try:
            await asyncio.wait_for(_alert_phase(state, rt, results, paths),
                                   Cfg.alert_phase_deadline_s)
        except asyncio.TimeoutError:
            # The corpus is ALREADY on disk. Losing this cycle's alerts is the
            # designed-for outcome; losing the corpus is not.
            logger.error(f"[alert] phase exceeded {Cfg.alert_phase_deadline_s}s — "
                         f"alerts skipped, corpus unaffected ({len(paths)} snapshot(s)).")
        except Exception as e:
            logger.error(f"[alert] phase failed (corpus unaffected): {e}")

    if health is not None:
        health.cycle_ok(len(paths))
        L.write_heartbeat(Cfg.heartbeat_path, health, rt.symbols)
    return len(paths)


# --------------------------- entry point --------------------------- #

class _RT:
    """Resolved runtime config (CLI overrides env)."""
    def __init__(self, args):
        self.symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                        if args.symbols else Cfg.watchlist)
        self.once = args.once
        self.retention = args.retention                # None -> engine default; <=0 -> unlimited
        # alerting needs Telegram configured AND not explicitly collect-only. No Gemini key ->
        # narration returns an error dict -> deterministic-only Telegram (still "alert mode").
        self.alert_mode = (not args.collect_only) and bool(Cfg.bot_token and Cfg.chat_id)
        self.collect_only = not self.alert_mode
        # Corpus-box optimization: chart_series (~half of each snapshot) is read ONLY by the web
        # dashboard -- never by the Gemini prompt nor the calibration pipeline (labeller replays
        # freshly-fetched candles). On a headless corpus VM set CRYPTERA_NO_CHART_SERIES=1 to skip
        # building/persisting it. Unset (local) => unchanged.
        self.skip_chart_series = (
            os.environ.get("CRYPTERA_NO_CHART_SERIES", "").strip().lower() in ("1", "true", "yes"))


def _parse_args():
    ap = argparse.ArgumentParser(description="Cryptera unified collector + Telegram monitor")
    ap.add_argument("--collect-only", action="store_true",
                    help="corpus only: no Gemini, no Telegram (keyless)")
    ap.add_argument("--once", action="store_true", help="one immediate cycle then exit")
    ap.add_argument("--symbols", default=None, help="override WATCHLIST (comma-separated)")
    ap.add_argument("--retention", type=int, default=None,
                    help="max snapshots kept per symbol (0 = unlimited; default = engine setting)")
    ap.add_argument("--no-color", action="store_true")
    return ap.parse_args()


async def main():
    args = _parse_args()
    if args.no_color:
        _Clr.on = False
    else:
        _enable_win_ansi()
    rt = _RT(args)

    state = None
    if rt.alert_mode:
        Cfg.assert_budget_safe()
        state = State(Cfg.state_path)
        mode = "collect + Telegram alerts"
    else:
        mode = "collect-only (no Gemini/Telegram)"

    print(f"[monitor] {mode} | watching {rt.symbols} | threshold {Cfg.threshold}"
          + (f" | budget {Cfg.daily_budget}-{Cfg.budget_reserve} reserve | cooldown {Cfg.min_realert_s}s"
             if rt.alert_mode else "")
          + (f" | retention {rt.retention}" if rt.retention is not None else ""))
    if not rt.alert_mode and not args.collect_only and not (Cfg.bot_token and Cfg.chat_id):
        print("[monitor] note: TELEGRAM_BOT_TOKEN/CHAT_ID unset — running collect-only.")
    for line in data_health_report():
        print(f"[health] {line}")

    if rt.alert_mode:
        # Startup heartbeat -- instant confirmation the Telegram chain works (no Gemini call).
        # Off the event loop: a slow/down Telegram must not block startup for up to ~45s.
        await asyncio.to_thread(T.send_message, Cfg, (
            f"\U0001F7E2 <b>Cryptera monitor online</b>\n"
            f"Watching {', '.join(rt.symbols)} @ score ≥ {Cfg.threshold}\n"
            f"Budget {Cfg.usable_budget()}/day (reset 00:00 UTC) · alerts on threshold crossings only."))

    # P0 liveness: `Restart=always` restarts a process that DIED, and has no opinion
    # about one that is alive and wedged (2026-07-21: 5 days of that). `health` is the
    # progress signal; the watchdog task turns a stall into a restart by WITHHOLDING
    # systemd's heartbeat. Gating on completed cycles -- not on a bare timer -- is what
    # makes it able to see a hang that lives inside an await on a worker thread.
    health = L.Health(Cfg.max_stall_s)
    L.write_heartbeat(Cfg.heartbeat_path, health, rt.symbols)
    L.sd_notify("READY=1")
    watchdog = None
    if not rt.once:
        watchdog = asyncio.create_task(L.watchdog_task(health))

    # immediate first cycle (scores the last CLOSED bar), then align to 15m boundaries
    try:
        await cycle(state, rt, health)
    except Exception as e:
        health.cycle_failed(e)
        print(f"[monitor] cycle error (continuing): {e}")
    if rt.once:
        return
    try:
        while True:
            await asyncio.sleep(_seconds_to_next_tick(Cfg.poll_second_offset))
            try:
                await cycle(state, rt, health)
            except Exception as e:
                health.cycle_failed(e)
                print(f"[monitor] cycle error (continuing): {e}")
    finally:
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    # FA-10: monitor.py runs the whole engine (main.py's logger.info/.warning
    # calls included) but never configured a handler -- entry points own this
    # per CLAUDE.md, and monitor.py is the 24/7 systemd entry point, so every
    # engine log line was silently dropped instead of landing in journald.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[monitor] stopped")
