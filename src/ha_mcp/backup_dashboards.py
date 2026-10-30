"""Auto-backup handlers for dashboards.

A dashboard snapshot, marked ``kind: "dashboard_settings"``, holds the
dashboard's ``lovelace/dashboards/list`` storage row beside its config body,
``{"dashboard": <row>, "config": <body>}``, so a restore can re-create a deleted
dashboard: Core's ``lovelace/config/save`` answers ``config_not_found`` for a
url_path it has no dashboard for. A restore reads the live list before any
write; a dashboard that still exists gets its config back, then its settings
through ``lovelace/dashboards/update``. The row is ``None`` for a dashboard the
list holds no storage row for (YAML, or a fresh install's default before Core
migrates it); an unreadable list fails the capture instead. The body is
``None`` for a dashboard whose config was never saved. A dashboard snapshot
without that ``kind`` predates the row and holds the body alone; it restores
and compares as the body only.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError

# Core's DASHBOARD_BASE_UPDATE_FIELDS: what lovelace/dashboards/update accepts.
_SETTINGS_FIELDS = ("title", "icon", "require_admin", "show_in_sidebar")
_DASHBOARD_KIND = "dashboard_settings"


def valid_dashboard_capture(config: Any) -> bool:
    """The shape a ``_DASHBOARD_KIND`` snapshot's config must have."""
    if not isinstance(config, dict) or set(config) != {"dashboard", "config"}:
        return False
    row = config["dashboard"]
    return row is None or (
        isinstance(row, dict) and isinstance(row.get("url_path"), str)
    )


