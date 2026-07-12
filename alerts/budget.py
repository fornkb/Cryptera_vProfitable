"""Persistent monitor state: daily Gemini budget (reset 00:00 UTC, reserve buffer) and
per-symbol last-alert state. Single JSON file, atomic writes, crash-safe. One monitor
process serializes Gemini calls, so a single in-flight lock is sufficient."""

import json
import os
import threading
from datetime import datetime, timezone


class State:
    def __init__(self, path):
        self.path = path
        self.call_lock = threading.Lock()      # <=1 Gemini call in flight
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)             # atomic -- survives a crash mid-write

    # ---- daily budget ----
    def _budget(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        b = self.data.get("_budget")
        if not b or b.get("utc_date") != today:
            b = {"utc_date": today, "used": 0}  # 00:00 UTC rollover
            self.data["_budget"] = b
        return b

    def budget_used(self):
        return self._budget()["used"]

    def can_spend(self, reserve, cap):
        return self._budget()["used"] < (cap - reserve)

    def spend(self):
        self._budget()["used"] += 1
        self.save()

    # ---- per-symbol last-alert state ----
    def sym(self, s):
        return self.data.get(s, {})

    def set_sym(self, s, d):
        self.data[s] = d
        self.save()
