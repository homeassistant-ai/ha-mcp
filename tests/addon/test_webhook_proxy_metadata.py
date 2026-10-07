"""Webhook Proxy base/read-only OAuth discovery response contracts."""

from unittest.mock import patch

import pytest

from . import test_webhook_proxy as proxy
from .test_webhook_proxy import _webhook_proxy_variant  # noqa: F401


class TestUnauthorizedResponseShape:
    """The 401 response on the webhook is the OAuth-discovery entry point.
    Its WWW-Authenticate must point at the provider's protected-resource
    metadata URL — not just contain the word 'Bearer'."""

    @pytest.fixture
    def setup(self, tmp_path):
        oauth, provider = proxy._provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        return oauth, provider

    def test_resource_metadata_url_uses_pinned_base(self, setup):
        oauth, provider = setup
        request = proxy._make_view_request(headers={"Host": "evil.example"})
        with patch.object(oauth.web, "Response") as resp_ctor:
            oauth.build_unauthorized_response(request, provider)
        kwargs = resp_ctor.call_args.kwargs
        ww = kwargs["headers"]["WWW-Authenticate"]
        # Pinned base means evil.example is NOT in the metadata URL
        assert "evil.example" not in ww
        if proxy._scoped_only_prm(oauth):
            # The pointer is the RFC 9728 §3.1 path-scoped URL — the only
            # protected-resource document left.
            assert (
                "https://legit.example/.well-known/oauth-protected-resource"
                "/api/webhook/mcp_webhook_id_aaaa" in ww
            )
        else:
            assert (
                f"https://legit.example{proxy.CURRENT['oauth_base']}/protected-resource" in ww
            )

    @pytest.mark.parametrize("suffix", ["", "/readonly"])
    async def test_readonly_resource_identity(self, setup, suffix):
        oauth, provider = setup
        view = oauth.WellKnownProtectedResourceView(provider)
        if proxy.CURRENT["key"] == "stable" and not getattr(view, "extra_urls", []):
            pytest.skip("readonly metadata has not been promoted to this flavor")
        path = f"/api/webhook/{provider.webhook_id}{suffix}"
        metadata_path = f"/.well-known/oauth-protected-resource{path}"
        assert metadata_path in [view.url, *getattr(view, "extra_urls", [])]
        request = proxy._make_view_request(headers={"Host": "ignored"}, path=path)
        with patch.object(oauth.web, "Response") as response:
            oauth.build_unauthorized_response(request, provider)
        assert response.call_args.kwargs["headers"]["WWW-Authenticate"] == (
            'Bearer realm="MCP Proxy", resource_metadata="https://legit.example'
            f'{metadata_path}"'
        )
        request.path = metadata_path
        with patch.object(oauth.web, "json_response") as response:
            await view.get(request)
        assert response.call_args.args[0] == {
            "resource": f"https://legit.example{path}",
            "authorization_servers": [f"https://legit.example{oauth.OAUTH_BASE}"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
        }
