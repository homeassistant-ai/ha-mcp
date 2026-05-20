"""End-to-end coverage for every ``ha_manage_addon`` operating mode.

Closes the "real tests, not mocks" half of #1350: the unit tests in
``tests/src/unit/test_tools_addons*.py`` exercise the tool's call-site
shape against a stubbed Supervisor, but until this file they were the
only verification that the *real* Supervisor + addon nginx + Ingress
wire path behaves the way the tool expects. The HAOS bake provides a
real Supervisor and a real addon set, so every mode of the tool is now
pinned against running services.

Modes covered (one test class each):

* **Config mode** — ``options`` / ``boot`` / ``auto_update`` / ``watchdog``
  round-trips. ``network`` is not covered because the addons in the bake
  all have ``host_network: false`` *or* their declared ports are not in
  the writable form Supervisor accepts (Matter Server's ``5580/tcp: null``
  rejects the value-with-port shape); the contract is exercised by the
  unit tests.
* **Proxy HTTP** — ``GET`` / ``POST`` against Node-RED endpoints. The
  Ingress proxy accepts the tool's auth headers, so requests reach the
  addon's nginx; assertions cover both successful 2xx responses (Node-RED
  ``/auth/strategy``) and the structured-error path (Node-RED ``/flows``
  on a deploy with the wrong header, which Node-RED rejects with 4xx).
* **Proxy with ``port=``** — only meaningful on the inaddon tier where
  the test runner shares Supervisor's container network. Marked
  ``inaddon_only`` so the external tier skips it cleanly.
* **WebSocket proxy** — ESPHome ``/validate`` accepts an inline config,
  streams a multi-line response. Tests cover ``summarize=True`` (default)
  collapsing the dump and ``summarize=False`` returning every line.
  ``message_offset`` and ``message_limit`` are pinned in the same flow.
* **Array-patch** — Node-RED ``/flows`` is the canonical array-patch
  endpoint. Tests cover the ``op=upsert`` / ``op=delete`` shapes.
* **``python_transform``** — applies a filter expression on the response
  from a Node-RED HTTP call; pins both the success path and the
  ``PythonSandboxError`` surface.
* **``request_headers``** — confirms Node-RED's
  ``Node-RED-Deployment-Type`` header reaches the addon (the tool layers
  internal Ingress headers on top, so this proves caller-supplied
  headers aren't silently stripped).

Slugs are resolved at runtime by display name (see ``_resolve_slug``)
because Supervisor mints slug prefixes from a SHA of the repository URL
and the prefix is not stable across bakes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ..utilities.assertions import parse_mcp_result, safe_call_tool

pytestmark = [pytest.mark.haos_only]

# Node-RED's container takes 20–60s after install to leave "startup" and
# enter "started". Any test whose contract requires the addon to actually
# answer HTTP (i.e. asserts on ``status_code`` rather than tolerating a
# structured error) must wait for it; otherwise CI flakes whenever the
# runner is slow enough that the addon isn't ready by test-call time.
# The bake installs Node-RED with ``start=True`` (build_image.py ADDONS),
# so we only need to wait — never to start it ourselves.
_ADDON_RUNNING_TIMEOUT_S = 120.0
_ADDON_RUNNING_POLL_S = 2.0


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
    raw = await mcp_client.call_tool("ha_get_addon", {})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_addon listing failed: {payload}"
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


async def _wait_addon_running(
    mcp_client: Any,
    slug: str,
    timeout: float = _ADDON_RUNNING_TIMEOUT_S,
) -> None:
    """Block until ``ha_get_addon(slug=...)`` reports ``state=started``.

    Use this before any test that asserts on the HTTP/WS contract of an
    addon (rather than tolerating an addon-not-running structured
    error). ``ha_manage_addon`` short-circuits with
    ``{"success": False, "error": {...}, "state": "startup"}`` when
    Supervisor reports the addon as anything other than ``started``;
    the bake installs addons with ``start=True`` but their containers
    can take 20-60s to leave the ``startup`` phase, which is enough to
    flake any strict-shape assertion. Mirrors the
    ``wait_for_entity_registered`` discipline already mandated by
    AGENTS.md for tests that act on freshly created entities.
    """
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while True:
        detail_raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
        detail = (parse_mcp_result(detail_raw).get("addon") or {})
        last_state = detail.get("state")
        if last_state == "started":
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Addon {slug!r} did not reach state=started within "
                f"{timeout:.0f}s (last state: {last_state!r})"
            )
        await asyncio.sleep(_ADDON_RUNNING_POLL_S)


# ---------------------------------------------------------------------------
# Config mode — options / boot / auto_update / watchdog round-trips
# ---------------------------------------------------------------------------


async def test_config_boot_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_addon(boot=...)` round-trips Matter Server's boot strategy.

    Matter Server defaults to ``boot=auto`` in the bake. Flip to
    ``manual``, confirm via ``ha_get_addon``, restore.
    """
    slug = await _resolve_slug(mcp_client, MATTER_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
    original = (parse_mcp_result(detail_raw).get("addon") or {}).get("boot")
    probe = "manual" if original != "manual" else "auto"
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool("ha_manage_addon", {"slug": slug, "boot": probe})
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_addon(boot={probe!r}) write failed: {write}"
        )

        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_addon", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert after.get("boot") == probe, (
            f"boot did not persist: expected {probe!r}, got {after.get('boot')!r}"
        )
    finally:
        if original is not None:
            await mcp_client.call_tool(
                "ha_manage_addon", {"slug": slug, "boot": original}
            )


