"""Final same-VM HAOS TLS regression for issue #2241.

This module intentionally contains exactly one test. The collection hook moves
its ``haos_tls`` item to the end, and xdist ``loadscope`` therefore schedules
this one-test module only after an existing embedded worker drains its ordinary
modules. Core is restarted in that worker's current VM, never a second lane.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from ha_mcp.client import HomeAssistantClient
from ha_mcp.tools.tools_addons import _resolve_http_route
from tests.src.haos_runtime import (
    HA_MCP_SERVER_WEBHOOK_ID,
    HAOS_TLS_CERTIFICATE_PATH,
    HAOS_TLS_KEY_PATH,
    _wait_http_ok,
    build_home_assistant_tls_config,
    clean_home_assistant_http_config,
    configure_home_assistant_http,
    get_home_assistant_http_config,
    promote_home_assistant_http_config,
)

from ..conftest import _wait_for_embedded_webhook_ready
from ..utilities.assertions import parse_mcp_result, safe_call_tool
from .test_manage_addon_modes import (
    NODERED_NAME,
    _resolve_slug,
    _wait_addon_running,
)

pytestmark = [pytest.mark.haos_only]


def _assert_manage_success(payload: dict[str, Any], operation: str) -> None:
    """Accept the two successful Supervisor config/action result shapes."""
    assert (
        payload.get("success") is True or payload.get("status") == "pending_restart"
    ), f"{operation} failed: {payload}"


async def _set_front_door(mcp: Client, slug: str, enabled: bool) -> None:
    """Set Node-RED direct auth, restart it, and wait for the live state."""
    configured = parse_mcp_result(
        await mcp.call_tool(
            "ha_manage_app",
            {"slug": slug, "options": {"leave_front_door_open": enabled}},
        )
    )
    _assert_manage_success(configured, f"set leave_front_door_open={enabled}")
    restarted = parse_mcp_result(
        await mcp.call_tool("ha_manage_app", {"slug": slug, "action": "restart"})
    )
    _assert_manage_success(restarted, "restart Node-RED")
    await _wait_addon_running(mcp, slug)


@pytest.mark.haos_tls
@pytest.mark.timeout(2400)
async def test_manage_app_reproduces_legacy_tls_failure_then_uses_fix(
    ha_container_with_fresh_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce #2241's old mismatch, then prove both fixes on real HAOS.

    The legacy request below uses the old implementation's exact HTTPX shape:
    an ``AsyncClient`` with a timeout and no ``verify`` argument. With the test
    certificate trusted but issued only for ``haos-e2e.local``, its request to
    the real ``https://127.0.0.1/.../flows`` Ingress route must fail with the
    reporter's certificate/IP mismatch. The real MCP tool then makes the same
    call successfully because ``client.verify_ssl=False`` reaches its proxy
    transport. Finally, a real Node-RED 401 proves the front-door hint and the
    hinted config/restart sequence are both actionable.
    """
    container = ha_container_with_fresh_config
    assert container.get("backend") == "haos_embedded", container
    http_base_url = str(container.get("base_url", ""))
    assert http_base_url.startswith("http://"), container
    token = str(container.get("token", ""))
    assert token, container
    ca_path = os.environ.get("HAOS_TEST_TLS_CA_PATH")
    assert ca_path and os.path.isfile(ca_path), (
        "HAOS embedded setup did not stage the issue #2241 TLS certificate"
    )

    http_state = get_home_assistant_http_config(http_base_url, token)
    stable = http_state.get("stable")
    assert isinstance(stable, dict), http_state
    original_http_config = clean_home_assistant_http_config(stable)
    tls_config = build_home_assistant_tls_config(
        stable,
        certificate_path=HAOS_TLS_CERTIFICATE_PATH,
        key_path=HAOS_TLS_KEY_PATH,
    )
    https_base_url = http_base_url.replace("http://", "https://", 1)
    https_webhook_url = f"{https_base_url}/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}"
    tls_requested = False
    https_ready = False

    try:
        tls_requested = True
        assert configure_home_assistant_http(http_base_url, token, tls_config), (
            "Core did not schedule the HTTP-to-HTTPS restart"
        )
        _wait_http_ok(f"{https_base_url}/manifest.json", timeout=300, verify_ssl=False)
        https_ready = True
        assert _wait_for_embedded_webhook_ready(
            https_webhook_url, timeout=180, verify=False
        ), "Embedded MCP webhook did not return after the Core TLS restart"
        # Cancel Core's five-minute pending-config auto-revert before the
        # deliberately thorough Node-RED sequence. The final block stages and
        # promotes the original HTTP config again.
        promote_home_assistant_http_config(https_base_url, token, verify_ssl=False)

        transport = StreamableHttpTransport(url=https_webhook_url, verify=False)
        async with Client(transport) as mcp:
            slug = await _resolve_slug(mcp, NODERED_NAME)
            await _wait_addon_running(mcp, slug)
            detail = (
                parse_mcp_result(await mcp.call_tool("ha_get_app", {"slug": slug})).get(
                    "addon"
                )
                or {}
            )
            options = detail.get("options") or {}
            assert isinstance(options, dict), detail
            assert "leave_front_door_open" in options, detail
            original_front_door = bool(options["leave_front_door_open"])
            front_door_touched = False

            # Resolve the real Ingress route with the same production helper.
            # The only legacy behavior being reproduced is the missing
            # verify=client.verify_ssl argument on the final HTTPX client.
            legacy_ha_client = HomeAssistantClient(
                base_url=https_base_url,
                token=token,
                verify_ssl=False,
            )
            try:
                ingress_url, ingress_headers = await _resolve_http_route(
                    legacy_ha_client, detail, "flows", None
                )
                monkeypatch.setenv("SSL_CERT_FILE", ca_path)
                monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
                with pytest.raises(httpx.ConnectError) as legacy_error:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(30)
                    ) as legacy_http:
                        await legacy_http.request(
                            method="GET",
                            url=ingress_url,
                            headers=ingress_headers,
                            content=None,
                        )
            finally:
                await legacy_ha_client.close()

            mismatch = str(legacy_error.value)
            assert "CERTIFICATE_VERIFY_FAILED" in mismatch, mismatch
            assert "IP address mismatch" in mismatch, mismatch
            assert "127.0.0.1" in mismatch, mismatch

            fixed_ingress = parse_mcp_result(
                await mcp.call_tool(
                    "ha_manage_app",
                    {"slug": slug, "path": "/flows", "method": "GET"},
                )
            )
            assert fixed_ingress.get("success") is True, fixed_ingress
            assert fixed_ingress.get("status_code") == 200, fixed_ingress
            assert isinstance(fixed_ingress.get("response"), list), fixed_ingress

            try:
                front_door_touched = True
                await _set_front_door(mcp, slug, False)
                locked = await safe_call_tool(
                    mcp,
                    "ha_manage_app",
                    {
                        "slug": slug,
                        "path": "/flows",
                        "method": "GET",
                        "port": 1880,
                    },
                )
                assert locked.get("success") is False, locked
                assert locked.get("status_code") == 401, locked
                locked_options = (locked.get("addon_config") or {}).get("options") or {}
                assert locked_options.get("leave_front_door_open") is False, locked
                suggestion = str(locked.get("suggestion", ""))
                assert "leave_front_door_open" in suggestion, locked
                assert "ha_manage_app" in suggestion, locked
                assert "restart" in suggestion.lower(), locked
                assert "security" in suggestion.lower(), locked
                assert "ingress" in suggestion.lower(), locked

                # Follow the hint with the same management tool, then prove the
                # formerly rejected direct request works on the real add-on.
                await _set_front_door(mcp, slug, True)
                opened = parse_mcp_result(
                    await mcp.call_tool(
                        "ha_manage_app",
                        {
                            "slug": slug,
                            "path": "/flows",
                            "method": "GET",
                            "port": 1880,
                        },
                    )
                )
                assert opened.get("success") is True, opened
                assert opened.get("status_code") == 200, opened
                assert isinstance(opened.get("response"), list), opened
            finally:
                if front_door_touched:
                    await _set_front_door(mcp, slug, original_front_door)
    finally:
        # If the configure socket closed while Core accepted the command, the
        # assignment above may not have observed readiness. Probe briefly; if
        # HTTPS never came up, wait for Core's five-minute trial auto-revert so
        # the shared fixture never tears down while its expected HTTP URL is dark.
        if tls_requested and not https_ready:
            try:
                _wait_http_ok(
                    f"{https_base_url}/manifest.json", timeout=30, verify_ssl=False
                )
                https_ready = True
            except TimeoutError:
                _wait_http_ok(f"{http_base_url}/manifest.json", timeout=330)
        if tls_requested and https_ready:
            assert configure_home_assistant_http(
                https_base_url,
                token,
                original_http_config,
                verify_ssl=False,
            ), "Core did not schedule the HTTPS-to-HTTP restoration restart"
            _wait_http_ok(f"{http_base_url}/manifest.json", timeout=300)
            restored = get_home_assistant_http_config(http_base_url, token)
            # A failure before TLS promotion restores the already-stable HTTP
            # config by clearing pending, so promote only when one remains.
            if restored.get("pending") is not None:
                promote_home_assistant_http_config(http_base_url, token)
                restored = get_home_assistant_http_config(http_base_url, token)
            assert restored.get("pending") is None, restored
            assert restored.get("active_config_type") == "stable", restored
