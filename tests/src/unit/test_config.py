"""Unit tests for configuration handling."""

import json
import os
import subprocess
import sys

import pytest


def test_tool_security_policies_disabled_by_default():
    """enable_tool_security_policies defaults to False (opt-in for #966)."""
    from ha_mcp.config import Settings

    assert Settings().enable_tool_security_policies is False


@pytest.mark.slow
class TestConfigErrorHandling:
    """Test configuration error handling and user-friendly messages."""

    def test_missing_env_vars_shows_friendly_message(self):
        """When HOMEASSISTANT_URL and TOKEN are missing, show friendly error."""
        # Run ha-mcp without any env vars set
        env = os.environ.copy()
        # Remove any HA env vars that might be set
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should exit with error code
        assert result.returncode != 0

        # Should show friendly message, not raw stacktrace
        stderr = result.stderr
        assert "Configuration Error" in stderr
        assert "HOMEASSISTANT_URL" in stderr
        assert "HOMEASSISTANT_TOKEN" in stderr
        assert "Long-Lived Access Tokens" in stderr
        assert "github.com/homeassistant-ai/ha-mcp" in stderr

        # Should NOT show raw pydantic validation error
        assert "pydantic_core._pydantic_core.ValidationError" not in stderr
        assert "Field required [type=missing" not in stderr

    def test_missing_only_url_shows_that_var(self):
        """When only HOMEASSISTANT_URL is missing, show that in message."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HAMCP_ENV_FILE", None)
        env["HOMEASSISTANT_TOKEN"] = "test_token_value"

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "HOMEASSISTANT_URL" in result.stderr

    def test_missing_only_token_shows_that_var(self):
        """When only HOMEASSISTANT_TOKEN is missing, show that in message."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)
        env["HOMEASSISTANT_URL"] = "http://test.local:8123"

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "HOMEASSISTANT_TOKEN" in result.stderr

    def test_no_env_file_warning_removed(self):
        """No warning should be shown when .env file is missing."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should NOT contain the old noisy warning
        combined_output = result.stdout + result.stderr
        assert "[ENV] WARNING: No environment file found" not in combined_output

    def test_smoke_test_still_works(self):
        """Smoke test should work with dummy credentials."""
        env = os.environ.copy()
        # Smoke test sets its own dummy credentials
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp", "--smoke-test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0
        assert "SMOKE TEST PASSED" in result.stdout


def test_enable_beta_features_default_false(monkeypatch) -> None:
    """Master beta toggle is opt-in. Without env var or override file,
    Settings.enable_beta_features stays False (matches existing beta
    sub-flag defaults)."""
    monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
    from ha_mcp.config import Settings

    s = Settings()
    assert s.enable_beta_features is False


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point ``get_data_dir`` at a tmp dir so override-file tests don't
    bleed across cases.

    ``get_data_dir`` is ``@lru_cache(maxsize=1)`` — without clearing
    the cache, the first test bakes in whichever tmp_path the runner
    saw and every subsequent test reads from a stale path. Clear the
    cache both before AND after the test so the fixture is symmetric
    with `_reset_data_dir_cache` in ``test_settings_ui.py``.
    """
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    yield tmp_path
    get_data_dir.cache_clear()


def _clear_all_feature_envs(monkeypatch):
    """Delete every env var that any FEATURE_FLAG_FIELDS or
    ADVANCED_SETTINGS_FIELDS entry might read — guards against runner-
    level env leaks."""
    from ha_mcp.config import ADVANCED_SETTINGS_FIELDS, FEATURE_FLAG_FIELDS

    for _fname, ename, _ftype in FEATURE_FLAG_FIELDS:
        monkeypatch.delenv(ename, raising=False)
    for _fname, ename, *_ in ADVANCED_SETTINGS_FIELDS:
        monkeypatch.delenv(ename, raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


def test_master_off_forces_beta_subflags_false_even_with_file_overrides(
    isolated_data_dir, monkeypatch
) -> None:
    """beta-master OFF + file override saying yaml=True → yaml is forced
    False at runtime. This is the key #1164 contract: master is the gate."""
    _clear_all_feature_envs(monkeypatch)

    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps(
            {
                "enable_beta_features": False,
                "enable_yaml_config_editing": True,
                "enable_filesystem_tools": True,
            }
        )
    )

    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()

    assert s.enable_beta_features is False
    assert s.enable_yaml_config_editing is False
    assert s.enable_filesystem_tools is False


