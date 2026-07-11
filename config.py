"""
Central configuration for Cryptera (audit §5): timeframe limits, swing widths,
gate/score thresholds, retention, and model name in one place instead of
scattered across main.py/smc.py/indicators.py/strategies.py. Values are
unchanged from their prior per-module definitions — this is a pure extraction,
not a re-tune.
"""

import os

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
SNAPSHOTS_DIR = os.path.join(ROOT_DIR, "snapshots")
OI_HISTORY_PATH = os.path.join(DATA_DIR, "oi_history.json")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
REGIME_STATE_PATH = os.path.join(DATA_DIR, "regime_state.json")
SNAPSHOT_INDEX_PATH = os.path.join(DATA_DIR, "snapshot_index.json")

SCHEMA_VERSION = "3.3.0"  # bump on any snapshot-SHAPE change (CLAUDE.md invariant 5)

# ---------------- market / timeframes ---------------- #
MARKET = "binance-usdm"          # single namespace (BUG-01) — never mix spot/futures
TIMEFRAMES = ["1d", "4h", "1h", "15m"]
REQUIRED_TIMEFRAMES = {"4h", "1h", "15m"}  # 1D is a best-effort HTF overlay (BUG-15)
# >= 260 closed bars so EMA_200 / ATR_Pct(100) are live after the forming candle drops (BUG-02)
LIMITS = {"1d": 300, "4h": 300, "1h": 300, "15m": 300}
WINDOW_SIZE = {"1d": 14, "4h": 10, "1h": 20, "15m": 30}
TF_MS = {"1d": 24 * 3600_000, "4h": 4 * 3600_000, "1h": 3600_000, "15m": 15 * 60_000}
CHART_BARS = {"1d": 90, "4h": 120, "1h": 150, "15m": 180}

# ---------------- OHLCV cache ---------------- #
OHLCV_CACHE_MAX = 50
CACHE_TTL_SECONDS = {"1d": 900, "4h": 300, "1h": 120, "15m": 20}

# ---------------- swing detection (shared by smc.py / indicators.py / price_action.py) ---------------- #
SWING_WIDTHS = {"1d": (4, 4), "4h": (5, 5), "1h": (4, 3), "15m": (3, 2)}
DEFAULT_WIDTH = (3, 3)

# ---------------- regime ---------------- #
ADX_TREND_FLOOR = 20.0            # 4H ADX must clear this for a "Trending" classification (BUG-08)
REGIME_HYSTERESIS_RUNS = 2        # consecutive raw runs required before a rubric flip (BUG-08)

# ICT kill-zone UTC hour windows (start, end, name) — first match wins, else "OFF".
KILL_ZONES = (
    (7, 10, "LONDON"),
    (12, 15, "NEWYORK"),
    (0, 4, "ASIA"),
)

# ---------------- scoring / gates ---------------- #
ACTIVE_TRADE_THRESHOLD = 60
CONDITIONAL_ENTRY_THRESHOLD = 45
VOLUME_HARD_GATE = 0.1
VOLUME_LOW_VOL_WARNING = 0.3
LOCATION_CAP = 25                 # C2+C5+C6 "location" composite cap (rubric decorrelation)
LOCATION_KEYS = ("c2_ob_proximity", "c5_fvg_magnet", "c6_ote_bonus")
SL_ATR_CAP = 3.0                  # max structural SL width in 15m-ATR multiples (BUG-06)
EVENT_GUARD_WINDOW_HOURS = 2

# ---------------- retention / horizon ---------------- #
SNAPSHOT_RETENTION = 1000         # per symbol
HORIZON_HOURS = 24                # forward window the geometry/targets are judged against

# ---------------- Gemini ---------------- #
GEMINI_MODEL_NAME = "gemini-3.5-flash"
# Gemini 3.5 uses string-enum thinking levels instead of numeric budgets:
# minimal | low | medium (default) | high. As of v3.3 the LLM must SELECT the
# best candidate from the engine menu (the highest-stakes decision in the
# output), which rewards deeper thinking, hence "high". This is the
# pre-measurement default: tools/ab_thinking.py A/B-tests high vs medium over
# labeled snapshots; switch to the measured winner once enough labeled
# snapshots exist (empty corpus as of Phase 2/3 — current_status.md).
GEMINI_THINKING_LEVEL = "high"
