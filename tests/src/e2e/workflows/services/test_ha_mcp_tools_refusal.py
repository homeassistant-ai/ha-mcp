"""E2E coverage for the caller-token auth contract.

Runs cross-lane (no backend marker) — the same assertions execute against
the testcontainer backend, the HAOS external lane (``ha-mcp`` running
externally against booted HAOS), and the HAOS inaddon lane (``ha-mcp``
running inside the dev addon container with the supervisor token):

* ``ha_call_service`` refuses ``domain == ha_mcp_tools`` at the wrapper
  layer. Verified across literal, upper-case, mixed-case, and
  whitespace variants of the domain so the refusal cannot be
  sidestepped by HA core's domain-lowercasing fallback
  (``homeassistant/core.py`` ``ServiceRegistry.async_call``).
* Positive smoke: ``ha_list_files`` round-trips successfully — implicit
  proof that ``call_mcp_tools_service`` bootstrap-fetches the caller
  token via ``ha_mcp_tools.get_caller_token``, injects it, and the
  handler accepts it. In addon mode the supervisor token maps to the
  admin-forced ``hassio_user`` and therefore passes the admin-only
  service registration; in container / external HAOS mode the
  user-supplied admin LLAT does the same.
* Admin gate: a non-admin caller is refused by HA core for every
  ``ha_mcp_tools`` service, independently of the caller-token check.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

from tests.test_constants import NON_ADMIN_TEST_TOKEN

from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
)
from ...utilities.component_services import post_service

logger = logging.getLogger(__name__)

# The caller-token contract is a property of the ha_mcp_tools services, which
# register only in the "File & YAML Tools" config entry (#2292).
pytestmark = pytest.mark.requires_tools_entry

FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"


@pytest.fixture(scope="module")
def _filesystem_feature_flag(ha_container_with_fresh_config):
    """Enable the filesystem feature flag for the bootstrap positive smoke.

    Mirrors ``test_file_operations.py``'s ``filesystem_tools_enabled``
    fixture; this is a thin local copy so this module stays independent
    of the filesystem suite's imports.
    """
    os.environ[FEATURE_FLAG] = "true"
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_for_refusal(
    _filesystem_feature_flag,
    mcp_server,
    mcp_client,
    ha_container_with_fresh_config,
):
    """Yield an MCP client capable of exercising both the refusal and the
    bootstrap-and-inject smoke.

    In inaddon mode ``mcp_server`` is None (the addon is the server);
    the session-scope ``mcp_client`` already speaks to the addon and is
    handed back directly. Outside inaddon mode we wrap an in-process
    FastMCP client around ``mcp_server.mcp`` exactly as the filesystem
    suite does.
    """
    if mcp_server is None:
        yield mcp_client
        return

    from ha_mcp._vendor.fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        yield client


# ---------------------------------------------------------------------------
# ha_call_service refusal of the ha_mcp_tools domain
# ---------------------------------------------------------------------------


class TestHaCallServiceRefusesMcpToolsDomain:
    """Wrapper-layer refusal: ``ha_call_service(domain="ha_mcp_tools", …)``
    must raise before the call is forwarded to HA's service registry."""

    async def test_literal_domain_refused(self, mcp_client_for_refusal):
        """The exact-string ``ha_mcp_tools`` is refused with a clear error
        that points the LLM at the dedicated wrapper tools."""
        result = await safe_call_tool(
            mcp_client_for_refusal,
            "ha_call_service",
            {
                "domain": "ha_mcp_tools",
                "service": "write_file",
                "data": {"path": "www/should-never-be-written.txt", "content": "x"},
            },
        )

        error_msg = extract_error_message(result)
        assert "ha_mcp_tools" in error_msg, (
            f"Refusal must name the blocked domain; got: {error_msg!r}"
        )
        assert any(
            hint in error_msg
            for hint in (
                "ha_write_file",
                "ha_list_files",
                "ha_read_file",
                "ha_delete_file",
                "ha_config_set_yaml",
            )
        ), f"Refusal must hint at the dedicated tool; got: {error_msg!r}"

    @pytest.mark.parametrize(
        "variant",
        [
            "HA_MCP_TOOLS",
            "Ha_Mcp_Tools",
            "  ha_mcp_tools  ",
            " HA_MCP_TOOLS ",
        ],
        ids=["upper", "mixed", "padded_lower", "padded_upper"],
    )
    async def test_case_and_whitespace_variants_refused(
        self, mcp_client_for_refusal, variant
    ):
        """HA core's ``ServiceRegistry.async_call`` lowercases the domain
        on its fallback lookup, so a mixed-case ``HA_MCP_TOOLS`` would
        otherwise slip past an exact-string refusal and still resolve
        downstream. Verify normalization closes that hole."""
        result = await safe_call_tool(
            mcp_client_for_refusal,
            "ha_call_service",
            {
                "domain": variant,
                "service": "write_file",
                "data": {"path": "www/should-never-be-written.txt", "content": "x"},
            },
        )

        error_msg = extract_error_message(result)
        assert "ha_mcp_tools" in error_msg, (
            f"Variant {variant!r} must be refused with a ha_mcp_tools "
            f"mention; got: {error_msg!r}"
        )

    async def test_unrelated_domain_not_refused(self, mcp_client_for_refusal):
        """Refusal is narrow — ``persistent_notification`` (and any other
        domain) must pass the refusal check and reach HA. We don't assert
        success of the underlying call; we only assert the refusal text is
        absent so we know the refusal didn't fire spuriously."""
        nonce = uuid.uuid4().hex[:8]
        result = await safe_call_tool(
            mcp_client_for_refusal,
            "ha_call_service",
            {
                "domain": "persistent_notification",
                "service": "create",
                "data": {
                    "message": f"caller-token-refusal-test {nonce}",
                    "title": "ha_mcp_tools refusal coverage",
                    "notification_id": f"pr1459_{nonce}",
                },
            },
        )

        # The refusal would mention ha_mcp_tools by name. Its absence is
        # the load-bearing assertion. The call itself may succeed or fail
        # for unrelated reasons; we don't care here.
        if isinstance(result, dict):
            error_msg = result.get("error")
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", "")
            elif not isinstance(error_msg, str):
                error_msg = ""
            assert "ha_mcp_tools" not in error_msg, (
                f"Refusal must not fire for persistent_notification; got: {error_msg!r}"
            )


