"""Config-entry wiring for the in-process MCP server entry type (issue #1527).

Runs the full ha-mcp FastMCP server in-process inside Home Assistant and exposes
it remotely through a Home Assistant webhook. Creating the "server" config entry
starts the server; disabling the entry pauses it (HA calls
:func:`async_unload_server_entry` via the domain dispatcher in ``__init__``), and
so does the entry's access switch (:func:`async_set_access`, #2801) while the
entry stays loaded; removing the entry deletes the account and token an older
release provisioned.

``__init__.async_setup_entry`` dispatches to these functions for the "server"
entry type; the "tools" services entry is handled separately. This module is
intentionally thin — the HA entry-point wiring only. The bring-up / teardown
orchestration lives in :mod:`embedded_setup`, and the server thread + webhook
ingress in :mod:`embedded_server` / :mod:`mcp_webhook`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    DATA_ACCESS_ENABLED,
    DATA_ACCESS_LOCK,
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_MANAGER,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_STARTED_ENTRIES,
    DATA_UNLOADED_ENTRIES,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_ACCESS_OFF_AFTER_RESTART,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_WEBHOOK,
    OPT_OAUTH_ALLOW_ANY_CALLBACK,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_OAUTH_REGENERATE,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    SERVER_ENTRY_TITLE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from .entry_device import async_register_entry_device

# NOTE: embedded_setup (and its embedded_server / mcp_webhook
# chain), plus websocket_api, are imported lazily inside the entry lifecycle
# functions below, not at module top level. They pull in aiohttp, yaml and
# several homeassistant.* submodules (auth, requirements, util.package,
# components.http/webhook, components.websocket_api, half a dozen helpers) that
# the entry-point wiring here never touches directly, so a top-level import
# would make importing this package require that whole stack — breaking
# hermetic unit tests that stub only the modules they use.

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

# The access switch (#2801). Plain strings: Home Assistant accepts them, and
# the unit tests' ``homeassistant.const`` stubs carry no ``Platform``.
_PLATFORMS = ("switch",)


async def async_setup_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the server entry and, unless access is off, start the server.

    On the entry's first setup in a Home Assistant run,
    ``OPT_ACCESS_OFF_AFTER_RESTART`` turns access off before the switch platform
    is forwarded, so the switch starts out off. The bring-up (an override's pip
    install of the fastmcp tree, token checks, thread start, webhook
    registration) can take minutes, so it must not stall HA startup. It runs as
    a config-entry background task — automatically cancelled on unload. The
    secret webhook id and secret path are generated first, before
    the update listener is registered, so those ``entry.data`` writes never
    trigger a mid-setup reload. The ``ha_mcp_tools/*`` WebSocket command surface
    is registered up front, before any of that.
    """
    # Imported lazily (see the import note) so the aiohttp / auth / requirements
    # chain is pulled in only when an entry is actually set up.
    from .ui_panel import async_register_ui_panel
    from .websocket_api import async_register_commands

    # The ha_mcp_tools/* WS commands are entry-agnostic read/search machinery
    # over HA-core state (entity components, storage collections, lovelace,
    # registries) — nothing in them reads the tools entry's hass.data, and HA
    # core authenticates the connection while @require_admin gates each command.
    # So the server entry registers them too (#2289): a server-entry-only
    # install used to have no ha_mcp_tools/* commands at all, silently dropping
    # every ha_search to the legacy path. Only the filesystem/YAML HA *services*
    # stay tools-entry-only. Registered first, before anything that can fail or
    # await, so a broken bring-up later never costs the install this surface;
    # registration is idempotent, so a dual-entry install re-registering is
    # harmless.
    async_register_commands(hass)

    _ensure_secrets(hass, entry)
    # Bind every applicable OAuth route synchronously here — before the slow
    # background bring-up below — so it is live at boot (see the helper).
    _prebind_oauth_views(hass, entry)

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

    # The server package arrives with the component release HACS delivers
    # (the manifest pins it), so the server update entity, its PyPI poll and
    # the automatic reinstall are gone. Drop the entity they left behind.
    _remove_retired_update_entity(hass, entry)
    await async_register_entry_device(
        hass, entry, name=SERVER_ENTRY_TITLE, model="ha-mcp (in-process server)"
    )

    async with _access_lock(hass):
        domain_data.setdefault(DATA_UNLOADED_ENTRIES, set()).discard(entry.entry_id)
        started = domain_data.setdefault(DATA_STARTED_ENTRIES, set())
        if entry.entry_id not in started:
            started.add(entry.entry_id)
            if entry.options.get(OPT_ACCESS_OFF_AFTER_RESTART):
                _store_access(hass, entry, False)
        # Before the switch exists. Nothing should be live here: the unload
        # cancels the bring-up, and a switch-on queued behind it starts nothing.
        # The _server_live check is a defensive guard against a second start.
        if access_enabled(entry) and not _server_live(hass):
            _schedule_bring_up(hass, entry, announce=True)
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the server + ingress webhook (reload-safe; keeps the credentials)."""
    from .ui_panel import async_unregister_ui_panel

    async with _access_lock(hass):
        domain_data = hass.data.setdefault(DOMAIN, {})
        # A switch-on still waiting for the lock must not start a server for an
        # entry that is no longer loaded.
        domain_data.setdefault(DATA_UNLOADED_ENTRIES, set()).add(entry.entry_id)
        try:
            unloaded = await hass.config_entries.async_unload_platforms(
                entry, _PLATFORMS
            )
        finally:
            await _async_stop_server(hass)
            async_unregister_ui_panel(hass)
            domain_data.pop(DATA_LAST_OPTIONS, None)
    return bool(unloaded)


def access_enabled(entry: ConfigEntry) -> bool:
    """Whether the server's access switch (#2801) is on."""
    return bool(entry.data.get(DATA_ACCESS_ENABLED, True))


