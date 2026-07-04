"""Config-entry wiring for the in-process MCP server entry type (issue #1527).

Runs the full ha-mcp FastMCP server in-process inside Home Assistant and exposes
it remotely through a Home Assistant webhook. Creating the "server" config entry
starts the server; disabling the entry pauses it (HA calls
:func:`async_unload_server_entry` via the domain dispatcher in ``__init__``);
removing the entry revokes the provisioned credentials.

``__init__.async_setup_entry`` dispatches to these functions for the "server"
entry type; the "tools" services entry is handled separately. This module is
intentionally thin — the HA entry-point wiring only. The bring-up / teardown
orchestration lives in :mod:`embedded_setup`, and the server thread + webhook
ingress in :mod:`embedded_server` / :mod:`mcp_webhook`.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import (
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
)

# NOTE: embedded_setup (and its embedded_server / mcp_webhook chain) is imported
# lazily inside the entry lifecycle functions below, not at module top level. It
# pulls in aiohttp and several homeassistant.* submodules (auth, requirements,
# util.package, components.http/webhook) that the entry-point wiring here never
# touches directly, so a top-level import would make importing this package
# require that whole stack — breaking hermetic unit tests that stub only the
# modules they use.

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_setup_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the server entry: schedule the server bring-up as a background task.

    The bring-up (first pip install of the fastmcp tree, token provisioning,
    thread start, webhook registration) can take minutes, so it must not stall HA
    startup. It runs as a config-entry background task — automatically cancelled
    on unload. The secret webhook id and secret path are generated first, before
    the update listener is registered, so those ``entry.data`` writes never
    trigger a mid-setup reload.
    """
    # Imported lazily (see the import note) so the aiohttp / auth / requirements
    # chain is pulled in only when an entry is actually set up.
    from .embedded_setup import async_bring_up_server

    _ensure_secrets(hass, entry)

    domain_data = hass.data.setdefault(DOMAIN, {})
    # Snapshot the options so the update listener reloads only on a genuine
    # options change — the background bring-up persists ids/token/pip spec to
    # entry.data, and those writes must not self-reload.
    domain_data[DATA_LAST_OPTIONS] = dict(entry.options)

    task = entry.async_create_background_task(
        hass, async_bring_up_server(hass, entry), f"{DOMAIN}_bring_up"
    )
    domain_data[DATA_BRINGUP_TASK] = task

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the server + ingress webhook (reload-safe; keeps the provisioned token).

    Cancels the bring-up task first so a still-in-flight install/start is torn
    down before the explicit teardown runs.
    """
    from .embedded_setup import async_teardown_server  # lazy (see import note)

    domain_data = hass.data.get(DOMAIN, {})
    task = domain_data.pop(DATA_BRINGUP_TASK, None)
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await async_teardown_server(hass)
    domain_data.pop(DATA_LAST_OPTIONS, None)
    return True


async def async_remove_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke the provisioned credentials when the server config entry is removed."""
    from .embedded_setup import (  # lazy (see import note)
        async_revoke_credentials_on_remove,
    )

    await async_revoke_credentials_on_remove(hass, entry)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its OPTIONS change (port / auth / pip spec / URL).

    Ignores the ``entry.data`` writes the background bring-up performs (webhook
    id, secret path, provisioned token ids, last pip spec): those fire the same
    update listener but must not reload the entry.
    """
    domain_data = hass.data.get(DOMAIN, {})
    if domain_data.get(DATA_LAST_OPTIONS) == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Generate + persist the stable webhook id and secret path on first setup.

    Both live in ``entry.data`` and stay stable across restarts so the connect
    URL never changes.
    """
    data = dict(entry.data)
    changed = False
    if not data.get(DATA_WEBHOOK_ID):
        data[DATA_WEBHOOK_ID] = f"mcp_{secrets.token_hex(16)}"
        changed = True
    if not data.get(DATA_SECRET_PATH):
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=data)
