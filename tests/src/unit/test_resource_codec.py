"""How the dashboard-resource tools read and write inline resource URLs.

Inline JS/CSS lives in Core's lovelace resource collection as a base64
``data:`` URL (older releases wrote a legacy worker URL). Each test sets up
Core's ``lovelace`` in storage resource mode and runs the tools on the
server's own client connected to it; a URL another client or another release
of this server wrote is registered with Core directly, over its WebSocket
command. The tools' wider contract is in ``test_tools_resources.py``.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.resource_codec import LEGACY_WORKER_BASE_URL, MAX_CONTENT_SIZE
from ha_mcp.tools.tools_resources import ResourceTools

from ._component_core import ws_result
from ._legacy_resource_urls import legacy_url
from ._tool_errors import rejected_call_body

CONTENT = "export const x = 1;"


@pytest.fixture(autouse=True)
async def lovelace(hass: HomeAssistant) -> None:
    """Core's lovelace, whose resources are kept in storage by default."""
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
async def ws(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> MockHAClientWebSocket:
    return await hass_ws_client(hass)


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> ResourceTools:
    return ResourceTools(core_client)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def _listed(
    tools: ResourceTools, ws: MockHAClientWebSocket, url: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register ``url`` with Core as another client would; list it back.

    Returns the listing and its one row.
    """
    await ws_result(ws, "lovelace/resources/create", res_type="module", url=url)
    listing = await tools.ha_config_list_dashboard_resources(include_content=True)
    (row,) = listing["resources"]
    return listing, row


@pytest.mark.parametrize(
    ("content", "resource_type"),
    [
        ("export const x = 'ünïcode';", "module"),
        ('.card::before { content: "ünïcode ✓"; }', "css"),
        ("x" * MAX_CONTENT_SIZE, "module"),
    ],
    ids=["module-non-ascii", "css-non-ascii", "module-at-the-cap"],
)
async def test_saved_inline_content_lists_back_unchanged(
    tools: ResourceTools, content: str, resource_type: str
) -> None:
    """What an agent saves is what it reads back to edit; a changed byte breaks the card."""
    await tools.ha_config_set_dashboard_resource(
        content=content, resource_type=resource_type
    )

    listing = await tools.ha_config_list_dashboard_resources(include_content=True)

    (row,) = listing["resources"]
    assert row["_content"] == content
    assert row["url"] == "[inline]"


@pytest.mark.parametrize(
    "url",
    [
        f"data:text/javascript;base64,{_b64(CONTENT.encode())}",
        f"data:text/javascript;charset=utf-8;base64,{_b64(CONTENT.encode())}",
        f"data:text/css;base64,{_b64(CONTENT.encode())}",
        f"data:text/css;charset=utf-8;base64,{_b64(CONTENT.encode())}",
        f"DATA:TEXT/JAVASCRIPT;BASE64,{_b64(CONTENT.encode())}",
    ],
    ids=["js", "js-charset", "css", "css-charset", "upper-case-head"],
)
async def test_inline_urls_of_every_form_are_shown_decoded(
    tools: ResourceTools, ws: MockHAClientWebSocket, url: str
) -> None:
    """A resource another release wrote, with or without a charset, or with the
    scheme in capitals, stays an inline resource: one not recognized would
    escape the truncated-preview guard, so an agent could overwrite it with a
    preview."""
    listing, row = await _listed(tools, ws, url)

    assert row["_content"] == CONTENT
    assert "_legacy_worker" not in row
    assert listing["inline_count"] == 1


async def test_legacy_worker_url_with_a_capitalized_origin_is_shown_decoded(
    tools: ResourceTools, ws: MockHAClientWebSocket
) -> None:
    """Scheme and host case must not cost a legacy resource its migration path."""
    url = legacy_url(CONTENT).replace("https://", "HTTPS://", 1)

    _, row = await _listed(tools, ws, url)

    assert row["_content"] == CONTENT
    assert row["_legacy_worker"] is True


@pytest.mark.parametrize(
    "url",
    [
        "/local/card.js",
        "https://cdn.example.com/card.js",
        "data:text/javascript,console.log(1)",
        "data:text/html;base64,aGVsbG8=",
        LEGACY_WORKER_BASE_URL.replace("https://", "https://evil.example.com/")
        + "/YWJj?type=module",
        f"{LEGACY_WORKER_BASE_URL}.evil.example.com/YWJj",
    ],
    ids=[
        "local",
        "cdn",
        "percent-encoded-data",
        "html-data",
        "lookalike-path",
        "lookalike-host",
    ],
)
async def test_resources_this_server_did_not_write_are_listed_as_they_are(
    tools: ResourceTools, ws: MockHAClientWebSocket, url: str
) -> None:
    """A URL that is not one of ours keeps its URL and claims no inline content,
    even when it embeds the legacy worker's host."""
    listing, row = await _listed(tools, ws, url)

    assert row["url"] == url
    assert "_inline" not in row
    assert "_decode_error" not in row
    assert listing["inline_count"] == 0


@pytest.mark.parametrize(
    "url",
    [
        # A lenient decoder skips the junk and reads the content back.
        f"data:text/javascript;base64,{_b64(CONTENT.encode())}!!",
        f"data:text/javascript;base64,{_b64(bytes([255, 254, 0]) + b'abc')}",
        f"{LEGACY_WORKER_BASE_URL}/$$$$aGVsbG8=",
        f"{LEGACY_WORKER_BASE_URL}/some-random-path",
    ],
    ids=["junk-after-payload", "not-utf8", "legacy-junk", "legacy-not-utf8"],
)
async def test_inline_url_that_does_not_decode_is_flagged_not_shown(
    tools: ResourceTools, ws: MockHAClientWebSocket, url: str
) -> None:
    """A payload the browser would reject is never shown as content: reading it
    as valid points diagnosis of a dead card away from the cause."""
    listing, row = await _listed(tools, ws, url)

    assert row["url"] == url
    assert row["_decode_error"] is True
    assert "_content" not in row
    assert listing["inline_count"] == 0


@pytest.mark.parametrize(
    "url",
    [
        " data:text/javascript,x",
        "\tdata:text/javascript,x",
        "\n data:text/javascript,x",
        "\x00data:text/javascript,x",
        "da\tta:text/javascript,x",
    ],
    ids=["space", "tab", "newline", "nul", "tab-inside"],
)
async def test_padded_data_url_cannot_bypass_the_inline_guards(
    tools: ResourceTools, ws: MockHAClientWebSocket, url: str
) -> None:
    """A browser strips leading controls and embedded tabs before it reads the
    scheme, so these still load as ``data:`` and must be refused like one."""
    body = await rejected_call_body(tools.ha_config_set_dashboard_resource(url=url))

    assert "content=" in body["error"]["message"]
    assert await ws_result(ws, "lovelace/resources") == []


async def test_url_with_data_in_its_path_is_registered(
    tools: ResourceTools, ws: MockHAClientWebSocket
) -> None:
    """Only the scheme makes a ``data:`` URL; a path that contains one is a URL."""
    url = "https://cdn.example.com/data:x"

    result = await tools.ha_config_set_dashboard_resource(url=url)

    assert result["action"] == "created"
    assert [row["url"] for row in await ws_result(ws, "lovelace/resources")] == [url]
