"""Env-driven configuration for the Telegram monitor, plus the startup budget-safety
assertion that makes the <=daily-budget Gemini-calls/day bound true by construction."""

import os
from dotenv import load_dotenv

from config import DATA_DIR, ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD

load_dotenv()


class Cfg:
    watchlist = [s.strip() for s in os.getenv("WATCHLIST", "SOL/USDT,BTC/USDT,ETH/USDT").split(",") if s.strip()]
    # Global gate thresholds -- this version has no per-module ACTIVE cutoff (unlike v4.x's
    # regime router): both bands come straight from config.py, the same constants the
    # engine itself scores against, so an env override can't silently drift from what
    # ACTIVE_TRADE/CONDITIONAL_ENTRY actually mean.
    threshold = int(os.getenv("SCORE_THRESHOLD", str(ACTIVE_TRADE_THRESHOLD)))
    conditional_threshold = int(os.getenv("CONDITIONAL_SCORE_THRESHOLD", str(CONDITIONAL_ENTRY_THRESHOLD)))
    material_delta = int(os.getenv("MATERIAL_DELTA", "10"))         # score jump that re-narrates an active setup
    min_realert_s = int(os.getenv("MIN_REALERT_SECONDS", "7200"))   # per-symbol cooldown (2h)
    # FA-12: this bounds SUCCESSFUL narrations (State.spend() only fires on success), not
    # raw Gemini API call attempts -- narrate() can internally retry across keys/schema
    # tiers, and monitor._narrate_with_retry adds one more outer attempt, all before a
    # single spend. On an error-heavy day, real calls can exceed this number.
    daily_budget = int(os.getenv("DAILY_BUDGET", "40"))             # usable 36 across 3 keys (12/key)
    budget_reserve = int(os.getenv("BUDGET_RESERVE", "4"))
    min_call_spacing_s = int(os.getenv("MIN_CALL_SPACING", "8"))    # serialize <=1 in flight, well under 5/min
    alert_on_conditional = os.getenv("ALERT_ON_CONDITIONAL", "false").lower() == "true"
    poll_second_offset = int(os.getenv("POLL_SECOND_OFFSET", "30"))  # seconds past each 15m close
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    state_path = os.path.join(DATA_DIR, "telegram_state.json")
    heartbeat_path = os.path.join(DATA_DIR, "monitor_heartbeat.json")

    # ---- unattended liveness bounds (P0, 2026-07-21 five-day silent hang) ---- #
    # Every one of these is WALL CLOCK, enforced with asyncio.wait_for, and every
    # one is well under the 900s tick so a stall can never eat the next cycle.
    # The ordering that matters: corpus collection is bounded SEPARATELY from and
    # BEFORE narration, so a wedged Gemini call can never cost a single snapshot.
    # Budget, and it MUST close: request 90 <= gemini call 180 <= narration 380
    # <= alert phase 450; collect 300 (+30s grace in cycle()) + alert 450 = 780 < 900.
    narration_deadline_s = int(os.getenv("NARRATION_DEADLINE", "380"))    # one symbol's narrate
    alert_phase_deadline_s = int(os.getenv("ALERT_PHASE_DEADLINE", "450"))  # whole alert path
    collect_deadline_s = int(os.getenv("COLLECT_DEADLINE", "300"))        # per symbol, cheap pass
    # A cycle that has not COMPLETED within this long means the process is wedged:
    # the watchdog heartbeat stops and systemd restarts us. Must exceed one tick
    # (900s) plus a full cycle, or a normal quiet period would look like a hang.
    max_stall_s = int(os.getenv("MAX_STALL_SECONDS", "2700"))             # 45 min = 3 missed ticks

    @classmethod
    def usable_budget(cls):
        return cls.daily_budget - cls.budget_reserve

    @classmethod
    def assert_budget_safe(cls):
        """The HARD guarantee -- API calls/day <= daily_budget - reserve -- is enforced
        unconditionally by State.can_spend (the reserve is never spent). This startup
        check is an advisory sanity gate on watchlist size: it fails only when worst-case
        demand could exceed the hard daily cap (a clearly misconfigured watchlist).
        Between `usable` and `daily_budget`, excess transitions degrade gracefully to
        deterministic-only alerts -- that's a note, not a failure."""
        worst = len(cls.watchlist) * (86400.0 / cls.min_realert_s)
        if worst > cls.daily_budget:
            raise SystemExit(
                f"[config] watchlist too large: {len(cls.watchlist)} symbols x "
                f"{86400.0 / cls.min_realert_s:.1f} max alerts/day = {worst:.1f} "
                f"> hard cap {cls.daily_budget}. Raise MIN_REALERT_SECONDS or shrink WATCHLIST.")
        if worst > cls.usable_budget():
            print(f"[config] note: worst-case demand {worst:.1f} > usable "
                  f"{cls.usable_budget()} -- on extreme days up to "
                  f"{worst - cls.usable_budget():.0f} alert(s) may be deterministic-only "
                  f"(reserve-protected, never exceeds quota).")
        return True
