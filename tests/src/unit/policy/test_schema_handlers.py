"""Schema + value-source handlers for the predicate-builder UI (#966).

The tool-schema endpoint reads a real FastMCP catalog; the value-source
endpoint reads a real Home Assistant through the server's own client. The
server object itself is this repository's collaborator, so the tests hand
the handlers one carrying just the catalog and the client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from starlette.applications import Starlette
from starlette.routing import Route

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.policy.value_sources import clear_value_source_cache

SCHEMA = "/api/policy/tool-schema"
VALUES = "/api/policy/value-source"


@pytest.fixture(autouse=True)
def _fresh_value_cache() -> Iterator[None]:
    """Fetched values are cached for 30s per process; each test reads its own Core."""
    clear_value_source_cache()
    yield
    clear_value_source_cache()


def _catalog() -> FastMCP:
    mcp = FastMCP("policy schema test")

    @mcp.tool(name="ha_get_state", annotations={"readOnlyHint": True})
    async def get_state(entity_id: str) -> str:
        return entity_id

    @mcp.tool(name="ha_call_service", annotations={"destructiveHint": True})
    async def call_service(
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Call a service.

        Args:
            domain: HA domain
        """
        return domain

    @mcp.tool(name="ha_made_up", annotations={"readOnlyHint": False})
    async def made_up(mode: Literal["fast", "slow"]) -> str:
        return mode

    @mcp.tool(name="ha_set_integration", annotations={"readOnlyHint": False})
    async def set_integration(
        entry_id: str,
        enabled: bool | None = None,
        domain: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> str:
        return entry_id

    return mcp


async def _app(
    tmp_path: Path, server: object | None
) -> AsyncIterator[httpx.AsyncClient]:
    h = build_policy_handlers(data_dir=tmp_path, queue=ApprovalQueue(), server=server)
    app = Starlette(
        routes=[
            Route(SCHEMA, h["policy_get_tool_schema"], methods=["GET"]),
            Route(VALUES, h["policy_get_value_source"], methods=["GET"]),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://settings"
    ) as client:
        yield client


@pytest.fixture
async def schema_api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _app(tmp_path, SimpleNamespace(mcp=_catalog(), client=None)):
        yield client


@pytest.fixture
async def values_api(
    tmp_path: Path, core_client: HomeAssistantClient
) -> AsyncIterator[httpx.AsyncClient]:
    server = SimpleNamespace(mcp=FastMCP("unused"), client=core_client)
    async for client in _app(tmp_path, server):
        yield client


@pytest.fixture
async def sidecar_api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The settings sidecar builds the handlers with no server to introspect."""
    async for client in _app(tmp_path, None):
        yield client


async def _values(api: httpx.AsyncClient, query: str) -> list[str]:
    r = await api.get(f"{VALUES}?{query}")
    assert r.status_code == 200, r.text
    values: list[str] = r.json()["values"]
    return values


# --- tool-schema ---


async def test_tool_schema_without_a_name_is_a_bad_request(
    schema_api: httpx.AsyncClient,
) -> None:
    assert (await schema_api.get(SCHEMA)).status_code == 400


async def test_tool_schema_in_the_sidecar_says_it_is_unavailable(
    sidecar_api: httpx.AsyncClient,
) -> None:
    r = await sidecar_api.get(f"{SCHEMA}?name=ha_call_service")
    assert r.status_code == 503


async def test_tool_schema_for_an_unknown_tool_is_not_found(
    schema_api: httpx.AsyncClient,
) -> None:
    r = await schema_api.get(f"{SCHEMA}?name=ha_nonexistent")
    assert r.status_code == 404


async def test_a_read_only_tool_offers_no_pickers(
    schema_api: httpx.AsyncClient,
) -> None:
    r = await schema_api.get(f"{SCHEMA}?name=ha_get_state")
    assert r.status_code == 200
    data = r.json()
    assert data["is_write_or_destructive"] is False
    assert data["paths"] == []
    assert data["value_sources"] == {}


async def test_a_write_tool_offers_its_arguments_and_live_value_lists(
    schema_api: httpx.AsyncClient,
) -> None:
    r = await schema_api.get(f"{SCHEMA}?name=ha_call_service")
    assert r.status_code == 200
    data = r.json()
    assert data["is_write_or_destructive"] is True
    paths = {p["path"]: p for p in data["paths"]}
    assert set(paths) == {"args.domain", "args.service", "args.entity_id", "args.data"}
    assert paths["args.domain"]["required"] is True
    assert paths["args.data"]["required"] is False
    assert paths["args.domain"]["description"] == "HA domain"
    vs = data["value_sources"]
    assert vs == {
        "args.domain": "ha_domains",
        "args.service": "ha_services",
        "args.entity_id": "ha_entities",
    }


async def test_an_enum_argument_offers_its_choices(
    schema_api: httpx.AsyncClient,
) -> None:
    r = await schema_api.get(f"{SCHEMA}?name=ha_made_up")
    paths = {p["path"]: p for p in r.json()["paths"]}
    assert paths["args.mode"]["enum"] == ["fast", "slow"]


async def test_set_integration_offers_the_config_entry_list(
    schema_api: httpx.AsyncClient,
) -> None:
    r = await schema_api.get(f"{SCHEMA}?name=ha_set_integration")
    assert r.status_code == 200
    assert r.json()["value_sources"]["args.entry_id"] == "ha_config_entries"


async def test_tool_schema_reports_a_broken_catalog_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FastMCP version bump that breaks the unfiltered tool listing should
    surface the failure rather than answer 200 with empty paths."""
    mcp = _catalog()
    monkeypatch.setattr(
        mcp.local_provider,
        "_list_tools",
        AsyncMock(side_effect=AttributeError("FastMCP rename broke us")),
    )
    async for api in _app(tmp_path, SimpleNamespace(mcp=mcp, client=None)):
        r = await api.get(f"{SCHEMA}?name=ha_get_state")
    assert r.status_code == 500
    assert "FastMCP rename" in r.json()["error"]


async def test_a_property_without_an_object_schema_is_skipped(
    tmp_path: Path,
) -> None:
    """JSON Schema allows ``true`` as a property schema; a hand-built tool
    schema carrying one must not take the endpoint down."""
    mcp = FastMCP("quirky")

    async def quirky(ok: str) -> str:
        return ok

    tool = Tool.from_function(quirky, name="ha_quirky")
    mcp.add_tool(
        tool.model_copy(
            update={
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "string"}, "anything": True},
                }
            }
        )
    )
    async for api in _app(tmp_path, SimpleNamespace(mcp=mcp, client=None)):
        r = await api.get(f"{SCHEMA}?name=ha_quirky")
    assert r.status_code == 200
    assert {p["path"] for p in r.json()["paths"]} == {"args.ok"}


