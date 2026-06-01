"""Tests for the ha_mcp_tools caller-token auth path.

Covers three layers:

1. **Custom component handlers** — token check, unauthorized response shape,
   get_caller_token bootstrap surface.
2. **ha-mcp wrapper helper** (``call_mcp_tools_service``) — fetches and
   caches the token, injects it on every call, refetches on unauthorized.
3. **ha_call_service refusal** — the wrapper-layer block on the
   ``ha_mcp_tools`` domain.

Custom-component tests stub the homeassistant.* imports the same way
``test_custom_component_filesystem.py`` does so they can run without
the real HA package available.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

# --- Custom-component-side imports (must stub HA first) ---------------------

sys.modules.setdefault("voluptuous", MagicMock())
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.components", MagicMock())
sys.modules.setdefault("homeassistant.components.persistent_notification", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.config_validation", MagicMock())
sys.modules.setdefault("homeassistant.helpers.storage", MagicMock())
sys.modules.setdefault("homeassistant.loader", MagicMock())

from custom_components.ha_mcp_tools import (  # noqa: E402
    CALLER_TOKEN_FIELD,
    _caller_is_admin,
    _caller_token_ok,
    _unauthorized_response,
)
from custom_components.ha_mcp_tools.const import DOMAIN  # noqa: E402

# --- ha-mcp wrapper-side imports --------------------------------------------
from ha_mcp.tools.tools_filesystem import (  # noqa: E402
    CALLER_TOKEN_BOOTSTRAP_SERVICE,
    MCP_TOOLS_DOMAIN,
    _reset_caller_token_cache,
    call_mcp_tools_service,
)

# =============================================================================
# Custom-component side: handler-level token check
# =============================================================================


def _fake_hass(token: str | None) -> MagicMock:
    """Build a hass stand-in whose .data carries the expected caller token."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"caller_token": token}} if token is not None else {}
    return hass


def _fake_call(presented_token: str | None, **extra: str) -> MagicMock:
    """Build a ServiceCall stand-in with .data containing the presented token."""
    call = MagicMock()
    payload: dict[str, str] = dict(extra)
    if presented_token is not None:
        payload[CALLER_TOKEN_FIELD] = presented_token
    call.data = payload
    return call


class TestCallerTokenOk:
    """The handler's pre-flight check that gates all dangerous services."""

    def test_accepts_matching_token(self):
        hass = _fake_hass("good-token-xyz")
        call = _fake_call("good-token-xyz")
        assert _caller_token_ok(hass, call) is True

    def test_rejects_missing_token(self):
        hass = _fake_hass("good-token-xyz")
        call = _fake_call(None)
        assert _caller_token_ok(hass, call) is False

    def test_rejects_mismatched_token(self):
        hass = _fake_hass("good-token-xyz")
        call = _fake_call("not-the-right-one")
        assert _caller_token_ok(hass, call) is False

    def test_rejects_when_hass_data_missing_token(self):
        """If setup_entry hasn't run, no caller is authorized."""
        hass = _fake_hass(None)
        call = _fake_call("anything")
        assert _caller_token_ok(hass, call) is False

    def test_rejects_empty_string_token(self):
        hass = _fake_hass("good-token-xyz")
        call = _fake_call("")
        assert _caller_token_ok(hass, call) is False

    @pytest.mark.parametrize("bad_token", [123, ["good-token-xyz"], {"token": "x"}])
    def test_rejects_non_string_presented_token(self, bad_token):
        """isinstance guard must fail-closed on type confusion (the
        ServiceCall.data dict can contain anything voluptuous coerced)."""
        hass = _fake_hass("good-token-xyz")
        call = MagicMock()
        call.data = {CALLER_TOKEN_FIELD: bad_token}
        assert _caller_token_ok(hass, call) is False


