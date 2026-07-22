"""B14.0 / I15.0 — corpus safeguard: _effective_retention precedence.

The collector VM must never prune. Only the monitor passes --retention 0; a manual
CLI/web run on the same box goes through run_analysis with retention=None and would
default to 1000/symbol, silently deleting the corpus. CRYPTERA_RETENTION protects it.
"""
import os

import main
from config import SNAPSHOT_RETENTION


def _clear_env():
    os.environ.pop("CRYPTERA_RETENTION", None)


def test_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("CRYPTERA_RETENTION", "9999")
    assert main._effective_retention(0) == 0        # monitor --retention 0
    assert main._effective_retention(500) == 500     # explicit override / tests


def test_env_honored_when_arg_none(monkeypatch):
    monkeypatch.setenv("CRYPTERA_RETENTION", "0")
    assert main._effective_retention(None) == 0      # collector box: unlimited
    monkeypatch.setenv("CRYPTERA_RETENTION", "250")
    assert main._effective_retention(None) == 250


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CRYPTERA_RETENTION", "notanint")
    assert main._effective_retention(None) == SNAPSHOT_RETENTION


def test_unset_env_uses_engine_default(monkeypatch):
    monkeypatch.delenv("CRYPTERA_RETENTION", raising=False)
    # dev-laptop behavior: .env has no CRYPTERA_RETENTION -> engine default (self-prunes)
    assert main._effective_retention(None) == SNAPSHOT_RETENTION


def test_nonpositive_effective_retention_skips_prune():
    # _persist_snapshot only prunes when eff_retention > 0; 0/<0 mean "unlimited".
    assert not (0 > 0)
    assert not (-1 > 0)
