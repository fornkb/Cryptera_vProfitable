"""P0: the archived pre-fix corpus must never pool with the clean one (invariant 5).

`schema_version` and `label_version` were written by the engine/labeller and read by
NOTHING. Every tool globbed snapshots/*.json and fitted or measured whatever it found.
Measured against the real pre-wave archive (130 files, schema 3.3.1, pre-B1.0 labels):
`calibrate._iter_labelled` accepted 92 of them while the engine was at schema 3.9.0.

On deploy day the operator archives the old corpus and restarts clean. If one old file
survives — a stray copy, a restored backup, a half-finished move — the calibration fit
silently trains on the geometry the whole of Waves 1-3 existed to fix.
"""

import json

import pytest

import config
from tools import corpus


# --------------------------- generation parsing --------------------------- #

def test_generation_is_major_minor_not_patch():
    """A patch bump is a non-shape change and must stay poolable; a minor bump is
    exactly when the snapshot SHAPE moved and pooling becomes wrong."""
    assert corpus.schema_generation("3.9.0") == "3.9"
    assert corpus.schema_generation("3.9.7") == "3.9"
    assert corpus.schema_generation("3.3.1") == "3.3"
    assert corpus.schema_generation("4.0.0") == "4.0"
    assert corpus.schema_generation(None) is None
    assert corpus.schema_generation("3") is None
    assert corpus.schema_generation("") is None


def test_current_generation_tracks_config():
    assert corpus.CURRENT_GENERATION == corpus.schema_generation(config.SCHEMA_VERSION)


def test_missing_label_version_reads_as_generation_1():
    """B1.0 introduced the stamp, so its ABSENCE inside an outcome IS the version
    signal — not missing data. The real pre-wave archive is exactly this shape."""
    assert corpus.label_version_of({"outcome": {"bars_observed": 96}}) == 1
    assert corpus.label_version_of({"outcome": {"label_version": 2}}) == 2
    assert corpus.label_version_of({}) is None
    assert corpus.label_version_of({"outcome": {}}) is None


# --------------------------- the filter --------------------------- #

_ABSENT = object()


def _snap(schema=_ABSENT, label=None):
    """`schema=_ABSENT` -> current; `schema=None` -> the key is genuinely missing
    (a pre-versioning snapshot), which is a distinct case from the default."""
    s = {} if schema is None else {
        "schema_version": config.SCHEMA_VERSION if schema is _ABSENT else schema}
    if label is not None:
        s["outcome"] = {"label_version": label}
    return s


def test_filter_accepts_current_and_rejects_old_schema():
    f = corpus.GenerationFilter()
    assert f.accept(_snap()) is True
    assert f.accept(_snap(schema="3.3.1")) is False
    assert f.accept(_snap(schema=None)) is False
    assert f.accepted == 1 and f.rejected == 2
    assert f.by_schema == {"3.3": 1, "<none>": 1}


def test_filter_rejects_stale_labels_only_when_required():
    stale = _snap(label=1)
    assert corpus.GenerationFilter(require_label=False).accept(stale) is True
    f = corpus.GenerationFilter(require_label=True)
    assert f.accept(stale) is False
    assert f.by_label == {"1": 1}
    assert f.accept(_snap(label=config.LABEL_VERSION)) is True


def test_filter_reports_loudly_never_silently():
    """Silently dropping rows is nearly as bad as silently including them — an
    operator who cannot see the exclusion cannot tell it from an empty corpus."""
    f = corpus.GenerationFilter(require_label=True)
    f.accept(_snap(schema="3.3.1"))
    f.accept(_snap(label=1))
    lines = f.report()
    assert len(lines) == 2
    assert any("3.3" in ln and "EXCLUDED" in ln for ln in lines)
    assert any("label" in ln.lower() and "--force" in ln for ln in lines)
    assert corpus.GenerationFilter().report() == []


def test_allow_mixed_is_an_explicit_loud_opt_out():
    f = corpus.GenerationFilter(allow_mixed=True, require_label=True)
    assert f.accept(_snap(schema="1.0.0", label=1)) is True
    assert "WARNING" in f.report()[0] and "Never fit" in f.report()[0]


# --------------------------- consumers are actually wired --------------------------- #

def _write(dirpath, name, snap):
    p = dirpath / name
    p.write_text(json.dumps(snap))
    return p


