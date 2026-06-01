"""Addon lifecycle E2E for the HAOS test tier (see #1349 items 1 + 3, #1350).

Covers what the testcontainer suite physically can't reach — start/stop/
restart against a real Supervisor, addon-config round-trips, and
container-log shape — for the addon set baked by ``build_image.py``.

Two coverage modes:

1. **Running addons** (Node-RED, ESPHome Device Builder, Matter Server,
   AppDaemon — all ``start=True`` in the bake): options-get / options-set
   persistence and a log-shape check. Each test leaves the addon in
   ``started`` state via an explicit cleanup call so later tests in the
   same session find it running.

   The full ``hassio.addon_stop`` → ``addon_start`` → ``addon_restart``
   lifecycle round-trip is exercised by **Matter Server only**. All four
   roundtrip variants used the same ``_addon_action`` helper hitting the
   same MCP wire path, so one is sufficient signal; Matter Server is the
   lightest addon to cycle (consistently <13s) and Node-RED's roundtrip
   produced a recurring Supervisor 500 flake — see #1414.

2. **Stopped addons** (Mosquitto, MQTT IO — stay ``start=False`` because
   their schemas require config they don't have in the bake): exercise
   the surface that's reachable *without* the addon running —
   ``ha_get_addon`` info + options dict + a logs-fetch shape check.

Slugs are repository-hash-derived by Supervisor at install time, so every
test resolves the slug at runtime by matching against ``ha_get_addon``'s
display-name listing — mirroring the canary's pattern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from ..utilities.assertions import (
    parse_mcp_result,
    safe_call_tool,
)

# Addon log fetches (``ha_get_logs(source="supervisor", slug=<addon>)``)
# return the addon container's raw stdout. Addons write to stdout
# without timestamps (verified live: Node-RED's log starts with
# ``s6-rc: info: service ...``, ESPHome same; the journald-style
# ``YYYY-MM-DDTHH:MM:SS`` prefix in ``log_shapes.LOG_TIMESTAMP_RE``
# only applies to Supervisor's own service logs, not addon stdout).
# So addon-log tests assert non-empty + minimum length rather than
# a timestamp pattern.

LOG = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]


# Display names as they appear in the Supervisor store (and in
# ``build_image.py``'s ADDONS tuple). Slugs are looked up dynamically.
NODERED_NAME = "Node-RED"
ESPHOME_NAME = "ESPHome Device Builder"
MATTER_NAME = "Matter Server"
APPDAEMON_NAME = "AppDaemon"
MQTT_IO_NAME = "MQTT IO"
MOSQUITTO_NAME = "Mosquitto broker"

# Supervisor reports a variety of values for an addon that's installed
# but not running — exact value depends on whether the last attempted
# start crashed, never ran, or the schema is incomplete. Treat the whole
# family as "not running" rather than coupling tests to a single value.
STOPPED_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "unknown", "error"})


# Polling parameters for "did the addon state actually move?" — Supervisor
# returns success once the request is accepted, not once the container
# reached the target state. Real CI runners need 1-3s on average,
# occasionally more on cache-cold restarts.
_STATE_POLL_TIMEOUT = 30.0
_STATE_POLL_INTERVAL = 0.5


async def _resolve_slug(mcp_client: Any, display_name: str) -> str:
    """Look up the runtime Supervisor slug for an addon by display name.

    Slugs are SHA-derived from the repository URL and therefore unstable
    across rebuilds; the display name is the only field guaranteed to
    match ``build_image.py``'s ADDONS tuple.
    """
    raw = await mcp_client.call_tool("ha_get_addon", {})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_addon listing failed: {payload}"

    for entry in payload.get("addons", []):
        if entry.get("name") == display_name:
            slug = entry.get("slug")
            assert slug, f"Addon {display_name!r} listed but has no slug field: {entry}"
            return str(slug)

    # Filter ``None`` names so a future Supervisor addition with a
    # missing display name can't crash the sort with a TypeError on
    # str-vs-None comparison.
    installed = sorted(
        n for n in (a.get("name") for a in payload.get("addons", [])) if n
    )
    pytest.fail(
        f"Addon {display_name!r} not found in installed listing. "
        f"Installed: {installed}. Check build_image.py ADDONS tuple."
    )


async def _get_addon_detail(mcp_client: Any, slug: str) -> dict[str, Any]:
    """Fetch ``ha_get_addon(slug=...)`` and return the inner ``addon`` dict."""
    raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_addon({slug!r}) failed: {payload}"
    detail = payload.get("addon")
    assert isinstance(detail, dict), (
        f"ha_get_addon({slug!r}) returned no addon dict: {payload}"
    )
    return detail


async def _addon_action(mcp_client: Any, slug: str, action: str) -> dict[str, Any]:
    """Invoke ``hassio.addon_{action}`` via ``ha_call_service``.

    Returns the parsed result (success or failure). Caller decides how
    to assert. The MCP tool's parameter is ``data`` (see
    ``src/ha_mcp/tools/tools_service.py::ha_call_service``); the older
    ``service_data`` keyword does NOT exist on this tool and pydantic
    rejects it with ``unexpected_keyword_argument``.
    """
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
    """Poll ``ha_get_addon(slug=...)`` until ``state`` matches ``expected``.

    Supervisor returns success from ``addon_{start,stop,restart}`` once
    the request is accepted, NOT once the container has reached the
    target state. Without polling the lifecycle tests would race the
    Supervisor on every assertion. ``expected`` can be a single string
    or a set of acceptable states (e.g. STOPPED_STATES for the
    stopped-family).
    """
    expected_set: frozenset[str] = (
        frozenset({expected}) if isinstance(expected, str) else frozenset(expected)
    )
    import time as _time  # local import — keep module-level imports tidy

    deadline = _time.monotonic() + timeout
    last_state: str | None = None
    while _time.monotonic() < deadline:
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
    """Best-effort cleanup: leave the addon in ``started`` state.

    Used as a teardown step so a lifecycle test that stops or restarts an
    addon doesn't leak ``stopped`` state into a sibling test that assumes
    it's running. Failures here are logged but not raised — the addon's
    state isn't part of the contract under test, the lifecycle call is.

    Wrapped in a broad ``try/except`` so a flake during cleanup (e.g.
    ``_get_addon_detail``'s internal ``assert`` raising on a transient
    Supervisor response) doesn't replace the original test exception in
    the traceback head.
    """
    try:
        detail = await _get_addon_detail(mcp_client, slug)
        if detail.get("state") == "started":
            return
        result = await _addon_action(mcp_client, slug, "start")
        if not result.get("success"):
            LOG.warning(
                "Cleanup: hassio.addon_start(%s) returned failure: %s",
                slug,
                result,
            )
    except Exception:
        LOG.exception(
            "Cleanup of addon %s failed; original test exception preserved",
            slug,
        )


# ---------------------------------------------------------------------------
# Node-RED lifecycle (running by default after bake start=True flip)
# ---------------------------------------------------------------------------


async def test_nodered_options_get_returns_dict(mcp_client: Any) -> None:
    """`ha_get_addon(slug=...)` exposes the addon's options as a dict."""
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    detail = await _get_addon_detail(mcp_client, slug)
    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Node-RED options field should be a dict, got {type(options).__name__}: "
        f"{options!r}"
    )


