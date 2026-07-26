"""Corpus generation guard — the enforcement arm of CLAUDE.md invariant 5.

    "Never optimize against corrupted labels. After any fix that changes snapshot
     semantics, relabel snapshots and refit calibration before comparing metrics."

`schema_version` and `label_version` were being WRITTEN by the engine and the
labeller and read by NOTHING. Every tool globbed `snapshots/*.json` and pooled
whatever it found. So a single archived pre-fix snapshot left in (or restored
into) `snapshots/` would silently join the clean corpus and corrupt a calibration
fit — with no warning, and no way to tell afterwards. Measured on the real
pre-wave archive: 92 of 130 schema-3.3.1 rows were accepted by
`calibrate._iter_labelled` while the engine was at schema 3.9.0 / label 2.

This module makes generation explicit and, above all, LOUD. Silently dropping
rows would be nearly as bad as silently including them — an operator who cannot
see the exclusion cannot tell a version mismatch from an empty corpus.

Policy
------
* Snapshots are compatible when their schema **generation** (major.minor) matches
  the engine's. Patch bumps (3.3.0 -> 3.3.1) are non-shape changes and stay
  poolable; a minor bump means the snapshot SHAPE moved, which is exactly when
  pooling becomes wrong.
* Labelled rows must additionally carry the current `LABEL_VERSION`. A snapshot
  with an `outcome` but no `label_version` is a pre-B1.0 label (version 1) — the
  labeller only began stamping the field in B1.0.
* Every consumer reports what it excluded and why. `--allow-mixed-schema` is the
  deliberate, documented escape hatch for cross-version archaeology; it must
  never be used for a fit.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SCHEMA_VERSION, LABEL_VERSION  # noqa: E402


def schema_generation(version) -> str | None:
    """'3.9.0' -> '3.9'. None when absent/unparseable (pre-versioning or corrupt)."""
    if not version:
        return None
    parts = str(version).split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


CURRENT_GENERATION = schema_generation(SCHEMA_VERSION)


def snapshot_generation(snap: dict) -> str | None:
    return schema_generation((snap or {}).get("schema_version"))


def label_version_of(snap: dict):
    """The label generation of a snapshot, or None when it is unlabelled.

    An `outcome` block with no `label_version` key is a pre-B1.0 label — B1.0 is
    what introduced the field, so its absence is itself the version signal (=1),
    not missing data.
    """
    outcome = (snap or {}).get("outcome")
    if not isinstance(outcome, dict) or not outcome:
        return None
    return outcome.get("label_version", 1)


def is_current_schema(snap: dict) -> bool:
    return snapshot_generation(snap) == CURRENT_GENERATION


def is_current_label(snap: dict) -> bool:
    return label_version_of(snap) == LABEL_VERSION


class GenerationFilter:
    """Accept/reject snapshots by corpus generation, counting every rejection so the
    caller can report it. Stateless w.r.t. the filesystem — just a tally + predicate.

    `require_label` is for the fitting/metrics tools (calibrate, eval_engine); the
    display tools (status) only care about schema.
    """

    def __init__(self, allow_mixed: bool = False, require_label: bool = False):
        self.allow_mixed = bool(allow_mixed)
        self.require_label = bool(require_label)
        self.accepted = 0
        self.by_schema = {}      # generation (or '<none>') -> count rejected
        self.by_label = {}       # label version (or '<none>') -> count rejected

    def accept(self, snap: dict) -> bool:
        if self.allow_mixed:
            self.accepted += 1
            return True
        gen = snapshot_generation(snap)
        if gen != CURRENT_GENERATION:
            key = gen or "<none>"
            self.by_schema[key] = self.by_schema.get(key, 0) + 1
            return False
        if self.require_label and not is_current_label(snap):
            key = label_version_of(snap)
            key = "<unlabelled>" if key is None else str(key)
            self.by_label[key] = self.by_label.get(key, 0) + 1
            return False
        self.accepted += 1
        return True

    @property
    def rejected(self) -> int:
        return sum(self.by_schema.values()) + sum(self.by_label.values())

    def report(self) -> list:
        """Human-readable lines describing what was excluded. Empty when nothing was."""
        if self.allow_mixed:
            return ["[corpus] WARNING: --allow-mixed-schema is ON — snapshots from "
                    "DIFFERENT engine generations are being pooled. Never fit on this."]
        lines = []
        if self.by_schema:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_schema.items()))
            lines.append(
                f"[corpus] EXCLUDED {sum(self.by_schema.values())} snapshot(s) from other "
                f"schema generations (current {CURRENT_GENERATION}) — {detail}. "
                f"These carry different snapshot SEMANTICS and must not pool "
                f"(CLAUDE.md invariant 5); archive them out of snapshots/.")
        if self.by_label:
            detail = ", ".join(f"v{k}: {v}" for k, v in sorted(self.by_label.items()))
            lines.append(
                f"[corpus] EXCLUDED {sum(self.by_label.values())} snapshot(s) labelled by an "
                f"older labeller (current v{LABEL_VERSION}) — {detail}. "
                f"Re-run `python -m tools.label_snapshots --force` to bring them current.")
        return lines

    def print_report(self, stream=None):
        out = stream or sys.stdout
        for line in self.report():
            print(line, file=out)


def add_cli_flag(parser):
    """Shared `--allow-mixed-schema` opt-out for the corpus tools."""
    parser.add_argument(
        "--allow-mixed-schema", action="store_true",
        help="pool snapshots from other schema/label generations (cross-version "
             "archaeology ONLY — never for a calibration fit, see invariant 5)")
    return parser
