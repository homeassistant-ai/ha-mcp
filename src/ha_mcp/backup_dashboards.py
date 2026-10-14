"""Auto-backup handlers for dashboards.

A storage dashboard's snapshot holds its ``lovelace/dashboards/list`` row beside
its config body, ``{"dashboard": <row>, "config": <body>}``, so a restore can
re-create a deleted dashboard: Core's ``lovelace/config/save`` answers
``config_not_found`` for a url_path it has no dashboard for. A dashboard whose
config was never saved is captured with ``config: None``. A dashboard without
a storage row (YAML, or a fresh install's default before Core migrates it)
keeps the body-only shape, as do snapshots taken before the row was captured.
"""

from __future__ import annotations

import logging
from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError

logger = logging.getLogger(__name__)


def _snapshot_row(config: Any) -> dict[str, Any] | None:
    """The dashboard row of a snapshot that carries one, else ``None``."""
    if isinstance(config, dict) and set(config) == {"dashboard", "config"}:
        row = config["dashboard"]
        if isinstance(row, dict) and row.get("url_path"):
            return row
    return None


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
    dashboards: list[dict[str, Any]] | None = None
    try:
        match, dashboards = await _resolve_dashboard(client, fetch_path)
        if match and match.get("url_path"):
            fetch_path = match["url_path"]
    except (HomeAssistantError, ToolError) as err:
        # Resolver failure (transport/shape) — fall through with the
        # original identifier; the canonical form is often already correct.
        logger.debug(
            "Auto-backup: dashboard resolve failed for %r: %s — using as-is",
            entity_id,
            err,
        )

    row = next(
        (
            d
            for d in dashboards or []
            if d.get("url_path") == fetch_path and d.get("mode") == "storage"
        ),
        None,
    )
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
            if "not_found" not in msg and "unknown config" not in msg:
                raise
            config = None
            if row is None:
                return None
    return config if row is None else {"dashboard": row, "config": config}


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
    try:
        await _save(client, url_path, config)
    except HomeAssistantCommandError as err:
        if err.code != "config_not_found":
            raise
        return False
    return True


async def restore_dashboard(client: Any, entity_id: str, config: Any) -> Any:
    bm = _manager()
    row = _snapshot_row(config)
    if row is None:
        return await _restore_body(client, entity_id, config)
    url_path = row["url_path"]
    body = config["config"]
    if await _saved(client, url_path, body):
        return None
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
    """Restore a body-only snapshot to the dashboard it was taken from."""
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
        f"Dashboard {url_path} no longer exists, and this snapshot was taken "
        "before dashboard settings were saved with it, so it cannot re-create "
        "the dashboard. Nothing was changed.",
        reason="entry_deleted",
        suggestions=[
            f"Re-create it with ha_config_set_dashboard(url_path={url_path!r}, "
            "title=...), then restore this snapshot again."
        ],
    )


def dashboard_comparison(stored: Any, current: Any) -> Any:
    """The live value to diff a snapshot against: a body-only snapshot
    compares with the live body."""
    if _snapshot_row(stored) is None and _snapshot_row(current) is not None:
        return current["config"]
    return current
