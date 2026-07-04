"""Embedded-aware routing + in-memory connection channel in ``ha_mcp.config``
(issue #1527).

The four Supervisor-check sites in ``config.py`` (``get_feature_flag_origin``,
``get_backup_setting_origin``, ``_apply_backup_overrides``,
``_apply_feature_flag_overrides``) were switched from a raw ``SUPERVISOR_TOKEN``
read to ``is_running_in_addon()`` so the in-process server — which carries
``SUPERVISOR_TOKEN`` on HAOS but is not a Supervisor add-on — is treated as a
standalone deployment (settings/backup edits persist to override files, not
routed to a non-existent add-on). Also covers ``set_embedded_connection`` — the
in-memory URL/token channel that keeps the admin token out of ``os.environ``.

Needs pydantic (``ha_mcp.config``); skipped in the hermetic local tier and run in
CI where the package is installed. The underlying ``is_running_in_addon()``
embedded-awareness itself is covered hermetically in ``test_version_embedded``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

config = pytest.importorskip("ha_mcp.config")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    config._reset_embedded_connection()
    yield
    # Drop the embedded connection AND the settings singleton so a test that
    # exercised get_global_settings() can't leak the in-memory url/token into a
    # later test's Settings.
    config._reset_embedded_connection()
    config._reset_global_settings()


def _a_backup_env_name() -> str:
    return config.BACKUP_OVERRIDE_FIELDS[0][1]


def _a_plain_bool_feature_flag_field() -> tuple[str, str]:
    """Return (field_name, env_name) of a non-master, non-beta bool feature flag.

    Non-beta fields take the unambiguous ``if in_addon and not is_beta: continue``
    skip in add-on mode, so their applied/skipped state cleanly distinguishes the
    embedded (applied) vs add-on (skipped) routing.
    """
    for field_name, env_name, ftype in config.FEATURE_FLAG_FIELDS:
        if (
            field_name == "enable_beta_features"
            or field_name in config.BETA_FEATURE_FIELDS
        ):
            continue
        if ftype is bool:
            return field_name, env_name
    raise AssertionError("no plain (non-master, non-beta) bool feature flag found")


def _a_plain_feature_flag_env_name() -> str:
    """An env name whose field is neither the master nor a beta sub-flag.

    Those take the direct ``return "addon"`` arm in add-on mode, which is the
    unambiguous branch to assert the embedded flip against.
    """
    for field_name, env_name, _ in config.FEATURE_FLAG_FIELDS:
        if field_name == "enable_beta_features":
            continue
        if field_name in config.BETA_FEATURE_FIELDS:
            continue
        return env_name
    raise AssertionError("no plain (non-master, non-beta) feature flag field found")


class TestBackupSettingOrigin:
    def test_addon_when_supervisor_only(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        assert config.get_backup_setting_origin(_a_backup_env_name()) == "addon"

    def test_not_addon_when_embedded(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        # No env var + no override file ⇒ "default"; the point is it is NOT
        # routed to the (non-existent) add-on.
        assert config.get_backup_setting_origin(_a_backup_env_name()) != "addon"


class TestFeatureFlagOrigin:
    def test_addon_when_supervisor_only(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        assert (
            config.get_feature_flag_origin(_a_plain_feature_flag_env_name()) == "addon"
        )

    def test_not_addon_when_embedded(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert (
            config.get_feature_flag_origin(_a_plain_feature_flag_env_name()) != "addon"
        )


class TestApplyBackupOverrides:
    def test_addon_short_circuits_without_reading_file(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        reader = MagicMock(return_value={})
        monkeypatch.setattr(config, "_read_backup_override_file", reader)
        config._apply_backup_overrides(MagicMock())
        reader.assert_not_called()

    def test_embedded_reads_override_file(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        reader = MagicMock(return_value={})
        monkeypatch.setattr(config, "_read_backup_override_file", reader)
        config._apply_backup_overrides(MagicMock())
        reader.assert_called_once()


class TestApplyFeatureFlagOverrides:
    """The 4th routed site: a non-beta override is applied in embedded mode but
    skipped in add-on mode (where start.py owns the env)."""

    def test_non_beta_override_applied_when_embedded(self, monkeypatch):
        field, env_name = _a_plain_bool_feature_flag_field()
        monkeypatch.delenv(env_name, raising=False)  # env-var-wins must not fire
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setattr(
            config, "_read_feature_flag_override_file", lambda: {field: True}
        )
        settings = MagicMock()
        settings.enable_beta_features = True  # keep the master gate from clearing
        config._apply_feature_flag_overrides(settings)
        assert getattr(settings, field) is True

    def test_non_beta_override_skipped_in_addon(self, monkeypatch):
        field, env_name = _a_plain_bool_feature_flag_field()
        monkeypatch.delenv(env_name, raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")  # add-on, not embedded
        monkeypatch.setattr(
            config, "_read_feature_flag_override_file", lambda: {field: True}
        )
        settings = MagicMock()
        settings.enable_beta_features = True
        sentinel = object()
        setattr(settings, field, sentinel)
        config._apply_feature_flag_overrides(settings)
        # Add-on mode short-circuits the non-beta field, leaving it untouched.
        assert getattr(settings, field) is sentinel


class TestEmbeddedConnection:
    """The in-memory URL/token channel: set_embedded_connection stages the
    loopback URL + admin token so they never pass through os.environ."""

    def test_set_then_apply_sets_url_and_token(self):
        config.set_embedded_connection("http://127.0.0.1:8123/", "tok123")
        settings = config.Settings(_env_file=None)
        config._apply_embedded_connection(settings)
        assert (
            settings.homeassistant_url == "http://127.0.0.1:8123"
        )  # trailing / trimmed
        assert settings.homeassistant_token == "tok123"

    def test_apply_is_noop_when_unregistered(self):
        settings = config.Settings(
            _env_file=None,
            HOMEASSISTANT_URL="http://ha.local:8123",
            HOMEASSISTANT_TOKEN="orig",
        )
        config._apply_embedded_connection(settings)  # nothing registered
        assert settings.homeassistant_token == "orig"

    def test_reaches_settings_and_survives_reset(self, tmp_path, monkeypatch):
        # The real runtime path: get_global_settings applies the in-memory
        # connection LAST, and it survives a settings-UI reset+rebuild — proving
        # the admin token reaches Settings without ever touching os.environ.
        monkeypatch.delenv("HOMEASSISTANT_URL", raising=False)
        monkeypatch.delenv("HOMEASSISTANT_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))

        config.set_embedded_connection("http://127.0.0.1:8123", "tok-mem")
        config._reset_global_settings()
        settings = config.get_global_settings()
        assert settings.homeassistant_url == "http://127.0.0.1:8123"
        assert settings.homeassistant_token == "tok-mem"

        # Settings-UI reset+rebuild re-applies the registered connection.
        config._reset_global_settings()
        rebuilt = config.get_global_settings()
        assert rebuilt.homeassistant_url == "http://127.0.0.1:8123"
        assert rebuilt.homeassistant_token == "tok-mem"

    def test_registration_after_singleton_already_built(self, tmp_path, monkeypatch):
        # Regression (live-found): importing ha_mcp runs the package's eager
        # import chain, and tools/smart_search/_config.py builds the settings
        # singleton AT IMPORT TIME — before the integration can possibly call
        # set_embedded_connection (it imports that function from the very
        # package whose import builds the singleton). Registration must
        # therefore retro-apply to an existing singleton, not only on build.
        monkeypatch.delenv("HOMEASSISTANT_URL", raising=False)
        monkeypatch.delenv("HOMEASSISTANT_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))

        config._reset_global_settings()
        stale = config.get_global_settings()  # built first, sentinel connection
        assert stale.homeassistant_url == config.OAUTH_MODE_URL

        config.set_embedded_connection("http://127.0.0.1:8123", "tok-late")

        # The ALREADY-BUILT singleton is patched in place...
        assert stale.homeassistant_url == "http://127.0.0.1:8123"
        assert stale.homeassistant_token == "tok-late"
        # ...and the cached accessor returns the same patched object.
        assert config.get_global_settings().homeassistant_url == (
            "http://127.0.0.1:8123"
        )