def dashboard_snapshot(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """A snapshot's ``{"dashboard", "config"}`` value, and whether it is a
    legacy body-only snapshot (one written without ``_DASHBOARD_KIND``)."""
    if data.get("kind") == _DASHBOARD_KIND:
        return data["config"], False
    return {"dashboard": None, "config": data["config"]}, True


def _storage_row(
    dashboards: list[dict[str, Any]] | None, url_path: str
) -> dict[str, Any] | None:
    return next(
        (
            d
            for d in dashboards or []
            if d.get("url_path") == url_path and d.get("mode") == "storage"
        ),
        None,
    )


async def fetch_dashboard(client: Any, entity_id: str) -> Any:
    """Fetch a dashboard config via the same helper the get tool uses.

    The identifier is pre-resolved to its canonical url_path via the shared
    ``_resolve_dashboard`` (component ``list`` when available), then the config is
    read through the component ``get`` (one in-process frame) with a fall back to
    the legacy ``lovelace/config`` read (``_get_dashboard_config_internal``, which
    handles the WS envelope, force-cache-bypass, and structured error wrapping).
    The component refuses YAML bodies, so those capture through legacy unchanged.
    Imported lazily to avoid an import cycle.
    """
    from ha_mcp._vendor.fastmcp.exceptions import ToolError

    from .tools.dashboard_read_errors import UNKNOWN_CONFIG_PREFIX
    from .tools.tools_config_dashboards import (
        _component_dashboard_config,
        _get_dashboard_config_internal,
        _resolve_dashboard,
    )

    # The set/delete tools accept the canonical url_path, HA's internal
    # (underscored) dashboard id and the ``default`` alias for Core's
    # ``lovelace`` item. ``_get_dashboard_config_internal`` does NOT
    # lazy-resolve (an internal id 404s with "Unknown config specified" and
    # the snapshot is skipped), so pre-resolve to the form the tool targets.
    fetch_path = "lovelace" if entity_id == "default" else entity_id
    unread = (
        "Could not read lovelace/dashboards/list, so the dashboard's settings "
        "could not be captured"
    )
    try:
        match, dashboards = await _resolve_dashboard(client, fetch_path)
    except (HomeAssistantError, ToolError) as err:
        raise HomeAssistantError(unread) from err
    if dashboards is None:
        raise HomeAssistantError(unread)
    if match and match.get("url_path"):
        fetch_path = match["url_path"]

    row = _storage_row(dashboards, fetch_path)
    config = await _component_dashboard_config(client, fetch_path)
    if config is None:
        # Legacy read: YAML dashboards, no component, and not-found.
        try:
            config, _config_hash = await _get_dashboard_config_internal(
                client, fetch_path
            )
        except (ToolError, HomeAssistantError) as err:
            # Both carry HA's preserved ``config_not_found`` code: an unknown
            # url_path ("Unknown config specified", also the create path), or
            # a dashboard whose config was never saved, which keeps its row.
            msg = str(err).lower()
            if "not_found" not in msg and UNKNOWN_CONFIG_PREFIX.lower() not in msg:
                raise
            config = None
            if row is None:
                return None
    return {"dashboard": row, "config": config}


def _config_write(url_path: str | None, body: Any) -> dict[str, Any]:
    """Put back ``body``; ``None`` was "no stored config", which Core's
    ``lovelace/config/delete`` restores (it succeeds when nothing is stored).
    No ``url_path`` targets Core's default store."""
    message: dict[str, Any] = (
        {"type": "lovelace/config/delete"}
        if body is None
        else {"type": "lovelace/config/save", "config": body}
    )
    return message if url_path is None else {**message, "url_path": url_path}


async def _save(client: Any, url_path: str, config: Any) -> Any:
    return await _manager()._ws_send(client, _config_write(url_path, config))


async def _saved(client: Any, url_path: str, config: Any) -> bool:
    """Write; ``False`` when Core has no dashboard at ``url_path``."""
    from .tools.dashboard_read_errors import CONFIG_NOT_FOUND_CODE

    try:
        await _save(client, url_path, config)
    except HomeAssistantCommandError as err:
        if err.code != CONFIG_NOT_FOUND_CODE:
            raise
        return False
    return True


async def restore_dashboard(client: Any, entity_id: str, config: Any) -> Any:
    from .tools.tools_config_dashboards import _resolve_dashboard

    row = config["dashboard"]
    body = config["config"]
    if row is None:
        return await _restore_body(client, entity_id, body)
    url_path = row["url_path"]
    _match, dashboards = await _resolve_dashboard(client, url_path)
    if dashboards is None:
        raise HomeAssistantError(
            "Could not read lovelace/dashboards/list, so nothing was restored"
        )
    live = _storage_row(dashboards, url_path)
    if live is None:
        return await _recreate(client, row, body)
    await _save(client, url_path, body)
    return await _restore_settings(client, live["id"], row)


async def _recreate(client: Any, row: dict[str, Any], body: Any) -> Any:
    bm = _manager()
    url_path = row["url_path"]
    # The component's rows carry ``icon: None`` for an unset icon, which Core's
    # create schema rejects; an omitted field takes Core's default.
    fields = bm._strip_readonly(row, "id", "mode")
    create = {
        "type": "lovelace/dashboards/create",
        **{key: value for key, value in fields.items() if value is not None},
    }
    if "-" not in url_path:
        # Core's own single-word dashboards (``lovelace``, ``map``) need this.
        create["allow_single_word"] = True
    try:
        await bm._ws_send(client, create)
    except HomeAssistantCommandError as err:
        # Deleting ``lovelace`` re-registers Core's built-in panel there, so
        # create raises url_already_exists, sent as home_assistant_error.
        if url_path != "lovelace" or err.code != "home_assistant_error":
            raise
        return await _restore_default_store(client, body)
    if body is not None:
        await _save(client, url_path, body)
    return None


async def _restore_settings(
    client: Any, dashboard_id: str, row: dict[str, Any]
) -> None:
    """Put back the row's settings after its config; ``icon: None`` clears it."""
    bm = _manager()
    fields = {key: row[key] for key in _SETTINGS_FIELDS if key in row}
    # Core's own list omits ``icon`` when none is stored; the component's sends None.
    fields.setdefault("icon", None)
    update = {"type": "lovelace/dashboards/update", "dashboard_id": dashboard_id}
    try:
        await bm._ws_send(client, {**update, **fields})
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        raise bm.BackupRestoreError(
            f"The config of dashboard {row['url_path']} was restored, but its "
            "settings (title, icon, sidebar, admin) may not have been.",
            apply_status="unknown",
            reason="settings_not_restored",
        ) from err


async def _restore_default_store(client: Any, body: Any) -> dict[str, Any]:
    """Put the body in Core's default store, which the built-in panel reads."""
    if body is None:
        return {
            "note": "Home Assistant shows its auto-generated default dashboard "
            "at /lovelace again, as before; its saved title and icon are not "
            "restored."
        }
    await _manager()._ws_send(client, _config_write(None, body))
    return {
        "note": "The config was restored to Home Assistant's default dashboard "
        "store, shown at /lovelace. At the next restart Home Assistant moves it "
        "back into a 'lovelace' dashboard with its default title and icon."
    }


async def _restore_body(client: Any, entity_id: str, config: Any) -> Any:
    """Restore a snapshot's body alone to the dashboard it was taken from."""
    from .tools.tools_config_dashboards import _resolve_dashboard

    url_path = "lovelace" if entity_id == "default" else entity_id
    if await _saved(client, url_path, config):
        return None
    # The snapshot may name the dashboard by its internal id.
    match, dashboards = await _resolve_dashboard(client, url_path)
    if dashboards is None:
        raise HomeAssistantError("Could not read lovelace/dashboards/list")
    if match and match["url_path"] != url_path:
        return await _save(client, match["url_path"], config)
    raise _manager().BackupRestoreError(
        f"Dashboard {url_path} no longer exists, and this snapshot holds no "
        "dashboard settings to re-create it from. Nothing was changed.",
        reason="entry_deleted",
        suggestions=[
            f"Re-create it with ha_config_set_dashboard(url_path={url_path!r}, "
            "title=...), then restore this snapshot again."
        ],
    )


def dashboard_comparison(current: Any, *, legacy: bool) -> Any:
    """The live value to diff a snapshot against: a legacy snapshot compares
    with the live body."""
    if current is None or not legacy:
        return current
    return current["config"]
