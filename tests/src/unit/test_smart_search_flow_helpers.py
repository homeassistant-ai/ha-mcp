"""Unit tests for ``ha_deep_search`` coverage of UI-created flow-based
helpers (template, group, utility_meter, derivative, ...).

Issue #1457: deep_search previously hard-coded the helper list to
``input_*`` only, so config-entry helpers were invisible. The flow-helper
branch now lists config entries for any domain in ``FLOW_HELPER_TYPES``
and probes each entry's options flow so the helper's current config
(template body, group members, etc.) is searchable alongside the
storage-based helpers.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools(client: Any) -> SmartSearchTools:
    """Construct SmartSearchTools without loading global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _make_flow_form(suggested_value: str) -> dict[str, Any]:
    """Build the options-flow response shape HA returns for a template helper."""
    return {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "binary_sensor",
        "data_schema": [
            {
                "name": "state",
                "selector": {"template": {}},
                "description": {"suggested_value": suggested_value},
                "required": True,
            }
        ],
    }


@pytest.mark.asyncio
class TestFlowHelperDeepSearch:
    """``_search_flow_helpers`` surfaces flow-helper config entries."""

    async def test_template_helper_matches_on_title(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEWEATHER",
                    "domain": "template",
                    "title": "Weather Message",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXTEMPLATEOTHER",
                    "domain": "template",
                    "title": "Unrelated",
                    "supports_options": True,
                },
            ]
        )
        # Title-only exact match — no probe needed for the matching entry.
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ states('sensor.x') }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        semaphore = asyncio.Semaphore(8)
        results = await tools._search_flow_helpers(
            "weather", exact_match=True, semaphore=semaphore, include_config=False
        )

        entry_ids = {r["entry_id"] for r in results}
        assert "01HXTEMPLATEWEATHER" in entry_ids
        assert "01HXTEMPLATEOTHER" not in entry_ids
        match = next(r for r in results if r["entry_id"] == "01HXTEMPLATEWEATHER")
        assert match["helper_type"] == "template"
        assert match["name"] == "Weather Message"
        assert match["match_in_name"] is True
        # include_config=False → no config in result, no options probe needed.
        assert "config" not in match

    async def test_template_helper_matches_on_template_body(self) -> None:
        # Query is a substring of the template body but NOT the title —
        # forces the options-flow probe path.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEX",
                    "domain": "template",
                    "title": "Renamed Sensor",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form(
                "{{ states('sensor.outside_temperature') | float }}"
            )
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results = await tools._search_flow_helpers(
            "outside_temperature",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )

        assert len(results) == 1
        assert results[0]["entry_id"] == "01HXTEMPLATEX"
        assert results[0]["match_in_config"] is True
        client.start_options_flow.assert_awaited_once_with("01HXTEMPLATEX")
        client.abort_options_flow.assert_awaited_once_with("flow-1")

    async def test_include_config_attaches_options_body(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXTEMPLATEZ",
                    "domain": "template",
                    "title": "Comfort Index",
                    "supports_options": True,
                }
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ comfort_index() }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results = await tools._search_flow_helpers(
            "comfort",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=True,
        )
        assert results[0]["config"] == {"state": "{{ comfort_index() }}"}

    async def test_skips_non_flow_helper_domains(self) -> None:
        # Mixed list: only the template entry should be considered.
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXLIGHTHUE",
                    "domain": "hue",
                    "title": "Hue Bridge",
                    "supports_options": True,
                },
                {
                    "entry_id": "01HXTEMPLATEAA",
                    "domain": "template",
                    "title": "Match Me",
                    "supports_options": True,
                },
            ]
        )
        client.start_options_flow = AsyncMock(
            return_value=_make_flow_form("{{ true }}")
        )
        client.abort_options_flow = AsyncMock()

        tools = _make_tools(client)
        results = await tools._search_flow_helpers(
            "Match",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert [r["entry_id"] for r in results] == ["01HXTEMPLATEAA"]

    async def test_skips_entries_without_supports_options(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXNOOPTS",
                    "domain": "template",
                    "title": "Locked Template",
                    "supports_options": False,
                }
            ]
        )
        tools = _make_tools(client)
        results = await tools._search_flow_helpers(
            "locked",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []

    async def test_returns_empty_when_rest_call_fails(self) -> None:
        client = MagicMock()
        client._request = AsyncMock(side_effect=RuntimeError("REST down"))
        tools = _make_tools(client)
        results = await tools._search_flow_helpers(
            "anything",
            exact_match=True,
            semaphore=asyncio.Semaphore(8),
            include_config=False,
        )
        assert results == []
