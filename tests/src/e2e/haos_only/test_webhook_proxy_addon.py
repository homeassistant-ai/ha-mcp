"""Webhook-proxy addon runtime E2E for the HAOS test tier.

The webhook-proxy addon (``homeassistant-addon-webhook-proxy/``) is baked
into the qcow2 with ``boot: manual`` (see
``tests/haos_image_build/build_image.py::install_webhook_proxy_addon``).
The bake validates that the addon installs cleanly and pre-builds its
Docker image; tests in this module start the addon via a session
fixture so the build's success acts as install coverage and the runtime
tests only run once per session against a known-good container.

Why boot=manual + session fixture rather than boot=auto:

- ``start.py`` writes ``/config/.mcp_proxy_config.json`` on every run
  with a freshly-generated webhook_id. If the addon auto-started on
  qcow2 resume, it would clobber the deterministic config that the
  bake's ``bake_test_state`` step injected for sibling tests
  (testcontainer-only ``test_webhook_proxy.py`` relies on the bake's
  webhook_id ``mcp_e2e_test_webhook_proxy``).
- The dev MCP addon and the webhook-proxy both have ``startup:
  application`` and would race on parallel auto-start; webhook-proxy's
  Supervisor auto-discovery is faster than the dev addon's MCP server
  reaching ready, so auto-discovery returns "not running" and the
  addon exits 1 → Supervisor escalates to boot_fail before the dev
  addon stabilises.

``mcp_server_url`` is pinned to ``http://127.0.0.1:9583<secret_path>``
in the bake's options so the session fixture's start doesn't go through
auto-discovery (both addons run on host_network so 127.0.0.1 reaches the
dev addon's MCP server port from inside the webhook-proxy container).
A dedicated test (``test_auto_discovery_finds_dev_addon_when_url_blank``)
clears that option, restarts, and asserts auto-discovery still works —
that path is exercised deliberately rather than relied on for setup.

Tests exercise the addon's runtime through three observable surfaces:

1. Supervisor / MCP tools (``ha_get_addon``, ``ha_manage_addon``,
   ``ha_call_service`` with ``hassio.addon_*``, ``ha_get_logs``).
2. The HA Core webhook endpoint (``/api/webhook/<webhook_id>``) over
   HTTP using the bearer token the conftest yields.
3. Addon stdout via ``ha_get_logs(source='supervisor', slug=...)`` —
   start.py logs the registered webhook path and the discovered MCP
   slug on startup.
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

STOPPED_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "unknown", "error"})

_STATE_POLL_TIMEOUT = 60.0
_STATE_POLL_INTERVAL = 0.5

# start.py logs the registered webhook path as ``/api/webhook/<id>`` on
# every startup. The ID is alphanumeric + underscore + hyphen; match
# liberally rather than couple to a specific length.
_WEBHOOK_PATH_RE = re.compile(r"/api/webhook/([A-Za-z0-9_-]+)")
# start.py logs "Discovered running MCP addon: <slug> at <ip>" on
# successful auto-discovery.
_DISCOVERY_RE = re.compile(r"Discovered running MCP addon:\s*(\S+)")


# ---------------------------------------------------------------------------
# Helpers
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


async def _get_addon_logs(mcp_client: Any, slug: str) -> str:
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
    """Pull the most recent webhook ID start.py logged on startup."""
    matches = _WEBHOOK_PATH_RE.findall(log_text)
    return matches[-1] if matches else None


async def _set_options(mcp_client: Any, slug: str, options: dict[str, Any]) -> None:
    """Update addon options via ha_manage_addon, asserting success."""
    raw = await mcp_client.call_tool(
        "ha_manage_addon", {"slug": slug, "options": dict(options)}
    )
    payload = parse_mcp_result(raw)
    ok = payload.get("success") is True or payload.get("status") == "pending_restart"
    assert ok, f"ha_manage_addon options write failed: {payload}"


# ---------------------------------------------------------------------------
# Module-scope fixture: start the addon for this module's lifetime.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def webhook_proxy_started(mcp_client: Any) -> Any:
    """Start the webhook-proxy addon for the duration of this module's tests.

    The bake leaves the addon installed but not started (boot=manual).
    This fixture brings it up at module scope so the per-test overhead
    is bounded to "send hassio.addon_start once". On teardown, stop the
    addon so sibling test modules that share the HAOS instance (only
    a concern in inaddon-tier dispatch) see the addon back in stopped
    state — preventing state contamination from start.py's writes to
    /config/.mcp_proxy_config.json across module boundaries.
    """
    result = await _addon_action(mcp_client, WEBHOOK_PROXY_SLUG, "start")
    assert result.get("success"), (
        f"Fixture failed to start webhook-proxy addon: {result}"
    )
    await _wait_for_state(mcp_client, WEBHOOK_PROXY_SLUG, "started")
    # start.py writes the webhook path to stdout on first start; give
    # it a moment so the first test reads a populated log.
    deadline = time.monotonic() + _STATE_POLL_TIMEOUT
    while time.monotonic() < deadline:
        log_text = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
        if _extract_webhook_id(log_text):
            break
        await asyncio.sleep(_STATE_POLL_INTERVAL)
    try:
        yield
    finally:
        try:
            await _addon_action(mcp_client, WEBHOOK_PROXY_SLUG, "stop")
            await _wait_for_state(
                mcp_client, WEBHOOK_PROXY_SLUG, STOPPED_STATES, timeout=30.0
            )
        except Exception:
            LOG.exception("Teardown stop of webhook-proxy addon failed")


# ---------------------------------------------------------------------------
# Post-start contract
# ---------------------------------------------------------------------------


async def test_addon_started_after_fixture(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """Fixture brought the bake-installed addon to ``started``."""
    detail = await _get_addon_detail(mcp_client, WEBHOOK_PROXY_SLUG)
    assert detail.get("name") == WEBHOOK_PROXY_NAME, (
        f"Expected name {WEBHOOK_PROXY_NAME!r}, got {detail.get('name')!r}"
    )
    assert detail.get("state") == "started", (
        f"Webhook-proxy addon should be ``started`` after fixture; "
        f"got state={detail.get('state')!r}"
    )


async def test_addon_logs_fetch_shape(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """``ha_get_logs(source='supervisor')`` returns substantial log text."""
    log_text = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
    assert len(log_text.strip()) >= 100, (
        f"Webhook-proxy stdout should have substantial content "
        f"(>=100 chars), got {len(log_text)} chars: {log_text[:200]!r}"
    )


async def test_addon_options_get_returns_dict(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """``ha_get_addon`` exposes webhook-proxy options as a dict."""
    detail = await _get_addon_detail(mcp_client, WEBHOOK_PROXY_SLUG)
    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Webhook-proxy options field should be a dict, got "
        f"{type(options).__name__}: {options!r}"
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_addon_start_stop_restart_roundtrip(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """Stop → start → restart cycles via Supervisor.

    Mirrors ``test_matter_server_start_stop_restart_roundtrip`` in
    test_addon_lifecycle.py. Cleanup leaves the addon in ``started`` so
    sibling tests in this module see it running. The fixture's teardown
    is the durable stop.
    """
    slug = WEBHOOK_PROXY_SLUG
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


# ---------------------------------------------------------------------------
# Options round-trip — mirror Node-RED's options-persist pattern.
# ---------------------------------------------------------------------------


async def test_addon_remote_url_round_trip(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """``remote_url`` written via ha_manage_addon persists in Supervisor options.

    The addon's schema declares ``remote_url: str?``. Tests assert the
    write path round-trips through Supervisor; whether the addon actually
    reaches the URL is a downstream concern. Restored to empty in
    ``finally`` so sibling tests aren't observably mutated.
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
        await _set_options(mcp_client, slug, probe_options)
        detail_after = await _get_addon_detail(mcp_client, slug)
        options_after = detail_after.get("options") or {}
        assert options_after.get("remote_url") == probe, (
            f"remote_url probe={probe!r} did not persist. "
            f"After-write options: {options_after}"
        )
    finally:
        restore = dict(current_options)
        restore["remote_url"] = original_remote
        try:
            await _set_options(mcp_client, slug, restore)
        except Exception:
            LOG.exception("Failed to restore remote_url to %s", original_remote)


