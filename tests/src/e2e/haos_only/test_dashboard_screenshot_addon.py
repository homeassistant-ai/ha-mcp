"""Dashboard screenshot-engine addon runtime E2E for the HAOS test tier.

The screenshot engine addon (``homeassistant-addon-screenshot/``, vendored
ha-puppet) is baked into the qcow2 with ``boot: manual`` (see
``tests/haos_image_build/build_image.py::install_screenshot_addon``). The
bake validates that the addon installs cleanly and pre-builds its Chromium
Docker image; a session fixture starts it so runtime tests run once per
session against a known-good container.

Crucially, the bake sets NO ``access_token`` option, so the engine exercises
its Supervisor-token AUTO-AUTH path (``ha-puppet/const.js`` falls back to
``SUPERVISOR_TOKEN`` when no token is configured). This is the path "whoever
installs the addon" hits with zero setup — proving it works end to end is the
point of this module.

Tests:

1. The addon reaches ``started`` and serves screenshots.
2. ``ha_get_dashboard_screenshot`` returns a valid, correctly-sized PNG of
   the default dashboard (engine render + tool wiring + Image return).
3. ``ha_config_get_dashboard(include_screenshot=True)`` returns config + PNG.
4. ``ha_config_set_dashboard(return_screenshot=True)`` returns the write
   result + a PNG (the dashboard create-and-see loop).
5. AUTH PROOF: with a deliberately-invalid configured token (which overrides
   auto-auth), the engine can only reach the login screen and FAILS to render
   a real dashboard, whereas the no-token auto-auth baseline renders a valid
   PNG. The contrast proves the Supervisor token is what authenticates.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from ..utilities.assertions import parse_mcp_result, safe_call_tool
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

LOG = logging.getLogger(__name__)

# inaddon_only: the screenshot engine is reachable only from a server that
# shares the Supervisor container network and holds a SUPERVISOR_TOKEN — i.e.
# the ha-mcp dev addon running INSIDE the booted HAOS (the inaddon tier). On
# the external-HAOS tier the MCP server runs in-process on the CI runner, which
# has no SUPERVISOR_TOKEN and cannot route to the engine's internal addon
# hostname, so these would fail there rather than test anything. Same
# constraint the port= proxy test documents in test_manage_addon_modes.py.
pytestmark = [pytest.mark.haos_only, pytest.mark.inaddon_only]

SCREENSHOT_ADDON_SLUG = "local_ha_mcp_screenshot"
DEFAULT_DASHBOARD_PATH = "lovelace/0"

STOPPED_STATES: frozenset[str] = frozenset({"stopped", "boot_fail", "unknown", "error"})

_STATE_POLL_TIMEOUT = 90.0
_STATE_POLL_INTERVAL = 1.0
# Chromium cold-start + first render inside the addon is slow; give the
# engine generous time to begin serving valid screenshots after start.
_ENGINE_READY_TIMEOUT = 180.0
# Transient + during-boot signals that should be retried (not treated as a
# hard failure) while polling the engine. AssertionError is included because
# _png_dimensions/_screenshot raise it on a not-yet-PNG body during cold
# start; any OTHER exception (ToolError, TypeError, ...) is a real wiring
# failure and must surface immediately rather than burn the timeout (#1266).
_ENGINE_POLL_RETRY_ERRORS = (*_POLLING_TRANSIENT_ERRORS, AssertionError)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Image helpers (Pillow is not a dependency — parse PNG header directly)
# ---------------------------------------------------------------------------


def _extract_png_bytes(result: Any) -> bytes | None:
    """Pull raw PNG bytes from an MCP CallToolResult's image content block."""
    content = getattr(result, "content", None)
    if not content:
        return None
    import base64

    for block in content:
        if getattr(block, "type", None) == "image" or hasattr(block, "data"):
            data = getattr(block, "data", None)
            if isinstance(data, str):
                try:
                    return base64.b64decode(data)
                except (ValueError, TypeError):
                    continue
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
    return None


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Return (width, height) from a PNG's IHDR chunk. Raises on non-PNG."""
    if data[:8] != _PNG_MAGIC:
        raise AssertionError(
            f"Not a PNG (magic bytes: {data[:8]!r}). The engine may have "
            "returned an error page or a non-PNG body."
        )
    # IHDR width/height are the two big-endian uint32s at offset 16.
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def _screenshot(mcp_client: Any, path: str, **kw: Any) -> bytes:
    """Call ha_get_dashboard_screenshot and return the PNG bytes."""
    args: dict[str, Any] = {"dashboard_path": path}
    args.update(kw)
    result = await mcp_client.call_tool("ha_get_dashboard_screenshot", args)
    png = _extract_png_bytes(result)
    assert png is not None, (
        f"ha_get_dashboard_screenshot({path!r}) returned no image content: "
        f"{getattr(result, 'content', result)!r}"
    )
    return png


# ---------------------------------------------------------------------------
# Addon lifecycle helpers (mirror test_webhook_proxy_addon.py)
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
    expected: str | frozenset[str],
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


