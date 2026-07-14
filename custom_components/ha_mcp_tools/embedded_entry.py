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

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .const import (
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_UPDATE_COORDINATOR,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_WEBHOOK,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REGENERATE,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_LEGACY,
)

# NOTE: embedded_setup / coordinator (and their embedded_server / mcp_webhook
# chain) are imported lazily inside the entry lifecycle functions below, not at
# module top level. They pull in aiohttp and several homeassistant.* submodules
# (auth, requirements, util.package, components.http/webhook) that the
# entry-point wiring here never touches directly, so a top-level import would
# make importing this package require that whole stack — breaking hermetic unit
# tests that stub only the modules they use.

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
    from .coordinator import ServerVersionCoordinator
    from .embedded_setup import async_bring_up_server, async_maybe_auto_update
    from .ui_panel import async_register_ui_panel

    _ensure_secrets(hass, entry)
    # Bind the legacy OAuth root views synchronously here — before the slow
    # background bring-up below — so they are live at boot (see the helper).
    _prebind_legacy_oauth_views(hass, entry)

    # Admin-only "Open Web UI" sidebar panel + proxy. Registered while the entry
    # exists (its proxy returns 503 until the server is actually running), so the
    # user sees the panel immediately and it reflects the running state. Gated on
    # the sidebar-panel option; a change to it reloads the entry, and unload's
    # unconditional async_unregister_ui_panel then removes the panel this skips.
    if bool(entry.options.get(OPT_ENABLE_SIDEBAR_PANEL, True)):
        await async_register_ui_panel(hass)

    domain_data = hass.data.setdefault(DOMAIN, {})
    # Snapshot the options so the update listener reloads only on a genuine
    # options change — the background bring-up persists ids/token/pip spec to
    # entry.data, and those writes must not self-reload.
    domain_data[DATA_LAST_OPTIONS] = dict(entry.options)

    # Server-version visibility + automatic updates (issue #1760): the
    # coordinator polls PyPI on its own UPDATE_CHECK_INTERVAL regardless of the
    # auto_update option, backing the `update` platform entity forwarded below.
    # Its listener forwards every refresh to async_maybe_auto_update, which
    # decides whether to actually reload. Created and stored BEFORE the
    # bring-up task: bring-up's success path (_async_finish_update_cycle)
    # refreshes this coordinator, so it must already be in hass.data whenever
    # that task runs.
    coordinator = ServerVersionCoordinator(hass, entry)
    domain_data[DATA_UPDATE_COORDINATOR] = coordinator

    task = entry.async_create_background_task(
        hass, async_bring_up_server(hass, entry), f"{DOMAIN}_bring_up"
    )
    domain_data[DATA_BRINGUP_TASK] = task

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _on_version_update() -> None:
        # A reload must never run synchronously from inside this listener
        # callback: it would unload the UPDATE platform this very coordinator
        # drives (forwarded below), tearing the coordinator down mid-callback.
        #
        # hass-owned, NOT entry.async_create_background_task: entry background
        # tasks are cancelled by the very unload that async_maybe_auto_update's
        # reload performs, so an entry-owned task would cancel itself mid-reload
        # and leave the entry unloaded without ever setting back up (server down
        # until restart). The interval-timer wiring this replaces ran its checks
        # as plain hass jobs for the same reason.
        hass.async_create_background_task(
            async_maybe_auto_update(hass, entry, coordinator.data),
            f"{DOMAIN}_server_auto_update",
        )

    entry.async_on_unload(coordinator.async_add_listener(_on_version_update))

    # Background, not awaited: entry setup must not block on a PyPI round-trip
    # (this is why async_config_entry_first_refresh is NOT used here). The
    # coordinator reschedules itself on UPDATE_CHECK_INTERVAL after this first
    # refresh completes.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN}_server_version_refresh"
    )

    await hass.config_entries.async_forward_entry_setups(entry, [Platform.UPDATE])
    return True


