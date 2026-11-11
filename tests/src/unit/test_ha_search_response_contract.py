"""ha_search's response contract when a surface fails or nothing warns.

A failed surface leaves a partial response while another surface answers; when
none answers, the first surface's error is raised. A response without warnings
carries no ``warnings`` key.

``ha_search`` runs on the server's own client against a real ``hass``. Without
the component, Core answers its capability probe with ``unknown_command`` and
the search takes the legacy route; a surface's failure is set up by replacing
that surface, which is this repository's own method.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_search
from ha_mcp.tools.search import legacy as search_legacy

from ._search_tool import _build_ha_search, _setup_visibility_disabled


def _surface(name: str, replacement: AsyncMock) -> AbstractContextManager[AsyncMock]:
    """Replace one ``ha_search`` surface of every ``SearchTools``."""
    return patch.object(tools_search.SearchTools, name, replacement)


async def test_branch_exception_without_a_message_reports_its_type(
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``str(TimeoutError())`` is "", which used to yield ``"entities: "``.

    The config surface answers, so the response is partial, not an error."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    monkeypatch.setattr(
        search_legacy,
        "_prefetch_shared_search_snapshots",
        AsyncMock(return_value=(None, None)),
    )

    # A query alone runs both the entity and the config surface.
    with (
        _surface("_ha_search_entities", AsyncMock(side_effect=TimeoutError)),
        _surface("_ha_deep_search", AsyncMock(return_value={"total_matches": 0})),
    ):
        response = await _build_ha_search(core_client)(query="lamp")

    assert response["partial"] is True
    assert response["errors"] == [{"surface": "entities", "error": "TimeoutError"}]
    assert response["partial_reason"] == "entities: TimeoutError"


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
        _surface("_ha_search_entities", AsyncMock(side_effect=TimeoutError)),
        pytest.raises(ToolError) as excinfo,
    ):
        await _build_ha_search(core_client)(domain_filter="light")

    payload = json.loads(str(excinfo.value))
    assert payload["success"] is False
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"


async def test_no_surface_answering_keeps_the_first_branch_tool_error(
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first surface's own structured error is re-raised as is, not the
    error of a surface after it."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    branch_error = ToolError('{"success": false, "error": {"code": "X"}}')
    monkeypatch.setattr(
        search_legacy,
        "_prefetch_shared_search_snapshots",
        AsyncMock(return_value=(None, None)),
    )

    with (
        _surface("_ha_search_entities", AsyncMock(side_effect=branch_error)),
        _surface("_ha_deep_search", AsyncMock(side_effect=TimeoutError)),
        pytest.raises(ToolError) as excinfo,
    ):
        await _build_ha_search(core_client)(query="lamp")

    assert excinfo.value is branch_error


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