async def async_set_access(
    hass: HomeAssistant, entry: ConfigEntry, enabled: bool
) -> None:
    """Open or close every way into the server while the entry stays loaded.

    Off runs the same stop as an unload, so the webhook with its ``/readonly``
    alias, the direct port and the conversation-agent LLM API close together;
    it always runs, so turning off again runs the stop again. On while on
    starts the server again only when nothing is running, which retries a
    failed start. The switch cannot just disable the entry: it belongs to the
    entry and would unload with it, leaving nothing to turn access back on.
    """
    async with _access_lock(hass):
        if not enabled:
            _store_access(hass, entry, False)
            await _async_stop_server(hass)
            return
        if entry.entry_id in hass.data[DOMAIN].get(DATA_UNLOADED_ENTRIES, ()):
            return
        if _server_live(hass):
            return
        # Clears what a failed or crashed start left registered.
        await _async_stop_server(hass)
        _store_access(hass, entry, True)
        _schedule_bring_up(hass, entry, announce=False)


def _access_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Serialize the entry's setup, its unload and the access switch: a stop can
    take seconds, and a start overlapping it would claim the port while the old
    server still holds it."""
    lock: asyncio.Lock = hass.data.setdefault(DOMAIN, {}).setdefault(
        DATA_ACCESS_LOCK, asyncio.Lock()
    )
    return lock


def bring_up_in_progress(hass: HomeAssistant) -> bool:
    task = hass.data.get(DOMAIN, {}).get(DATA_BRINGUP_TASK)
    return task is not None and not task.done()


def server_running(hass: HomeAssistant) -> bool:
    from .embedded_server import EmbeddedServerManager  # lazy (see import note)

    manager = hass.data.get(DOMAIN, {}).get(DATA_MANAGER)
    return isinstance(manager, EmbeddedServerManager) and manager.is_running


def _server_live(hass: HomeAssistant) -> bool:
    return bring_up_in_progress(hass) or server_running(hass)


def _store_access(hass: HomeAssistant, entry: ConfigEntry, enabled: bool) -> None:
    # entry.data only: _async_options_updated compares options, so no reload.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, DATA_ACCESS_ENABLED: enabled}
    )


def _schedule_bring_up(
    hass: HomeAssistant, entry: ConfigEntry, *, announce: bool
) -> None:
    """Start the server as an entry background task, cancelled on unload."""
    from .embedded_setup import async_bring_up_server  # lazy (see import note)

    hass.data.setdefault(DOMAIN, {})[DATA_BRINGUP_TASK] = (
        entry.async_create_background_task(
            hass,
            async_bring_up_server(hass, entry, announce=announce),
            f"{DOMAIN}_bring_up",
        )
    )


async def _async_stop_server(hass: HomeAssistant) -> None:
    """Cancel an in-flight bring-up, then tear the server down.

    A bare teardown would let a bring-up still running re-register the webhook
    and the LLM API after it.
    """
    from .embedded_setup import async_teardown_server  # lazy (see import note)

    task = hass.data.get(DOMAIN, {}).pop(DATA_BRINGUP_TASK, None)
    try:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        await async_teardown_server(hass)


def _remove_retired_update_entity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the server update entity that component 2.x registered.

    Its platform no longer exists, so without this the registry keeps a
    permanently unavailable "HA-MCP server" update entity on every upgraded
    install.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "update", DOMAIN, f"{entry.entry_id}_server_update"
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


async def async_remove_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release the credentials an older release provisioned when the entry is removed."""
    from .embedded_setup import (  # lazy (see import note)
        async_revoke_credentials_on_remove,
    )

    await async_revoke_credentials_on_remove(hass, entry)


