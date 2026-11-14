"""The component's flow helpers are the helper flows the running Core lists.

The ``helpers_list`` and ``search`` commands run on a real ``hass`` with the
component's commands registered; Core's loader answers the helper flow read
itself (this file switches off the conftest's stand-in for it), a custom helper
integration is a real ``manifest.json`` the loader finds, and ``secrets.yaml``
is a real file in the test's own config directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api import flow_domains
from custom_components.ha_mcp_tools.websocket_api.flow_domains import (
    HELPER_FLOWS_DEGRADED_WARNING,
)
from custom_components.ha_mcp_tools.websocket_api.secrets import (
    SCRUB_DEGRADED_MATCH_WARNING,
    SCRUB_DEGRADED_WARNING,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

from ._component_core import ws_result
from ._core_setup import install_custom_helper, write_secrets
from ._search_tool import _setup_visibility_disabled

_CUSTOM = "my_helper"
_MARKER = "flowdomainoption5521"
_CORE_MARKER = "coreoverrideoption7713"
_UNREADABLE_SECRETS = "key: [unclosed\n"


@pytest.fixture
def _core_helper_flows() -> None:
    """Core's loader answers the component's helper flow read itself."""


@pytest.fixture
def custom_helper_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_custom_integrations: None
) -> None:
    """A custom helper integration installed where Core's loader looks."""
    install_custom_helper(tmp_path, monkeypatch, _CUSTOM)


def _entry(hass: HomeAssistant, domain: str, **kwargs: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, **kwargs)
    entry.add_to_hass(hass)
    return entry


async def _write_secrets(hass: HomeAssistant, text: str) -> None:
    await hass.async_add_executor_job(write_secrets, hass, text)


async def _helpers_list(ws: MockHAClientWebSocket, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(ws, "ha_mcp_tools/helpers_list", **params)
    return result


async def _search(ws: MockHAClientWebSocket, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(ws, "ha_mcp_tools/search", **params)
    return result


async def _helper_search(
    ws: MockHAClientWebSocket, query: str, **params: Any
) -> dict[str, Any]:
    return await _search(
        ws, query=query, search_types=["helper"], include_config=True, **params
    )


def _flow(res: dict[str, Any]) -> list[dict[str, Any]]:
    return [h for h in res["helpers"] if h["kind"] == "flow"]


@pytest.mark.parametrize(("domain", "listed"), [("otp", True), ("bayesian", False)])
async def test_listing_covers_the_helpers_core_lists(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, domain: str, listed: bool
) -> None:
    _entry(hass, domain, title="Helper")

    res = await _helpers_list(component_ws)

    flow = _flow(res)
    assert bool(flow) is listed
    if listed:
        assert flow[0]["helper_type"] == domain
    assert (domain in res["covered_types"]) is listed


@pytest.mark.parametrize(("domain", "listed"), [("otp", True), ("bayesian", False)])
async def test_search_indexes_only_the_helpers_core_lists(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, domain: str, listed: bool
) -> None:
    """A Core helper (otp) is searched with its options; an entry outside Core's
    helper list (bayesian) reaches neither the results nor the options."""
    _entry(hass, domain, title="Sun Helper", options={"note": _MARKER})

    res = await _helper_search(component_ws, "sun helper")

    assert bool(_flow(res)) is listed
    assert (_MARKER in json.dumps(res)) is listed


@pytest.mark.usefixtures("custom_helper_integration")
@pytest.mark.parametrize(
    "search_types", [["helper"], None], ids=["helper-search", "default-search"]
)
async def test_custom_helper_integrations_reach_search(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    search_types: list[str] | None,
) -> None:
    """Search takes Core's helper flows, custom helper integrations included,
    and withholds the options of the custom-only domain alone."""
    _entry(hass, _CUSTOM, title="Sun Custom", options={"api_key": _MARKER})
    _entry(hass, "template", title="Sun Template", options={"k": _CORE_MARKER})
    params: dict[str, Any] = {"query": "sun", "include_config": True}
    if search_types is not None:
        params["search_types"] = search_types

    res = await _search(component_ws, **params)

    flow = {h["helper_type"]: h for h in _flow(res)}
    assert flow[_CUSTOM]["options_withheld"] == "custom_integration"
    # template is in Core's own list, so its options are kept.
    assert "options_withheld" not in flow["template"]
    assert _MARKER not in json.dumps(res)


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("ha_mcp_tools/helpers_list", {"include_flow_helpers": False}),
        ("ha_mcp_tools/search", {"query": "sun", "search_types": ["automation"]}),
    ],
    ids=["listing-without-flow-helpers", "search-without-helpers"],
)
async def test_requests_without_flow_helpers_do_not_wait_on_the_loader(
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    params: dict[str, Any],
) -> None:
    loader = AsyncMock(wraps=flow_domains.async_get_config_flows)
    monkeypatch.setattr(flow_domains, "async_get_config_flows", loader)

    await ws_result(component_ws, command, **params)

    loader.assert_not_awaited()


