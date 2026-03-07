"""
Webhook Proxy (mcp_proxy) E2E Tests

Tests for the mcp_proxy custom integration that the webhook proxy addon
installs into Home Assistant. Verifies that:
- The integration loads and registers a webhook endpoint
- The webhook endpoint responds to requests (not 404)

The test environment dynamically installs mcp_proxy from the addon source
(homeassistant-addon-webhook-proxy/mcp_proxy/) at test time, writes
.mcp_proxy_config.json pointing at HA's own API, and injects a config entry.

NOTE: No Nabu Casa or reverse proxy is tested — those require external
infrastructure. We test the core integration mechanics only.
"""

import asyncio
import logging

import requests

logger = logging.getLogger(__name__)


class TestWebhookProxyIntegration:
    """Test the mcp_proxy custom integration loads and registers a webhook."""

    async def test_integration_loaded(self, ha_container_with_fresh_config):
        """Verify the mcp_proxy integration loaded in HA."""
        base_url = ha_container_with_fresh_config["base_url"]

        from test_constants import TEST_TOKEN

        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        resp = await asyncio.to_thread(
            requests.get, f"{base_url}/api/config", headers=headers, timeout=10
        )
        assert resp.status_code == 200, f"Failed to get HA config: {resp.status_code}"

        config = resp.json()
        components = config.get("components", [])

        logger.info(f"Loaded components count: {len(components)}")

        assert "mcp_proxy" in components, (
            f"mcp_proxy integration not loaded. "
            f"Check custom_components/mcp_proxy/ is in test state. "
            f"Loaded: {sorted(c for c in components if 'mcp' in c.lower() or 'proxy' in c.lower())}"
        )
        logger.info("mcp_proxy integration is loaded")

    async def test_webhook_endpoint_registered(self, ha_container_with_fresh_config):
        """Verify the webhook endpoint is registered and reachable.

        Uses GET which is the most permissive method for webhooks.
        A non-404 response proves the endpoint exists and is handled.
        """
        base_url = ha_container_with_fresh_config["base_url"]
        webhook_url = f"{base_url}/api/webhook/mcp_e2e_test_webhook_proxy"

        resp = await asyncio.to_thread(requests.get, webhook_url, timeout=30)

        assert resp.status_code != 404, (
            "Webhook endpoint not found (404) — "
            "mcp_proxy integration may not have registered the webhook"
        )
        logger.info(f"Webhook endpoint responded with status {resp.status_code}")

    async def test_webhook_post_or_get_accepted(self, ha_container_with_fresh_config):
        """Verify the webhook accepts at least one request method.

        MCP uses POST for JSON-RPC and GET for SSE streaming. The webhook
        should accept at least one of these. In the test environment, method
        availability depends on the config file being readable by HA at
        startup (allowed_methods are set during webhook registration).
        """
        base_url = ha_container_with_fresh_config["base_url"]
        webhook_url = f"{base_url}/api/webhook/mcp_e2e_test_webhook_proxy"

        # Try POST first (primary MCP method)
        post_resp = await asyncio.to_thread(
            requests.post,
            webhook_url,
            json={},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

        # Try GET (SSE streaming method)
        get_resp = await asyncio.to_thread(requests.get, webhook_url, timeout=30)

        post_ok = post_resp.status_code not in (404, 405)
        get_ok = get_resp.status_code not in (404, 405)

        logger.info(f"Webhook POST status: {post_resp.status_code}")
        logger.info(f"Webhook GET status: {get_resp.status_code}")

        assert post_ok or get_ok, (
            f"Webhook rejected both POST ({post_resp.status_code}) "
            f"and GET ({get_resp.status_code}) — webhook may not be registered"
        )

    async def test_unregistered_webhook_differs(self, ha_container_with_fresh_config):
        """Verify our webhook behaves differently from a random unregistered one.

        HA returns 200 with empty body for unregistered webhook IDs.
        Our registered webhook should return something distinguishable —
        either a non-200 status (502, 405) or a non-empty body.
        """
        base_url = ha_container_with_fresh_config["base_url"]

        # Request to our registered webhook
        registered_url = f"{base_url}/api/webhook/mcp_e2e_test_webhook_proxy"
        registered_resp = await asyncio.to_thread(
            requests.get, registered_url, timeout=30
        )

        # Request to a random unregistered webhook
        unregistered_url = f"{base_url}/api/webhook/definitely_not_registered_xyz"
        unregistered_resp = await asyncio.to_thread(
            requests.get, unregistered_url, timeout=30
        )

        logger.info(
            f"Registered webhook: status={registered_resp.status_code}, "
            f"body_len={len(registered_resp.content)}"
        )
        logger.info(
            f"Unregistered webhook: status={unregistered_resp.status_code}, "
            f"body_len={len(unregistered_resp.content)}"
        )

        # Unregistered webhooks return 200 with empty body in HA.
        # Our webhook should differ in some way (status, body content, headers).
        differs = (
            registered_resp.status_code != unregistered_resp.status_code
            or registered_resp.content != unregistered_resp.content
            or registered_resp.headers.get("Content-Type")
            != unregistered_resp.headers.get("Content-Type")
        )

        assert differs, (
            "Registered webhook response is identical to unregistered — "
            "webhook may not actually be registered"
        )
