"""End-to-end coverage for representative ``ha_manage_app`` operating modes.

The HAOS bake provides a real Supervisor and app set. Lifecycle and config
cases assert live state changes, while HTTP probes require real responses over
Ingress and direct-port routes. The WebSocket proxy remains unit-tested because
the baked ESPHome dashboard has no stable, version-current test endpoint. Unit
tests in ``tests/src/unit/test_tools_addons*.py`` cover the remaining call
shapes against a stubbed Supervisor.

Modes and options covered:

* **Config mode** — ``boot`` / ``auto_update`` / ``watchdog`` round-trips.
  ``options`` and ``network`` remain unit-tested.
* **Action (lifecycle) mode** — ``stop`` / ``start`` / ``restart`` against a
  real running app, asserting the Supervisor state after each. The
  in-app self-update guard is checked without submitting an update. Other
  long-timeout ``install`` / ``update`` / ``rebuild`` paths are pinned by unit
  tests rather than run live (a real install rebuilds an app image and would
  add minutes to every CI run).
* **Proxy HTTP** — outside the in-app tier, a ``GET`` smoke check requires a
  successful Node-RED response through Ingress. In-app routing is covered by
  the direct-port probe because ingress session creation is Core-only.
* **Proxy with ``port=``** — only meaningful in the in-app tier, where the
  ha-mcp server under test runs on Supervisor's app network. Marked
  ``inaddon_only`` so other tiers skip it cleanly.
* **Array-patch** — no live mutation round-trip is covered. The E2E test pins
  structured validation for an empty operation list; mutation is unit-tested.
* **``python_transform``** — applies a filter expression on the response
  from a Node-RED HTTP call; pins both the success path and the
  ``PythonSandboxError`` surface.

Slugs are resolved at runtime by display name (see ``_resolve_slug``)
because Supervisor mints slug prefixes from a SHA of the repository URL
and the prefix is not stable across bakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from haos_runtime import HA_MCP_DEV_ADDON_SLUG, is_haos_inaddon_mode

from ..utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]

# Lifecycle state polling allows for cache-cold runners.
_ADDON_RUNNING_TIMEOUT_S = 120.0
_ADDON_RUNNING_POLL_S = 2.0


# Display names as they appear in build_image.py's ADDONS tuple — slugs
# are looked up dynamically below to survive the SHA-derived slug prefix.
NODERED_NAME = "Node-RED"
MATTER_NAME = "Matter Server"
APPDAEMON_NAME = "AppDaemon"


async def _resolve_slug(mcp_client: Any, display_name: str) -> str:
    """Map an addon display name to its Supervisor slug at runtime.

    Mirrors the helper in ``test_addon_lifecycle.py``. Not imported from
    there because pytest's module collection treats sibling test files
    as independent — a shared utility belongs in ``utilities/`` if more
    files start needing it, but for now two private copies is simpler
    than reshuffling the helpers tree.
    """
    raw = await mcp_client.call_tool("ha_get_app", {})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_app listing failed: {payload}"
    for entry in payload.get("addons", []):
        if entry.get("name") == display_name:
            slug = entry.get("slug")
            assert slug, f"Addon {display_name!r} listed without slug: {entry}"
            return str(slug)
    installed = sorted(
        n for n in (a.get("name") for a in payload.get("addons", [])) if n
    )
    pytest.fail(
        f"Addon {display_name!r} not found in installed listing. "
        f"Installed: {installed}. Check build_image.py ADDONS tuple."
    )
    raise AssertionError(
        "unreachable: pytest.fail always raises"
    )  # explicit for CodeQL


# Terminal states that must fail a lifecycle assertion rather than count as
# successful stop completion.
_UNHEALTHY_ADDON_STATES: frozenset[str] = frozenset({"boot_fail", "unknown", "error"})


async def _wait_addon_state(
    mcp_client: Any,
    slug: str,
    expected: frozenset[str],
    *,
    failure_states: frozenset[str] = frozenset(),
    timeout: float = _ADDON_RUNNING_TIMEOUT_S,
) -> str:
    """Block until ``ha_get_app(slug=...)`` reports a state in ``expected``.

    Lifecycle actions use this for transitions to ``stopped`` or ``started``.
    Transient read errors are retried until the deadline.
    States in ``failure_states`` fail immediately. Other unexpected states
    keep polling with the same transient-error discipline until the deadline.
    """
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            detail_raw = await mcp_client.call_tool(
                "ha_get_app", {"slug": slug}, timeout=remaining
            )
            detail = parse_mcp_result(detail_raw).get("addon") or {}
            last_state = detail.get("state")
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Transient error polling addon {slug!r}: {e}")
            last_state = f"<transient: {str(e)[:60]}>"
        if time.monotonic() >= deadline:
            break
        if last_state in expected:
            return str(last_state)
        if last_state in failure_states:
            pytest.fail(
                f"Addon {slug!r} entered unhealthy state {last_state!r}; "
                f"expected one of {sorted(expected)!r}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_ADDON_RUNNING_POLL_S, remaining))
    pytest.fail(
        f"Addon {slug!r} did not reach a state in {sorted(expected)!r} "
        f"within {timeout:.0f}s (last state: {last_state!r})"
    )
    raise AssertionError("unreachable: pytest.fail always raises")


async def _wait_addon_running(
    mcp_client: Any,
    slug: str,
    timeout: float = _ADDON_RUNNING_TIMEOUT_S,
) -> None:
    """Block until an app reaches ``started`` for sibling HAOS tests."""
    await _wait_addon_state(
        mcp_client,
        slug,
        frozenset({"started"}),
        failure_states=frozenset({"boot_fail", "error"}),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Config mode — boot / auto_update / watchdog round-trips
# ---------------------------------------------------------------------------


async def test_config_boot_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_app(boot=...)` round-trips Matter Server's boot strategy.

    Matter Server defaults to ``boot=auto`` in the bake. Flip to
    ``manual``, confirm via ``ha_get_app``, restore.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_app", {"slug": slug})
    original = (parse_mcp_result(detail_raw).get("addon") or {}).get("boot")
    probe = "manual" if original != "manual" else "auto"
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool("ha_manage_app", {"slug": slug, "boot": probe})
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_app(boot={probe!r}) write failed: {write}"
        )

        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_app", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert after.get("boot") == probe, (
            f"boot did not persist: expected {probe!r}, got {after.get('boot')!r}"
        )
    finally:
        if original is not None:
            await mcp_client.call_tool(
                "ha_manage_app", {"slug": slug, "boot": original}
            )


async def test_config_auto_update_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_app(auto_update=...)` round-trips an addon's auto-update flag."""
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_app", {"slug": slug})
    original = bool(
        (parse_mcp_result(detail_raw).get("addon") or {}).get("auto_update")
    )
    probe = not original
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_manage_app", {"slug": slug, "auto_update": probe}
            )
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_app(auto_update={probe!r}) write failed: {write}"
        )
        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_app", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert bool(after.get("auto_update")) == probe, (
            f"auto_update did not persist: expected {probe!r}, got "
            f"{after.get('auto_update')!r}"
        )
    finally:
        await mcp_client.call_tool(
            "ha_manage_app", {"slug": slug, "auto_update": original}
        )