async def test_nodered_options_set_persists(mcp_client: Any) -> None:
    """`ha_manage_addon(options=...)` round-trips a probe key through Supervisor.

    Reads current options, writes a probe value (toggles ``log_level``
    between "info" and "debug" — a schema-known Node-RED option that's
    safe to flip mid-test), reads back, asserts the probe took effect,
    then restores the original value in ``finally``.

    A pure no-op write would only verify that Supervisor accepts the
    request, not that the merge-write path actually persisted; the
    probe-key roundtrip catches a regression where Supervisor silently
    drops the payload.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    try:
        detail = await _get_addon_detail(mcp_client, slug)
        current_options = detail.get("options") or {}
        assert isinstance(current_options, dict), (
            f"Pre-write options must be a dict, got {current_options!r}"
        )
        # Pick a probe value distinct from current so the round-trip
        # actually moves the field. Node-RED's schema accepts "info" /
        # "debug" / "warning" / "error" — flip between info and debug.
        original_log_level = current_options.get("log_level", "info")
        probe_log_level = "debug" if original_log_level != "debug" else "info"
        probe_options = dict(current_options)
        probe_options["log_level"] = probe_log_level

        try:
            write_raw = await mcp_client.call_tool(
                "ha_manage_addon",
                {"slug": slug, "options": probe_options},
            )
            write_payload = parse_mcp_result(write_raw)
            # ha_manage_addon's options-write returns either
            # ``{"success": True, ...}`` (config applied immediately) OR
            # ``{"status": "pending_restart", ...}`` when ``options`` /
            # ``network`` keys are written (see tools_addons.py:response
            # shape). BOTH indicate the write was accepted; the
            # pending_restart branch just means the addon needs a restart
            # for runtime to pick it up. The next ``ha_get_addon`` read
            # below verifies the persisted state regardless.
            ok = write_payload.get("success") is True or (
                write_payload.get("status") == "pending_restart"
            )
            assert ok, f"ha_manage_addon options probe write failed: {write_payload}"

            detail_after = await _get_addon_detail(mcp_client, slug)
            options_after = detail_after.get("options") or {}
            assert options_after.get("log_level") == probe_log_level, (
                f"Probe log_level={probe_log_level!r} did not persist. "
                f"After-write options: {options_after}"
            )
        finally:
            # Restore the original log_level so the addon's runtime
            # config matches its pre-test state. Swallow restore errors
            # here only — the parent ``finally`` (_ensure_started) is
            # the durable cleanup.
            restore_options = dict(current_options)
            if "log_level" in restore_options or current_options:
                try:
                    await mcp_client.call_tool(
                        "ha_manage_addon",
                        {"slug": slug, "options": restore_options},
                    )
                except Exception:
                    LOG.exception("Failed to restore Node-RED options")
    finally:
        await _ensure_started(mcp_client, slug)


async def test_nodered_logs_fetch_shape(mcp_client: Any) -> None:
    """`ha_get_logs(source='supervisor')` returns shaped Supervisor log text."""
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_logs(supervisor, {slug}) failed: {payload}"
    log_text = payload.get("log", "")
    assert isinstance(log_text, str) and len(log_text.strip()) >= 100, (
        f"Supervisor log for running addon should have substantial "
        f"content (>=100 chars), got {len(log_text)} chars: "
        f"{log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# ESPHome Device Builder lifecycle (running by default after bake start=True)
# ---------------------------------------------------------------------------


async def test_esphome_options_get_returns_dict(mcp_client: Any) -> None:
    """`ha_get_addon(slug=...)` exposes ESPHome's options as a dict."""
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    detail = await _get_addon_detail(mcp_client, slug)
    options = detail.get("options")
    assert isinstance(options, dict), (
        f"ESPHome options field should be a dict, got {type(options).__name__}: "
        f"{options!r}"
    )


