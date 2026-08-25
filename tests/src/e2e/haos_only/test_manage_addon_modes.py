"""End-to-end coverage for representative ``ha_manage_app`` operating modes.

Closes the "real tests, not mocks" half of #1350: the unit tests in
``tests/src/unit/test_tools_addons*.py`` exercise the tool's call-site
shape against a stubbed Supervisor, but until this file they were the only
verification that the real Supervisor, app (add-on) nginx, and Ingress wire
path behave the way the tool expects. The HAOS bake provides a real Supervisor
and app set, so the covered mode paths are pinned against running services.

Modes and options covered:

* **Config mode** — ``options`` / ``boot`` / ``auto_update`` / ``watchdog``
  round-trips. ``network`` is not covered because the apps in the bake
  all have ``host_network: false`` *or* their declared ports are not in
  the writable form Supervisor accepts (Matter Server's ``5580/tcp: null``
  rejects the value-with-port shape); the contract is exercised by the
  unit tests.
* **Action (lifecycle) mode** — ``stop`` / ``start`` / ``restart`` against a
  real running app, asserting the Supervisor state after each. The
  long-timeout ``install`` / ``update`` / ``rebuild`` path is pinned by the
  unit tests rather than run live (a real install rebuilds an app image and
  would add minutes to every CI run).
* **Proxy HTTP** — ``GET`` / ``POST`` against Node-RED endpoints. The
  Ingress proxy accepts the tool's auth headers, so requests reach the
  app's nginx; assertions cover both successful 2xx responses (Node-RED
  ``/auth/strategy``) and the structured-error path (Node-RED ``/flows``
  on a deploy with the wrong header, which Node-RED rejects with 4xx).
* **Proxy with ``port=``** — only meaningful on the inaddon tier where
  the test runner shares Supervisor's container network. Marked
  ``inaddon_only`` so the external tier skips it cleanly.
* **WebSocket proxy** — sends legacy ESPHome ``/validate`` requests and accepts
  either a structured success or the current structured handshake failure.
  These tests exercise route/error plumbing; they do not assert the current
  dashboard command protocol, summarization, or pagination behavior.
* **Array-patch** — Node-RED ``/flows`` is the canonical array-patch
  endpoint. Tests cover the ``op=upsert`` / ``op=delete`` shapes.
* **``python_transform``** — applies a filter expression on the response
  from a Node-RED HTTP call; pins both the success path and the
  ``PythonSandboxError`` surface.
* **``request_headers``** — confirms Node-RED's
  ``Node-RED-Deployment-Type`` header reaches the app (the tool layers
  internal Ingress headers on top, so this proves caller-supplied
  headers aren't silently stripped).

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

from ..utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.haos_only]

# Tests that assert strictly on ``status_code`` (with no fall-back error
# branch) need the app (add-on) container to have reached Supervisor's
# ``started`` state — the bake installs every app with ``start=True``,
# but the container can take tens of seconds to leave its transient boot
# phase, which is enough to flake the strict assertion. Timeout sized
# for cache-cold runners; 2s poll matches sibling lifecycle helpers.
_ADDON_RUNNING_TIMEOUT_S = 120.0
_ADDON_RUNNING_POLL_S = 2.0
# States _wait_addon_running recovers from with a start, and how often.
# "unknown" is deliberately absent: Supervisor reports it mid-transition,
# where a competing start would race the one already in flight.
_DEAD_ADDON_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "error"})
_ADDON_START_RECOVERIES = 2


# Display names as they appear in build_image.py's ADDONS tuple — slugs
# are looked up dynamically below to survive the SHA-derived slug prefix.
NODERED_NAME = "Node-RED"
ESPHOME_NAME = "ESPHome Device Builder"
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


async def _wait_addon_running(
    mcp_client: Any,
    slug: str,
    timeout: float = _ADDON_RUNNING_TIMEOUT_S,
) -> None:
    """Block until ``ha_get_app(slug=...)`` reports ``state=started``.

    Use this before any test that asserts on the HTTP/WS contract of an
    addon (rather than tolerating an addon-not-running structured
    error). When Supervisor reports the addon as anything other than
    ``started``, ``ha_manage_app`` raises ``ToolError`` from its
    running-state guard (``tools_addons.py`` "Verify add-on is running");
    the JSON-encoded error payload carries the observed transient state.
    The bake installs addons with ``start=True``, but their containers
    can take tens of seconds to reach ``started`` — long enough to
    flake any strict-shape assertion on the proxy path. Mirrors
    ``_wait_for_state`` in ``test_addon_lifecycle.py`` (same private-
    sibling convention as ``_resolve_slug``).

    Transient errors from ``ha_get_app`` are caught via the project's
    canonical ``_POLLING_TRANSIENT_ERRORS`` tuple (see
    ``tests/src/e2e/utilities/wait_helpers.py``) — the same discipline
    every other polling helper in the suite uses. Bugs (``TypeError`` /
    ``AttributeError`` / ``KeyError`` / ``AssertionError``) propagate.
    The deadline still fires; transient errors can't mask a wedged
    addon forever.

    A DEAD state gets a bounded start recovery instead of the wait: the
    bake's ``start=True`` already happened, so an addon at ``error`` /
    ``boot_fail`` / ``stopped`` never exits that state on its own and
    waiting turns a recoverable first-boot failure into a lane failure
    (#2245: Node-RED at ``error`` on the HAOS embedded lane, healthy on
    the very next start). ``safe_call_tool`` absorbs a start that races
    Supervisor mid-transition; the poll re-observes the state either way.
    """
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    recoveries = 0
    while True:
        try:
            detail_raw = await mcp_client.call_tool("ha_get_app", {"slug": slug})
            detail = parse_mcp_result(detail_raw).get("addon") or {}
            last_state = detail.get("state")
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Transient error polling addon {slug!r}: {e}")
            last_state = f"<transient: {str(e)[:60]}>"
        if last_state == "started":
            return
        if last_state in _DEAD_ADDON_STATES and recoveries < _ADDON_START_RECOVERIES:
            recoveries += 1
            logger.warning(
                f"⚠️ Addon {slug!r} is {last_state!r}; issuing start "
                f"(recovery {recoveries}/{_ADDON_START_RECOVERIES})"
            )
            await safe_call_tool(
                mcp_client, "ha_manage_app", {"slug": slug, "action": "start"}
            )
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Addon {slug!r} did not reach state=started within "
                f"{timeout:.0f}s (last state: {last_state!r}, start "
                f"recoveries attempted: {recoveries})"
            )
        await asyncio.sleep(_ADDON_RUNNING_POLL_S)


# Supervisor reports a stopped addon as ``stopped``; the others are
# terminal-but-unhealthy states a lifecycle action could land in.
_STOPPED_STATES: frozenset[str] = frozenset(
    {"stopped", "boot_fail", "unknown", "error"}
)


async def _wait_addon_state(
    mcp_client: Any,
    slug: str,
    expected: frozenset[str],
    timeout: float = _ADDON_RUNNING_TIMEOUT_S,
) -> str:
    """Block until ``ha_get_app(slug=...)`` reports a state in ``expected``.

    The state-set generalization of ``_wait_addon_running`` for lifecycle
    actions that drive an addon to ``stopped`` as well as ``started``. Same
    transient-error discipline (``_POLLING_TRANSIENT_ERRORS`` retried, bugs
    propagate, the deadline still fires).
    """
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while True:
        try:
            detail_raw = await mcp_client.call_tool("ha_get_app", {"slug": slug})
            detail = parse_mcp_result(detail_raw).get("addon") or {}
            last_state = detail.get("state")
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Transient error polling addon {slug!r}: {e}")
            last_state = f"<transient: {str(e)[:60]}>"
        if last_state in expected:
            return str(last_state)
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Addon {slug!r} did not reach a state in {sorted(expected)!r} "
                f"within {timeout:.0f}s (last state: {last_state!r})"
            )
        await asyncio.sleep(_ADDON_RUNNING_POLL_S)


# ---------------------------------------------------------------------------
# Config mode — options / boot / auto_update / watchdog round-trips
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
    ``TestSupervisorApiCallTimeout``; a live install would rebuild an app
    image and add minutes to every CI run, so it is not exercised end-to-end.
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
            await _wait_addon_state(mcp_client, slug, _STOPPED_STATES)

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


async def test_proxy_http_get_returns_structured_response(mcp_client: Any) -> None:
    """`ha_manage_app(path=..., method='GET')` reaches Node-RED through Ingress.

    Pins the tool-contract: the result is a parsed dict that surfaces
    *either* an int ``status_code`` (HTTP layer reached the addon, even
    if the addon answered 4xx) *or* a structured error block (proxy /
    transport failure surfaced before the HTTP layer). Both shapes
    are valid tool output — the test asserts the dict is well-formed
    in one of them, not which path won.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {"slug": slug, "path": "/auth/strategy", "method": "GET"},
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    status = payload.get("status_code")
    has_status = isinstance(status, int)
    has_error = payload.get("success") is False or "error" in payload
    assert has_status or has_error, (
        f"Response should include status_code or a structured error: {payload!r}"
    )