class TestCallerIsAdmin:
    """Bootstrap service is admin-gated explicitly (HA doesn't gate
    service calls by default — verified against HA core)."""

    @pytest.mark.asyncio
    async def test_accepts_admin_user(self):
        hass = MagicMock()
        admin = MagicMock(is_admin=True)
        hass.auth.async_get_user = AsyncMock(return_value=admin)
        call = MagicMock()
        call.context.user_id = "admin-uid"
        assert await _caller_is_admin(hass, call) is True

    @pytest.mark.asyncio
    async def test_rejects_non_admin_user(self):
        hass = MagicMock()
        non_admin = MagicMock(is_admin=False)
        hass.auth.async_get_user = AsyncMock(return_value=non_admin)
        call = MagicMock()
        call.context.user_id = "non-admin-uid"
        assert await _caller_is_admin(hass, call) is False

    @pytest.mark.asyncio
    async def test_rejects_unknown_user_id(self):
        """async_get_user returning None (deleted user, stale token) → reject."""
        hass = MagicMock()
        hass.auth.async_get_user = AsyncMock(return_value=None)
        call = MagicMock()
        call.context.user_id = "deleted-uid"
        assert await _caller_is_admin(hass, call) is False

    @pytest.mark.asyncio
    async def test_no_user_context_is_trusted(self):
        """call.context.user_id is None for system-internal events; HA's
        own async_admin_handler_factory treats these as trusted, so we
        match that convention rather than locking everyone out."""
        hass = MagicMock()
        hass.auth.async_get_user = AsyncMock()
        call = MagicMock()
        call.context.user_id = None
        assert await _caller_is_admin(hass, call) is True
        hass.auth.async_get_user.assert_not_awaited()


class TestUnauthorizedResponse:
    """The structured 'unauthorized' reply the helper emits."""

    def test_has_error_code_unauthorized(self):
        """Clients detect via error_code, not by string-matching error text."""
        resp = _unauthorized_response("write_file")
        assert resp["error_code"] == "unauthorized"
        assert resp["success"] is False

    def test_mentions_service_name_in_error(self):
        resp = _unauthorized_response("delete_file")
        assert "delete_file" in resp["error"]

    def test_extra_kwargs_merged(self):
        """list_files expects a 'files' key even on error — extras propagate."""
        resp = _unauthorized_response("list_files", files=[])
        assert resp["files"] == []
        assert resp["error_code"] == "unauthorized"


# =============================================================================
# ha-mcp wrapper side: bootstrap + injection + refetch
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_token_cache_between_tests():
    """Each test starts with an empty per-client token cache."""
    _reset_caller_token_cache()
    yield
    _reset_caller_token_cache()


def _make_client_with_token(token: str) -> AsyncMock:
    """Client that returns ``token`` from the get_caller_token bootstrap and
    echoes payload back for downstream service calls so tests can assert
    that the token was injected."""
    client = AsyncMock()

    # _fetch_caller_token now pre-flights via /api/services to detect old
    # (pre-0.5.0) custom components that lack get_caller_token. The mock
    # must report the bootstrap service as registered or the bootstrap
    # path raises COMPONENT_NOT_INSTALLED before ever calling call_service.
    client.get_services.return_value = [
        {
            "domain": MCP_TOOLS_DOMAIN,
            "services": {
                CALLER_TOKEN_BOOTSTRAP_SERVICE: {},
                "list_files": {},
                "read_file": {},
                "write_file": {},
                "delete_file": {},
                "edit_yaml_config": {},
            },
        }
    ]

    async def fake_call_service(domain, service, payload, **kwargs):
        if domain == MCP_TOOLS_DOMAIN and service == CALLER_TOKEN_BOOTSTRAP_SERVICE:
            # HA wraps response under "service_response" — mirror that so
            # unwrap_service_response finds the token. ``version`` is
            # reported alongside the token so ha-mcp's MIN_COMPONENT_VERSION
            # gate (added with the packages-only-keys PR) sees a current
            # version and doesn't reject the test setup as "too old".
            from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

            return {
                "service_response": {
                    "success": True,
                    "token": token,
                    "version": MIN_COMPONENT_VERSION,
                }
            }
        # Echo the payload so the test can verify token injection.
        return {
            "service_response": {
                "success": True,
                "received_payload": dict(payload),
                "domain": domain,
                "service": service,
            }
        }

    client.call_service.side_effect = fake_call_service
    return client


