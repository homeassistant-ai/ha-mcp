"""ha_config_list_helpers shows what the component could not give it: options it
withheld, a degraded secret scrub, and helper flows its failed loader read left
out.

The listing runs on the server's client against a real ``hass`` with the
component's commands. An unreadable ``secrets.yaml`` is a real file in the
test's own config directory, and a custom helper integration is a real
``manifest.json`` Core's loader finds. Only the component's own read of Core's
helper flows is made to fail, which Core cannot be made to do while it still
answers the server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

_CUSTOM = "my_helper"
_LOADER = (
    "custom_components.ha_mcp_tools.websocket_api.flow_domains.async_get_config_flows"
)


@pytest.fixture
def _core_helper_flows() -> None:
    """Core's loader answers the component's helper flow read itself."""


@pytest.fixture
def _server_helper_flows() -> None:
    """Core's ``flow_handlers?type=helper`` answers the server itself."""


@pytest.fixture
def custom_helper_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_custom_integrations: None
) -> None:
    """A custom helper integration installed where Core's loader looks."""
    integration = tmp_path / "extra" / "custom_components" / _CUSTOM
    integration.mkdir(parents=True)
    manifest = {
        "domain": _CUSTOM,
        "name": "My Helper",
        "config_flow": True,
        "integration_type": "helper",
        "version": "1.0.0",
        "documentation": "https://example.invalid",
        "codeowners": [],
        "requirements": [],
    }
    (integration / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.syspath_prepend(str(tmp_path / "extra"))


@pytest.fixture
async def tools(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> AsyncIterator[HelperConfigTools]:
    """Every storage helper set up, and one template helper to list."""
    # ``config`` serves the registry commands and Core's helper flow list.
    for domain in ("config", *sorted(SIMPLE_HELPER_TYPES)):
        assert await async_setup_component(hass, domain, {})
    MockConfigEntry(
        domain="template",
        title="Probe",
        options={"name": "Probe", "template_type": "sensor", "state": "{{ 1 }}"},
    ).add_to_hass(hass)
    yield HelperConfigTools(component_core_client)
    await hass.async_block_till_done()


def _write_secrets(hass: HomeAssistant, world: str) -> None:
    body = {"unreadable": "key: [unclosed\n", "readable": "key: value\n"}.get(world)
    if body is not None:
        Path(hass.config.path("secrets.yaml")).write_text(body)


def _scrub_warnings(response: dict[str, object]) -> list[str]:
    warnings = response.get("warnings") or []
    assert isinstance(warnings, list)
    return [w for w in warnings if "secrets.yaml" in str(w)]


def _component_loader(world: str) -> AbstractContextManager[object]:
    """The component's read of Core's helper flows, failing in that world."""
    if world != "loader_failed":
        return nullcontext()
    return patch(
        _LOADER, AsyncMock(side_effect=OSError("custom_components unreadable"))
    )


_WORLDS = ["unreadable", "readable", "loader_failed"]


@pytest.mark.parametrize(
    ("helper_type", "world", "warned"),
    [
        ("template", "unreadable", True),
        ("template", "readable", False),
        ("template", "loader_failed", False),
        ("input_boolean", "unreadable", False),
    ],
)
async def test_single_type_listing_warns_when_the_flow_helper_read_degraded(
    hass: HomeAssistant,
    tools: HelperConfigTools,
    helper_type: str,
    world: str,
    warned: bool,
) -> None:
    """An unreadable secrets.yaml left a flow helper's options unscrubbed; a
    storage helper's body carries no secret, and a failed loader read alone
    does not touch Core's own helper flows."""
    _write_secrets(hass, world)
    with _component_loader(world):
        response = await tools.ha_config_list_helpers(helper_type=helper_type)

    assert bool(_scrub_warnings(response)) is warned
    if helper_type == "template":
        assert [h["name"] for h in response["helpers"]] == ["Probe"]


@pytest.mark.parametrize("world", _WORLDS)
async def test_all_types_listing_warns_when_the_flow_helper_read_degraded(
    hass: HomeAssistant, tools: HelperConfigTools, world: str
) -> None:
    _write_secrets(hass, world)
    with _component_loader(world):
        response = await tools.ha_config_list_helpers(helper_type="all")

    assert bool(_scrub_warnings(response)) is (world == "unreadable")
    assert "template" in {h["helper_type"] for h in response["helpers"]}


@pytest.mark.usefixtures("custom_helper_integration")
async def test_a_helper_flow_left_out_by_a_failed_loader_read_is_not_blamed_on_the_component(
    tools: HelperConfigTools,
) -> None:
    """Core lists the custom helper flow, but the component's own loader read
    failed: "update the component" would mislead."""
    with _component_loader("loader_failed"), pytest.raises(ToolError) as exc_info:
        await tools.ha_config_list_helpers(helper_type="all")

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert _CUSTOM in error["message"]


@pytest.mark.usefixtures("custom_helper_integration")
@pytest.mark.parametrize("helper_type", ["all", _CUSTOM])
async def test_a_custom_helper_is_marked_as_withheld_not_empty(
    hass: HomeAssistant, tools: HelperConfigTools, helper_type: str
) -> None:
    """An agent must be able to tell withheld options from a helper without any."""
    entry = MockConfigEntry(domain=_CUSTOM, title="Mine", options={"token": "x"})
    entry.add_to_hass(hass)

    response = await tools.ha_config_list_helpers(helper_type=helper_type)

    by_type = {h.get("helper_type", helper_type): h for h in response["helpers"]}
    custom = by_type[_CUSTOM]
    assert custom["entry_id"] == entry.entry_id
    assert custom["options_withheld"] == "custom_integration"
    assert "options" not in custom
    if helper_type == "all":
        assert "options_withheld" not in by_type["template"]
        assert by_type["template"]["options"]["state"] == "{{ 1 }}"
