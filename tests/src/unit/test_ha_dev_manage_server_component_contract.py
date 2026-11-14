"""Cross-seam contract test for the ``server_entry_update`` write capability.

The component's ``server_entry_update`` command runs on a real ``hass`` with a
real server config entry, under the server's ``ha_dev_manage_server
(update_source)`` consumer, so a shape drift on either side of the write seam
fails here rather than shipping a component reply the consumer mis-maps.

Pinned end to end: the component answers before it writes, and the deferred
write applies the MERGED options (the pip_spec delta applied, the other keys
preserved); a raw frame's literal ``clear`` is normalized inside the
component; and with no server entry anywhere the consumer reports the
component as not installed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_dev

from ._component_core import ws_result
from ._server_update import PIP_SPEC, SERVER_URL, applied, server_entry, update_source


@pytest.fixture(autouse=True)
def _embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


async def test_real_component_write_answers_first_and_applies_merged_options(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The reply comes back before the write (it reloads the entry serving the
    reply), and the write merges the delta into every other stored option."""
    entry = server_entry(hass, channel="stable", pip_spec="", server_url=SERVER_URL)

    result = await update_source(component_core_client, PIP_SPEC)

    assert result["data"]["scheduled"] is True
    assert entry.options["pip_spec"] == ""
    await applied(entry, PIP_SPEC)
    assert dict(entry.options) == {
        "channel": "stable",
        "pip_spec": PIP_SPEC,
        "server_url": SERVER_URL,
    }


async def test_real_component_normalizes_literal_clear_pip_spec(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A raw server_entry_update frame carrying the literal 'clear' must
    collapse to "" inside the COMPONENT: raw WS callers and older servers
    never pass through the ha_dev_manage_server alias, and persisting the
    word verbatim would fail at install time as a pip requirement."""
    entry = server_entry(hass, channel="stable", pip_spec="ha-mcp==9.9.9")

    result = await ws_result(
        component_ws, tools_dev.WS_SERVER_ENTRY_UPDATE, pip_spec="Clear"
    )

    assert result["applying"] == {"pip_spec": ""}
    await applied(entry, "")


async def test_no_server_entry_is_component_not_installed(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The component refuses the write when it holds no server entry and the
    legacy lookup finds none either: the consumer reports the component as not
    installed. A refusal is not ``unknown_command``, so the capability stays."""
    assert await async_setup_component(hass, "config", {})
    invalidated = MagicMock(wraps=tools_dev.invalidate_caps)
    monkeypatch.setattr(tools_dev, "invalidate_caps", invalidated)

    with pytest.raises(ToolError, match="server entry"):
        await update_source(component_core_client, PIP_SPEC)

    invalidated.assert_not_called()
    assert hass.config_entries.options.async_progress() == []
