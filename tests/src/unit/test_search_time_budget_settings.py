"""Regression tests for issue #1538 — the three smart-search config
time-budget knobs must be first-class, UI-tunable ``Settings`` fields
rather than direct ``os.environ`` reads that bypass ``config.py``.

Before #1538 these lived only as ``os.environ.get(...)`` reads inside
``tools/smart_search/_config.py``, so add-on users (who cannot set raw
env vars) had no way to tune them — the web Settings UI only surfaces
fields registered in ``config.py``. These tests lock in that they are
now registered ``Settings`` fields, rendered in the Advanced panel, and
resolved through the same env / override-file precedence as every other
advanced setting.
"""

import pytest

from ha_mcp.config import (
    _ADVANCED_SETTINGS_BOUNDS,
    ADVANCED_SETTINGS_FIELDS,
    Settings,
    _reset_global_settings,
    get_global_settings,
)

# (settings field name, env alias, default)
BUDGET_FIELDS = (
    ("automation_config_time_budget", "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET", 30.0),
    ("script_config_time_budget", "HAMCP_SCRIPT_CONFIG_TIME_BUDGET", 20.0),
    ("scene_config_time_budget", "HAMCP_SCENE_CONFIG_TIME_BUDGET", 20.0),
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


def _clear_budget_envs(monkeypatch):
    for _name, env, _default in BUDGET_FIELDS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
def test_budget_field_exists_with_alias_and_default(field, env, default):
    model_field = Settings.model_fields.get(field)
    assert model_field is not None, f"{field} must be a Settings field"
    assert model_field.alias == env
    settings = Settings()
    assert getattr(settings, field) == default
    assert isinstance(getattr(settings, field), float)


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
def test_budget_field_is_editable_float_in_advanced_search_section(field, env, default):
    row = next((r for r in ADVANCED_SETTINGS_FIELDS if r.field == field), None)
    assert row is not None, f"{field} must be in ADVANCED_SETTINGS_FIELDS"
    assert row.env == env
    assert row.ftype is float
    assert row.editable is True
    assert row.section == "search"
    # Numeric advanced fields must carry UI/POST bounds.
    assert field in _ADVANCED_SETTINGS_BOUNDS


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
def test_env_var_flows_to_resolved_settings(field, env, default, monkeypatch):
    _clear_budget_envs(monkeypatch)
    monkeypatch.setenv(env, "45")
    _reset_global_settings()
    try:
        assert getattr(get_global_settings(), field) == 45.0
    finally:
        _reset_global_settings()


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
def test_override_file_value_is_applied(
    field, env, default, isolated_data_dir, monkeypatch
):
    """Standalone (file) mode: the web UI persists advanced settings to
    ``feature_flags.json``; ``get_global_settings`` must apply them."""
    import json

    _clear_budget_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(json.dumps({field: 55.0}))
    _reset_global_settings()
    try:
        assert getattr(get_global_settings(), field) == 55.0
    finally:
        _reset_global_settings()


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
@pytest.mark.parametrize("bad_value", [9999.0, 0.0, -5.0])
def test_out_of_range_override_file_value_is_rejected(
    field, env, default, bad_value, isolated_data_dir, monkeypatch
):
    """An override-file value outside 1.0-600.0 is ignored; the field stays
    at its default (``_apply_advanced_overrides`` bounds check)."""
    import json

    _clear_budget_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({field: bad_value})
    )
    _reset_global_settings()
    try:
        assert getattr(get_global_settings(), field) == default
    finally:
        _reset_global_settings()


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
@pytest.mark.parametrize(
    "bad_value", ["not-a-number", "", "   ", "0", "-5", "9999", "nan", "inf"]
)
def test_invalid_or_out_of_range_env_value_falls_back_to_default(
    field, env, default, bad_value, monkeypatch
):
    """Lenient contract: an empty, unparseable, or out-of-range (incl.
    non-finite) env value falls back to the default rather than crashing
    startup or smuggling a budget past _ADVANCED_SETTINGS_BOUNDS. A ``<= 0``
    budget would silently disable the per-id config-fetch scan."""
    _clear_budget_envs(monkeypatch)
    monkeypatch.setenv(env, bad_value)
    assert getattr(Settings(), field) == default


@pytest.mark.parametrize("field,env,default", BUDGET_FIELDS)
@pytest.mark.parametrize("good_value", ["1", "45", "600"])
def test_in_range_env_value_is_honored(field, env, default, good_value, monkeypatch):
    """In-range env values (including the inclusive 1.0 / 600.0 bounds) are
    parsed and kept."""
    _clear_budget_envs(monkeypatch)
    monkeypatch.setenv(env, good_value)
    assert getattr(Settings(), field) == float(good_value)


def test_smart_search_constants_track_settings_field_defaults():
    """The module-level constants the smart-search mixins import must be
    sourced from ``Settings`` (issue #1538), not re-hardcoded.

    The constants are read once at import (restart-required by design — the
    consuming ``SmartSearchTools`` is a startup singleton), so this asserts
    against the static ``Settings`` field defaults rather than a live
    ``get_global_settings()`` value. That keeps the check deterministic
    regardless of any settings rebuild / caching elsewhere in the session,
    while still catching a constant that drifts away from the Settings
    default (e.g. someone re-hardcoding it)."""
    from ha_mcp.tools.smart_search import _config

    constants = {
        "automation_config_time_budget": _config.AUTOMATION_CONFIG_TIME_BUDGET,
        "script_config_time_budget": _config.SCRIPT_CONFIG_TIME_BUDGET,
        "scene_config_time_budget": _config.SCENE_CONFIG_TIME_BUDGET,
    }
    for field, _env, default in BUDGET_FIELDS:
        assert constants[field] == default
        assert constants[field] == Settings.model_fields[field].default