_NO_RELOAD_OPTIONS = frozenset(
    {
        OPT_OAUTH_REDIRECT_ALLOWLIST,
        OPT_OAUTH_ALLOW_ANY_CALLBACK,
        OPT_ACCESS_OFF_AFTER_RESTART,
    }
)


def _reload_relevant(options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if options is None:
        return None
    return {k: v for k, v in options.items() if k not in _NO_RELOAD_OPTIONS}


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its OPTIONS change (port / auth / pip spec / URL).

    Ignores the ``entry.data`` writes the background bring-up performs (webhook
    id, secret path, an adopted token, last pip spec): those fire the same
    update listener but must not reload the entry. Options read per request
    (the callback allowlist) or only at the entry's first setup in a run (access
    off after a restart) need no reload either.
    """
    domain_data = hass.data.get(DOMAIN, {})
    if _reload_relevant(domain_data.get(DATA_LAST_OPTIONS)) == _reload_relevant(
        entry.options
    ):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _prebind_oauth_views(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register OAuth routes before the background bring-up's slow install.

    An aiohttp route is only ever live if it is registered before Home Assistant
    freezes its HTTP app at the end of startup. Binding these views from the
    background bring-up task races HA reaching RUNNING: on a slow-install boot
    the advertised metadata, scoped authorize/token, and DCR routes would never
    become live until a restart. Bind the shared metadata/scoped pair for every
    remote mode, DCR for none/ha_auth, and legacy's root aliases when credentials
    are available. The later webhook registration reuses these bound views and
    supplies the live per-request providers through ``hass.data``.

    A legacy root-route ownership conflict with the webhook-proxy add-on is
    swallowed here; the scoped pair remains live and background bring-up selects
    its scoped-only provider.
    """
    if not bool(entry.options.get(OPT_ENABLE_WEBHOOK, True)):
        return
    # Same default bring-up applies (embedded_setup.async_bring_up_server).
    auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
    if auth_mode not in (
        WEBHOOK_AUTH_NONE,
        WEBHOOK_AUTH_HA,
        WEBHOOK_AUTH_LEGACY,
    ):
        return

    from .mcp_webhook import _register_metadata_views
    from .oauth_autoapprove import bind_autoapprove_views

    _register_metadata_views(hass)
    bind_autoapprove_views(hass)

    if auth_mode != WEBHOOK_AUTH_LEGACY:
        from .oauth_dcr import bind_dcr_view

        bind_dcr_view(hass)
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
    plus the two override fields.

    ANY credential change — regenerate, a client_id override, or a
    client_secret override — also rotates ``signing_key``, making every
    rotation a hard revocation of outstanding tokens (they take effect at the
    restart that rebinds the views). Rotating the key on a secret change is
    load-bearing (token validation never involves the secret, so the cid
    claim alone would not evict anything). Rotating it on a client_id change
    is defence in depth: the cid claim already evicts on a normal rotation,
    but without a fresh key an ``A → B → A`` client_id sequence would
    resurrect id-A's still-unexpired tokens, since they re-match the cid
    claim under the unchanged-key HMAC. Rotating the key kills that echo at
    zero cost. Rotation does NOT shorten the pre-restart window — the bound
    views keep serving the old identity until the restart (see
    ``oauth_legacy.bind_legacy_views``) — which is why the Configure screen and
    the startup log say rotated credentials take effect after a restart (via
    ``oauth_legacy.legacy_credentials_active``; review findings on #1880).

    Returns True if ``data``/``options`` were mutated.
    """
    changed = False
    if options.get(OPT_OAUTH_REGENERATE):
        data[DATA_OAUTH_CLIENT_ID] = f"hamcp-{secrets.token_hex(16)}"
        data[DATA_OAUTH_CLIENT_SECRET] = secrets.token_urlsafe(32)
        # Every credential change rotates the key — see docstring (kills the
        # A->B->A client_id resurrection; regenerate mints random ids so it
        # can't recur to a former id, but the key rotation is kept uniform).
        data[DATA_OAUTH_SIGNING_KEY] = secrets.token_hex(32)
        options[OPT_OAUTH_REGENERATE] = False
        options[OPT_OAUTH_CLIENT_ID] = ""
        options[OPT_OAUTH_CLIENT_SECRET] = ""
        changed = True
    else:
        client_id_override = str(options.get(OPT_OAUTH_CLIENT_ID) or "").strip()
        if client_id_override and data.get(DATA_OAUTH_CLIENT_ID) != client_id_override:
            data[DATA_OAUTH_CLIENT_ID] = client_id_override
            # Rotate the key so a re-used former client_id can't resurrect its
            # old tokens (see docstring).
            data[DATA_OAUTH_SIGNING_KEY] = secrets.token_hex(32)
            changed = True
        client_secret_override = str(options.get(OPT_OAUTH_CLIENT_SECRET) or "").strip()
        if (
            client_secret_override
            and data.get(DATA_OAUTH_CLIENT_SECRET) != client_secret_override
        ):
            data[DATA_OAUTH_CLIENT_SECRET] = client_secret_override
            # Evict outstanding tokens along with the old secret — see the
            # docstring for why a secret-only rotation must not leave them
            # valid for the rest of their TTL.
            data[DATA_OAUTH_SIGNING_KEY] = secrets.token_hex(32)
            changed = True
        # Consume the OAuth override fields once applied. entry.options is
        # readable through the server's own tools
        # (ha_get_integration(include_options=True) rebuilds it from the
        # options-form suggested_values), so a rotated client_secret left here
        # in cleartext would let a pre-restart old-identity token holder read
        # the NEW secret that way — the exact party the rotation evicts, and
        # the same leak the startup log withholds. The resolved values live in
        # entry.data and on the admin-only Configure screen
        # (config_flow._oauth_creds_hint), so nothing is lost. Cleared even
        # when the override matched the current value: the cleartext must not
        # linger regardless of whether it changed data.
        #
        # DELIBERATE divergence from the webhook_id / secret_path overrides
        # above, which persist. Three reasons the OAuth secret is different:
        # (1) A client_secret override IS the OAuth rotation path and gates a
        #     per-connection revocable bearer, so a lingering copy defeats the
        #     very revocation the rotation performs. secret_path's own rotation
        #     (OPT_REGENERATE_SECRETS) already clears its override, so that
        #     path is not self-defeating; a standalone secret_path override is
        #     configuration, not rotation.
        # (2) A connected legacy client learns the OAuth secret only via this
        #     options leak (entry.data is not tool-exposed; the webhook is
        #     OAuth-gated). secret_path gates the direct LAN port, and per
        #     SECURITY.md the local network is the trusted zone and the client
        #     is a trusted principal — LAN-peer access to standard-mode
        #     endpoints is explicitly out of scope.
        # (3) Persisting the webhook_id/secret_path override is deliberate UX
        #     (the admin sees their configured value in the form).
        if options.get(OPT_OAUTH_CLIENT_ID):
            options[OPT_OAUTH_CLIENT_ID] = ""
            changed = True
        if options.get(OPT_OAUTH_CLIENT_SECRET):
            options[OPT_OAUTH_CLIENT_SECRET] = ""
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