# --- value-source ---


async def test_value_source_without_a_source_is_a_bad_request(
    values_api: httpx.AsyncClient,
) -> None:
    assert (await values_api.get(VALUES)).status_code == 400


async def test_an_unknown_value_source_is_a_bad_request(
    values_api: httpx.AsyncClient,
) -> None:
    r = await values_api.get(f"{VALUES}?source=does_not_exist")
    assert r.status_code == 400


async def test_value_source_in_the_sidecar_says_it_is_unavailable(
    sidecar_api: httpx.AsyncClient,
) -> None:
    r = await sidecar_api.get(f"{VALUES}?source=ha_domains")
    assert r.status_code == 503


async def test_the_domain_list_is_every_domain_with_services_sorted(
    hass: HomeAssistant, values_api: httpx.AsyncClient
) -> None:
    assert await async_setup_component(hass, "counter", {})
    assert await async_setup_component(hass, "input_boolean", {})

    values = await _values(values_api, "source=ha_domains")

    assert {"counter", "input_boolean"} <= set(values)
    assert values == sorted(hass.services.async_services())


async def test_the_service_list_is_filtered_to_the_chosen_domain(
    hass: HomeAssistant, values_api: httpx.AsyncClient
) -> None:
    assert await async_setup_component(hass, "counter", {})
    assert await async_setup_component(hass, "input_boolean", {})

    values = await _values(values_api, "source=ha_services&domain=counter")

    assert "increment" in values
    assert "turn_on" not in values
    assert values == sorted(hass.services.async_services_for_domain("counter"))


async def test_the_entity_list_is_filtered_to_the_chosen_domain(
    hass: HomeAssistant, values_api: httpx.AsyncClient
) -> None:
    hass.states.async_set("light.bed", "on")
    hass.states.async_set("switch.fan", "off")
    hass.states.async_set("light.kitchen", "off")

    values = await _values(values_api, "source=ha_entities&domain=light")

    assert values == ["light.bed", "light.kitchen"]


async def test_the_config_entry_list_names_every_entry(
    hass: HomeAssistant, values_api: httpx.AsyncClient
) -> None:
    assert await async_setup_component(hass, "config", {})
    MockConfigEntry(domain="hue", entry_id="abc123").add_to_hass(hass)
    MockConfigEntry(domain="group", entry_id="def456").add_to_hass(hass)

    values = await _values(values_api, "source=ha_config_entries")

    assert values == ["abc123", "def456"]


async def test_a_value_source_that_gets_no_answer_is_a_bad_gateway(
    values_api: httpx.AsyncClient, core_client: HomeAssistantClient
) -> None:
    with patch.object(
        core_client,
        "get_services",
        AsyncMock(side_effect=HomeAssistantConnectionError("HA unreachable")),
    ):
        r = await values_api.get(f"{VALUES}?source=ha_domains")
    assert r.status_code == 502
    assert "HA unreachable" in r.json()["error"]


async def test_cached_values_are_kept_apart_per_filter(
    hass: HomeAssistant, values_api: httpx.AsyncClient
) -> None:
    hass.states.async_set("light.bed", "on")
    hass.states.async_set("lock.front", "locked")

    assert await _values(values_api, "source=ha_entities&domain=light") == ["light.bed"]
    assert await _values(values_api, "source=ha_entities&domain=lock") == ["lock.front"]


async def test_a_fetched_list_is_served_from_the_cache(
    hass: HomeAssistant,
    values_api: httpx.AsyncClient,
    core_client: HomeAssistantClient,
) -> None:
    hass.states.async_set("light.bed", "on")
    with patch.object(
        core_client, "get_states", wraps=core_client.get_states
    ) as get_states:
        first = await _values(values_api, "source=ha_entities&domain=light")
        hass.states.async_set("light.kitchen", "on")
        second = await _values(values_api, "source=ha_entities&domain=light")
    assert first == second == ["light.bed"]
    assert get_states.await_count == 1


async def test_an_empty_list_is_fetched_again_next_time(
    hass: HomeAssistant,
    values_api: httpx.AsyncClient,
    core_client: HomeAssistantClient,
) -> None:
    """An empty answer must not stick in the dropdown for the cache window."""
    with patch.object(
        core_client, "get_states", wraps=core_client.get_states
    ) as get_states:
        first = await _values(values_api, "source=ha_entities&domain=light")
        hass.states.async_set("light.bed", "on")
        second = await _values(values_api, "source=ha_entities&domain=light")
    assert first == []
    assert second == ["light.bed"]
    assert get_states.await_count == 2
