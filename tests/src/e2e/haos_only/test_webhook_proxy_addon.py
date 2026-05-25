"""Webhook-proxy addon runtime E2E for the HAOS test tier.

The webhook-proxy addon (``homeassistant-addon-webhook-proxy/``) is now
baked + installed during the HAOS image build (see
``stage_webhook_proxy_addon_source`` and ``install_webhook_proxy_addon``
in ``tests/haos_image_build/build_image.py``). The addon's ``start.py``
runtime — Supervisor auto-discovery of the ha-mcp dev addon, webhook
registration into HA Core, ``/data/webhook_id.txt`` persistence (#1020),
the OAuth fail-closed gate (#1184), and the addon-options round-trip —
cannot be exercised by the testcontainer suite (no Supervisor) and gets
its first real coverage here.

Slugs:

- Webhook-proxy: ``local_ha_mcp_webhook_proxy`` (constant
  ``HA_MCP_WEBHOOK_PROXY_ADDON_SLUG`` in build_image.py).
- Dev addon (the discovery target): ``local_ha_mcp_dev``.

Tests interact with the addon through three observable surfaces:

1. Supervisor / MCP tools (``ha_get_addon``, ``ha_manage_addon``,
   ``ha_call_service`` with the ``hassio.addon_*`` services,
   ``ha_get_logs``). Same shape as ``test_addon_lifecycle.py``.
2. The HA Core webhook endpoint (``/api/webhook/<webhook_id>``) via
   direct HTTP using the bearer token the conftest yields. The addon
   registers this endpoint via the ``mcp_proxy`` custom integration
   it installs into HA on first start.
3. Addon stdout logs via ``ha_get_logs(source='supervisor', slug=...)``
   — start.py logs the discovered MCP slug and the registered webhook
   path on startup, which is what we pattern-match for the discovery
   and webhook-ID persistence checks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import pytest
import requests

from ..utilities.assertions import parse_mcp_result, safe_call_tool

LOG = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]

WEBHOOK_PROXY_NAME = "Nabu Casa / Webhook Proxy for HA MCP"
WEBHOOK_PROXY_SLUG = "local_ha_mcp_webhook_proxy"
DEV_ADDON_SLUG = "local_ha_mcp_dev"

# Mirrors test_addon_lifecycle.py — Supervisor's "addon not running"
# family. Used by lifecycle round-trip assertions.
STOPPED_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "unknown", "error"})

_STATE_POLL_TIMEOUT = 30.0
_STATE_POLL_INTERVAL = 0.5

# start.py logs the registered webhook path as ``/api/webhook/<id>`` on
# every startup (see homeassistant-addon-webhook-proxy/start.py:853 and
# log lines around the proxy_config write). The ID is alphanumeric +
# underscore + hyphen — match liberally rather than coupling to a
# specific length, since the addon picks the format.
_WEBHOOK_PATH_RE = re.compile(r"/api/webhook/([A-Za-z0-9_-]+)")
# start.py:247 logs "Discovered running MCP addon: <slug> at <ip>" on
# successful auto-discovery. The MCP slug suffix match accepts
# ``_ha_mcp_dev`` so the dev-channel addon is the expected target.
_DISCOVERY_RE = re.compile(r"Discovered running MCP addon:\s*(\S+)")


# ---------------------------------------------------------------------------
# Helpers — mirror test_addon_lifecycle.py shapes so future drift between
# this module and the lifecycle suite stays visible.
# ---------------------------------------------------------------------------


async def _get_addon_detail(mcp_client: Any, slug: str) -> dict[str, Any]:
    raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_addon({slug!r}) failed: {payload}"
    detail = payload.get("addon")
    assert isinstance(detail, dict), (
        f"ha_get_addon({slug!r}) returned no addon dict: {payload}"
    )
    return detail


async def _addon_action(mcp_client: Any, slug: str, action: str) -> dict[str, Any]:
    return await safe_call_tool(
        mcp_client,
        "ha_call_service",
        {
            "domain": "hassio",
            "service": f"addon_{action}",
            "data": {"addon": slug},
        },
    )


async def _wait_for_state(
    mcp_client: Any,
    slug: str,
    expected: str | frozenset[str] | set[str],
    *,
    timeout: float = _STATE_POLL_TIMEOUT,
) -> str:
    expected_set: frozenset[str] = (
        frozenset({expected}) if isinstance(expected, str) else frozenset(expected)
    )
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while time.monotonic() < deadline:
        detail = await _get_addon_detail(mcp_client, slug)
        last_state = detail.get("state")
        if last_state in expected_set:
            return str(last_state)
        await asyncio.sleep(_STATE_POLL_INTERVAL)
    raise AssertionError(
        f"Addon {slug!r} state did not reach {sorted(expected_set)!r} "
        f"within {timeout}s (last observed: {last_state!r})"
    )


async def _ensure_started(mcp_client: Any, slug: str) -> None:
    try:
        detail = await _get_addon_detail(mcp_client, slug)
        if detail.get("state") == "started":
            return
        result = await _addon_action(mcp_client, slug, "start")
        if not result.get("success"):
            LOG.warning(
                "Cleanup: hassio.addon_start(%s) returned failure: %s", slug, result
            )
    except Exception:
        LOG.exception(
            "Cleanup of addon %s failed; original test exception preserved", slug
        )


async def _get_addon_logs(mcp_client: Any, slug: str) -> str:
    """Fetch addon container stdout via ha_get_logs(source='supervisor')."""
    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_logs(supervisor, {slug}) failed: {payload}"
    log_text = payload.get("log", "")
    assert isinstance(log_text, str), (
        f"ha_get_logs returned non-string log field: {type(log_text).__name__}"
    )
    return log_text


def _extract_webhook_id(log_text: str) -> str | None:
    """Pull the most recent webhook ID start.py logged on startup, or None."""
    matches = _WEBHOOK_PATH_RE.findall(log_text)
    return matches[-1] if matches else None


async def _restore_options(mcp_client: Any, slug: str, options: dict[str, Any]) -> None:
    """Restore an options dict via ha_manage_addon. Logs and swallows failures."""
    try:
        await mcp_client.call_tool(
            "ha_manage_addon", {"slug": slug, "options": dict(options)}
        )
    except Exception:
        LOG.exception("Failed to restore webhook-proxy options to %s", options)


# ---------------------------------------------------------------------------
# Installation + first-start contract
# ---------------------------------------------------------------------------


async def test_addon_installed_and_running(mcp_client: Any) -> None:
    """The bake-installed webhook-proxy addon resolves and is in ``started``."""
    detail = await _get_addon_detail(mcp_client, WEBHOOK_PROXY_SLUG)
    assert detail.get("name") == WEBHOOK_PROXY_NAME, (
        f"Expected name {WEBHOOK_PROXY_NAME!r}, got {detail.get('name')!r}"
    )
    assert detail.get("state") == "started", (
        f"Webhook-proxy addon should be ``started`` after bake; "
        f"got state={detail.get('state')!r}. Full detail: {detail}"
    )


async def test_addon_supervisor_auto_discovery_logs_dev_slug(
    mcp_client: Any,
) -> None:
    """``start.py`` auto-discovery finds and logs the dev MCP addon slug.

    start.py:188-249 lists installed addons via the Supervisor API, matches
    slug suffixes ``_ha_mcp`` / ``_ha_mcp_dev``, and logs the discovered
    target before continuing. With ``mcp_server_url`` left empty in the
    bake's options POST (see ``install_webhook_proxy_addon``), this is the
    code path that runs on first start. A regression that breaks the slug
    suffix match or the addon-listing call would show up here as the dev
    slug failing to appear in the log.
    """
    log_text = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
    discovered = _DISCOVERY_RE.findall(log_text)
    assert discovered, (
        "Webhook-proxy logs do not contain "
        '"Discovered running MCP addon: <slug>". Either the auto-discovery '
        "code path regressed or the addon failed to reach it. "
        f"Log tail: ...{log_text[-2000:]!r}"
    )
    assert DEV_ADDON_SLUG in discovered, (
        f"Expected webhook-proxy to discover the dev addon "
        f"({DEV_ADDON_SLUG!r}); observed discoveries: {discovered}"
    )


# ---------------------------------------------------------------------------
# Lifecycle round-trip — mirror Matter Server's stop/start/restart test
# ---------------------------------------------------------------------------


async def test_addon_start_stop_restart_roundtrip(mcp_client: Any) -> None:
    """Stop → start → restart cycles the webhook-proxy via Supervisor.

    Mirrors ``test_matter_server_start_stop_restart_roundtrip``. Webhook-
    proxy's startup is heavier than Matter Server (writes ``/data/webhook_id.txt``,
    installs the mcp_proxy custom integration, registers the webhook with HA
    Core) so the ``_STATE_POLL_TIMEOUT`` headroom matters more here than for
    the lighter addons. Cleanup leaves it running so later tests observe a
    started addon.
    """
    slug = WEBHOOK_PROXY_SLUG
    try:
        stop_result = await _addon_action(mcp_client, slug, "stop")
        assert stop_result.get("success"), (
            f"hassio.addon_stop({slug}) failed: {stop_result}"
        )
        await _wait_for_state(mcp_client, slug, STOPPED_STATES)

        start_result = await _addon_action(mcp_client, slug, "start")
        assert start_result.get("success"), (
            f"hassio.addon_start({slug}) failed: {start_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")

        restart_result = await _addon_action(mcp_client, slug, "restart")
        assert restart_result.get("success"), (
            f"hassio.addon_restart({slug}) failed: {restart_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")
    finally:
        await _ensure_started(mcp_client, slug)


async def test_addon_logs_fetch_shape(mcp_client: Any) -> None:
    """``ha_get_logs(source='supervisor')`` returns substantial log text."""
    log_text = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
    assert len(log_text.strip()) >= 100, (
        f"Webhook-proxy stdout should have substantial content "
        f"(>=100 chars), got {len(log_text)} chars: {log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Options round-trip — mirror Node-RED's options-persist pattern.
# ---------------------------------------------------------------------------


async def test_addon_options_get_returns_dict(mcp_client: Any) -> None:
    """``ha_get_addon`` exposes webhook-proxy options as a dict."""
    detail = await _get_addon_detail(mcp_client, WEBHOOK_PROXY_SLUG)
    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Webhook-proxy options field should be a dict, got "
        f"{type(options).__name__}: {options!r}"
    )


async def test_addon_remote_url_round_trip(mcp_client: Any) -> None:
    """``remote_url`` written via ha_manage_addon persists in Supervisor options.

    The addon's schema declares ``remote_url: str?``. Tests assert the
    write path round-trips through Supervisor; whether the addon actually
    reaches the URL is a downstream concern (the addon doesn't try to
    contact ``remote_url`` until something hits the webhook). Restored to
    empty in ``finally`` so sibling tests aren't observably mutated.
    """
    slug = WEBHOOK_PROXY_SLUG
    detail = await _get_addon_detail(mcp_client, slug)
    current_options = detail.get("options") or {}
    assert isinstance(current_options, dict)
    original_remote = current_options.get("remote_url", "")

    probe = "https://e2e-test.invalid/webhook-proxy-probe"
    probe_options = dict(current_options)
    probe_options["remote_url"] = probe
    try:
        write_raw = await mcp_client.call_tool(
            "ha_manage_addon", {"slug": slug, "options": probe_options}
        )
        write_payload = parse_mcp_result(write_raw)
        ok = write_payload.get("success") is True or (
            write_payload.get("status") == "pending_restart"
        )
        assert ok, f"ha_manage_addon remote_url write failed: {write_payload}"

        detail_after = await _get_addon_detail(mcp_client, slug)
        options_after = detail_after.get("options") or {}
        assert options_after.get("remote_url") == probe, (
            f"remote_url probe={probe!r} did not persist. "
            f"After-write options: {options_after}"
        )
    finally:
        restore = dict(current_options)
        restore["remote_url"] = original_remote
        await _restore_options(mcp_client, slug, restore)
        await _ensure_started(mcp_client, slug)


# ---------------------------------------------------------------------------
# Webhook-ID persistence (#1020 regression)
# ---------------------------------------------------------------------------


async def test_webhook_id_persists_across_restart(mcp_client: Any) -> None:
    """The webhook ID survives an addon restart (PR #1020 regression).

    start.py reads/writes ``/data/webhook_id.txt`` (see
    ``_get_or_create_webhook_id``). ``/data`` is the Supervisor-mounted
    persistent volume, so an ``addon_restart`` keeps the file and the URL
    stays the same. A regression that re-generated the ID on every start
    would break the contract that an MCP client's saved webhook URL
    doesn't change unless the user explicitly rotates it.

    Reads the most recent ``/api/webhook/<id>`` from the addon log,
    restarts the addon, reads again, asserts the IDs match. ``-n2``
    pytest-xdist scope is fine here because Supervisor logs accumulate
    over the addon's whole life; each restart appends new lines without
    truncating the file we read.
    """
    slug = WEBHOOK_PROXY_SLUG
    try:
        pre_log = await _get_addon_logs(mcp_client, slug)
        pre_id = _extract_webhook_id(pre_log)
        assert pre_id, (
            "Could not find ``/api/webhook/<id>`` line in webhook-proxy "
            "logs before restart — start.py either didn't log it or the "
            "log fetch returned an incomplete tail. "
            f"Log tail: ...{pre_log[-2000:]!r}"
        )

        restart_result = await _addon_action(mcp_client, slug, "restart")
        assert restart_result.get("success"), (
            f"hassio.addon_restart({slug}) failed: {restart_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")

        # start.py logs the webhook path on every startup, but the
        # Supervisor log endpoint races the addon's stdout buffer. Poll
        # the log until a webhook-path line appears AFTER the restart.
        deadline = time.monotonic() + _STATE_POLL_TIMEOUT
        post_id: str | None = None
        while time.monotonic() < deadline:
            post_log = await _get_addon_logs(mcp_client, slug)
            # Look at the suffix beyond pre_log's length so we only match
            # lines emitted in the new (post-restart) run.
            new_section = post_log[len(pre_log) :]
            post_id = _extract_webhook_id(new_section)
            if post_id:
                break
            await asyncio.sleep(_STATE_POLL_INTERVAL)

        assert post_id, (
            "Webhook-proxy did not re-log the webhook path within "
            f"{_STATE_POLL_TIMEOUT}s of restart. Either the addon failed "
            "to reach _register_webhook or stdout buffering is hiding "
            "the line."
        )
        assert post_id == pre_id, (
            f"Webhook ID changed across restart (pre={pre_id!r}, "
            f"post={post_id!r}). #1020 regression — /data/webhook_id.txt "
            "is no longer persisting the ID."
        )
    finally:
        await _ensure_started(mcp_client, slug)


# ---------------------------------------------------------------------------
# Webhook endpoint reachability — addon-registered, not bake-injected.
# ---------------------------------------------------------------------------


async def test_webhook_endpoint_registered_in_ha_core(
    mcp_client: Any, ha_container_with_fresh_config: dict[str, Any]
) -> None:
    """The webhook the addon registered is reachable on HA Core's HTTP API.

    The addon's first-start flow installs the ``mcp_proxy`` custom
    integration and creates a config entry that calls
    ``webhook.async_register`` with the persisted webhook ID. A reachable
    endpoint should NOT 404 — the integration replies with a structured
    error or proxy response, not the generic HA "no such webhook" 404
    that an unregistered ID returns.
    """
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config["token"]
    headers = {"Authorization": f"Bearer {token}"}

    pre_log = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
    webhook_id = _extract_webhook_id(pre_log)
    assert webhook_id, (
        "Could not extract registered webhook ID from addon log. "
        "test_webhook_id_persists_across_restart should be passing too — "
        "fix that first if both fail."
    )

    registered_url = f"{base_url}/api/webhook/{webhook_id}"
    unregistered_url = f"{base_url}/api/webhook/definitely-not-registered"

    # The HA Core webhook endpoint accepts POST without auth (webhooks
    # are auth-by-URL-secrecy by design). GET with auth is also accepted
    # for HA's own webhook handlers; mcp_proxy registers POST + GET. The
    # raw status code suffices to distinguish "registered" (anything but
    # 404) from "unregistered" (404). Failure mode we care about: a
    # registration regression silently drops the webhook and BOTH URLs
    # return 404 identically.
    registered_resp = await asyncio.to_thread(
        requests.post, registered_url, headers=headers, timeout=10
    )
    unregistered_resp = await asyncio.to_thread(
        requests.post, unregistered_url, headers=headers, timeout=10
    )
    assert registered_resp.status_code != 404, (
        f"Registered webhook URL returned 404 — addon's webhook "
        f"registration did not take effect. "
        f"URL: {registered_url}, body: {registered_resp.text[:300]!r}"
    )
    # Sanity check: unknown ID returns the generic 404. If HA changes
    # this to e.g. 401 for all webhook IDs the assertion above would
    # pass falsely; pin the discrimination here.
    assert unregistered_resp.status_code == 404, (
        f"Unregistered webhook URL should return 404, got "
        f"{unregistered_resp.status_code}: {unregistered_resp.text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# OAuth gate (#1184): enabling enable_oauth must close the webhook to
# unauthenticated callers.
# ---------------------------------------------------------------------------


async def test_addon_oauth_toggle_blocks_unauthenticated_webhook(
    mcp_client: Any, ha_container_with_fresh_config: dict[str, Any]
) -> None:
    """Enabling ``enable_oauth`` makes the webhook reject unauth requests.

    PR #1184 added an OAuth fail-closed gate: when ``enable_oauth=true``
    the addon refuses to register the webhook (or unregisters it) until
    the OAuth integration is verified loaded. The user-facing effect is
    that previously-working POSTs to the webhook URL stop succeeding
    with the same plain-status response. The exact failure mode depends
    on whether the integration was successfully reloaded — accept either
    a 4xx response OR an outright 404 (webhook unregistered while the
    OAuth check ran), as long as it differs from the un-gated POST's
    status. Restored to ``enable_oauth=false`` in ``finally``.
    """
    slug = WEBHOOK_PROXY_SLUG
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config["token"]
    headers = {"Authorization": f"Bearer {token}"}

    detail = await _get_addon_detail(mcp_client, slug)
    current_options = detail.get("options") or {}
    assert isinstance(current_options, dict)

    pre_log = await _get_addon_logs(mcp_client, slug)
    webhook_id = _extract_webhook_id(pre_log)
    assert webhook_id, "Could not extract webhook ID from addon log."
    webhook_url = f"{base_url}/api/webhook/{webhook_id}"

    # Baseline: OAuth off, webhook endpoint accepts POST (status != 401/403).
    baseline_resp = await asyncio.to_thread(
        requests.post, webhook_url, headers=headers, timeout=10
    )
    assert baseline_resp.status_code not in (401, 403), (
        f"Baseline (OAuth off) POST should not be auth-rejected; "
        f"got {baseline_resp.status_code}: {baseline_resp.text[:300]!r}"
    )

    probe_options = dict(current_options)
    probe_options["enable_oauth"] = True
    try:
        write_raw = await mcp_client.call_tool(
            "ha_manage_addon", {"slug": slug, "options": probe_options}
        )
        write_payload = parse_mcp_result(write_raw)
        ok = write_payload.get("success") is True or (
            write_payload.get("status") == "pending_restart"
        )
        assert ok, f"enable_oauth=true write failed: {write_payload}"

        # ha_manage_addon's options write reports ``pending_restart`` when
        # the addon needs a restart to pick up runtime changes — the
        # OAuth gate is a runtime concern, so kick the restart ourselves.
        restart_result = await _addon_action(mcp_client, slug, "restart")
        assert restart_result.get("success"), (
            f"hassio.addon_restart({slug}) for OAuth toggle failed: {restart_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")

        # Poll until the post-restart webhook behavior diverges from
        # baseline. start.py's OAuth gate is async (probes the
        # /api/mcp_proxy/oauth endpoint with retries), so the close-down
        # may not be observable for several seconds after the addon
        # reports ``started``.
        deadline = time.monotonic() + _STATE_POLL_TIMEOUT
        last_status: int | None = None
        gated = False
        while time.monotonic() < deadline:
            resp = await asyncio.to_thread(
                requests.post, webhook_url, headers=headers, timeout=10
            )
            last_status = resp.status_code
            # Gate kicked in: either OAuth-rejected (401/403) or webhook
            # unregistered (404). Anything else means the gate hasn't
            # taken effect yet.
            if last_status in (401, 403, 404):
                gated = True
                break
            await asyncio.sleep(_STATE_POLL_INTERVAL)

        assert gated, (
            "enable_oauth=true did not close the webhook within "
            f"{_STATE_POLL_TIMEOUT}s of addon restart. Last status: "
            f"{last_status} (baseline was {baseline_resp.status_code}). "
            "PR #1184 fail-closed gate may have regressed."
        )
    finally:
        await _restore_options(mcp_client, slug, current_options)
        # Restart so the restored OAuth-off state takes effect for sibling
        # tests, then leave the addon running.
        try:
            await _addon_action(mcp_client, slug, "restart")
            await _wait_for_state(mcp_client, slug, "started")
        except Exception:
            LOG.exception("OAuth cleanup restart failed")
        await _ensure_started(mcp_client, slug)