async def test_a_failed_loader_read_lists_core_helpers_and_says_so(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loader failure must not fail the command (the server would report the
    component as missing); it falls back to Core's own list and reports the gap.
    Core's loader cannot be made to fail while it still answers the server."""
    monkeypatch.setattr(
        flow_domains,
        "async_get_config_flows",
        AsyncMock(side_effect=OSError("custom_components unreadable")),
    )
    _entry(hass, "template", title="Sun Helper", options={"k": 1})

    listing = await _helpers_list(component_ws)
    found = await _search(component_ws, query="sun helper", search_types=["helper"])

    assert [h["helper_type"] for h in _flow(listing)] == ["template"]
    assert listing["helper_flows_degraded"] is True
    assert HELPER_FLOWS_DEGRADED_WARNING in found["warnings"]


@pytest.mark.usefixtures("custom_helper_integration")
async def test_listing_one_custom_helper_type_withholds_its_options(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A listing filtered to the custom-only type still lists it, and its
    options appear nowhere in the reply."""
    _entry(hass, _CUSTOM, options={"api_key": _MARKER})
    _entry(hass, "template", options={"k": _CORE_MARKER})

    only = await _helpers_list(component_ws, helper_types=[_CUSTOM])

    assert [h["helper_type"] for h in _flow(only)] == [_CUSTOM]
    assert _CUSTOM in only["covered_types"]
    assert _MARKER not in json.dumps(only)


@pytest.mark.usefixtures("custom_helper_integration")
async def test_search_matches_custom_only_helpers_on_title_only(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _entry(hass, _CUSTOM, title="Custom Helper", options={"api_key": _MARKER})

    by_title = await _helper_search(component_ws, "custom helper")
    by_option = await _helper_search(component_ws, _MARKER)

    (record,) = _flow(by_title)
    assert (record["helper_type"], record["options"]) == (_CUSTOM, None)
    assert record["options_withheld"] == "custom_integration"
    assert _MARKER not in json.dumps(by_title)
    assert _flow(by_option) == []


async def test_search_scrubs_resolved_secrets_from_flow_helper_options(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _write_secrets(hass, "camera_url: sekret\n")
    _entry(
        hass,
        "template",
        title="Sun Helper",
        options={"state": "{{ 1 }}", "nested": {"url": "sekret"}},
    )

    res = await _helper_search(component_ws, "sun helper")

    assert _flow(res)[0]["options"] == {
        "state": "{{ 1 }}",
        "nested": {"url": "**redacted**"},
    }


@pytest.mark.parametrize(
    ("secrets", "include_config", "warning"),
    [
        (_UNREADABLE_SECRETS, True, SCRUB_DEGRADED_WARNING),
        (_UNREADABLE_SECRETS, False, SCRUB_DEGRADED_MATCH_WARNING),
        ("key: value\n", True, None),
    ],
    ids=["unreadable-with-config", "unreadable-match-only", "readable"],
)
async def test_search_warns_when_options_were_handled_without_the_scrub(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    secrets: str,
    include_config: bool,
    warning: str | None,
) -> None:
    """Emitted options and the match corpus both go unscrubbed when secrets.yaml
    is unreadable; each case gets its own warning."""
    await _write_secrets(hass, secrets)
    _entry(hass, "template", title="Sun Helper")

    res = await _search(
        component_ws,
        query="sun helper",
        search_types=["helper"],
        include_config=include_config,
    )

    assert res.get("warnings", []) == ([warning] if warning else [])


@pytest.mark.usefixtures("custom_helper_integration")
async def test_ha_search_shows_the_scrub_warning_and_the_withheld_marker(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ha_search: the component's warning and the
    options_withheld marker survive the server's response shaping."""
    assert await async_setup_component(hass, "config", {})
    _setup_visibility_disabled(tmp_path, monkeypatch)
    await _write_secrets(hass, _UNREADABLE_SECRETS)
    _entry(hass, _CUSTOM, title="Sun Helper", options={"k": _MARKER})
    tools = SearchTools(
        component_core_client, SmartSearchTools(client=component_core_client)
    )

    resp = await tools.ha_search(
        query="sun helper", search_types=["helper"], include_config=True
    )

    assert SCRUB_DEGRADED_WARNING in resp["warnings"]
    (record,) = resp["helpers"]
    assert record["options_withheld"] == "custom_integration"
    assert record["config"] is None
