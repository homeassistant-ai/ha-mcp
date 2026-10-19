"""
Configuration management for Home Assistant MCP Server.

The ``Settings`` model, the runtime-editable registries and the override
loaders live in the ``config_*`` modules and are re-exported here. This
module holds the settings singleton and the embedded-mode connection.
"""

# isort: off
from ha_mcp.config_backup import (
    _apply_backup_overrides as _apply_backup_overrides,
    get_backup_setting_origin as get_backup_setting_origin,
)
from ha_mcp.config_overrides import (
    _BETA_GATE_LOGGED as _BETA_GATE_LOGGED,
    _apply_advanced_overrides as _apply_advanced_overrides,
    _apply_feature_flag_overrides as _apply_feature_flag_overrides,
    _coerce_advanced_override_value as _coerce_advanced_override_value,
    _read_feature_flag_override_file as _read_feature_flag_override_file,
    get_feature_flag_origin as get_feature_flag_origin,
)
from ha_mcp.config_registry import (
    _ADVANCED_SETTINGS_BOUNDS as _ADVANCED_SETTINGS_BOUNDS,
    _ADVANCED_SETTINGS_CHOICES as _ADVANCED_SETTINGS_CHOICES,
    _ADVANCED_SETTINGS_SENTINELS as _ADVANCED_SETTINGS_SENTINELS,
    _FEATURE_FLAG_INT_BOUNDS as _FEATURE_FLAG_INT_BOUNDS,
    _FEATURE_FLAG_OVERRIDE_FILENAME as _FEATURE_FLAG_OVERRIDE_FILENAME,
    ADDON_SYNCED_ADVANCED_FIELDS as ADDON_SYNCED_ADVANCED_FIELDS,
    ADVANCED_SETTINGS_FIELDS as ADVANCED_SETTINGS_FIELDS,
    BACKUP_OVERRIDE_FIELDS as BACKUP_OVERRIDE_FIELDS,
    BETA_FEATURE_FIELDS as BETA_FEATURE_FIELDS,
    FEATURE_FLAG_FIELDS as FEATURE_FLAG_FIELDS,
    BackupOverrideField as BackupOverrideField,
    FeatureFlagField as FeatureFlagField,
)
from ha_mcp.config_settings import (
    _PACKAGE_VERSION as _PACKAGE_VERSION,
    OAUTH_MODE_TOKEN as OAUTH_MODE_TOKEN,
    OAUTH_MODE_URL as OAUTH_MODE_URL,
    Settings as Settings,
    get_settings as get_settings,
)
# isort: on

__all__ = [
    "ADDON_SYNCED_ADVANCED_FIELDS",
    "ADVANCED_SETTINGS_FIELDS",
    "BACKUP_OVERRIDE_FIELDS",
    "BETA_FEATURE_FIELDS",
    "FEATURE_FLAG_FIELDS",
    "OAUTH_MODE_TOKEN",
    "OAUTH_MODE_URL",
    "_ADVANCED_SETTINGS_BOUNDS",
    "_ADVANCED_SETTINGS_CHOICES",
    "_ADVANCED_SETTINGS_SENTINELS",
    "_FEATURE_FLAG_INT_BOUNDS",
    "_FEATURE_FLAG_OVERRIDE_FILENAME",
    "_PACKAGE_VERSION",
    "BackupOverrideField",
    "FeatureFlagField",
    "Settings",
    "_coerce_advanced_override_value",
    "_read_feature_flag_override_file",
    "_reset_global_settings",
    "_settings",
    "get_backup_setting_origin",
    "get_embedded_config_dir",
    "get_feature_flag_origin",
    "get_global_settings",
    "get_settings",
    "parse_extra_yaml_write_keys",
    "reset_global_settings",
    "set_embedded_connection",
    "should_emit_llm_api_metadata",
]