async def _set_options(mcp_client: Any, slug: str, options: dict[str, Any]) -> None:
    raw = await mcp_client.call_tool(
        "ha_manage_addon", {"slug": slug, "options": dict(options)}
    )
    payload = parse_mcp_result(raw)
    ok = payload.get("success") is True or payload.get("status") == "pending_restart"
    assert ok, f"ha_manage_addon options write failed: {payload}"


async def _engine_diagnostics(mcp_client: Any) -> str:
    """Best-effort: the engine add-on's Supervisor state + container stdout.

    Embedded in the timeout message so a crash-on-start is self-diagnosing in
    CI (the engine's stdout is not in the bundled HAOS diagnostics artifact).
    """
    parts: list[str] = []
    try:
        detail = await _get_addon_detail(mcp_client, SCREENSHOT_ADDON_SLUG)
        parts.append(f"addon state={detail.get('state')!r}")
    except Exception as exc:  # pragma: no cover - diagnostics are best-effort
        parts.append(f"(could not read addon state: {exc})")
    try:
        raw = await mcp_client.call_tool(
            "ha_get_logs", {"source": "supervisor", "slug": SCREENSHOT_ADDON_SLUG}
        )
        log_text = parse_mcp_result(raw).get("log", "")
        if isinstance(log_text, str) and log_text.strip():
            parts.append("engine log tail:\n" + log_text[-2000:])
    except Exception as exc:  # pragma: no cover - diagnostics are best-effort
        parts.append(f"(could not read engine log: {exc})")
    return " | ".join(parts)


async def _wait_engine_serving(
    mcp_client: Any, *, context: str = "addon start"
) -> None:
    """Poll the standalone tool until the engine returns a valid PNG.

    Retries only on transient transport blips and on AssertionError (the
    engine returns a not-yet-PNG body during Chromium cold start). Any other
    exception is a genuine wiring/config failure (e.g. ToolError from a
    disabled feature flag) and surfaces immediately instead of burning the
    timeout — see wait_helpers.py / issue #1266.
    """
    deadline = time.monotonic() + _ENGINE_READY_TIMEOUT
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            png = await _screenshot(mcp_client, DEFAULT_DASHBOARD_PATH)
            _png_dimensions(png)  # validates magic
            return
        except _ENGINE_POLL_RETRY_ERRORS as exc:
            last_err = exc
        await asyncio.sleep(2.0)
    diagnostics = await _engine_diagnostics(mcp_client)
    raise AssertionError(
        f"Screenshot engine did not serve a valid PNG within "
        f"{_ENGINE_READY_TIMEOUT}s ({context}). Last error: {last_err!r}. "
        f"Engine diagnostics: {diagnostics}"
    )


# ---------------------------------------------------------------------------
# Module-scope fixture: start the engine addon for this module's lifetime.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def screenshot_engine_started(mcp_client: Any) -> Any:
    """Start the baked (boot=manual) screenshot engine for this module."""
    result = await _addon_action(mcp_client, SCREENSHOT_ADDON_SLUG, "start")
    assert result.get("success"), (
        f"Fixture failed to start screenshot engine addon: {result}"
    )
    await _wait_for_state(mcp_client, SCREENSHOT_ADDON_SLUG, "started")
    await _wait_engine_serving(mcp_client)
    try:
        yield
    finally:
        try:
            await _addon_action(mcp_client, SCREENSHOT_ADDON_SLUG, "stop")
            await _wait_for_state(
                mcp_client, SCREENSHOT_ADDON_SLUG, STOPPED_STATES, timeout=30.0
            )
        except Exception:  # pragma: no cover - cleanup best-effort
            LOG.exception("Teardown stop of screenshot engine addon failed")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_addon_started_after_fixture(
    mcp_client: Any, screenshot_engine_started: Any
) -> None:
    """Fixture brought the bake-installed engine addon to ``started``."""
    detail = await _get_addon_detail(mcp_client, SCREENSHOT_ADDON_SLUG)
    assert detail.get("state") == "started", (
        f"Screenshot engine should be ``started`` after fixture; "
        f"got state={detail.get('state')!r}"
    )


async def test_get_dashboard_screenshot_returns_png(
    mcp_client: Any, screenshot_engine_started: Any
) -> None:
    """ha_get_dashboard_screenshot returns a valid PNG of the requested size.

    Auto-auth path (no token configured): a valid, correctly-dimensioned,
    non-trivial PNG proves the engine launched Chromium, authenticated to HA
    Core via the Supervisor token, navigated the dashboard, and rendered.
    """
    png = await _screenshot(mcp_client, DEFAULT_DASHBOARD_PATH, width=1024, height=768)
    width, height = _png_dimensions(png)
    assert (width, height) == (1024, 768), (
        f"Rendered PNG is {width}x{height}, expected 1024x768."
    )
    assert len(png) > 3000, (
        f"Rendered PNG is suspiciously small ({len(png)} bytes) — the engine "
        "may have returned a blank/error frame."
    )