def _labelled(schema, label, score=70):
    """A snapshot shaped enough to be fittable by calibrate."""
    snap = {
        "schema_version": schema,
        "symbol": "SOL/USDT",
        "generated_at": "2026-07-20T10:00:00Z",
        "strategies": {"confluence_score": score, "score_mode": "trend",
                       "confluence_breakdown_raw": {"c1_trend_alignment": 20},
                       "engine_trade": {"direction": "BUY", "entry": 100.0,
                                        "stop_loss": 99.0, "take_profit": 102.0}},
        "outcome": {"engine_trade_outcome": {"triggered": True, "outcome": "tp_first",
                                             "mfe_r": 2.0, "end_rr": 2.0}},
    }
    if label is not None:
        snap["outcome"]["label_version"] = label
    return snap


def test_calibrate_excludes_other_generations(tmp_path, monkeypatch):
    from tools import calibrate
    monkeypatch.setattr(calibrate, "SNAPSHOTS_DIR", str(tmp_path))
    _write(tmp_path, "snapshot_SOL_USDT_20260720_100000.json",
           _labelled(config.SCHEMA_VERSION, config.LABEL_VERSION))
    _write(tmp_path, "snapshot_SOL_USDT_20260710_100000.json", _labelled("3.3.1", None))
    _write(tmp_path, "snapshot_SOL_USDT_20260711_100000.json",
           _labelled(config.SCHEMA_VERSION, 1))

    f = corpus.GenerationFilter(require_label=True)
    rows = list(calibrate._iter_labelled(f))
    assert len(rows) == 1, "a fit pooled snapshots across engine generations"
    assert f.by_schema == {"3.3": 1} and f.by_label == {"1": 1}


def test_calibrate_default_filter_is_on(tmp_path, monkeypatch):
    """The guard must be the DEFAULT, not something a caller opts into."""
    from tools import calibrate
    monkeypatch.setattr(calibrate, "SNAPSHOTS_DIR", str(tmp_path))
    _write(tmp_path, "snapshot_SOL_USDT_20260710_100000.json", _labelled("3.3.1", None))
    assert list(calibrate._iter_labelled()) == []


def test_eval_engine_excludes_other_generations(tmp_path, monkeypatch):
    from tools import eval_engine
    monkeypatch.setattr(eval_engine, "SNAPSHOTS_DIR", str(tmp_path))
    _write(tmp_path, "snapshot_SOL_USDT_20260720_100000.json",
           _labelled(config.SCHEMA_VERSION, config.LABEL_VERSION))
    _write(tmp_path, "snapshot_SOL_USDT_20260710_100000.json", _labelled("3.3.1", None))
    assert len(list(eval_engine._iter_snapshots(None))) == 1
    # eval does NOT require a current label (it reports on whatever is labelled),
    # only a current schema.
    _write(tmp_path, "snapshot_SOL_USDT_20260711_100000.json",
           _labelled(config.SCHEMA_VERSION, 1))
    assert len(list(eval_engine._iter_snapshots(None))) == 2


def test_labeller_refuses_to_stamp_a_current_label_on_an_old_snapshot(tmp_path):
    """Labelling an old-schema snapshot makes a pre-fix row LOOK current to every
    downstream consumer — the pooling this guard exists to prevent."""
    from tools import label_snapshots
    p = _write(tmp_path, "snapshot_SOL_USDT_20260710_100000.json", _labelled("3.3.1", None))
    assert label_snapshots.label_snapshot_file(str(p), 24, force=True) == "skipped_schema"
    # untouched on disk
    assert "label_version" not in json.loads(p.read_text())["outcome"]


# --------------------------- against the REAL pre-wave archive --------------------------- #

REAL = pytest.importorskip("pathlib").Path(__file__).resolve().parent.parent / "snapshot_pre_waves"


@pytest.mark.skipif(not REAL.is_dir(), reason="pre-wave fixture archive not present")
def test_real_pre_wave_archive_is_rejected_wholesale(tmp_path, monkeypatch):
    """The actual failure this guards: 130 real schema-3.3.1 snapshots from the Azure
    collector, 92 of which calibrate would have fitted on before this change."""
    from tools import calibrate
    files = sorted(REAL.glob("*.json"))
    assert files, "fixture archive unexpectedly empty"

    monkeypatch.setattr(calibrate, "SNAPSHOTS_DIR", str(REAL))
    f = corpus.GenerationFilter(require_label=True)
    assert list(calibrate._iter_labelled(f)) == [], \
        "pre-wave corpus still reaches the calibration fit"
    assert f.accepted == 0
    assert f.rejected == len(files)
    assert set(f.by_schema) == {"3.3"}, f"unexpected generations: {f.by_schema}"

    # And it is still readable with the deliberate escape hatch (archaeology, never a fit).
    f2 = corpus.GenerationFilter(allow_mixed=True, require_label=True)
    assert len(list(calibrate._iter_labelled(f2))) > 0
