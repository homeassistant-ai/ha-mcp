"""The component-gated config-entry ``unique_id`` read.

Home Assistant withholds a config entry's ``unique_id`` from every endpoint it
has, so this helper is the only source — and the value it returns is
three-valued, not two. Every test here is about keeping "we could not read it"
distinct from "the entry has none": conflating them is what made the anchor
dead in the first place, and what would silently disarm it again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_config_entries as mod
from ha_mcp.tools.component_config_entries import (
    UNKNOWN_UNIQUE_ID,
    fetch_config_entry_unique_id,
)


@pytest.fixture
def client() -> Any:
    c = MagicMock()
    c.base_url = "http://homeassistant.local"
    c.token = "test-token"
    return c


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supported: bool = True,
    send: Any = None,
) -> MagicMock:
    """Stub the caps probe and the pooled WS send."""
    monkeypatch.setattr(mod, "get_component_caps", AsyncMock(return_value={}))
    monkeypatch.setattr(mod, "component_supports", lambda caps, name: supported)
    ws = MagicMock()
    ws.send_command = send if send is not None else AsyncMock(return_value={})
    monkeypatch.setattr(mod, "get_websocket_client", AsyncMock(return_value=ws))
    return ws


@pytest.mark.asyncio
async def test_returns_the_value_when_the_component_reports_one(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(
        monkeypatch,
        send=AsyncMock(
            return_value={
                "result": {"entries": [{"entry_id": "e1", "unique_id": "abc-123"}]}
            }
        ),
    )

    assert await fetch_config_entry_unique_id(client, "e1") == (True, "abc-123")


@pytest.mark.asyncio
async def test_known_absent_when_the_entry_genuinely_has_none(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MQTT-style entries have no unique_id — that is an answer, not a gap."""
    _arrange(
        monkeypatch,
        send=AsyncMock(
            return_value={
                "result": {"entries": [{"entry_id": "e1", "unique_id": None}]}
            }
        ),
    )

    result = await fetch_config_entry_unique_id(client, "e1")

    assert result.known is True
    assert result.value is None


@pytest.mark.asyncio
async def test_unknown_without_the_capability(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No component (add-on / Docker / PyPI) — nothing is sent at all."""
    ws = _arrange(monkeypatch, supported=False)

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID
    ws.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_when_the_row_predates_the_field(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older component omits the KEY — the capability probe.

    This must not read as "the entry has no unique_id", which is what makes the
    field additive within schema_version 1 with no version gate.
    """
    _arrange(
        monkeypatch,
        send=AsyncMock(
            return_value={"result": {"entries": [{"entry_id": "e1", "title": "X"}]}}
        ),
    )

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID


@pytest.mark.asyncio
async def test_unknown_when_the_unique_id_is_not_a_string(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed is unknown, never known-absent.

    Coercing junk to None would report "this entry has no unique_id" and
    silently disarm the anchor.
    """
    _arrange(
        monkeypatch,
        send=AsyncMock(
            return_value={"result": {"entries": [{"entry_id": "e1", "unique_id": 42}]}}
        ),
    )

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID


@pytest.mark.asyncio
async def test_unknown_when_the_row_is_for_another_entry(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a different entry's anchor is worse than reading none."""
    _arrange(
        monkeypatch,
        send=AsyncMock(
            return_value={
                "result": {"entries": [{"entry_id": "other", "unique_id": "abc"}]}
            }
        ),
    )

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not-a-dict", id="non_dict_response"),
        pytest.param({}, id="no_result"),
        pytest.param({"result": "nope"}, id="non_dict_result"),
        pytest.param({"result": {}}, id="no_entries"),
        pytest.param({"result": {"entries": []}}, id="empty_entries"),
        pytest.param({"result": {"entries": ["nope"]}}, id="non_dict_row"),
    ],
)
async def test_unknown_on_a_malformed_payload(
    client: Any, monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    """The helper never raises: every malformed shape degrades to unknown."""
    _arrange(monkeypatch, send=AsyncMock(return_value=payload))

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID


@pytest.mark.asyncio
async def test_unknown_command_invalidates_the_cached_caps(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downgraded component must not be probed again on stale caps."""
    _arrange(
        monkeypatch,
        send=AsyncMock(side_effect=HomeAssistantCommandError("unknown_command")),
    )
    monkeypatch.setattr(mod, "is_unknown_command", lambda exc: True)
    invalidate = MagicMock()
    monkeypatch.setattr(mod, "invalidate_caps", invalidate)

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID
    invalidate.assert_called_once_with(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(HomeAssistantCommandTimeout("slow"), id="timeout"),
        pytest.param(HomeAssistantCommandError("boom"), id="command_error"),
        pytest.param(RuntimeError("socket died"), id="transport"),
    ],
)
async def test_unknown_on_any_transport_failure(
    client: Any, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A dead socket degrades the anchor; it does not fail the reconfigure."""
    _arrange(monkeypatch, send=AsyncMock(side_effect=exc))
    monkeypatch.setattr(mod, "is_unknown_command", lambda e: False)

    assert await fetch_config_entry_unique_id(client, "e1") == UNKNOWN_UNIQUE_ID