async def async_unload_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the server + ingress webhook (reload-safe; keeps the provisioned token).

    Unloads the UPDATE platform first so the coordinator's entity is torn down
    before the coordinator itself is popped from hass.data, then cancels the
    bring-up task so a still-in-flight install/start is torn down before the
    explicit teardown runs.
    """
    from .embedded_setup import async_teardown_server  # lazy (see import note)
    from .ui_panel import async_unregister_ui_panel

    await hass.config_entries.async_unload_platforms(entry, [Platform.UPDATE])

    domain_data = hass.data.get(DOMAIN, {})
    task = domain_data.pop(DATA_BRINGUP_TASK, None)
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await async_teardown_server(hass)
    async_unregister_ui_panel(hass)
    domain_data.pop(DATA_LAST_OPTIONS, None)
    domain_data.pop(DATA_UPDATE_COORDINATOR, None)
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


def _prebind_legacy_oauth_views(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the legacy OAuth root ``/authorize`` + ``/token`` views during
    entry setup, before the background bring-up's (slow) package install.

    An aiohttp route is only ever live if it is registered before Home Assistant
    freezes its HTTP app at the end of startup. Binding these views from the
    background bring-up task races HA reaching RUNNING: on a slow-install boot
    the routes would register AFTER the freeze — never live until a restart —
    and ``bind_legacy_views`` would see ``hass.is_running`` True and file a
    restart repair that the restart cannot clear. Binding here, while the entry
    is still setting up (``hass.is_running`` is False at boot), mirrors the
    webhook-proxy add-on, which binds in its own ``async_setup_entry``. The
    bring-up's ``async_register_webhook`` then reuses this already-bound provider.

    Only relevant when legacy is the configured mode and the webhook endpoint is
    enabled (legacy OAuth guards that endpoint; with no webhook there is nothing
    to protect). A route-ownership conflict with the webhook-proxy add-on is
    swallowed here — the bring-up re-encounters it and files the user-facing
    start-failed repair.
    """
    if str(entry.options.get(OPT_WEBHOOK_AUTH, "")) != WEBHOOK_AUTH_LEGACY:
        return
    if not bool(entry.options.get(OPT_ENABLE_WEBHOOK, True)):
        return
    client_id = entry.data.get(DATA_OAUTH_CLIENT_ID)
    client_secret = entry.data.get(DATA_OAUTH_CLIENT_SECRET)
    signing_key = entry.data.get(DATA_OAUTH_SIGNING_KEY)
    if not (client_id and client_secret and signing_key):
        # _ensure_secrets mints these whenever legacy mode is configured; a gap
        # means a partial config — let the bring-up path surface it.
        return
    from .oauth_legacy import LegacyOAuthRouteConflict, bind_legacy_views

    with suppress(LegacyOAuthRouteConflict):
        bind_legacy_views(hass, client_id, client_secret, signing_key)


