"""Regression tests for issue #1784 — the Attempt-C per-request timeout
and fetch batch size must be first-class, UI-tunable ``Settings`` fields
rather than constants hardcoded in ``tools/smart_search/_config.py``.

Before #1784 these two knobs were hardcoded (5.0 s / 10), so on HA
servers that serve the per-id config endpoint serially — where a batch's
tail requests queue past the per-request timeout while perfectly healthy
— no configuration could stop deep search from reporting nondeterministic
per-id "failures". These tests lock in that the knobs are now registered
``Settings`` fields, rendered in the Advanced panel, and resolved through
the same env / override-file precedence as every other advanced setting
(mirrors ``test_search_time_budget_settings.py`` for the #1538 budgets).
"""

import json

import pytest

from ha_mcp.config import (
    _ADVANCED_SETTINGS_BOUNDS,
    ADVANCED_SETTINGS_FIELDS,
    Settings,
    _reset_global_settings,
    get_global_settings,
)

# (settings field name, env alias, default, python type)
KNOB_FIELDS = (
    ("individual_config_timeout", "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT", 5.0, float),
    ("individual_fetch_batch_size", "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE", 10, int),
)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point ``get_data_dir`` at a tmp dir so override-file tests don't
    read the developer's real ``feature_flags.json``."""
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    yield tmp_path
    get_data_dir.cache_clear()


def _clear_knob_envs(monkeypatch):
    for _name, env, _default, _ftype in KNOB_FIELDS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


@pytest.mark.parametrize("field,env,default,ftype", KNOB_FIELDS)
def test_knob_field_exists_with_alias_and_default(field, env, default, ftype):
    model_field = Settings.model_fields.get(field)
    assert model_field is not None, f"{field} must be a Settings field"
    assert model_field.alias == env
    settings = Settings()
    assert getattr(settings, field) == default
    assert isinstance(getattr(settings, field), ftype)


@pytest.mark.parametrize("field,env,default,ftype", KNOB_FIELDS)
def test_knob_field_is_editable_in_advanced_search_section(field, env, default, ftype):
    row = next((r for r in ADVANCED_SETTINGS_FIELDS if r.field == field), None)
    assert row is not None, f"{field} must be in ADVANCED_SETTINGS_FIELDS"
    assert row.env == env
    assert row.ftype is ftype
    assert row.editable is True
    assert row.section == "search"
    # Numeric advanced fields must carry UI/POST bounds.
    assert field in _ADVANCED_SETTINGS_BOUNDS


@pytest.mark.parametrize("field,env,default,ftype", KNOB_FIELDS)
def test_env_var_flows_to_resolved_settings(field, env, default, ftype, monkeypatch):
    _clear_knob_envs(monkeypatch)
    monkeypatch.setenv(env, "3")
    _reset_global_settings()
    try:
        assert getattr(get_global_settings(), field) == ftype(3)
    finally:
        _reset_global_settings()


@pytest.mark.parametrize("field,env,default,ftype", KNOB_FIELDS)
def test_override_file_value_is_applied(
    field, env, default, ftype, isolated_data_dir, monkeypatch
):
    """Standalone (file) mode: the web UI persists advanced settings to
    ``feature_flags.json``; ``get_global_settings`` must apply them."""
    _clear_knob_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(json.dumps({field: 2}))
    _reset_global_settings()
    try:
        assert getattr(get_global_settings(), field) == ftype(2)
    finally:
        _reset_global_settings()


@pytest.mark.parametrize("field,env,default,ftype", KNOB_FIELDS)
@pytest.mark.parametrize(
    "bad_value", ["not-a-number", "", "   ", "0", "-5", "9999", "nan", "inf"]
)
def test_invalid_or_out_of_range_env_value_falls_back_to_default(
    field, env, default, ftype, bad_value, monkeypatch
):
    """Lenient contract (same as the #1538 budgets): an empty, unparseable,
    or out-of-range (incl. non-finite) env value falls back to the default
    rather than crashing startup. A ``<= 0`` timeout would fail every per-id
    fetch; a ``<= 0`` batch size would deadlock the batching loop."""
    _clear_knob_envs(monkeypatch)
    monkeypatch.setenv(env, bad_value)
    assert getattr(Settings(), field) == default


def test_fractional_batch_size_falls_back_to_default(monkeypatch):
    """The batch size is an int field: a fractional env value is rejected
    (fall back to default) rather than silently truncated."""
    _clear_knob_envs(monkeypatch)
    monkeypatch.setenv("HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE", "2.5")
    assert Settings().individual_fetch_batch_size == 10


@pytest.mark.parametrize(
    "env,field,good_value,expected",
    [
        ("HAMCP_INDIVIDUAL_CONFIG_TIMEOUT", "individual_config_timeout", "1", 1.0),
        ("HAMCP_INDIVIDUAL_CONFIG_TIMEOUT", "individual_config_timeout", "600", 600.0),
        ("HAMCP_INDIVIDUAL_CONFIG_TIMEOUT", "individual_config_timeout", "12.5", 12.5),
        ("HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE", "individual_fetch_batch_size", "1", 1),
        (
            "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE",
            "individual_fetch_batch_size",
            "100",
            100,
        ),
    ],
)
def test_in_range_env_value_is_honored(env, field, good_value, expected, monkeypatch):
    """In-range env values (including the inclusive bounds — batch size 1
    is exactly the serialized-server remedy from #1784) are parsed and
    kept."""
    _clear_knob_envs(monkeypatch)
    monkeypatch.setenv(env, good_value)
    assert getattr(Settings(), field) == expected


def test_smart_search_constants_track_settings_field_defaults():
    """The module-level constants the smart-search mixins import must be
    sourced from ``Settings`` (issue #1784), not re-hardcoded. Asserts
    against the static ``Settings`` field defaults (see the #1538 twin in
    ``test_search_time_budget_settings.py`` for the rationale)."""
    from ha_mcp.tools.smart_search import _config

    constants = {
        "individual_config_timeout": _config.INDIVIDUAL_CONFIG_TIMEOUT,
        "individual_fetch_batch_size": _config.INDIVIDUAL_FETCH_BATCH_SIZE,
    }
    for field, _env, default, _ftype in KNOB_FIELDS:
        assert constants[field] == default
        assert constants[field] == Settings.model_fields[field].default