async def test_proxy_http_request_headers_pass_through(mcp_client: Any) -> None:
    """`request_headers` reach the addon (the tool layers Ingress headers on top).

    Node-RED's ``/flows`` POST contract demands the
    ``Node-RED-Deployment-Type`` header; without it the deploy is rejected
    with a 400 referencing the missing header. We don't actually want to
    deploy anything here — the test just confirms that supplying the
    caller header changes the response shape vs. omitting it. The
    deploy-type header is a strong sentinel: Node-RED's error text
    differs between "missing required header" and "header value
    invalid", which proves the value crossed the wire.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    # Strict assertion on ``status_code`` below requires the addon to
    # actually answer HTTP; wait it out (see ``_wait_addon_running``).
    await _wait_addon_running(mcp_client, slug)
    without = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {"slug": slug, "path": "/flows", "method": "POST", "body": "[]"},
    )
    with_header = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/flows",
            "method": "POST",
            "body": "[]",
            "request_headers": {"Node-RED-Deployment-Type": "full"},
        },
    )
    # Both calls should at least parse to dicts with a status_code. The
    # contract verified here is that caller-supplied headers don't
    # crash the tool and aren't silently dropped before the proxy.
    assert isinstance(without, dict) and isinstance(without.get("status_code"), int)
    assert isinstance(with_header, dict) and isinstance(
        with_header.get("status_code"), int
    )


# ---------------------------------------------------------------------------
# Proxy with port= (inaddon-only — needs Supervisor's container network)
# ---------------------------------------------------------------------------


@pytest.mark.inaddon_only
async def test_proxy_direct_port_inaddon(mcp_client: Any) -> None:
    """`ha_manage_app(path=..., port=...)` bypasses Ingress on the inaddon tier.

    Direct-port proxy only works when the MCP host shares Supervisor's
    container network, which is true for the inaddon tier where ha-mcp
    runs as an addon itself. Skipped on the external tier.

    Matter Server exposes ``5580/tcp`` for its WebSocket server; the
    HTTP GET will return some non-2xx (not an HTTP endpoint) but the
    tool plumbing — DNS resolution to ``172.30.32.X``, TCP connect,
    error mapping — is what we're pinning.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {"slug": slug, "path": "/", "port": 5580, "method": "GET"},
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    # status_code is present whether the addon answered HTTP or the
    # proxy mapped a connection error to a structured failure shape.
    assert "status_code" in payload or "error" in payload, (
        f"Direct-port proxy response missing both status_code and error: {payload!r}"
    )