async def test_config_watchdog_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_app(watchdog=...)` round-trips the Supervisor watchdog flag."""
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_app", {"slug": slug})
    original = bool((parse_mcp_result(detail_raw).get("addon") or {}).get("watchdog"))
    probe = not original
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_manage_app", {"slug": slug, "watchdog": probe}
            )
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_app(watchdog={probe!r}) write failed: {write}"
        )
        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_app", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert bool(after.get("watchdog")) == probe, (
            f"watchdog did not persist: expected {probe!r}, got "
            f"{after.get('watchdog')!r}"
        )
    finally:
        await mcp_client.call_tool(
            "ha_manage_app", {"slug": slug, "watchdog": original}
        )


# ---------------------------------------------------------------------------
# Action (lifecycle) mode — stop / start / restart
# ---------------------------------------------------------------------------


@pytest.mark.inaddon_only
@pytest.mark.addon_disruptive
async def test_action_self_update_returns_manual_guidance(mcp_client: Any) -> None:
    """The live self slug is recognized before Supervisor receives an update."""
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_failure(
            "ha_manage_app",
            {"slug": HA_MCP_DEV_ADDON_SLUG, "action": "update"},
            expected_error="cannot update itself",
        )

    assert payload.get("error", {}).get("code") == "SERVICE_CALL_FAILED"
    assert payload.get("self_slug") == HA_MCP_DEV_ADDON_SLUG
    assert any(
        "Apps UI" in suggestion
        for suggestion in payload.get("error", {}).get("suggestions", [])
    )


async def test_action_stop_start_restart_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_app(action=...)` drives a real app (add-on) lifecycle.

    Exercises ``_execute_action_mode`` → ``_supervisor_api_call`` through
    direct Supervisor REST in the in-app lane and Core's ``supervisor/api``
    WebSocket proxy in the external, embedded, and stdio HAOS lanes. It stops, starts,
    and restarts the app while asserting the observed state after each action.
    This also covers the per-action timeout plumbing (stop=60s,
    start/restart=120s, each with a 15-second client margin).

    AppDaemon is the target — it is installed and running in the bake and is
    not proxied by any other test in this module, so cycling its run state in
    one test (restored at the end) does not perturb the proxy/WS suites.

    The long-timeout install/update/rebuild path (1800s) is pinned by
    ``TestSupervisorApiCallTimeout``; the separate self-update test exercises
    only the local guard. A live install would rebuild an app image and add
    minutes to every CI run, so no update operation is submitted end-to-end.
    """
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    async with MCPAssertions(mcp_client) as mcp:
        baseline = await mcp.call_tool_success("ha_get_app", {"slug": slug})
        original = (baseline.get("addon") or {}).get("state")
        assert original in {"started", "stopped"}, (
            f"AppDaemon has no restorable baseline state: {baseline}"
        )

        async def _action(action: str) -> dict[str, Any]:
            payload = await mcp.call_tool_success(
                "ha_manage_app", {"slug": slug, "action": action}
            )
            assert payload.get("action") == action, (
                f"action echo mismatch: expected {action!r}, got {payload!r}"
            )
            return payload

        try:
            await _action("stop")
            await _wait_addon_state(
                mcp_client,
                slug,
                frozenset({"stopped"}),
                failure_states=_UNHEALTHY_ADDON_STATES,
            )

            await _action("start")
            await _wait_addon_state(mcp_client, slug, frozenset({"started"}))

            await _action("restart")
            await _wait_addon_state(mcp_client, slug, frozenset({"started"}))
        finally:
            # Restore the app's original run state so sibling tests (and reruns)
            # see the baked baseline regardless of how this test exited.
            try:
                detail = await safe_call_tool(mcp_client, "ha_get_app", {"slug": slug})
                current = (detail.get("addon") or {}).get("state")
                if current != original:
                    restore_action = "start" if original == "started" else "stop"
                    await safe_call_tool(
                        mcp_client,
                        "ha_manage_app",
                        {"slug": slug, "action": restore_action},
                    )
            except Exception:  # pragma: no cover - cleanup best-effort
                logger.exception(
                    "Failed to restore AppDaemon to original state %s", original
                )


# ---------------------------------------------------------------------------
# Proxy HTTP mode
# ---------------------------------------------------------------------------


async def test_proxy_http_get_returns_successful_response(mcp_client: Any) -> None:
    """`ha_manage_app(path=..., method='GET')` reaches Node-RED through Ingress.

    Requires a real successful response rather than accepting a proxy or
    transport error as evidence that the route works.
    """
    if is_haos_inaddon_mode():
        pytest.skip(
            "In-app servers cannot mint Core-only ingress sessions; "
            "the in-app HTTP route is covered by the direct-port probe"
        )
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success(
            "ha_manage_app",
            {"slug": slug, "path": "/", "method": "GET"},
        )
    status = payload.get("status_code")
    assert isinstance(status, int) and status < 400, (
        f"Node-RED Ingress request did not return a successful status: {payload!r}"
    )


# ---------------------------------------------------------------------------
# Proxy with port= (inaddon-only — needs Supervisor's container network)
# ---------------------------------------------------------------------------


@pytest.mark.inaddon_only
async def test_proxy_direct_port_inaddon(mcp_client: Any) -> None:
    """Exercise direct-port routing from the in-app HAOS tier.

    Direct-port proxy only works when the ha-mcp app shares Supervisor's app
    network, so this probe is skipped on tiers that run ha-mcp elsewhere.

    Matter Server exposes ``5580/tcp`` for its WebSocket server. Any HTTP
    status proves the TCP/HTTP exchange reached that endpoint; the root path
    need not be a successful Matter API request.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {"slug": slug, "path": "/", "port": 5580, "method": "GET"},
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    status = payload.get("status_code")
    assert isinstance(status, int), (
        f"Direct-port probe did not complete an HTTP exchange: {payload!r}"
    )


