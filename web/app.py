"""
Flask backend for the Cryptera v3.2 dashboard.

Returns the snapshot AND the parsed Gemini JSON analysis from `/api/run` and
`/api/history/<filename>` so the SPA can render structured fields directly.
"""

import os
import sys
import json
import glob
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PARENT_DIR)

from main import run_analysis
from config import (SNAPSHOTS_DIR, DATA_DIR, SNAPSHOT_INDEX_PATH,
                    ACTIVE_TRADE_THRESHOLD, CONDITIONAL_ENTRY_THRESHOLD)

logger = logging.getLogger("cryptera.web")

app = Flask(__name__)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
async def run():
    data = request.get_json() or {}
    symbol = data.get("symbol", "SOL/USDT").strip()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400

    logger.info(f"Starting live market analysis for: {symbol}")
    try:
        # Native async view (Flask/asgiref) instead of asyncio.run() inside the
        # handler (BUG-23) — the old pattern spun up a fresh event loop per
        # request and, combined with the single-threaded dev server, blocked
        # every other request (including /api/history polling) for the whole
        # pipeline + LLM call.
        snapshot, analysis = await run_analysis(symbol)
        return jsonify({
            "success": True,
            "snapshot": snapshot,
            "analysis": analysis,
        })
    except Exception as e:
        logger.exception(f"Analysis failed for {symbol}")
        return jsonify({"success": False, "error": str(e)}), 500


def _parse_snapshot_epoch(filename: str):
    """snapshot_<base>_<quote>_<YYYYMMDD>_<HHMMSS>.json -> epoch seconds (UTC)."""
    parts = filename.replace(".json", "").split("_")
    if len(parts) < 5:
        return None
    date_p, time_p = parts[-2], parts[-1]
    try:
        dt = datetime.strptime(f"{date_p}{time_p}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _parse_history_header(file_path: str, filename: str) -> dict:
    parts = filename.replace(".json", "").split("_")
    if len(parts) >= 5:
        symbol = f"{parts[1]}/{parts[2]}"
        date_p, time_p = parts[3], parts[4]
        timestamp = f"{date_p[:4]}-{date_p[4:6]}-{date_p[6:8]} {time_p[:2]}:{time_p[2:4]}:{time_p[4:6]}"
    else:
        symbol = "Unknown"
        timestamp = "Unknown"

    with open(file_path, "r") as f:
        data = json.load(f)
    strategies = data.get("strategies", {}) or {}
    bias = (strategies.get("trend_bias") or "neutral").upper()
    score = strategies.get("confluence_score", 0) or 0
    regime = data.get("market_regime", "Unknown")
    price = (
        data.get("smc_context", {}).get("15m", {}).get("current_price")
        or strategies.get("current_price")
        or 0.0
    )

    analysis = data.get("analysis") or {}
    action = (analysis.get("header") or {}).get("action")
    if not action:
        if score >= ACTIVE_TRADE_THRESHOLD:
            action = "ACTIVE_TRADE"
        elif score >= CONDITIONAL_ENTRY_THRESHOLD:
            action = "CONDITIONAL_ENTRY"
        else:
            action = "HOLD"

    file_time = _parse_snapshot_epoch(filename)
    if file_time is None:
        file_time = os.path.getmtime(file_path)

    return {
        "filename": filename,
        "symbol": symbol,
        "timestamp": timestamp,
        "bias": bias,
        "action": action,
        "score": score,
        "regime": regime,
        "price": price,
        "file_time": file_time,
    }


def _load_index() -> dict:
    if not os.path.exists(SNAPSHOT_INDEX_PATH):
        return {}
    try:
        with open(SNAPSHOT_INDEX_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(idx: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(SNAPSHOT_INDEX_PATH, "w") as f:
            json.dump(idx, f)
    except Exception as e:
        logger.warning(f"Failed to persist snapshot index: {e}")


def _history_from_index() -> list:
    """
    Self-healing header index (audit §5): `/api/history` used to re-parse
    every snapshot JSON on every call, which gets slow at 1000+ snapshots.
    The index caches header fields per filename; only NEW files since the
    last call are parsed, and entries for files retention has deleted are
    dropped automatically.
    """
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(SNAPSHOTS_DIR, "snapshot_*.json"))}
    idx = _load_index()
    changed = False

    for filename in list(idx.keys()):
        if filename not in on_disk:
            del idx[filename]
            changed = True

    for filename in on_disk:
        if filename in idx:
            continue
        file_path = os.path.join(SNAPSHOTS_DIR, filename)
        try:
            idx[filename] = _parse_history_header(file_path, filename)
            changed = True
        except Exception as e:
            logger.warning(f"Error parsing history file {filename}: {e}")

    if changed:
        _save_index(idx)

    return sorted(idx.values(), key=lambda x: x["file_time"], reverse=True)


@app.route("/api/history", methods=["GET"])
def get_history():
    return jsonify(_history_from_index())


@app.route("/api/history/<filename>", methods=["GET"])
def get_snapshot(filename):
    filename = os.path.basename(filename)
    file_path = os.path.join(SNAPSHOTS_DIR, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "Snapshot file not found"}), 404
    try:
        with open(file_path, "r") as f:
            snapshot_data = json.load(f)
        return jsonify({
            "snapshot": snapshot_data,
            "analysis": snapshot_data.get("analysis"),
        })
    except Exception as e:
        return jsonify({"error": f"Failed to load snapshot: {e}"}), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("\n" + "=" * 50)
    print("=== CRYPTERA v3.2 WEB TERMINAL RUNNING ===")
    print("Open http://127.0.0.1:5000 in your web browser.")
    print("=" * 50 + "\n")
    # debug=True (werkzeug debugger + auto-reloader) removed (BUG-23) — the
    # debugger allows arbitrary code execution if ever reachable off-localhost.
    # threaded=True so a long-running /api/run (pipeline + LLM call) doesn't
    # block concurrent requests like /api/history polling.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