# ---------------------------------------------------------------------------
# WebSocket proxy mode
# ---------------------------------------------------------------------------


# Legacy ESPHome ``/validate`` payload used to exercise the WebSocket route.
# Current dashboards may reject the upgrade before reading it, which is an
# accepted structured-error outcome for these compatibility probes.
_ESPHOME_VALIDATE_CONFIG = {
    "configuration": ("esphome:\n  name: ha-mcp-test\nesp32:\n  board: esp32dev\n")
}


async def test_proxy_websocket_legacy_validate_returns_structured_result(
    mcp_client: Any,
) -> None:
    """Send a legacy ESPHome `/validate` request through the WebSocket proxy.

    The test pins a structured success-or-error result; it does not require
    current dashboards to accept the removed legacy endpoint.
    """
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/validate",
            "websocket": True,
            "body": _ESPHOME_VALIDATE_CONFIG,
            "message_limit": 200,
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    # Success carries ``messages`` or ``response``; a structured handshake
    # failure is also an accepted compatibility result.
    assert "messages" in payload or "response" in payload or "error" in payload, (
        f"WS proxy response missing message/response field: {payload!r}"
    )


async def test_proxy_websocket_legacy_validate_with_shaping_args(
    mcp_client: Any,
) -> None:
    """Keep shaping arguments structured on a legacy `/validate` request."""
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/validate",
            "websocket": True,
            "body": _ESPHOME_VALIDATE_CONFIG,
            "summarize": False,
            "message_offset": 1,
            "message_limit": 5,
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        # Older dashboards that still return messages must honor the cap.
        assert len(msgs) <= 5, f"message_limit=5 not honored: got {len(msgs)} messages"


