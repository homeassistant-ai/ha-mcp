"""404 suggestion branching for ``system_service`` logs.

``_handle_system_service_api_error`` must keep three states apart: the add-on
route (Supervisor itself answered), a confirmed unsupervised install, and an
inconclusive supervision probe (which must not assert the instance is
unmanaged).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_logs import LogTools


def _error() -> HomeAssistantAPIError:
    return HomeAssistantAPIError("API error: 404 - Not Found", status_code=404)


def _client(*, probed: bool, confirmed: bool | None) -> AsyncMock:
    client = AsyncMock()
    client._is_supervised_install = AsyncMock(return_value=probed)
    client._supervised_detected = confirmed
    return client


def _suggestions(exc_info: pytest.ExceptionInfo[ToolError]) -> list[str]:
    payload = json.loads(str(exc_info.value))
    return payload["error"]["suggestions"]


@pytest.mark.asyncio
async def test_addon_404_skips_the_probe_and_names_the_os_version() -> None:
    client = _client(probed=False, confirmed=None)
    tools = LogTools(client)

    with (
        patch(
            "ha_mcp.tools.log_sources_supervisor.is_running_in_addon",
            return_value=True,
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools._handle_system_service_api_error(_error(), "core")

    assert any(
        "not expose it on this HA OS version" in s for s in _suggestions(exc_info)
    )
    client._is_supervised_install.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_supervised_404_names_the_os_version() -> None:
    client = _client(probed=True, confirmed=True)
    tools = LogTools(client)

    with (
        patch(
            "ha_mcp.tools.log_sources_supervisor.is_running_in_addon",
            return_value=False,
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools._handle_system_service_api_error(_error(), "core")

    assert any(
        "not expose it on this HA OS version" in s for s in _suggestions(exc_info)
    )
    client._is_supervised_install.assert_awaited()


@pytest.mark.asyncio
async def test_confirmed_unsupervised_404_names_the_install_type() -> None:
    client = _client(probed=False, confirmed=False)
    tools = LogTools(client)

    with (
        patch(
            "ha_mcp.tools.log_sources_supervisor.is_running_in_addon",
            return_value=False,
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools._handle_system_service_api_error(_error(), "core")

    assert any("not managed by Supervisor" in s for s in _suggestions(exc_info))


@pytest.mark.asyncio
async def test_inconclusive_probe_404_does_not_assert_unsupervised() -> None:
    # A failed probe fail-opens to False but leaves the flag None.
    client = _client(probed=False, confirmed=None)
    tools = LogTools(client)

    with (
        patch(
            "ha_mcp.tools.log_sources_supervisor.is_running_in_addon",
            return_value=False,
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools._handle_system_service_api_error(_error(), "core")

    suggestions = _suggestions(exc_info)
    assert any("could not be confirmed" in s for s in suggestions)
    assert not any(
        "this instance is not managed by Supervisor" in s for s in suggestions
    )
