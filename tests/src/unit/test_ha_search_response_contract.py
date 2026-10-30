"""ha_search's response contract when a surface fails or nothing warns.

A failed surface leaves a partial response while another surface answers; when
none answers, the first surface's error is raised. A response without warnings
carries no ``warnings`` key.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_search
from ha_mcp.tools.search import component as search_component
from ha_mcp.tools.search import legacy as search_legacy
from ha_mcp.tools.search.response import _ResolvedSearch

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_search_component_routing import (
    _CAPS_SEARCH,
    RoutingClient,
    _build_ha_search,
    _entity_search_result,
    _setup_visibility_disabled,
)


def _entities_only_request() -> _ResolvedSearch:
    """A resolved request that fans out to the entity branch only."""
    return _ResolvedSearch(
        query="lamp",
        query_text="lamp",
        domain_filter=None,
        area_filter=None,
        state_filter=None,
        parsed_search_types=None,
        parsed_fields=None,
        result_fields=None,
        limit=10,
        offset=0,
        exact_match=False,
        include_hidden=False,
        include_config=False,
        group_by_domain=False,
        per_domain_limit=None,
        config_time_budget=None,
        registry_eligible=True,
        body_eligible=False,
        body_skipped_by_intent_gate=False,
    )


async def _time_out(**kwargs: object) -> dict:
    raise TimeoutError


@pytest.mark.asyncio
async def test_branch_exception_without_a_message_reports_its_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``str(TimeoutError())`` is "", which used to yield ``"entities: "``.

    The config surface answers, so the response is partial, not an error."""
    monkeypatch.setattr(
        search_legacy,
        "_prefetch_shared_search_snapshots",
        AsyncMock(return_value=(None, None)),
    )
    tools = tools_search.SearchTools(MagicMock(), MagicMock())
    tools._ha_search_entities = _time_out  # type: ignore[method-assign]
    tools._ha_deep_search = AsyncMock(  # type: ignore[method-assign]
        return_value={"total_matches": 0}
    )
    request = dataclasses.replace(_entities_only_request(), body_eligible=True)

    response = await tools._legacy_ha_search(request, None)

    assert response["partial"] is True
    assert response["errors"] == [{"surface": "entities", "error": "TimeoutError"}]
    assert response["partial_reason"] == "entities: TimeoutError"


@pytest.mark.asyncio
async def test_no_surface_answering_is_a_structured_error() -> None:
    """Every surface failed: an error, not ``success`` with empty buckets that
    read as "nothing matched"."""
    tools = tools_search.SearchTools(MagicMock(), MagicMock())
    tools._ha_search_entities = _time_out  # type: ignore[method-assign]

    with pytest.raises(ToolError) as excinfo:
        await tools._legacy_ha_search(_entities_only_request(), None)

    payload = json.loads(str(excinfo.value))
    assert payload["success"] is False
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"


@pytest.mark.asyncio
async def test_no_surface_answering_keeps_the_first_branch_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first surface's own structured error is re-raised as is, not the
    error of a surface after it."""
    branch_error = ToolError('{"success": false, "error": {"code": "X"}}')

    async def _fail(**kwargs: object) -> dict:
        raise branch_error

    monkeypatch.setattr(
        search_legacy,
        "_prefetch_shared_search_snapshots",
        AsyncMock(return_value=(None, None)),
    )
    tools = tools_search.SearchTools(MagicMock(), MagicMock())
    tools._ha_search_entities = _fail  # type: ignore[method-assign]
    tools._ha_deep_search = _time_out  # type: ignore[method-assign]
    request = dataclasses.replace(_entities_only_request(), body_eligible=True)

    with pytest.raises(ToolError) as excinfo:
        await tools._legacy_ha_search(request, None)

    assert excinfo.value is branch_error


@pytest.mark.asyncio
async def test_component_search_without_warnings_has_no_warnings_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list is omitted, not returned as ``"warnings": []``."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result=_entity_search_result(),
    )
    ha_search = _build_ha_search(RoutingClient())

    with patch_ws(ws, search_component):
        resp = await ha_search(query="kitchen")

    assert resp["entities"][0]["entity_id"] == "light.kitchen"
    assert "warnings" not in resp