def test_master_on_allows_beta_subflag_file_overrides(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)

    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps(
            {
                "enable_beta_features": True,
                "enable_yaml_config_editing": True,
            }
        )
    )

    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()

    assert s.enable_beta_features is True
    assert s.enable_yaml_config_editing is True
    # Sub-flags not in the file stay at their default.
    assert s.enable_filesystem_tools is False


def test_master_env_var_off_overrides_subflag_env_var(monkeypatch) -> None:
    """ENABLE_YAML_CONFIG_EDITING=true alone is not enough — without
    ENABLE_BETA_FEATURES=true the master gate forces yaml false."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("ENABLE_BETA_FEATURES", raising=False)
    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "true")

    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()

    assert s.enable_beta_features is False
    assert s.enable_yaml_config_editing is False


def test_master_in_addon_mode_reads_override_file(
    isolated_data_dir, monkeypatch
) -> None:
    """Addon mode normally ignores feature_flags.json (start.py owns env
    vars from config.yaml). For the new beta fields, the file IS the
    source of truth — they're not in any addon's config.yaml schema."""
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token-for-test")

    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps(
            {
                "enable_beta_features": True,
                "enable_yaml_config_editing": True,
                # Non-beta field that should NOT be read in addon mode.
                "enable_tool_search": True,
            }
        )
    )

    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()

    assert s.enable_beta_features is True
    assert s.enable_yaml_config_editing is True
    # enable_tool_search is in FEATURE_FLAG_FIELDS but NOT in
    # BETA_FEATURE_FIELDS — addon mode still ignores it.
    assert s.enable_tool_search is False


def test_get_feature_flag_origin_beta_in_addon_mode_returns_file(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")

    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"enable_yaml_config_editing": True})
    )

    from ha_mcp.config import get_feature_flag_origin

    assert get_feature_flag_origin("ENABLE_YAML_CONFIG_EDITING") == "file"
    # Non-beta field still returns "addon" in addon mode.
    assert get_feature_flag_origin("ENABLE_TOOL_SEARCH") == "addon"


def test_get_feature_flag_origin_beta_master_default_in_addon(monkeypatch) -> None:
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")

    from ha_mcp.config import get_feature_flag_origin

    assert get_feature_flag_origin("ENABLE_BETA_FEATURES") == "default"


def test_get_feature_flag_origin_beta_master_env_set_in_addon_returns_addon(
    monkeypatch,
) -> None:
    """F.38 — dev addon path. start.py wrote ENABLE_BETA_FEATURES from
    /data/options.json (key is present in dev schema). The env-var-set
    + in-addon signal tells the web UI this is Supervisor-managed.
    """
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")

    from ha_mcp.config import get_feature_flag_origin

    assert get_feature_flag_origin("ENABLE_BETA_FEATURES") == "addon"


def test_get_feature_flag_origin_beta_master_env_set_standalone_returns_env(
    monkeypatch,
) -> None:
    """F.38 — standalone path. No SUPERVISOR_TOKEN, env var explicitly
    set (e.g. via docker -e or .env). Returns env-locked so the web UI
    surfaces "unset env var to edit here" copy."""
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")

    from ha_mcp.config import get_feature_flag_origin

    assert get_feature_flag_origin("ENABLE_BETA_FEATURES") == "env"


def test_get_feature_flag_origin_beta_master_file_override_in_addon(
    isolated_data_dir, monkeypatch
) -> None:
    """F.38 — stable addon path. Master not in stable schema, so
    start.py doesn't write the env var. With SUPERVISOR_TOKEN set but
    env unset, an override file value should win → origin='file'.
    """
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"enable_beta_features": True})
    )

    from ha_mcp.config import get_feature_flag_origin

    assert get_feature_flag_origin("ENABLE_BETA_FEATURES") == "file"


def test_advanced_override_applies_int_field(isolated_data_dir, monkeypatch) -> None:
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(json.dumps({"timeout": 90}))
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    assert get_global_settings().timeout == 90


def test_advanced_override_applies_str_field_with_choices(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"log_level": "DEBUG"})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    assert get_global_settings().log_level == "DEBUG"


def test_advanced_override_applies_float_field(isolated_data_dir, monkeypatch) -> None:
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"code_mode_max_duration": 60.0})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    # enable_beta_features is False default, but code_mode_max_duration
    # isn't a beta sub-flag (it's a numeric tuning param), so it
    # applies regardless.
    assert get_global_settings().code_mode_max_duration == 60.0


def test_advanced_override_skips_display_only_fields(
    isolated_data_dir, monkeypatch
) -> None:
    """Display-only fields (editable=False) MUST NOT be applied — guards
    against UI bug / hand-edited override file that bypasses chicken-
    and-egg safety check."""
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"homeassistant_url": "http://attacker"})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    assert get_global_settings().homeassistant_url != "http://attacker"


def test_advanced_override_rejects_out_of_bounds_int(
    isolated_data_dir, monkeypatch, caplog
) -> None:
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(json.dumps({"timeout": -5}))
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    # Out-of-bounds value silently ignored, field stays at default (30).
    assert get_global_settings().timeout == 30


def test_advanced_override_rejects_invalid_choice(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"log_level": "BANANAS"})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    # Invalid choice silently ignored, log_level stays at default (INFO).
    assert get_global_settings().log_level == "INFO"


def test_advanced_override_str_field_with_null_byte_rejected(
    isolated_data_dir, monkeypatch
) -> None:
    """str-typed advanced fields with embedded null bytes are rejected
    (defensive guard against filesystem-API confusion)."""
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"mcp_server_name": "foo\x00bar"})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()
    # Null-byte value silently ignored, field stays at default ("ha-mcp").
    assert s.mcp_server_name == "ha-mcp"


# Backward-compat tests (#1164 task 6.2b) — file from older version.


def test_old_override_file_with_subflag_no_master_force_false(
    isolated_data_dir, monkeypatch
) -> None:
    """User upgrades from pre-master ha-mcp; their feature_flags.json has
    enable_lite_docstrings=true. After upgrade, master defaults False
    and the sub-flag is gated off at runtime."""
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"enable_lite_docstrings": True})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()
    assert s.enable_beta_features is False
    assert s.enable_lite_docstrings is False  # forced off by master


def test_old_override_file_unknown_keys_ignored(isolated_data_dir, monkeypatch) -> None:
    """An override file from a future version with unknown keys must
    NOT crash Settings construction."""
    _clear_all_feature_envs(monkeypatch)
    (isolated_data_dir / "feature_flags.json").write_text(
        json.dumps({"future_flag_v99": True, "enable_beta_features": True})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()
    assert s.enable_beta_features is True


# Backup-override new fields (#1164) — coverage gaps caught by review.


def test_backup_override_dir_rejects_null_byte(isolated_data_dir, monkeypatch) -> None:
    """auto_backup_dir is a str field; values containing null bytes
    are rejected at apply time (defensive guard against filesystem
    confusion)."""
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.delenv("HAMCP_BACKUP_DIR", raising=False)
    (isolated_data_dir / "backup_settings.json").write_text(
        json.dumps({"auto_backup_dir": "/tmp/evil\x00bar"})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()
    assert s.auto_backup_dir == ""  # default — null-byte value silently dropped


def test_backup_override_lookahead_rejects_out_of_range(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.delenv("HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS", raising=False)
    (isolated_data_dir / "backup_settings.json").write_text(
        json.dumps({"auto_backup_calendar_lookahead_days": 9999})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    s = get_global_settings()
    assert s.auto_backup_calendar_lookahead_days == 7  # field default


def test_backup_override_lookahead_accepts_in_range(
    isolated_data_dir, monkeypatch
) -> None:
    _clear_all_feature_envs(monkeypatch)
    monkeypatch.delenv("HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS", raising=False)
    (isolated_data_dir / "backup_settings.json").write_text(
        json.dumps({"auto_backup_calendar_lookahead_days": 30})
    )
    from ha_mcp.config import _reset_global_settings, get_global_settings

    _reset_global_settings()
    assert get_global_settings().auto_backup_calendar_lookahead_days == 30