# ---------------------------------------------------------------------------
# Array-patch mode (Node-RED /flows)
# ---------------------------------------------------------------------------


async def test_array_patch_empty_operations_returns_validation_error(
    mcp_client: Any,
) -> None:
    """An empty array-patch operation list returns a structured validation error."""
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/flows",
            "array_patch": {"operations": []},
            "request_headers": {"Node-RED-Deployment-Type": "full"},
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    assert payload.get("success") is False, payload
    error = payload.get("error")
    assert isinstance(error, dict), payload
    assert error.get("code") == "VALIDATION_FAILED", error
    assert payload.get("parameter") == "array_patch.operations", payload


# ---------------------------------------------------------------------------
# python_transform
# ---------------------------------------------------------------------------


async def test_python_transform_filters_http_response(mcp_client: Any) -> None:
    """`python_transform` runs after a successful Node-RED HTTP response."""
    if is_haos_inaddon_mode():
        pytest.skip(
            "In-app servers cannot mint Core-only ingress sessions; "
            "the in-app HTTP route is covered by the direct-port probe"
        )
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success(
            "ha_manage_app",
            {
                "slug": slug,
                "path": "/",
                "method": "GET",
                "python_transform": 'response = {"trimmed": True}',
            },
        )
    assert payload.get("response") == {"trimmed": True}, payload


async def test_python_transform_sandbox_error_surfaced(mcp_client: Any) -> None:
    """A blocked import surfaces the expected sandbox validation error."""
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_failure(
            "ha_manage_app",
            {
                "slug": slug,
                "path": "/",
                "method": "GET",
                "python_transform": "import os",
            },
            expected_error="Expression validation failed",
        )
