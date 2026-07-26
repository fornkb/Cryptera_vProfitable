"""Unattended-liveness primitives for the 24/7 collector (P0, 2026-07-21).

On 2026-07-21 07:03 UTC the live collector stopped producing snapshots for FIVE
DAYS while `systemctl status` still read `active (running)`. A Gemini request with
no transport timeout never returned, `monitor.cycle()` awaited it forever, and the
`except Exception` guard never fired because **a hang is not an exception**.
`Restart=always` cannot see a hang either -- the process was alive, just useless.

The missing piece is a *progress* signal, not a *process* signal. This module
provides one:

  * `Health` records the last time a cycle actually COMPLETED.
  * `watchdog_task` pets systemd's watchdog ONLY while that timestamp is fresh.
    A naive heartbeat would defeat the whole mechanism: the hang lived inside an
    `await` on a worker thread, which does not block the event loop, so a timer
    task would have kept reporting health forever. Gating on real progress is
    what makes the watchdog able to see this exact failure.
  * `write_heartbeat` drops the same fact on disk, so an operator (and
    `tools/status.py`) can diagnose liveness without journald or systemd -- and
    so the mechanism still works when run outside systemd entirely.

Pure stdlib: `sd_notify` speaks the systemd notification protocol directly
(a datagram to $NOTIFY_SOCKET) rather than pulling in python-systemd.
"""

import json
import logging
import os
import socket
import time
from datetime import datetime, timezone

logger = logging.getLogger("cryptera.liveness")


# --------------------------- systemd notify protocol --------------------------- #

def sd_notify(message: str) -> bool:
    """Send `message` to systemd's $NOTIFY_SOCKET. Returns False (harmlessly) when
    not running under a `Type=notify` unit -- e.g. a local dev run or Windows.

    Handles the abstract-namespace form ("@/org/..." -> "\\0/org/...") that
    systemd uses on Linux.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
        return True
    except Exception as e:                                   # pragma: no cover - env-specific
        logger.debug(f"sd_notify({message!r}) failed: {e}")
        return False


def watchdog_interval_s() -> float:
    """Half of systemd's WatchdogSec (from $WATCHDOG_USEC), or 0.0 when the unit
    has no watchdog configured. Half is the interval systemd's own documentation
    recommends, so one dropped notification is not fatal."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", "") or 0)
    except ValueError:
        return 0.0
    # WATCHDOG_PID guards against inheriting the variable in a child process.
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid.strip().isdigit() and int(pid) != os.getpid():
        return 0.0
    return (usec / 1_000_000.0) / 2.0 if usec > 0 else 0.0


# --------------------------- progress tracking --------------------------- #

class Health:
    """Tracks the last COMPLETED cycle. `max_stall_s` is how long the collector may
    go without completing one before it is considered wedged.

    `time.monotonic` is deliberate: a wall-clock jump (NTP step, VM resume) must not
    be readable as either liveness or a stall.
    """

    def __init__(self, max_stall_s: float, clock=time.monotonic):
        self.max_stall_s = float(max_stall_s)
        self._clock = clock
        self.last_ok = clock()
        self.cycles = 0
        self.last_error = None

    def cycle_ok(self, snapshots: int = 0):
        self.last_ok = self._clock()
        self.cycles += 1
        self.last_snapshots = snapshots
        self.last_error = None

    def cycle_failed(self, err):
        """A cycle that RAISED still proves the loop is turning, but it produced no
        corpus -- so it must NOT refresh the liveness deadline. Recorded only."""
        self.last_error = str(err)

    def stalled_for(self) -> float:
        return self._clock() - self.last_ok

    def is_alive(self) -> bool:
        return self.stalled_for() <= self.max_stall_s


def write_heartbeat(path: str, health: Health, symbols=None) -> bool:
    """Atomically record liveness on disk (survives a crash mid-write). Never raises:
    a full or read-only disk must not take the collector down."""
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cycles_completed": health.cycles,
        "stalled_for_s": round(health.stalled_for(), 1),
        "max_stall_s": health.max_stall_s,
        "alive": health.is_alive(),
        "last_error": health.last_error,
        "last_snapshots": getattr(health, "last_snapshots", None),
        "symbols": list(symbols or []),
        "pid": os.getpid(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"heartbeat write failed ({path}): {e}")
        return False


async def watchdog_task(health: Health, interval_s: float = None):
    """Pet systemd's watchdog ONLY while cycles are still completing.

    This is the whole point: `Restart=always` restarts a process that DIED. It has
    no opinion about a process that is alive and doing nothing. By withholding
    WATCHDOG=1 once `health` goes stale, a hang becomes a restart.

    No-ops (returns immediately) when the unit has no WatchdogSec.
    """
    import asyncio

    interval = watchdog_interval_s() if interval_s is None else interval_s
    if not interval or interval <= 0:
        logger.info("watchdog: WATCHDOG_USEC unset — systemd watchdog disabled.")
        return
    logger.info(f"watchdog: petting every {interval:.0f}s while cycles stay under "
                f"{health.max_stall_s:.0f}s old.")
    while True:
        await asyncio.sleep(interval)
        if health.is_alive():
            sd_notify("WATCHDOG=1")
        else:
            # Deliberately silent toward systemd — withholding the notification is
            # the signal. Log loudly so journald explains the restart afterwards.
            logger.error(
                f"watchdog: NO completed cycle for {health.stalled_for():.0f}s "
                f"(limit {health.max_stall_s:.0f}s) — withholding heartbeat so "
                f"systemd restarts this process. last_error={health.last_error}")
            sd_notify(f"STATUS=stalled {health.stalled_for():.0f}s — awaiting watchdog restart")