async def test_get_dashboard_include_screenshot(
    mcp_client: Any, screenshot_engine_started: Any
) -> None:
    """ha_config_get_dashboard(include_screenshot=True) returns config + PNG."""
    raw = await mcp_client.call_tool(
        "ha_config_get_dashboard",
        {"url_path": "default", "include_screenshot": True},
    )
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"get_dashboard failed: {payload}"
    png = _extract_png_bytes(raw)
    assert png is not None, (
        "include_screenshot=True did not return an image content block "
        f"(warnings: {payload.get('warnings')})"
    )
    _png_dimensions(png)


async def test_set_dashboard_return_screenshot(
    mcp_client: Any, screenshot_engine_started: Any
) -> None:
    """ha_config_set_dashboard(return_screenshot=True) returns result + PNG.

    The dashboard create-and-see loop in a single call.
    """
    url_path = "screenshot-e2e-dash"
    config = {
        "views": [
            {
                "title": "Screenshot E2E",
                "cards": [{"type": "markdown", "content": "# Screenshot E2E"}],
            }
        ]
    }
    try:
        raw = await mcp_client.call_tool(
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config": config,
                "title": "Screenshot E2E",
                "return_screenshot": True,
            },
        )
        payload = parse_mcp_result(raw)
        assert payload.get("success"), f"set_dashboard failed: {payload}"
        png = _extract_png_bytes(raw)
        assert png is not None, (
            "return_screenshot=True did not return an image content block "
            f"(warnings: {payload.get('warnings')})"
        )
        _png_dimensions(png)
    finally:
        try:
            await mcp_client.call_tool(
                "ha_config_delete_dashboard", {"url_path": url_path}
            )
        except Exception:  # pragma: no cover - cleanup best-effort
            LOG.exception("Failed to delete screenshot E2E dashboard")


async def test_supervisor_token_auth_is_what_authenticates(
    mcp_client: Any, screenshot_engine_started: Any
) -> None:
    """Auto-auth renders a real dashboard; an invalid token cannot.

    The baked engine has no ``access_token``, so it authenticates via the
    add-on's Supervisor token and renders a valid PNG (asserted as the
    baseline). Setting a deliberately-invalid ``access_token`` overrides that
    with a token the HA frontend rejects, so the engine can only reach the
    login screen — it does NOT produce a valid dashboard render identical to
    the baseline.

    This is deterministic without depending on the engine's exact bad-auth
    behavior: whether it fails to render (engine raises) or renders a
    different (login) page, the outcome must differ from the working auto-auth
    baseline. If auto-auth were a no-op, both would render the same login page
    and match — so this fails closed on a regression. Runs last; restores the
    no-token state in ``finally`` and verifies the restore committed.
    """
    slug = SCREENSHOT_ADDON_SLUG

    # Baseline: auto-auth (the baked, no-token state) renders a valid PNG.
    authed_png = await _screenshot(mcp_client, DEFAULT_DASHBOARD_PATH)
    _png_dimensions(authed_png)
    assert len(authed_png) > 3000, (
        f"Auto-auth baseline render is suspiciously small ({len(authed_png)}B)"
    )

    try:
        await _set_options(
            mcp_client, slug, {"access_token": "invalid-token-for-e2e-contrast"}
        )
        await _addon_action(mcp_client, slug, "restart")
        await _wait_for_state(mcp_client, slug, "started")

        outcome = await _engine_outcome_after_restart(mcp_client)
        if outcome is None:
            # Invalid token blocked rendering entirely (engine raised) — a
            # clear contrast with the working auto-auth baseline.
            pass
        else:
            assert outcome != authed_png, (
                "Invalid-token render is byte-identical to the no-token "
                "auto-auth render — the Supervisor-token auto-auth path is "
                "NOT what authenticates (both produced the same image)."
            )
    finally:
        await _set_options(mcp_client, slug, {"access_token": ""})
        await _addon_action(mcp_client, slug, "restart")
        await _wait_for_state(mcp_client, slug, "started")
        # Confirm the no-token (auto-auth) state actually committed, so a
        # leaked invalid token can't poison the next run's baseline.
        detail = await _get_addon_detail(mcp_client, slug)
        assert (detail.get("options") or {}).get("access_token", "") == "", (
            "Restore of auto-auth (no-token) state did not commit; "
            f"options.access_token still set: {detail.get('options')}"
        )


async def _engine_outcome_after_restart(mcp_client: Any) -> bytes | None:
    """Return the rendered PNG after a restart, or None if the engine responds
    but cannot render (raises a non-transient ToolError).

    Retries only on transient transport blips / cold-start (AssertionError on a
    not-yet-PNG body); a ToolError from the engine itself is a terminal
    "responded but could not render" signal and returns None.
    """
    deadline = time.monotonic() + _ENGINE_READY_TIMEOUT
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await _screenshot(mcp_client, DEFAULT_DASHBOARD_PATH)
        except ToolError:
            return None
        except _ENGINE_POLL_RETRY_ERRORS as exc:
            last_err = exc
        await asyncio.sleep(2.0)
    raise AssertionError(
        f"Engine did not respond after restart within {_ENGINE_READY_TIMEOUT}s: "
        f"{last_err!r}"
    )