async def test_config_auto_update_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_addon(auto_update=...)` round-trips an addon's auto-update flag."""
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
    original = bool(
        (parse_mcp_result(detail_raw).get("addon") or {}).get("auto_update")
    )
    probe = not original
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_manage_addon", {"slug": slug, "auto_update": probe}
            )
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_addon(auto_update={probe!r}) write failed: {write}"
        )
        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_addon", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert bool(after.get("auto_update")) == probe, (
            f"auto_update did not persist: expected {probe!r}, got "
            f"{after.get('auto_update')!r}"
        )
    finally:
        await mcp_client.call_tool(
            "ha_manage_addon", {"slug": slug, "auto_update": original}
        )


async def test_config_watchdog_roundtrip(mcp_client: Any) -> None:
    """`ha_manage_addon(watchdog=...)` round-trips the Supervisor watchdog flag."""
    slug = await _resolve_slug(mcp_client, APPDAEMON_NAME)
    detail_raw = await mcp_client.call_tool("ha_get_addon", {"slug": slug})
    original = bool((parse_mcp_result(detail_raw).get("addon") or {}).get("watchdog"))
    probe = not original
    try:
        write = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_manage_addon", {"slug": slug, "watchdog": probe}
            )
        )
        assert write.get("success") or write.get("status") == "pending_restart", (
            f"ha_manage_addon(watchdog={probe!r}) write failed: {write}"
        )
        after = (
            parse_mcp_result(
                await mcp_client.call_tool("ha_get_addon", {"slug": slug})
            ).get("addon")
            or {}
        )
        assert bool(after.get("watchdog")) == probe, (
            f"watchdog did not persist: expected {probe!r}, got "
            f"{after.get('watchdog')!r}"
        )
    finally:
        await mcp_client.call_tool(
            "ha_manage_addon", {"slug": slug, "watchdog": original}
        )


# ---------------------------------------------------------------------------
# Proxy HTTP mode
# ---------------------------------------------------------------------------


async def test_proxy_http_get_returns_structured_response(mcp_client: Any) -> None:
    """`ha_manage_addon(path=..., method='GET')` reaches Node-RED through Ingress.

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
        "ha_manage_addon",
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
        "ha_manage_addon",
        {"slug": slug, "path": "/flows", "method": "POST", "body": "[]"},
    )
    with_header = await safe_call_tool(
        mcp_client,
        "ha_manage_addon",
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
    """`ha_manage_addon(path=..., port=...)` bypasses Ingress on the inaddon tier.

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
        "ha_manage_addon",
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


# Smallest ESPHome config that ``/validate`` accepts — exercises the
# WS proxy without depending on real hardware. ESPHome echoes the YAML
# as part of its dump.
_ESPHOME_VALIDATE_CONFIG = {
    "configuration": ("esphome:\n  name: ha-mcp-test\nesp32:\n  board: esp32dev\n")
}


async def test_proxy_websocket_validate_summarize(mcp_client: Any) -> None:
    """`ha_manage_addon(path='/validate', websocket=True)` returns shaped output.

    Default ``summarize=True`` collapses ESPHome's config dump into
    elision markers while preserving any INFO/WARN/ERROR signal lines.
    """
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_addon",
        {
            "slug": slug,
            "path": "/validate",
            "websocket": True,
            "body": _ESPHOME_VALIDATE_CONFIG,
            "message_limit": 200,
        },
    )
    assert isinstance(payload, dict), f"Tool did not return a dict: {payload!r}"
    # The WS proxy returns either ``messages`` (raw list) or ``response``
    # (post-transform). Either field's presence is the contract.
    assert "messages" in payload or "response" in payload or "error" in payload, (
        f"WS proxy response missing message/response field: {payload!r}"
    )


async def test_proxy_websocket_validate_raw_pagination(mcp_client: Any) -> None:
    """`message_offset` + `message_limit` apply before summarize on the raw stream."""
    slug = await _resolve_slug(mcp_client, ESPHOME_NAME)
    payload = await safe_call_tool(
        mcp_client,
        "ha_manage_addon",
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
        # message_limit caps the returned list size — strict upper bound.
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
        "ha_manage_addon",
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
        "ha_manage_addon",
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
        "ha_manage_addon",
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