def parse_extra_yaml_write_keys(settings: "Settings") -> list[str]:
    """Parse ``extra_yaml_write_keys`` into a clean key list (#1887).

    Whitespace and empty entries are dropped and the result is deduplicated
    and sorted, so the service payload is deterministic and unaffected by how
    the operator spaced the setting.

    The component's ``YAML_KEY_DENYLIST`` is NOT applied here: that floor
    lives component-side, in the layer that authorizes the write. Mirroring it
    would mean a second copy to keep in lockstep for no gain: a denied key sent
    on the wire is dropped there anyway.

    Lives in this module rather than next to the YAML tool because the backup
    restore path needs it too, and ``backup_manager`` importing a ``tools_*``
    module would add an import edge that binds ``tools_yaml_config``'s
    module-level names at restore time.
    """
    # Direct attribute access, matching ``_disabled_packages_keys``: a future
    # rename must raise loudly rather than silently return an empty list,
    # which would read as "the operator configured nothing".
    raw = settings.extra_yaml_write_keys or ""
    return sorted({segment.strip() for segment in raw.split(",") if segment.strip()})


# Global settings instance
_settings: Settings | None = None

# In-process (embedded) HA connection, set only when ha-mcp runs inside Home
# Assistant core via the ha_mcp_tools custom component's in-process server entry.
# The component hands the loopback URL + the administrator's token to ha-mcp
# THROUGH THIS DICT — never
# via os.environ — so the admin token can't be read from the shared HA process
# environment. Applied onto the Settings singleton in ``get_global_settings``.
_EMBEDDED_CONNECTION: dict[str, str | bool] = {}


def set_embedded_connection(
    url: str,
    token: str,
    verify_ssl: bool | None = None,
    config_dir: str | None = None,
    llm_api_enabled: bool | None = None,
) -> None:
    """Register the in-process HA connection for embedded mode.

    Embedded-mode only: called by the ha_mcp_tools custom component's in-process
    server entry inside its server worker thread, before the server is
    constructed, so the loopback URL
    and admin token reach ``Settings`` in memory instead of through ``os.environ``.
    The values survive ``_reset_global_settings()``: the settings-UI reset+rebuild
    path re-applies them on the next ``get_global_settings()`` call.

    Also applies to an ALREADY-BUILT singleton: importing ``ha_mcp`` runs the
    package's eager import chain, and ``tools/smart_search/_config.py`` builds
    the settings singleton at import time (its documented read-once budgets).
    Registration therefore cannot assume it runs before the first build — the
    integration imports this function from the very package whose import
    creates the singleton.

    ``verify_ssl`` lets the component disable certificate verification when it
    derives an ``https://127.0.0.1`` loopback URL from Home Assistant's SSL
    config (issue #1890): HA's certificate is issued for its hostname, never
    for 127.0.0.1, so verification on the loopback connection can only fail.
    ``None`` (the default, and what pre-#1890 components pass implicitly)
    leaves ``Settings.verify_ssl`` alone.

    ``config_dir`` is Home Assistant's own configuration directory (#2329).
    Embedded mode is its only writer and it is read back through
    :func:`get_embedded_config_dir` alone — deliberately NOT a ``Settings``
    field, so no env var or override file can point it anywhere. It lets an
    in-process server read a blueprint's on-disk YAML directly instead of
    routing the read through the component, and grants nothing an external
    server could not already reach.
    """
    _EMBEDDED_CONNECTION["url"] = url
    _EMBEDDED_CONNECTION["token"] = token
    if verify_ssl is None:
        _EMBEDDED_CONNECTION.pop("verify_ssl", None)
    else:
        _EMBEDDED_CONNECTION["verify_ssl"] = verify_ssl
    if config_dir is None:
        _EMBEDDED_CONNECTION.pop("config_dir", None)
    else:
        _EMBEDDED_CONNECTION["config_dir"] = config_dir
    if llm_api_enabled is None:
        _EMBEDDED_CONNECTION.pop("llm_api_enabled", None)
    else:
        _EMBEDDED_CONNECTION["llm_api_enabled"] = llm_api_enabled
    if _settings is not None:
        _apply_embedded_connection(_settings)


