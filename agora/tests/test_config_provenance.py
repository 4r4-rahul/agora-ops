"""
agora/tests/test_config_provenance.py — settings-regime provenance for ML.

A new config_version is recorded only when a behaviour-affecting tunable changes, with an auto-diff
of what changed, so the ML/analytics layer can segment trade outcomes by the settings they ran under.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

from agora.ops.config_provenance import (
    _TRACKED,
    current_config_version,
    record_config_version,
)


def _settings(**overrides):
    base = {k: 0 for k in _TRACKED}     # all tracked knobs present with a default
    base.update(overrides)
    return types.SimpleNamespace(db_path="x", **base)


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def test_first_record_is_version_1_initial():
    db = _db()
    v = record_config_version(db, _settings())
    assert v == 1
    c = sqlite3.connect(db)
    desc = c.execute("SELECT description FROM config_versions WHERE version=1").fetchone()[0]
    assert desc == "initial snapshot"


def test_unchanged_settings_keep_same_version():
    db = _db()
    v1 = record_config_version(db, _settings(max_risk_per_trade_dollars=400))
    v2 = record_config_version(db, _settings(max_risk_per_trade_dollars=400))   # identical
    assert v1 == v2 == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 1   # no dup row


def test_changed_setting_bumps_version_with_diff():
    db = _db()
    record_config_version(db, _settings(max_risk_per_trade_dollars=0))
    v2 = record_config_version(db, _settings(max_risk_per_trade_dollars=400))     # changed
    assert v2 == 2
    c = sqlite3.connect(db)
    desc = c.execute("SELECT description FROM config_versions WHERE version=2").fetchone()[0]
    assert "max_risk_per_trade_dollars" in desc and "0→400" in desc


def test_current_version_returns_latest():
    db = _db()
    assert current_config_version(db) == 0                       # none yet
    record_config_version(db, _settings(long_options_min_conviction=2))
    record_config_version(db, _settings(long_options_min_conviction=3))
    assert current_config_version(db) == 2


def test_error_safe_on_bad_path():
    assert record_config_version("/nonexistent/dir/x.db", _settings()) == 0
    assert current_config_version("/nonexistent/dir/x.db") == 0