async def test_esphome_options_set_persists(mcp_client: Any) -> None:
    """`ha_manage_addon(options=...)` round-trips a probe key for ESPHome.

    ESPHome Device Builder's schema accepts ``leave_front_door_open``
    (a public toggle that's safe to flip mid-test). Use it as the
    probe field — same shape as the Node-RED test.
    """
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    try:
        detail = await _get_addon_detail(mcp_client, slug)
        current_options = detail.get("options") or {}
        assert isinstance(current_options, dict), (
            f"Pre-write options must be a dict, got {current_options!r}"
        )
        probe_key = "leave_front_door_open"
        original_value = current_options.get(probe_key, False)
        probe_value = not bool(original_value)
        probe_options = dict(current_options)
        probe_options[probe_key] = probe_value

        try:
            write_raw = await mcp_client.call_tool(
                "ha_manage_addon",
                {"slug": slug, "options": probe_options},
            )
            write_payload = parse_mcp_result(write_raw)
            # ha_manage_addon's options-write returns either
            # ``{"success": True, ...}`` (config applied immediately) OR
            # ``{"status": "pending_restart", ...}`` when ``options`` /
            # ``network`` keys are written (see tools_addons.py:response
            # shape). BOTH indicate the write was accepted; the
            # pending_restart branch just means the addon needs a restart
            # for runtime to pick it up. The next ``ha_get_addon`` read
            # below verifies the persisted state regardless.
            ok = write_payload.get("success") is True or (
                write_payload.get("status") == "pending_restart"
            )
            assert ok, f"ha_manage_addon options probe write failed: {write_payload}"

            detail_after = await _get_addon_detail(mcp_client, slug)
            options_after = detail_after.get("options") or {}
            assert options_after.get(probe_key) == probe_value, (
                f"Probe {probe_key}={probe_value!r} did not persist. "
                f"After-write options: {options_after}"
            )
        finally:
            try:
                await mcp_client.call_tool(
                    "ha_manage_addon",
                    {"slug": slug, "options": dict(current_options)},
                )
            except Exception:
                LOG.exception("Failed to restore ESPHome options")
    finally:
        await _ensure_started(mcp_client, slug)