# ---------------------------------------------------------------------------
# Auto-discovery — deliberately exercised by clearing mcp_server_url + restart.
# ---------------------------------------------------------------------------


async def test_auto_discovery_finds_dev_addon_when_url_blank(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """Clearing ``mcp_server_url`` forces start.py's auto-discovery.

    The bake pins ``mcp_server_url`` to the dev addon's host-network URL
    so the addon doesn't depend on auto-discovery timing during setup.
    This test exercises the auto-discovery code path explicitly: clear
    the field, restart the addon, assert the discovery log line names
    the dev addon slug, then restore the pinned URL so subsequent tests
    in this module use the reliable target.
    """
    slug = WEBHOOK_PROXY_SLUG
    detail = await _get_addon_detail(mcp_client, slug)
    current_options = detail.get("options") or {}
    assert isinstance(current_options, dict)
    pinned_url = current_options.get("mcp_server_url", "")

    cleared_options = dict(current_options)
    cleared_options["mcp_server_url"] = ""
    try:
        await _set_options(mcp_client, slug, cleared_options)
        restart_result = await _addon_action(mcp_client, slug, "restart")
        assert restart_result.get("success"), (
            f"hassio.addon_restart({slug}) failed: {restart_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")

        # Poll the log until the discovery line appears in the new run.
        deadline = time.monotonic() + _STATE_POLL_TIMEOUT
        discovered: list[str] = []
        while time.monotonic() < deadline:
            log_text = await _get_addon_logs(mcp_client, slug)
            discovered = _DISCOVERY_RE.findall(log_text)
            if discovered:
                break
            await asyncio.sleep(_STATE_POLL_INTERVAL)
        assert discovered, (
            "Webhook-proxy did not log "
            '"Discovered running MCP addon: <slug>" within '
            f"{_STATE_POLL_TIMEOUT}s after restart with blank "
            "mcp_server_url. Auto-discovery code path regressed."
        )
        assert DEV_ADDON_SLUG in discovered, (
            f"Expected discovery target {DEV_ADDON_SLUG!r}; observed: {discovered}"
        )
    finally:
        restore = dict(current_options)
        restore["mcp_server_url"] = pinned_url
        try:
            await _set_options(mcp_client, slug, restore)
            await _addon_action(mcp_client, slug, "restart")
            await _wait_for_state(mcp_client, slug, "started")
        except Exception:
            LOG.exception("Failed to restore mcp_server_url + restart")


# ---------------------------------------------------------------------------
# Webhook-ID persistence (#1020 regression)
# ---------------------------------------------------------------------------


async def test_webhook_id_persists_across_restart(
    mcp_client: Any, webhook_proxy_started: Any
) -> None:
    """The webhook ID survives an addon restart (PR #1020 regression).

    start.py reads/writes ``/data/webhook_id.txt`` — the Supervisor-
    mounted persistent volume — so an ``addon_restart`` keeps the file
    and the URL stays the same. A regression that re-generated the ID
    on every start would break the contract that an MCP client's saved
    webhook URL doesn't change unless the user explicitly rotates it.
    """
    slug = WEBHOOK_PROXY_SLUG
    pre_log = await _get_addon_logs(mcp_client, slug)
    pre_id = _extract_webhook_id(pre_log)
    assert pre_id, (
        "Could not find ``/api/webhook/<id>`` line in webhook-proxy "
        "logs before restart. "
        f"Log tail: ...{pre_log[-2000:]!r}"
    )

    restart_result = await _addon_action(mcp_client, slug, "restart")
    assert restart_result.get("success"), (
        f"hassio.addon_restart({slug}) failed: {restart_result}"
    )
    await _wait_for_state(mcp_client, slug, "started")

    deadline = time.monotonic() + _STATE_POLL_TIMEOUT
    post_id: str | None = None
    while time.monotonic() < deadline:
        post_log = await _get_addon_logs(mcp_client, slug)
        # Only consider log lines emitted after the pre-restart snapshot.
        new_section = post_log[len(pre_log) :]
        post_id = _extract_webhook_id(new_section)
        if post_id:
            break
        await asyncio.sleep(_STATE_POLL_INTERVAL)

    assert post_id, (
        "Webhook-proxy did not re-log the webhook path within "
        f"{_STATE_POLL_TIMEOUT}s of restart."
    )
    assert post_id == pre_id, (
        f"Webhook ID changed across restart (pre={pre_id!r}, "
        f"post={post_id!r}). #1020 regression — /data/webhook_id.txt "
        "is no longer persisting the ID."
    )


# ---------------------------------------------------------------------------
# Webhook endpoint registered in HA Core
# ---------------------------------------------------------------------------


async def test_webhook_endpoint_registered_in_ha_core(
    mcp_client: Any,
    webhook_proxy_started: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """The webhook the addon registered is reachable on HA Core's HTTP API.

    The addon's start.py installs the ``mcp_proxy`` custom integration
    and creates a config entry that calls ``webhook.async_register``
    with the persisted webhook ID. HA returns 200 with an empty body
    for any unregistered webhook ID (this is by design — webhook auth
    is URL-secrecy). We assert the integration's response to the
    registered URL differs from the unregistered baseline in at least
    one of (status, body, content-type).
    """
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config["token"]
    headers = {"Authorization": f"Bearer {token}"}

    pre_log = await _get_addon_logs(mcp_client, WEBHOOK_PROXY_SLUG)
    webhook_id = _extract_webhook_id(pre_log)
    assert webhook_id, "Could not extract registered webhook ID from addon log."

    registered_url = f"{base_url}/api/webhook/{webhook_id}"
    unregistered_url = f"{base_url}/api/webhook/definitely-not-registered-xyz"

    registered_resp = await asyncio.to_thread(
        requests.post, registered_url, headers=headers, timeout=10
    )
    unregistered_resp = await asyncio.to_thread(
        requests.post, unregistered_url, headers=headers, timeout=10
    )

    differs = (
        registered_resp.status_code != unregistered_resp.status_code
        or registered_resp.content != unregistered_resp.content
        or registered_resp.headers.get("Content-Type")
        != unregistered_resp.headers.get("Content-Type")
    )
    assert differs, (
        "Registered webhook response is identical to unregistered "
        f"({registered_resp.status_code} == {unregistered_resp.status_code}, "
        f"len={len(registered_resp.content)} == "
        f"{len(unregistered_resp.content)}). mcp_proxy may not have "
        "registered the webhook on first start."
    )


# ---------------------------------------------------------------------------
# OAuth gate (#1184)
# ---------------------------------------------------------------------------


async def test_addon_oauth_toggle_blocks_unauthenticated_webhook(
    mcp_client: Any,
    webhook_proxy_started: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Enabling ``enable_oauth`` makes the webhook reject unauth requests.

    PR #1184 added an OAuth fail-closed gate: when ``enable_oauth=true``
    the addon refuses to register the webhook (or unregisters it) until
    the OAuth integration is verified loaded. Accept either auth-
    rejection (401/403) or unregistered-404 as evidence the gate
    engaged. Restored to ``enable_oauth=false`` in ``finally``.
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
        await _set_options(mcp_client, slug, probe_options)
        restart_result = await _addon_action(mcp_client, slug, "restart")
        assert restart_result.get("success"), (
            f"hassio.addon_restart({slug}) for OAuth toggle failed: {restart_result}"
        )
        await _wait_for_state(mcp_client, slug, "started")

        deadline = time.monotonic() + _STATE_POLL_TIMEOUT
        last_status: int | None = None
        gated = False
        while time.monotonic() < deadline:
            resp = await asyncio.to_thread(
                requests.post, webhook_url, headers=headers, timeout=10
            )
            last_status = resp.status_code
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
        try:
            await _set_options(mcp_client, slug, current_options)
            await _addon_action(mcp_client, slug, "restart")
            await _wait_for_state(mcp_client, slug, "started")
        except Exception:
            LOG.exception("OAuth cleanup restart failed")