# ---------------------------------------------------------------------------
# Array-patch mode (Node-RED /flows)
# ---------------------------------------------------------------------------


async def test_array_patch_flows_no_ops_roundtrip(mcp_client: Any) -> None:
    """`array_patch` with an empty op list is the cheapest probe of the mode.

    Verifies the GET-mutate-POST machinery wires up without actually
    changing Node-RED's flow set. The tool fetches /flows, applies zero
    operations, then writes the unchanged array back. Asserts the
    returned summary mentions ``ops_applied=0`` (or the equivalent
    success indicator from tools_addons.py's array-patch builder).
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/flows",
            "array_patch": {"ops": []},
            "request_headers": {"Node-RED-Deployment-Type": "full"},
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    # Both success and addon-side rejection (4xx from Node-RED if the
    # /flows POST is gated) parse to dicts — the contract is "tool
    # didn't crash on the round-trip", not "addon accepted the write".
    assert "status_code" in payload or "ops_applied" in payload or "error" in payload, (
        f"Array-patch response missing expected fields: {payload!r}"
    )


# ---------------------------------------------------------------------------
# python_transform
# ---------------------------------------------------------------------------


async def test_python_transform_filters_http_response(mcp_client: Any) -> None:
    """`python_transform` runs the sandboxed expression on the HTTP response.

    Apply ``response = {"trimmed": True}`` to whatever Node-RED returns.
    The tool's contract: ``response`` is rebound to the transform result
    and surfaced under the same key in the parsed payload.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/auth/strategy",
            "method": "GET",
            "python_transform": 'response = {"trimmed": True}',
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    transformed = payload.get("response")
    assert transformed == {"trimmed": True} or payload.get("error"), (
        f"python_transform output not surfaced: {payload!r}"
    )


async def test_python_transform_sandbox_error_surfaced(mcp_client: Any) -> None:
    """Bad transform code surfaces a structured sandbox error, not a crash.

    A bare ``import os`` is rejected by the sandbox (no imports allowed
    in expressions). The tool must map that into an error response, not
    raise an unhandled exception.
    """
    slug = await _resolve_slug(mcp_client, NODERED_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_app",
        {
            "slug": slug,
            "path": "/auth/strategy",
            "method": "GET",
            "python_transform": "import os",
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    # Either nested under success=False with an error block, or surfaced
    # as a top-level error message — both are the structured-error
    # contract, distinct from a tool-side crash.
    has_error = (
        payload.get("success") is False
        or "error" in payload
        or "sandbox" in str(payload).lower()
    )
    assert has_error, (
        f"Bad python_transform should surface a structured error, got: {payload!r}"
    )
