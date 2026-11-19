"""ha_search's response contract when a surface fails or nothing warns.

A failed surface leaves a partial response while another surface answers; when
none answers, the first surface's error is raised. A response without warnings
carries no ``warnings`` key.

``ha_search`` runs on the server's own client against a real ``hass``. Without
the component, Core answers its capability probe with ``unknown_command`` and
the search takes the legacy route. Failures enter through the public search
collaborators or a client request that times out; successful replies come from
Core.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.smart_search import SmartSearchTools

from ._search_tool import _build_ha_search, _setup_visibility_disabled


async def test_empty_config_timeout_keeps_entity_results_and_reports_its_cause(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message-less timeout is structured before the surface is gathered."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})

    # A query alone runs both the entity and the config surface.
    with patch.object(
        SmartSearchTools, "deep_search", AsyncMock(side_effect=TimeoutError)
    ):
        response = await _build_ha_search(core_client)(query="lamp")

    assert response["partial"] is True
    assert [entity["entity_id"] for entity in response["entities"]] == ["light.lamp"]
    assert len(response["errors"]) == 1
    failure = response["errors"][0]
    assert failure["surface"] == "configs"
    payload = json.loads(failure["error"])
    assert payload["success"] is False
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"
    assert payload["error"]["message"].strip()
    assert response["partial_reason"] == f"configs: {failure['error']}"


async def test_no_surface_answering_is_a_structured_error(
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every surface failed: an error, not ``success`` with empty buckets that
    read as "nothing matched"."""
    _setup_visibility_disabled(tmp_path, monkeypatch)

    # A domain listing runs the entity surface only.
    with (
        patch.object(
            core_client,
            "guarded_request",
            AsyncMock(side_effect=httpx.ReadTimeout("States response did not arrive")),
        ),
        pytest.raises(ToolError) as excinfo,
    ):
        await _build_ha_search(core_client)(domain_filter="light")

    payload = json.loads(str(excinfo.value))
    assert payload["success"] is False
    assert payload["error"]["code"] == "CONNECTION_TIMEOUT"


async def test_no_surface_answering_keeps_the_first_branch_tool_error(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first surface's own structured error is re-raised as is, not the
    error of a surface after it."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "config", {})
    branch_error = ToolError(
        json.dumps(
            create_error_response(ErrorCode.CONNECTION_FAILED, "entities unavailable")
        )
    )

    with (
        patch.object(
            SmartSearchTools,
            "smart_entity_search",
            AsyncMock(side_effect=branch_error),
        ) as entity_search,
        patch.object(
            SmartSearchTools, "deep_search", AsyncMock(side_effect=TimeoutError)
        ) as config_search,
        pytest.raises(ToolError) as excinfo,
    ):
        await _build_ha_search(core_client)(query="lamp", exact_match=False)

    assert excinfo.value is branch_error
    entity_search.assert_awaited_once()
    config_search.assert_awaited_once()


async def test_component_search_without_warnings_has_no_warnings_key(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list is omitted, not returned as ``"warnings": []``."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    # Every config surface the component reads is loaded, so none is reported
    # as inaccessible.
    for domain in ("automation", "script", "scene", "lovelace"):
        assert await async_setup_component(hass, domain, {})
    entity_registry.async_get_or_create(
        "light", "test", "kitchen-light", suggested_object_id="kitchen"
    )
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})

    resp = await _build_ha_search(component_core_client)(query="kitchen")

    assert resp["entities"][0]["entity_id"] == "light.kitchen"
    assert "warnings" not in resp, resp.get("warnings")