class TestCallMcpToolsServiceInjectsToken:
    """call_mcp_tools_service must always pass the cached/fetched token."""

    @pytest.mark.asyncio
    async def test_bootstraps_token_on_first_call(self):
        client = _make_client_with_token("server-token-A")
        result = await call_mcp_tools_service(client, "list_files", {"path": "www"})

        # First call to client.call_service should be the bootstrap
        first_call = client.call_service.await_args_list[0]
        assert first_call.args[1] == CALLER_TOKEN_BOOTSTRAP_SERVICE

        # The downstream call_service was invoked with the token in payload
        downstream = client.call_service.await_args_list[1]
        downstream_payload = downstream.args[2]
        assert downstream_payload[CALLER_TOKEN_FIELD] == "server-token-A"
        assert downstream_payload["path"] == "www"

        # And the result came back wrapped — the helper does not unwrap
        # itself; that's the caller's job to keep parity with the prior
        # behavior of self._client.call_service(...).
        echoed = result["service_response"]
        assert echoed["received_payload"][CALLER_TOKEN_FIELD] == "server-token-A"

    @pytest.mark.asyncio
    async def test_caches_token_across_calls(self):
        client = _make_client_with_token("server-token-B")

        await call_mcp_tools_service(client, "list_files", {"path": "www"})
        await call_mcp_tools_service(
            client, "read_file", {"path": "configuration.yaml"}
        )

        # Bootstrap should fire exactly once — second call reuses cached token.
        bootstrap_calls = [
            c
            for c in client.call_service.await_args_list
            if c.args[1] == CALLER_TOKEN_BOOTSTRAP_SERVICE
        ]
        assert len(bootstrap_calls) == 1

    @pytest.mark.asyncio
    async def test_refetches_and_retries_once_on_unauthorized(self):
        """Token rotation flow: cached token is stale → refetch → succeed."""
        client = AsyncMock()
        # Bootstrap service must appear registered or _fetch_caller_token
        # short-circuits with COMPONENT_NOT_INSTALLED before retry happens.
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "write_file": {}},
            }
        ]
        state = {"current_token": "fresh-token", "downstream_calls": 0}

        async def fake_call_service(domain, service, payload, **kwargs):
            if domain == MCP_TOOLS_DOMAIN and service == CALLER_TOKEN_BOOTSTRAP_SERVICE:
                from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

                return {
                    "service_response": {
                        "success": True,
                        "token": state["current_token"],
                        "version": MIN_COMPONENT_VERSION,
                    }
                }
            state["downstream_calls"] += 1
            # First downstream call: simulate stale-cache rejection
            if state["downstream_calls"] == 1:
                return {
                    "service_response": {
                        "success": False,
                        "error_code": "unauthorized",
                        "error": "stale",
                    }
                }
            # Second downstream call: accept
            return {
                "service_response": {
                    "success": True,
                    "received_payload": dict(payload),
                }
            }

        client.call_service.side_effect = fake_call_service

        # Pre-seed the cache with a stale token so the first downstream call
        # uses it, gets rejected, refetches, and retries.
        from ha_mcp.tools.tools_filesystem import _CALLER_TOKEN_CACHE

        _CALLER_TOKEN_CACHE[client] = "stale-token"

        result = await call_mcp_tools_service(client, "write_file", {"path": "www/x"})

        # Two downstream attempts were made
        assert state["downstream_calls"] == 2
        # Second succeeded with the freshly-fetched token
        echoed = result["service_response"]
        assert echoed["received_payload"][CALLER_TOKEN_FIELD] == "fresh-token"

    @pytest.mark.asyncio
    async def test_raises_when_bootstrap_returns_no_token(self):
        """Service exists but returns a malformed response (race condition
        during integration setup, etc.) → structured ToolError, not a bare
        RuntimeError (which the wrapper's ``except Exception`` would
        otherwise re-map to a generic INTERNAL_ERROR)."""
        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        # Bootstrap returns a malformed response
        client.call_service.return_value = {"service_response": {"success": False}}
        with pytest.raises(ToolError, match="did not return a usable token"):
            await call_mcp_tools_service(client, "list_files", {"path": "www"})

    @pytest.mark.asyncio
    async def test_second_unauthorized_response_surfaces_no_further_retry(self):
        """If the refetch+retry path also returns unauthorized (e.g. genuine
        permanent rejection), the wrapper must NOT loop — it returns the
        unauthorized response so the caller surfaces a real error rather
        than spinning forever or silently succeeding."""
        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "write_file": {}},
            }
        ]
        downstream_attempts = {"n": 0}

        async def fake_call_service(domain, service, payload, **kwargs):
            if domain == MCP_TOOLS_DOMAIN and service == CALLER_TOKEN_BOOTSTRAP_SERVICE:
                from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

                return {
                    "service_response": {
                        "success": True,
                        "token": "freshly-fetched",
                        "version": MIN_COMPONENT_VERSION,
                    }
                }
            downstream_attempts["n"] += 1
            return {
                "service_response": {
                    "success": False,
                    "error_code": "unauthorized",
                    "error": "still rejected",
                }
            }

        client.call_service.side_effect = fake_call_service

        result = await call_mcp_tools_service(client, "write_file", {"path": "www/x"})

        # Exactly two downstream attempts — one cached, one after refetch.
        # No third attempt; no silent success.
        assert downstream_attempts["n"] == 2
        inner = result["service_response"]
        assert inner["error_code"] == "unauthorized"
        assert inner["success"] is False

    @pytest.mark.asyncio
    async def test_raises_component_too_old_when_bootstrap_service_missing(self):
        """Old (pre-0.5.0) custom component lacks get_caller_token → actionable
        COMPONENT_NOT_INSTALLED ToolError, not a generic 'no usable token' string.

        This is the failure mode an upgrade-skipping user hits: ha-mcp updates
        but the HACS integration doesn't, so the bootstrap call would otherwise
        hit a 400 from HA. We pre-flight via /api/services to detect this
        and surface a 'update via HACS' message instead.
        """
        client = AsyncMock()
        # Component is installed, but get_caller_token is NOT registered
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {"list_files": {}, "write_file": {}},  # no get_caller_token
            }
        ]
        with pytest.raises(ToolError) as exc_info:
            await call_mcp_tools_service(client, "list_files", {"path": "www"})
        msg = str(exc_info.value)
        assert "too old" in msg
        assert "pre-0.5.0" in msg
        # call_service must NOT have been called — pre-flight rejected upstream
        client.call_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_component_too_old_when_response_missing_version(self):
        """Bootstrap registered but response has no ``version`` field → still
        rejected as "too old" with the same actionable update prompt.

        This covers the 0.5.0 component case after this PR adds the
        version-reporting field: bootstrap service exists (passes the
        first gate) but the older code path returns ``{success, token}``
        without ``version``. ha-mcp treats the missing field as the
        signal that the component pre-dates version reporting.
        """
        from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        client.call_service = AsyncMock(
            return_value={
                "service_response": {"success": True, "token": "tok-no-version"}
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await call_mcp_tools_service(client, "list_files", {"path": "www"})
        msg = str(exc_info.value)
        assert "too old" in msg
        assert MIN_COMPONENT_VERSION in msg

    @pytest.mark.asyncio
    async def test_raises_component_too_old_when_version_below_minimum(self):
        """Bootstrap registered + version present but below
        ``MIN_COMPONENT_VERSION`` → rejected with the reported version
        in the error so the operator knows exactly what they have.
        """
        from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        # Hardcode an older version to ensure < MIN_COMPONENT_VERSION
        # holds regardless of where the minimum advances over time.
        client.call_service = AsyncMock(
            return_value={
                "service_response": {
                    "success": True,
                    "token": "tok-old",
                    "version": "0.0.1",
                }
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await call_mcp_tools_service(client, "list_files", {"path": "www"})
        msg = str(exc_info.value)
        assert "too old" in msg
        assert "0.0.1" in msg
        assert MIN_COMPONENT_VERSION in msg

    @pytest.mark.asyncio
    async def test_version_at_minimum_accepted(self):
        """A component reporting exactly ``MIN_COMPONENT_VERSION`` is accepted."""
        from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        client.call_service = AsyncMock(
            return_value={
                "service_response": {
                    "success": True,
                    "token": "tok-current",
                    "version": MIN_COMPONENT_VERSION,
                }
            }
        )
        # No exception means the gate passed and the downstream
        # list_files call also went through.
        await call_mcp_tools_service(client, "list_files", {"path": "www"})

    @pytest.mark.asyncio
    async def test_version_above_minimum_accepted(self):
        """A component reporting a version strictly ABOVE
        ``MIN_COMPONENT_VERSION`` is accepted. Guards against a future
        ``<`` → ``<=`` flip in the gate that would mis-reject
        legitimate upgrades (the inverse direction of the
        below-minimum test)."""
        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        # Hardcoded "0.99.0" so a future MIN bump still sorts BELOW it.
        client.call_service = AsyncMock(
            return_value={
                "service_response": {
                    "success": True,
                    "token": "tok-future",
                    "version": "0.99.0",
                }
            }
        )
        # No exception ⇒ accepted.
        await call_mcp_tools_service(client, "list_files", {"path": "www"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_version",
        [
            "1.x.0",  # non-numeric segment
            "1.2.3-beta",  # pre-release suffix (most likely real-world case)
            "v1.2.3",  # git-tag prefix
            "latest",  # non-numeric placeholder
        ],
    )
    async def test_malformed_version_string_rejected(self, bad_version):
        """A version _version_tuple can't parse (non-numeric segment like
        ``'1.x.0'``, a pre-release suffix, a ``v`` prefix, a placeholder)
        is rejected fail-closed rather than letting an integer prefix sneak
        past the gate (``'1.x.0'`` → ``(1, 0, 0)`` would otherwise compare
        ``>= (0, 5, 1)`` and falsely accept). It routes to a distinct
        "malformed version" error (reinstall / file-issue), NOT the
        "too old / update via HACS" remediation — updating can't fix a
        manifest whose version is wrong.

        (The empty string is handled one guard earlier as a missing-version
        "too old" case, so it never reaches this branch.)
        """
        client = AsyncMock()
        client.get_services.return_value = [
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {CALLER_TOKEN_BOOTSTRAP_SERVICE: {}, "list_files": {}},
            }
        ]
        client.call_service = AsyncMock(
            return_value={
                "service_response": {
                    "success": True,
                    "token": "tok-bad-version",
                    "version": bad_version,
                }
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await call_mcp_tools_service(client, "list_files", {"path": "www"})
        msg = str(exc_info.value)
        assert "malformed version" in msg
        assert bad_version in msg
        assert "Reinstall" in msg
        # Must NOT route to the "too old / update" remediation.
        assert "too old" not in msg


# =============================================================================
# ha_call_service refusal of the ha_mcp_tools domain
# =============================================================================


class TestHaCallServiceRefusesMcpToolsDomain:
    """ha_call_service must not forward to ha_mcp_tools — the dedicated
    wrapper tools are the only supported path into that domain."""

    @pytest.mark.asyncio
    async def test_blocks_ha_mcp_tools_domain(self):
        from ha_mcp.tools.tools_service import ServiceTools

        # Use a real ServiceTools instance with a mocked client — we never
        # reach the client because the refusal is the first check.
        tools = ServiceTools.__new__(ServiceTools)
        tools._client = AsyncMock()
        tools._device_tools = AsyncMock()

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_service(
                domain="ha_mcp_tools", service="write_file", data={"path": "www/x"}
            )

        # Error message should point the LLM at the dedicated tool.
        msg = str(exc_info.value)
        assert "ha_mcp_tools" in msg
        assert any(
            name in msg
            for name in ("ha_write_file", "ha_list_files", "ha_config_set_yaml")
        )
        # The downstream client was never touched.
        tools._client.call_service.assert_not_called()

    @pytest.mark.parametrize(
        "variant",
        ["HA_MCP_TOOLS", "Ha_Mcp_Tools", "  ha_mcp_tools  ", " HA_MCP_TOOLS "],
    )
    @pytest.mark.asyncio
    async def test_blocks_case_and_whitespace_variants(self, variant):
        """HA core's ServiceRegistry.async_call lowercases the domain on its
        fallback lookup, so a mixed-case `HA_MCP_TOOLS` would otherwise slip
        past the exact-string refusal and still resolve downstream. The
        refusal must normalize case + whitespace to match."""
        from ha_mcp.tools.tools_service import ServiceTools

        tools = ServiceTools.__new__(ServiceTools)
        tools._client = AsyncMock()
        tools._device_tools = AsyncMock()

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_service(
                domain=variant, service="write_file", data={"path": "www/x"}
            )
        assert "ha_mcp_tools" in str(exc_info.value)
        tools._client.call_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_block_other_domains(self):
        """light.turn_on must still pass the refusal gate — only ha_mcp_tools is refused."""
        from ha_mcp.tools.tools_service import ServiceTools

        tools = ServiceTools.__new__(ServiceTools)
        client = AsyncMock()
        client.call_service.return_value = {"context": {"id": "ctx"}, "result": []}
        tools._client = client
        tools._device_tools = AsyncMock()

        # Should NOT raise the domain-refusal ToolError. (May still raise
        # downstream for unrelated reasons; we just assert we got past
        # the refusal gate.) wait=False skips the state-change-verification
        # path so this test focuses on the refusal logic alone.
        try:
            await tools.ha_call_service(
                domain="light",
                service="turn_on",
                entity_id="light.kitchen",
                wait=False,
            )
        except ToolError as exc:
            assert "ha_mcp_tools" not in str(exc), (
                "Refusal incorrectly triggered for non-ha_mcp_tools domain"
            )