# ---------------------------------------------------------------------------
# Bootstrap-and-inject smoke — positive path of the gate
# ---------------------------------------------------------------------------


class TestCallerTokenBootstrapEndToEnd:
    """Calling ``ha_list_files`` proves the full chain: ``ha-mcp`` fetches
    the caller token via ``ha_mcp_tools.get_caller_token``, Core's admin-only
    service registration accepts the supervisor user (addon mode) or the admin
    LLAT (container / external HAOS mode), the wrapper injects ``_ha_mcp_token``,
    and the dangerous-handler's ``_caller_token_ok`` check accepts it."""

    async def test_list_files_round_trips(self, mcp_client_for_refusal):
        """Implicit end-to-end coverage of bootstrap + admin gate + token
        gate. Fails loudly if ha_mcp_tools is missing — the testcontainer
        conftest auto-installs it and the HAOS lanes bake it in, so a
        missing component means the install path regressed.
        ``TestMcpToolsComponentNotInstalled`` in
        ``workflows/filesystem/test_file_operations.py`` covers the
        deliberate not-installed scenario; this test does not."""
        async with MCPAssertions(mcp_client_for_refusal) as mcp:
            data = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/"},
            )
        assert data.get("success") is True, (
            f"ha_list_files must succeed once the bootstrap + token gate is "
            f"in place; got: {data!r}"
        )


# ---------------------------------------------------------------------------
# Admin gate — non-admin caller is rejected by HA core
# ---------------------------------------------------------------------------


@pytest.fixture
async def _dismiss_login_notification(ha_client):
    """http.ban posts a "Login attempt failed" notification for every 401."""
    yield
    await ha_client.call_service(
        "persistent_notification", "dismiss", {"notification_id": "http-login"}
    )


@pytest.mark.usefixtures("_dismiss_login_notification")
class TestCallerTokenAdminGate:
    """Every ``ha_mcp_tools`` service is registered admin-only.

    The seeded non-admin user lives in ``tests/initial_test_state/.storage/auth``
    (``a5973d59…``, ``system-users`` group). The matching LLAT is
    ``NON_ADMIN_TEST_TOKEN`` in ``tests/test_constants.py``. We call the
    HA REST endpoint directly with that token — no MCP wrapper, no
    ``HOMEASSISTANT_TOKEN`` shadowing — and assert HA core refuses the
    call with 401 before any handler runs.
    """

    async def test_non_admin_caller_rejected(
        self,
        _filesystem_feature_flag,
        ha_container_with_fresh_config,
    ):
        response = await post_service(
            ha_container_with_fresh_config["base_url"],
            NON_ADMIN_TEST_TOKEN,
            "get_caller_token",
            {},
        )

        # No graceful skip on missing component: the testcontainer conftest
        # auto-installs ha_mcp_tools and HAOS lanes bake it in, so a 400
        # ServiceNotFound here means the install path regressed.
        # TestMcpToolsComponentNotInstalled in
        # workflows/filesystem/test_file_operations.py covers the
        # deliberate not-installed scenario; this test does not.
        assert response.status_code == 401, (
            f"HA core must refuse a non-admin caller of the admin-only "
            f"service. Status={response.status_code}, body={response.text!r}"
        )

    async def test_non_admin_caller_rejected_on_edit_file(
        self,
        _filesystem_feature_flag,
        ha_container_with_fresh_config,
    ):
        """edit_file registers on its own, outside the loop the bootstrap is in."""
        response = await post_service(
            ha_container_with_fresh_config["base_url"],
            NON_ADMIN_TEST_TOKEN,
            "edit_file",
            {
                "path": f"www/non_admin_{uuid.uuid4().hex[:8]}.txt",
                "edits": [{"old": "a", "new": "b"}],
            },
        )
        # The handler itself answers 200, so a 401 can only come from Core.
        assert response.status_code == 401, (
            f"HA core must refuse a non-admin caller of edit_file. "
            f"Status={response.status_code}, body={response.text!r}"
        )
