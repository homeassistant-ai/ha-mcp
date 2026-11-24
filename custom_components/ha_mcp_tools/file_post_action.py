"""What Home Assistant needs after a file write or delete.

Core reads themes and custom templates once and keeps them until a reload,
serves a YAML dashboard from the file a ``lovelace: dashboards:`` entry names,
and serves ``www/`` at ``/local/`` only when the folder existed at startup.
The write_file and edit_file services report that here: they reload themes and
custom templates the way ``frontend.reload_themes`` and
``homeassistant.reload_custom_templates`` would, flag a file Core's template
loader skips or ``/local/`` does not serve yet, and name the YAML dashboards a
``dashboards/`` file backs. The delete_file service reloads the same way and
names the YAML dashboards and the ``/local/`` or ``/hacsfiles/`` dashboard
resources that still load the file.
"""

from __future__ import annotations

import logging
import os
import posixpath
from typing import Any
from urllib.parse import unquote, urlsplit

from homeassistant.core import HomeAssistant

from .custom_cards import _RESOURCE_PREFIXES

_LOGGER = logging.getLogger(__name__)

_YAML_SUFFIXES = (".yaml", ".yml")


async def reload_after_edit(
    hass: HomeAssistant, domain: str, service: str
) -> dict[str, Any]:
    """Call a reload service and report it as ``post_action``."""
    reload_service = f"{domain}.{service}"
    try:
        await hass.services.async_call(domain, service, {}, blocking=True)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("%s failed after a file change: %s", reload_service, err)
        return {
            "post_action": "reload_available",
            "reload_service": reload_service,
            "reload_error": str(err),
        }
    return {"post_action": "reload_performed", "reload_service": reload_service}


def _max_custom_template_size() -> int | None:
    """Core's size limit for a custom template, or None if Core no longer has it."""
    try:
        from homeassistant.helpers.template import MAX_CUSTOM_TEMPLATE_SIZE
    except ImportError:  # pragma: no cover - core drift
        return None
    return int(MAX_CUSTOM_TEMPLATE_SIZE)


async def _custom_template_action(
    hass: HomeAssistant, rel_path: str, size: int
) -> dict[str, Any]:
    # Core's loader: custom_templates/**/*.jinja, at most MAX_CUSTOM_TEMPLATE_SIZE.
    if not rel_path.endswith(".jinja"):
        return {
            "warnings": [
                f"Home Assistant loads only *.jinja files from custom_templates/, "
                f"so templates cannot import {rel_path}."
            ]
        }
    limit = _max_custom_template_size()
    if limit is not None and size > limit:
        return {
            "warnings": [
                f"{rel_path} is {size:,} bytes; Home Assistant skips custom "
                f"templates over {limit:,} bytes, so templates cannot import it."
            ]
        }
    return await reload_after_edit(hass, "homeassistant", "reload_custom_templates")


def _dashboards_using(
    hass: HomeAssistant, abs_path: str
) -> list[dict[str, Any]] | None:
    """Core's rows for the YAML dashboards served from ``abs_path``.

    None when Core's dashboards cannot be read, so nothing was checked.
    """
    from .websocket_api.dashboards import (
        _dashboard_list_rows,
        _lovelace_dashboards_map,
    )

    dashboards = _lovelace_dashboards_map(hass)
    if dashboards is None:
        return None
    wanted = os.path.normpath(abs_path)
    using = {
        url_path
        for url_path, dashboard in dashboards.items()
        if isinstance(getattr(dashboard, "path", None), str)
        and os.path.normpath(dashboard.path) == wanted
    }
    return [row for row in _dashboard_list_rows(dashboards) if row["url_path"] in using]


def _dashboard_action(hass: HomeAssistant, rel_path: str) -> dict[str, Any]:
    rows = _dashboards_using(hass, hass.config.path(rel_path))
    if rows is None:
        return {}
    if rows:
        # No reload: LovelaceYAML rereads the file once it is newer than its cache.
        return {"dashboards": rows}
    return {
        "warnings": [
            f"No YAML dashboard names {rel_path} as its file (files a dashboard "
            "!includes were not checked). A dashboard is added under lovelace: "
            "dashboards: (ha_config_set_yaml, yaml_path "
            "'lovelace.dashboards.<url_path>') and needs a restart."
        ]
    }


def _config_relative(hass: HomeAssistant, abs_path: str) -> str | None:
    """``abs_path`` relative to the config directory, or None outside it."""
    config_dir = os.path.normpath(hass.config.config_dir)
    try:
        relative = os.path.relpath(os.path.normpath(abs_path), config_dir)
    except ValueError:  # another drive on Windows
        return None
    if relative == os.curdir or relative.split(os.sep, 1)[0] == os.pardir:
        return None
    return relative