def get_embedded_config_dir() -> str | None:
    """Home Assistant's configuration directory, when running embedded.

    ``None`` outside embedded mode, and on an embedded install whose component
    predates the ``config_dir`` keyword. Consumers must treat it as an optional
    capability and fall back to their component / service path.
    """
    config_dir = _EMBEDDED_CONNECTION.get("config_dir")
    return config_dir if isinstance(config_dir, str) and config_dir else None


def should_emit_llm_api_metadata() -> bool:
    """Return whether ``tools/list`` needs the component's private metadata.

    Standalone and add-on clients have no consumer for ``_meta.ha_mcp``. An
    embedded server needs it only when the custom component exposes its LLM
    API. A missing flag means an older component, so preserve the historical
    embedded behavior until that component is upgraded.
    """
    if not _EMBEDDED_CONNECTION:
        return False
    enabled = _EMBEDDED_CONNECTION.get("llm_api_enabled")
    return enabled if isinstance(enabled, bool) else True


def _reset_embedded_connection() -> None:
    """Drop the registered embedded connection (test seam).

    Sibling to :func:`_reset_global_settings`; lets suites that exercise the
    in-process token channel isolate state between tests. Not used in production —
    the connection is registered once per worker thread and is meant to persist.
    """
    _EMBEDDED_CONNECTION.clear()


def _apply_embedded_connection(settings: "Settings") -> None:
    """Apply the in-process embedded HA connection (url/token/verify_ssl) if registered.

    No-op outside embedded mode. Plain ``setattr`` (``validate_assignment`` is off
    on ``Settings``, mirroring ``_apply_backup_overrides``), so the loopback URL
    and admin token are set in memory without ever passing through ``os.environ``.
    Applied last so it wins over any env/override-file connection values.
    """
    url = _EMBEDDED_CONNECTION.get("url")
    token = _EMBEDDED_CONNECTION.get("token")
    if isinstance(url, str) and url:
        settings.homeassistant_url = url.rstrip("/")
    if isinstance(token, str) and token:
        settings.homeassistant_token = token
    verify_ssl = _EMBEDDED_CONNECTION.get("verify_ssl")
    if isinstance(verify_ssl, bool):
        settings.verify_ssl = verify_ssl


def get_global_settings() -> Settings:
    """Get global settings instance (singleton pattern).

    Applies override files at first read so web-UI edits take effect
    on the next ``get_global_settings()`` call after
    ``_reset_global_settings()`` is called by the POST handler:

    - Feature flags persisted to ``<data_dir>/feature_flags.json``
    - Auto-backup settings persisted to ``<data_dir>/backup_settings.json``

    In embedded mode, the in-process HA connection registered via
    ``set_embedded_connection`` is applied last (so a settings-UI reset+rebuild
    re-picks it up).
    """
    global _settings
    if _settings is None:
        _settings = get_settings()
        _apply_feature_flag_overrides(_settings)
        _apply_backup_overrides(_settings)
        _apply_advanced_overrides(_settings)
        _apply_embedded_connection(_settings)
    return _settings


def reset_global_settings() -> None:
    """Public seam to drop the cached settings singleton.

    The in-process (embedded) server calls this at every start: a config-entry
    reload reuses the same Python process, so without an explicit reset the
    singleton built on the FIRST start would keep serving stale feature-flag /
    override values forever (the add-on gets fresh settings for free from its
    process restart).
    """
    _reset_global_settings()


def _reset_global_settings() -> None:
    """Drop the cached settings singleton.

    Test seam so suites that mutate env vars can force a re-read
    without reaching into module-private state. Also used by the
    feature-flag and auto-backup settings POST handlers to publish a
    freshly edited override file value to runtime consumers
    (``get_global_settings`` is the only documented read path; the
    ``@with_auto_backup`` decorator reads it per tool call).
    """
    global _settings
    _settings = None
    # Drop the gate-log dedup set too — once Settings has been
    # rebuilt, an operator who's re-investigating "why is my beta
    # tool off?" should see the next gate fire logged. This keeps
    # the dedup tight to the lifetime of one cached Settings.
    _BETA_GATE_LOGGED.clear()
