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

import json
import threading
from pathlib import Path

import pytest

from ha_mcp import config
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()
    config.reset_global_settings()
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    config.reset_embedded_connection()
    yield
    # Drop the embedded connection AND the settings singleton so a test that
    # exercised get_global_settings() can't leak the in-memory url/token into a
    # later test's Settings.
    config.reset_embedded_connection()
    config.reset_global_settings()
    get_data_dir.cache_clear()


def _a_backup_env_name() -> str:
    return config.BACKUP_OVERRIDE_FIELDS[0][1]


def test_reset_waits_for_a_complete_settings_build(monkeypatch):
    """A reset cannot expose or return the singleton halfway through a build."""
    entered_override = threading.Event()
    release_override = threading.Event()
    reset_done = threading.Event()
    built = []
    real_apply = config._apply_feature_flag_overrides  # noqa: SLF001

    def held_apply(settings):
        entered_override.set()
        assert release_override.wait(5)
        real_apply(settings)
        settings.timeout = 77

    monkeypatch.setattr(config, "_apply_feature_flag_overrides", held_apply)
    config.reset_global_settings()
    builder = threading.Thread(
        target=lambda: built.append(config.get_global_settings())
    )
    builder.start()
    assert entered_override.wait(5)

    def reset() -> None:
        config.reset_global_settings()
        reset_done.set()

    resetter = threading.Thread(target=reset)
    resetter.start()
    assert not reset_done.wait(0.05)
    release_override.set()
    builder.join(5)
    resetter.join(5)

    assert not builder.is_alive()
    assert not resetter.is_alive()
    assert built[0].timeout == 77
    assert reset_done.is_set()


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


@pytest.mark.parametrize("embedded", [False, True])
def test_backup_override_follows_deployment_mode(monkeypatch, tmp_path, embedded):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    if embedded:
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    monkeypatch.delenv("AUTO_BACKUP_RETAIN_PER_ENTITY", raising=False)
    (tmp_path / "backup_settings.json").write_text(
        json.dumps({"auto_backup_retain_per_entity": 37})
    )
    reads = []
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    settings = config.get_global_settings()
    assert (settings.auto_backup_retain_per_entity == 37) is embedded
    assert ((tmp_path / "backup_settings.json") in reads) is embedded


@pytest.mark.parametrize("embedded", [False, True])
def test_feature_override_follows_deployment_mode(monkeypatch, tmp_path, embedded):
    field, env_name = _a_plain_bool_feature_flag_field()
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    if embedded:
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    default = getattr(config.get_global_settings(), field)
    (tmp_path / "feature_flags.json").write_text(json.dumps({field: not default}))
    config.reset_global_settings()
    assert getattr(config.get_global_settings(), field) is (
        not default if embedded else default
    )


class TestEmbeddedConnection:
    """The in-memory URL/token channel: set_embedded_connection stages the
    loopback URL + admin token so they never pass through os.environ."""

    def test_set_then_apply_sets_url_and_token(self):
        config.set_embedded_connection("http://127.0.0.1:8123/", "tok123")
        settings = config.get_global_settings()
        assert (
            settings.homeassistant_url == "http://127.0.0.1:8123"
        )  # trailing / trimmed
        assert settings.homeassistant_token == "tok123"

    def test_apply_is_noop_when_unregistered(self, monkeypatch):
        monkeypatch.setenv("HOMEASSISTANT_URL", "http://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "orig")
        settings = config.get_global_settings()
        assert settings.homeassistant_token == "orig"

    def test_reaches_settings_and_survives_reset(self, tmp_path, monkeypatch):
        # The real runtime path: get_global_settings applies the in-memory
        # connection LAST, and it survives a settings-UI reset+rebuild — proving
        # the admin token reaches Settings without ever touching os.environ.
        monkeypatch.delenv("HOMEASSISTANT_URL", raising=False)
        monkeypatch.delenv("HOMEASSISTANT_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))

        config.set_embedded_connection("http://127.0.0.1:8123", "tok-mem")
        config.reset_global_settings()
        settings = config.get_global_settings()
        assert settings.homeassistant_url == "http://127.0.0.1:8123"
        assert settings.homeassistant_token == "tok-mem"

        # Settings-UI reset+rebuild re-applies the registered connection.
        config.reset_global_settings()
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

        config.reset_global_settings()
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

    def test_verify_ssl_false_applies_to_settings(self):
        # Issue #1890: on an SSL-enabled HA the component derives an
        # https://127.0.0.1 loopback URL whose certificate can never verify
        # (issued for the hostname, not 127.0.0.1) — the component passes
        # verify_ssl=False alongside it.
        config.set_embedded_connection(
            "https://127.0.0.1:8123", "tok123", verify_ssl=False
        )
        settings = config.get_global_settings()
        assert settings.homeassistant_url == "https://127.0.0.1:8123"
        assert settings.verify_ssl is False

    def test_verify_ssl_default_leaves_settings_alone(self):
        # A pre-#1890 component (or a plaintext loopback) registers only
        # url+token; Settings.verify_ssl keeps its own resolution (default
        # True, or whatever HA_VERIFY_SSL/the override file said).
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123")
        settings = config.get_global_settings()
        assert settings.verify_ssl is True

    def test_verify_ssl_reregistration_without_flag_clears_it(self):
        # Re-registering without the flag (e.g. the entry reloads with the
        # override URL restored to plain http) must not keep forcing the old
        # verify_ssl=False onto rebuilt Settings.
        config.set_embedded_connection(
            "https://127.0.0.1:8123", "tok123", verify_ssl=False
        )
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123")
        settings = config.get_global_settings()
        assert settings.verify_ssl is True

    def test_config_dir_is_read_back_only_through_the_getter(self):
        # Issue #2329: the component hands over HA's own config directory so
        # the in-process server can read blueprint files directly. It is
        # deliberately NOT a Settings field — nothing env- or override-file
        # driven can point it anywhere — so the getter is the only reader.
        config.set_embedded_connection(
            "http://127.0.0.1:8123", "tok123", config_dir="/config"
        )
        assert config.get_embedded_config_dir() == "/config"
        settings = config.get_global_settings()
        assert not hasattr(settings, "embedded_config_dir")
        assert not hasattr(settings, "config_dir")

    def test_config_dir_absent_by_default(self):
        assert config.get_embedded_config_dir() is None
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123")
        assert config.get_embedded_config_dir() is None

    def test_config_dir_reregistration_without_it_clears_it(self):
        # An entry reload against an older component that no longer passes
        # config_dir must not keep serving the previous directory.
        config.set_embedded_connection(
            "http://127.0.0.1:8123", "tok123", config_dir="/config"
        )
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123")
        assert config.get_embedded_config_dir() is None

    def test_blank_config_dir_reads_as_none(self):
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123", config_dir="")
        assert config.get_embedded_config_dir() is None

    def test_llm_metadata_is_off_outside_embedded_mode(self):
        assert config.should_emit_llm_api_metadata() is False

    def test_old_component_keeps_embedded_llm_metadata(self):
        config.set_embedded_connection("http://127.0.0.1:8123", "tok123")
        assert config.should_emit_llm_api_metadata() is True

    @pytest.mark.parametrize("enabled", [True, False])
    def test_embedded_llm_metadata_follows_component_option(self, enabled):
        config.set_embedded_connection(
            "http://127.0.0.1:8123", "tok123", llm_api_enabled=enabled
        )
        assert config.should_emit_llm_api_metadata() is enabled