def _config_path(hass: HomeAssistant, rel_path: str) -> str | None:
    """``rel_path`` normalized and relative to the config directory.

    An absolute path counts only inside the config directory, by its relative
    path there: elsewhere it is a HAOS sibling volume or a custom directory,
    which Core does not load from (None).
    """
    if os.path.isabs(rel_path) or rel_path.startswith("/"):
        inside = _config_relative(hass, rel_path)
        if inside is None:
            return None
        rel_path = inside
    return posixpath.normpath(rel_path.replace("\\", "/"))


def _local_served(hass: HomeAssistant) -> bool:
    """Whether Core serves ``/local/``; True when its HTTP server is not set up."""
    http = getattr(hass, "http", None)
    return http is None or any(
        resource.canonical == "/local" for resource in http.app.router.resources()
    )


def _resource_config_path(url: Any) -> str | None:
    """The config-relative file a same-origin resource URL serves, else None.

    Reads the URL as Core's ``_async_warn_missing_local_resources`` reads
    ``/local/`` URLs, and maps ``/hacsfiles/`` as ``custom_cards`` does.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(unquote(parts.path))
    for prefix, folder in _RESOURCE_PREFIXES:
        if path.startswith(prefix):
            return f"{folder}/{path[len(prefix) :]}"
    return None


async def _resources_loading(hass: HomeAssistant, rel_path: str) -> list[str] | None:
    """URLs of the dashboard resources that load ``rel_path``.

    [] without a Lovelace resource collection; None when it could not be read.
    """
    from .websocket_api.dashboards import _lovelace_container

    resources = getattr(_lovelace_container(hass), "resources", None)
    if resources is None:
        return []
    try:
        await resources.async_get_info()  # loads a storage-mode collection
    except Exception:
        _LOGGER.warning("Could not read dashboard resources", exc_info=True)
        return None
    return [
        item["url"]
        for item in resources.async_items()
        if _resource_config_path(item.get("url")) == rel_path
    ]


async def after_file_write(
    hass: HomeAssistant, rel_path: str, size: int
) -> dict[str, Any]:
    """Response fields for a file written at ``rel_path``; empty when none apply."""
    normalized = _config_path(hass, rel_path)
    if normalized is None:
        return {}
    top = normalized.split("/", 1)[0]
    if top == "themes" and normalized.endswith(_YAML_SUFFIXES):
        return await reload_after_edit(hass, "frontend", "reload_themes")
    if top == "custom_templates":
        return await _custom_template_action(hass, normalized, size)
    if top == "dashboards" and normalized.endswith(_YAML_SUFFIXES):
        return _dashboard_action(hass, normalized)
    if top == "www" and not _local_served(hass):
        return {
            "warnings": [
                "Home Assistant serves www/ at /local/ only if the folder existed "
                f"when it started, so /local/{normalized[4:]} is not served until "
                "Home Assistant restarts."
            ]
        }
    return {}


async def after_file_delete(hass: HomeAssistant, rel_path: str) -> dict[str, Any]:
    """Response fields for a file deleted at ``rel_path``; empty when none apply."""
    normalized = _config_path(hass, rel_path)
    if normalized is None:
        return {}
    top = normalized.split("/", 1)[0]
    if top == "themes" and normalized.endswith(_YAML_SUFFIXES):
        return await reload_after_edit(hass, "frontend", "reload_themes")
    if top == "custom_templates" and normalized.endswith(".jinja"):
        return await reload_after_edit(hass, "homeassistant", "reload_custom_templates")
    if top == "dashboards" and normalized.endswith(_YAML_SUFFIXES):
        rows = _dashboards_using(hass, hass.config.path(normalized))
        if rows:
            names = ", ".join(str(row["url_path"]) for row in rows)
            return {
                "warnings": [
                    f"The YAML dashboards {names} name {normalized} as their file "
                    "and fail to load until it is restored or their lovelace: "
                    "dashboards: entry is removed."
                ]
            }
    if top == "www":
        urls = await _resources_loading(hass, normalized)
        if urls is None:
            return {
                "warnings": [
                    "The dashboard resources could not be read, so whether one "
                    f"still loads {normalized} is unknown; the Home Assistant log "
                    "has the error."
                ]
            }
        if urls:
            return {
                "warnings": [
                    f"The dashboard resources {', '.join(urls)} load {normalized} "
                    "and fail in the browser until it is restored or they are "
                    "removed (ha_config_delete_dashboard_resource)."
                ]
            }
    return {}