def _ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Generate + persist the stable webhook id and secret path on first setup.

    Both live in ``entry.data`` and stay stable across restarts so the connect
    URL never changes. Three owner-requested management paths, applied in
    priority order on every (re)load:

    1. ``regenerate_secrets`` option: mint fresh random values for BOTH and
       clear any overrides plus the flag itself (one-shot rotation - the old
       URL dies on this reload).
    2. Override options: a non-empty ``webhook_id_override`` /
       ``secret_path_override`` replaces the stored value (normalized: the
       secret path gets a leading ``/``).
    3. First setup: mint random values for whatever is still missing.

    When the configured webhook auth mode is legacy, the same three-path
    lifecycle additionally applies to the legacy OAuth client_id/client_secret
    (see :func:`_ensure_legacy_oauth_secrets`) — folded into this same
    read-mutate-write cycle so a single ``async_update_entry`` call covers
    every field that changed this load, including when BOTH a webhook secret
    regenerate and an OAuth credential regenerate are requested together.
    """
    data = dict(entry.data)
    options = dict(entry.options)
    changed = False

    if options.get(OPT_REGENERATE_SECRETS):
        data[DATA_WEBHOOK_ID] = f"mcp_{secrets.token_hex(16)}"
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        # One-shot: clear the flag AND the overrides so the fresh random
        # values stick (leaving an override set would re-apply it below on
        # the next reload, silently undoing the rotation).
        options[OPT_REGENERATE_SECRETS] = False
        options[OPT_WEBHOOK_ID_OVERRIDE] = ""
        options[OPT_SECRET_PATH_OVERRIDE] = ""
        changed = True
    else:
        webhook_override = str(options.get(OPT_WEBHOOK_ID_OVERRIDE) or "").strip()
        if webhook_override and data.get(DATA_WEBHOOK_ID) != webhook_override:
            data[DATA_WEBHOOK_ID] = webhook_override
            changed = True
        path_override = str(options.get(OPT_SECRET_PATH_OVERRIDE) or "").strip()
        if path_override:
            if not path_override.startswith("/"):
                path_override = f"/{path_override}"
            if data.get(DATA_SECRET_PATH) != path_override:
                data[DATA_SECRET_PATH] = path_override
                changed = True

    if not data.get(DATA_WEBHOOK_ID):
        data[DATA_WEBHOOK_ID] = f"mcp_{secrets.token_hex(16)}"
        changed = True
    if not data.get(DATA_SECRET_PATH):
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        changed = True

    if options.get(OPT_WEBHOOK_AUTH) == WEBHOOK_AUTH_LEGACY:
        changed = _ensure_legacy_oauth_secrets(data, options) or changed

    if changed:
        hass.config_entries.async_update_entry(entry, data=data, options=options)


def _ensure_legacy_oauth_secrets(data: dict, options: dict) -> bool:
    """Mint/persist/rotate the legacy OAuth mode's credentials into ``data``
    (mutated in place, along with ``options`` for the one-shot regenerate
    flag). Only called while the configured webhook auth mode is legacy —
    switching away leaves whatever was last minted in place, so switching
    back reuses it rather than silently rotating.

    Mirrors the ``OPT_REGENERATE_SECRETS`` shape above: ``OPT_OAUTH_REGENERATE``
    is one-shot, minting a fresh client_id/client_secret and clearing itself
    plus the two override fields. ``signing_key`` is NEVER rotated here —
    rotating the client_id already revokes every outstanding token, because
    the signed token payload carries ``cid`` (see
    ``oauth_legacy.LegacyOAuthProvider._validate_token``); the only way to
    invalidate the signing key itself is to remove and re-add the entry.

    Returns True if ``data``/``options`` were mutated.
    """
    changed = False
    if options.get(OPT_OAUTH_REGENERATE):
        data[DATA_OAUTH_CLIENT_ID] = f"hamcp-{secrets.token_hex(16)}"
        data[DATA_OAUTH_CLIENT_SECRET] = secrets.token_urlsafe(32)
        options[OPT_OAUTH_REGENERATE] = False
        options[OPT_OAUTH_CLIENT_ID] = ""
        options[OPT_OAUTH_CLIENT_SECRET] = ""
        changed = True
    else:
        client_id_override = str(options.get(OPT_OAUTH_CLIENT_ID) or "").strip()
        if client_id_override and data.get(DATA_OAUTH_CLIENT_ID) != client_id_override:
            data[DATA_OAUTH_CLIENT_ID] = client_id_override
            changed = True
        client_secret_override = str(options.get(OPT_OAUTH_CLIENT_SECRET) or "").strip()
        if (
            client_secret_override
            and data.get(DATA_OAUTH_CLIENT_SECRET) != client_secret_override
        ):
            data[DATA_OAUTH_CLIENT_SECRET] = client_secret_override
            changed = True

    if not data.get(DATA_OAUTH_CLIENT_ID):
        data[DATA_OAUTH_CLIENT_ID] = f"hamcp-{secrets.token_hex(16)}"
        changed = True
    if not data.get(DATA_OAUTH_CLIENT_SECRET):
        data[DATA_OAUTH_CLIENT_SECRET] = secrets.token_urlsafe(32)
        changed = True
    if not data.get(DATA_OAUTH_SIGNING_KEY):
        data[DATA_OAUTH_SIGNING_KEY] = secrets.token_hex(32)
        changed = True
    return changed