async def test_esphome_logs_fetch_shape(mcp_client: Any) -> None:
    """`ha_get_logs(source='supervisor')` returns shaped log text for ESPHome."""
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_logs(supervisor, {slug}) failed: {payload}"
    log_text = payload.get("log", "")
    assert isinstance(log_text, str) and len(log_text.strip()) >= 100, (
        f"Supervisor log for running addon should have substantial "
        f"content (>=100 chars), got {len(log_text)} chars: "
        f"{log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Matter Server lifecycle (running; covers ingress_panel=false shape)
# ---------------------------------------------------------------------------


async def test_matter_server_start_stop_restart_roundtrip(mcp_client: Any) -> None:
    """Stop → start → restart cycles Matter Server via real Supervisor.

    Matter Server is the addon set's representative of the
    ``ingress_panel=false`` shape (hidden from the HA sidebar even though
    Ingress is wired up). The lifecycle contract is the same as the other
    running addons; the shape coverage comes from ``ha_get_addon`` detail.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
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


async def test_matter_server_ingress_panel_false_shape(mcp_client: Any) -> None:
    """`ha_get_addon(slug=...)` reports ``ingress_panel=false`` for Matter Server.

    This is the load-bearing assertion for keeping Matter Server in the
    addon set: it's the only one whose Ingress route is configured but
    deliberately hidden from the HA sidebar (``ingress_panel=false``),
    and ``ha_get_addon`` detail must surface that field. If a future
    Supervisor version drops the field or renames it, this test catches
    it before regression hits real users.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("ingress") is True, (
        f"Matter Server should have ingress=True, got {detail.get('ingress')!r}"
    )
    assert detail.get("ingress_panel") is False, (
        f"Matter Server should have ingress_panel=False (hidden sidebar), "
        f"got {detail.get('ingress_panel')!r}"
    )


async def test_matter_server_options_set_persists(mcp_client: Any) -> None:
    """`ha_manage_addon(options=...)` round-trips a probe key for Matter Server.

    Matter Server's schema accepts ``beta`` (boolean toggle) — safe to
    flip between True and False as a probe value.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    try:
        detail = await _get_addon_detail(mcp_client, slug)
        current_options = detail.get("options") or {}
        assert isinstance(current_options, dict), (
            f"Pre-write options must be a dict, got {current_options!r}"
        )
        probe_key = "beta"
        original_value = current_options.get(probe_key, False)
        probe_value = not bool(original_value)
        probe_options = dict(current_options)
        probe_options[probe_key] = probe_value

        try:
            write_raw = await mcp_client.call_tool(
                "ha_manage_addon",
                {"slug": slug, "options": probe_options},
            )
            write_payload = parse_mcp_result(write_raw)
            ok = write_payload.get("success") is True or (
                write_payload.get("status") == "pending_restart"
            )
            assert ok, f"ha_manage_addon options probe write failed: {write_payload}"

            detail_after = await _get_addon_detail(mcp_client, slug)
            options_after = detail_after.get("options") or {}
            assert options_after.get(probe_key) == probe_value, (
                f"Probe {probe_key}={probe_value!r} did not persist. "
                f"After-write options: {options_after}"
            )
        finally:
            try:
                await mcp_client.call_tool(
                    "ha_manage_addon",
                    {"slug": slug, "options": dict(current_options)},
                )
            except Exception:
                LOG.exception("Failed to restore Matter Server options")
    finally:
        await _ensure_started(mcp_client, slug)


async def test_matter_server_logs_fetch_shape(mcp_client: Any) -> None:
    """`ha_get_logs(source='supervisor')` returns shaped log text for Matter Server."""
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_logs(supervisor, {slug}) failed: {payload}"
    log_text = payload.get("log", "")
    assert isinstance(log_text, str) and len(log_text.strip()) >= 50, (
        f"Supervisor log for running Matter Server should have content "
        f"(>=50 chars), got {len(log_text)} chars: {log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# AppDaemon lifecycle (running; covers ingress=false + webui-set shape)
# ---------------------------------------------------------------------------


async def test_appdaemon_webui_no_ingress_shape(mcp_client: Any) -> None:
    """`ha_get_addon(slug=...)` reports ``webui`` set + ``ingress=false`` for AppDaemon.

    Load-bearing assertion: AppDaemon is the addon set's representative
    of the ``webui`` field (a port-based URL that the HA UI renders as
    an external link) — distinct from Ingress addons. The wire contract
    for ``ha_get_addon`` reading and surfacing ``webui`` would otherwise
    be uncovered.
    """
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("ingress") is False, (
        f"AppDaemon should have ingress=False, got {detail.get('ingress')!r}"
    )
    webui = detail.get("webui")
    assert isinstance(webui, str) and webui.startswith("http"), (
        f"AppDaemon should have a webui URL, got {webui!r}"
    )


async def test_appdaemon_logs_fetch_shape(mcp_client: Any) -> None:
    """`ha_get_logs(source='supervisor')` returns shaped log text for AppDaemon."""
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_logs(supervisor, {slug}) failed: {payload}"
    log_text = payload.get("log", "")
    assert isinstance(log_text, str) and len(log_text.strip()) >= 50, (
        f"Supervisor log for running AppDaemon should have content "
        f"(>=50 chars), got {len(log_text)} chars: {log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Mosquitto reachable-without-running (stays start=False — replaces #1281
# Frigate's stopped-addon-shape coverage)
# ---------------------------------------------------------------------------


async def test_mosquitto_info_and_options_reachable(mcp_client: Any) -> None:
    """Mosquitto is installed-but-stopped; info / options / logs still reachable.

    Mosquitto's default schema rejects empty cert paths when SSL is on,
    so the bake leaves it ``start=False``. The Supervisor-managed
    metadata (info dict, options stub, boot-fail log) must still be
    queryable — that's the contract this test pins.
    """
    slug = await _resolve_slug(mcp_client, MOSQUITTO_NAME)

    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("state") in STOPPED_STATES, (
        f"Mosquitto should be in a stopped-family state, got "
        f"{detail.get('state')!r}. Did build_image.py accidentally flip "
        f"it to start=True?"
    )

    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Mosquitto options should be a dict even when stopped, got "
        f"{type(options).__name__}: {options!r}"
    )

    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), (
        f"ha_get_logs(supervisor, {slug}) should succeed for an installed "
        f"stopped addon, got: {payload}"
    )
    log_text = payload.get("log", "")
    assert isinstance(log_text, str), (
        f"log field must be a string, got {type(log_text).__name__}"
    )


# ---------------------------------------------------------------------------
# MQTT IO reachable-without-running (stays start=False; replaces Z2M's
# start-fail + privileged-block coverage at a fraction of the size)
# ---------------------------------------------------------------------------


async def test_mqtt_io_info_and_options_reachable(mcp_client: Any) -> None:
    """MQTT IO is installed-but-stopped; metadata still reachable.

    MQTT IO needs a configured broker (and Mosquitto in the bake is also
    ``start=False``), so it doesn't reach the started state. Same shape
    contract as Mosquitto: info dict, options dict, logs string — all
    reachable via Supervisor even when the addon hasn't run.
    """
    slug = await _resolve_slug(mcp_client, MQTT_IO_NAME)

    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("state") in STOPPED_STATES, (
        f"MQTT IO should be in a stopped-family state, got "
        f"{detail.get('state')!r}. Did build_image.py accidentally flip "
        f"it to start=True?"
    )

    options = detail.get("options")
    assert isinstance(options, dict), (
        f"MQTT IO options should be a dict even when stopped, got "
        f"{type(options).__name__}: {options!r}"
    )

    raw = await mcp_client.call_tool(
        "ha_get_logs", {"source": "supervisor", "slug": slug}
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), (
        f"ha_get_logs(supervisor, {slug}) should succeed for an installed "
        f"stopped addon, got: {payload}"
    )
    log_text = payload.get("log", "")
    assert isinstance(log_text, str), (
        f"log field must be a string, got {type(log_text).__name__}"
    )


# Detail-shape coverage anchored to specific addons: keep these tests
# adjacent to the addon's lifecycle block above so a future drop of an
# addon from build_image.py also drops its shape assertion.
# same rationale: Supervisor's reject wording is too variable to assert
# against without flakiness. The ``_info_and_options_reachable`` test
# above is enough proof Z2M is installed + reachable.
