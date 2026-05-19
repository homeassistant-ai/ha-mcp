"""Addon lifecycle E2E for the HAOS test tier (see #1349 items 1 + 3).

Covers what the testcontainer suite physically can't reach — start/stop/
restart against a real Supervisor, addon-config round-trips, and
container-log shape — for the v1 addon set baked by ``build_image.py``.

Two coverage modes:

1. **Running addons** (Node-RED, ESPHome Device Builder — both flipped to
   ``start=True`` in the bake): full lifecycle round-trip plus
   options-get / options-set persistence and a log-shape check. Each
   test leaves the addon in ``started`` state via an explicit cleanup
   call so later tests in the same session find it running.

2. **Stopped addons** (Frigate, Zigbee2MQTT — stay ``start=False`` because
   their schemas require feeders/devices that don't exist in the bake):
   exercise the surface that's reachable *without* the addon running —
   ``ha_get_addon`` info + options dict + a logs-fetch shape check — and
   prove that ``hassio.addon_start`` returns a structured failure when the
   addon can't satisfy its own config schema. No ``pytest.skip()`` calls
   (per #1349 closeout rule); the tests run and assert the
   stopped-addon shape directly.

Slugs are repository-hash-derived by Supervisor at install time, so every
test resolves the slug at runtime by matching against ``ha_get_addon``'s
display-name listing — mirroring the canary's pattern.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import pytest

from ..utilities.assertions import (
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)

LOG = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]


# Display names as they appear in the Supervisor store (and in
# ``build_image.py``'s ADDONS tuple). Slugs are looked up dynamically.
NODERED_NAME = "Node-RED"
ESPHOME_NAME = "ESPHome Device Builder"
FRIGATE_NAME = "Frigate"
Z2M_NAME = "Zigbee2MQTT"

# Supervisor reports a variety of values for an addon that's installed
# but not running — exact value depends on whether the last attempted
# start crashed, never ran, or the schema is incomplete. Treat the whole
# family as "not running" rather than coupling tests to a single value.
STOPPED_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "unknown", "error"})

# Journald-style timestamp prefix that real Supervisor addon logs always
# carry (e.g. ``2026-05-18T14:23:01.234567+00:00 ...``). Avoid level-name
# tokens like INFO/DEBUG — addons configure their own loggers and may
# suppress those entirely.
_LOG_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

# Tight set of tokens that genuinely identify a Supervisor schema/
# config refusal (vs unrelated 500/timeout/slug-not-found errors).
# Wider sets like {"start", "boot", "config"} match almost any
# Supervisor error string and would pass for the wrong reasons.
_START_FAILURE_TOKENS: tuple[str, ...] = (
    "missing",
    "schema",
    "required",
    "unhealthy",
    "device",  # Frigate/Z2M without a configured device
)


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
            assert slug, (
                f"Addon {display_name!r} listed but has no slug field: {entry}"
            )
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


async def _addon_action(
    mcp_client: Any, slug: str, action: str
) -> dict[str, Any]:
    """Invoke ``hassio.addon_{action}`` via ``ha_call_service``.

    Returns the parsed result (success or failure). Caller decides how
    to assert.
    """
    return await safe_call_tool(
        mcp_client,
        "ha_call_service",
        {
            "domain": "hassio",
            "service": f"addon_{action}",
            "service_data": {"addon": slug},
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


async def test_nodered_start_stop_restart_roundtrip(mcp_client: Any) -> None:
    """Stop → start → restart cycles Node-RED via real Supervisor.

    Each transition is verified by re-reading ``ha_get_addon(slug=...)``
    and asserting the state moved as expected. Final assertion plus the
    cleanup hook leave the addon ``started`` for downstream tests.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
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
            assert write_payload.get("success"), (
                f"ha_manage_addon options probe write failed: {write_payload}"
            )

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
    assert isinstance(log_text, str) and log_text.strip(), (
        f"Supervisor log for running addon should be non-empty, got {log_text!r}"
    )
    assert _LOG_TIMESTAMP_RE.search(log_text), (
        f"Supervisor log should contain a journald-style timestamp; "
        f"first 200 chars: {log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# ESPHome Device Builder lifecycle (running by default after bake start=True)
# ---------------------------------------------------------------------------


async def test_esphome_start_stop_restart_roundtrip(mcp_client: Any) -> None:
    """Stop → start → restart cycles ESPHome via real Supervisor."""
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
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
            assert write_payload.get("success"), (
                f"ha_manage_addon options probe write failed: {write_payload}"
            )

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
    assert isinstance(log_text, str) and log_text.strip(), (
        f"Supervisor log for running addon should be non-empty, got {log_text!r}"
    )
    assert _LOG_TIMESTAMP_RE.search(log_text), (
        f"Supervisor log should contain a journald-style timestamp; "
        f"first 200 chars: {log_text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Frigate reachable-without-running (stays start=False in bake)
# ---------------------------------------------------------------------------


async def test_frigate_info_and_options_reachable(mcp_client: Any) -> None:
    """Frigate is installed-but-stopped; info / options / logs still reachable.

    The schema for Frigate requires at least one camera config block we
    can't reasonably stub in the bake, so the addon never reaches the
    ``started`` state. The Supervisor-managed metadata (info dict, the
    options stub, the boot-fail log) is still queryable, and tests for
    those endpoints are the whole point of including Frigate in the v1
    addon set: they prove the tools handle the not-running case without
    falling back to "addon must be started" errors.
    """
    slug = await _resolve_slug(mcp_client, FRIGATE_NAME)

    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("state") in STOPPED_STATES, (
        f"Frigate should be in a stopped-family state, got "
        f"{detail.get('state')!r}. Did build_image.py accidentally flip it "
        f"to start=True?"
    )

    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Frigate options should be a dict even when stopped, got "
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
    # A stopped addon's log can legitimately be empty (never started) OR
    # contain boot-fail output. Accept either; only assert the journald
    # timestamp shape when there's content to check.
    if log_text.strip():
        assert _LOG_TIMESTAMP_RE.search(log_text), (
            f"Non-empty Frigate log should still carry a journald timestamp; "
            f"first 200 chars: {log_text[:200]!r}"
        )


async def test_frigate_start_fails_with_recognizable_error(
    mcp_client: Any,
) -> None:
    """`hassio.addon_start` against a config-incomplete Frigate fails loudly.

    Frigate's bake-time install has no camera configured, so Supervisor
    refuses ``/addons/{slug}/start``. The structured failure shape is the
    contract under test — either a recognizable error token in the message
    OR a plain ``success=False`` (older Supervisor builds return the bare
    rejection). A future Supervisor change that lets Frigate start without
    cameras would fail this test loudly, which is correct.
    """
    slug = await _resolve_slug(mcp_client, FRIGATE_NAME)
    result = await _addon_action(mcp_client, slug, "start")
    if result.get("success"):
        pytest.fail(
            f"hassio.addon_start({slug}) should have failed for "
            f"config-incomplete Frigate, but returned success: {result}"
        )
    error_msg = extract_error_message(result).lower()
    # Tight assertion: the slug must appear in the error (proves it's
    # this addon failing, not an unrelated 401/500), AND one of the
    # tight schema-refusal tokens must appear (filters out generic
    # "start failed" wording that could mask totally unrelated bugs).
    assert slug.lower() in error_msg, (
        f"Frigate start-failure message should reference the slug "
        f"{slug!r} so we know Supervisor rejected the right addon, got: "
        f"{error_msg!r}"
    )
    assert any(token in error_msg for token in _START_FAILURE_TOKENS), (
        f"Frigate start-failure message should contain at least one of "
        f"{list(_START_FAILURE_TOKENS)} (proving schema/config refusal, "
        f"not e.g. a timeout or auth error), got: {error_msg!r}"
    )


# ---------------------------------------------------------------------------
# Zigbee2MQTT reachable-without-running (stays start=False in bake)
# ---------------------------------------------------------------------------


async def test_zigbee2mqtt_info_and_options_reachable(mcp_client: Any) -> None:
    """Zigbee2MQTT is installed-but-stopped; metadata still reachable.

    Z2M needs a real Zigbee coordinator on a serial port; the bake's QEMU
    image has none, so the addon never starts. Same shape contract as
    the Frigate test.
    """
    slug = await _resolve_slug(mcp_client, Z2M_NAME)

    detail = await _get_addon_detail(mcp_client, slug)
    assert detail.get("state") in STOPPED_STATES, (
        f"Zigbee2MQTT should be in a stopped-family state, got "
        f"{detail.get('state')!r}. Did build_image.py accidentally flip "
        f"it to start=True?"
    )

    options = detail.get("options")
    assert isinstance(options, dict), (
        f"Zigbee2MQTT options should be a dict even when stopped, got "
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
    if log_text.strip():
        assert _LOG_TIMESTAMP_RE.search(log_text), (
            f"Non-empty Zigbee2MQTT log should still carry a journald "
            f"timestamp; first 200 chars: {log_text[:200]!r}"
        )


async def test_zigbee2mqtt_start_fails_with_recognizable_error(
    mcp_client: Any,
) -> None:
    """`hassio.addon_start` against a device-less Zigbee2MQTT fails loudly."""
    slug = await _resolve_slug(mcp_client, Z2M_NAME)
    result = await _addon_action(mcp_client, slug, "start")
    if result.get("success"):
        pytest.fail(
            f"hassio.addon_start({slug}) should have failed for "
            f"device-less Zigbee2MQTT, but returned success: {result}"
        )
    error_msg = extract_error_message(result).lower()
    assert slug.lower() in error_msg, (
        f"Zigbee2MQTT start-failure message should reference the slug "
        f"{slug!r}, got: {error_msg!r}"
    )
    assert any(token in error_msg for token in _START_FAILURE_TOKENS), (
        f"Zigbee2MQTT start-failure message should contain at least one of "
        f"{list(_START_FAILURE_TOKENS)} (schema/config refusal markers), "
        f"got: {error_msg!r}"
    )
