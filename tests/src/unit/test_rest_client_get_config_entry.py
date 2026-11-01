"""``HomeAssistantClient.get_config_entry`` reads one entry over WebSocket.

Core's ``config_entries/get_single`` answers ``{"config_entry": fragment}`` for
one entry, or ``not_found``. Core has no REST read for one entry, so the REST
entry list stays as the fallback for the cases the WebSocket cannot serve: no
answer came back, or the token's user is not an admin (``get_single`` is
admin-only, the REST list is open to every user).
"""

import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)

FRAGMENT = {"entry_id": "e1", "domain": "sun", "title": "Sun", "state": "loaded"}


@pytest.fixture
def client() -> HomeAssistantClient:
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
    c.list_config_entries = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"entry_id": "other"}, dict(FRAGMENT)]
    )
    return c


def _ws(client: HomeAssistantClient, reply: Any) -> AsyncMock:
    send = AsyncMock(side_effect=reply) if isinstance(reply, Exception) else None
    send = send or AsyncMock(return_value=reply)
    client.send_websocket_message = send  # type: ignore[method-assign]
    return send


@pytest.mark.asyncio
async def test_reads_the_entry_with_get_single(client: HomeAssistantClient) -> None:
    send = _ws(client, {"success": True, "result": {"config_entry": FRAGMENT}})

    assert await client.get_config_entry("e1") == FRAGMENT

    send.assert_awaited_once_with(
        {"type": "config_entries/get_single", "entry_id": "e1"},
        expected_error_codes=frozenset({"not_found", "unauthorized"}),
    )
    client.list_config_entries.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_not_found_is_the_404_callers_expect(
    client: HomeAssistantClient,
) -> None:
    _ws(
        client,
        {
            "success": False,
            "error": "Command failed: Config entry not found",
            "error_code": "not_found",
        },
    )

    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_config_entry("missing")

    assert exc.value.status_code == 404
    assert str(exc.value) == "Config entry not found: missing"
    client.list_config_entries.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [HomeAssistantConnectionError("closed"), HomeAssistantCommandNotSent("down")],
)
async def test_no_websocket_answer_falls_back_to_the_rest_list(
    client: HomeAssistantClient, failure: Exception
) -> None:
    _ws(client, failure)

    assert await client.get_config_entry("e1") == FRAGMENT

    client.list_config_entries.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rest_fallback_still_reports_a_missing_entry(
    client: HomeAssistantClient,
) -> None:
    _ws(client, HomeAssistantConnectionError("closed"))

    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_config_entry("missing")

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_token_falls_back_to_the_rest_list(
    client: HomeAssistantClient,
) -> None:
    # get_single is admin-only; Core's REST entry list is open to every user, so
    # a non-admin token keeps the read it had before.
    _ws(
        client,
        {
            "success": False,
            "error": "Command failed: Unauthorized",
            "error_code": "unauthorized",
        },
    )

    assert await client.get_config_entry("e1") == FRAGMENT

    client.list_config_entries.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_other_core_error_is_raised_not_retried_over_rest(
    client: HomeAssistantClient,
) -> None:
    # Core answered, so its error is the result: listing every entry over REST
    # would only hide it.
    _ws(
        client,
        {
            "success": False,
            "error": "Command failed: boom",
            "error_code": "unknown_error",
        },
    )

    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_config_entry("e1")

    assert exc.value.status_code != 404
    assert "boom" in str(exc.value)
    client.list_config_entries.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["unauthorized", "not_found"])
async def test_an_answer_the_read_handles_is_not_logged_as_an_error(
    client: HomeAssistantClient, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    # A non-admin token gets "unauthorized" on every read and falls back to
    # REST; "not_found" becomes the 404. Neither is a failure worth an ERROR.
    client.base_url, client.token, client.verify_ssl = "http://ha", "t", True
    ws = MagicMock()
    ws.send_command = AsyncMock(
        side_effect=HomeAssistantCommandError("Command failed: x", code)
    )
    with (
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        caplog.at_level(logging.DEBUG, logger="ha_mcp.client.rest_client"),
        contextlib.suppress(HomeAssistantAPIError),
    ):
        await client.get_config_entry("e1")

    assert [r.levelname for r in caplog.records if "get_single" in r.message] == [
        "DEBUG"
    ]


@pytest.mark.asyncio
async def test_another_core_error_is_still_logged_as_an_error(
    client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.base_url, client.token, client.verify_ssl = "http://ha", "t", True
    ws = MagicMock()
    ws.send_command = AsyncMock(
        side_effect=HomeAssistantCommandError("Command failed: x", "unknown_error")
    )
    with (
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        caplog.at_level(logging.DEBUG, logger="ha_mcp.client.rest_client"),
        pytest.raises(HomeAssistantAPIError),
    ):
        await client.get_config_entry("e1")

    assert [r.levelname for r in caplog.records if "get_single" in r.message] == [
        "ERROR"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, None, {"config_entry": "e1"}])
async def test_a_malformed_answer_is_not_read_as_a_missing_entry(
    client: HomeAssistantClient, result: Any
) -> None:
    _ws(client, {"success": True, "result": result})

    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_config_entry("e1")

    assert exc.value.status_code != 404
    client.list_config_entries.assert_not_awaited()  # type: ignore[attr-defined]
