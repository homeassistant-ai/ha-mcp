"""Unit tests for the deep-search branch's error handling inside ha_search.

ha_deep_search is no longer @mcp.tool-decorated — it is invoked only as an
internal helper by the merged ``ha_search`` orchestrator. Errors raised by
the helper are captured by ``asyncio.gather(..., return_exceptions=True)``
and surfaced via the orchestrator's ``partial=True`` + ``errors[]`` path.
The structured-error formatting (no traceback / error_type leak, code +
message + suggestions) is still provided by ``exception_to_structured_error``
inside the helper and survives the gather capture as a stringified
``ToolError`` payload (issue #517).

``ha_search`` runs on the server's own client against a real ``hass``; only
the search helpers that fail (this repository's own ``SmartSearchTools``
methods) are replaced.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.smart_search import SmartSearchTools

from ._search_tool import _build_ha_search, _setup_visibility_disabled


def _extract_deep_search_error(response: dict) -> dict:
    """Parse the captured ToolError JSON for the configs (deep-search) branch."""
    assert response.get("partial") is True, "expected partial=True after helper failure"
    errors = response.get("errors", [])
    deep_errors = [e for e in errors if e.get("surface") == "configs"]
    assert deep_errors, f"expected one configs-surface error, got {errors}"
    # ``error`` is the str() of the captured ToolError, which is structured JSON.
    return json.loads(deep_errors[0]["error"])


@pytest.fixture
async def ha_search_tool(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    # ``config`` answers the entity-registry list both branches share.
    assert await async_setup_component(hass, "config", {})
    _setup_visibility_disabled(tmp_path, monkeypatch)
    return _build_ha_search(core_client)


class TestDeepSearchErrorHandling:
    """The deep-search helper's structured error survives the orchestrator capture."""

    async def test_error_does_not_leak_traceback_or_raw_fields(
        self, ha_search_tool: Any
    ) -> None:
        failing = AsyncMock(side_effect=RuntimeError("Connection refused"))

        with patch.object(SmartSearchTools, "deep_search", failing):
            response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        assert err["success"] is False
        assert isinstance(err["error"], dict), (
            "error must be structured dict, not raw string"
        )
        assert "code" in err["error"]
        assert "message" in err["error"]
        assert "traceback" not in err
        assert "error_type" not in err

    async def test_error_includes_search_specific_suggestions(
        self, ha_search_tool: Any
    ) -> None:
        failing = AsyncMock(side_effect=RuntimeError("Something went wrong"))

        with patch.object(SmartSearchTools, "deep_search", failing):
            response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        suggestions = err["error"]["suggestions"]
        assert "Check Home Assistant connection" in suggestions
        assert "Try simpler search terms" in suggestions

    async def test_operational_entities_tool_error_remains_partial(
        self, ha_search_tool: Any
    ) -> None:
        """An operational entity failure is captured, not re-raised as validation."""
        entities_down = AsyncMock(
            side_effect=ToolError(
                json.dumps(
                    create_error_response(
                        ErrorCode.CONNECTION_FAILED,
                        "entity registry unavailable",
                    )
                )
            )
        )
        configs_answer = AsyncMock(
            return_value={
                "success": True,
                "automations": [],
                "scripts": [],
                "scenes": [],
                "helpers": [],
                "warnings": [],
                "errors": [],
                "partial": False,
            }
        )

        with (
            patch.object(SmartSearchTools, "smart_entity_search", entities_down),
            patch.object(SmartSearchTools, "deep_search", configs_answer),
        ):
            response = await ha_search_tool(query="test_query", exact_match=False)

        assert response["partial"] is True
        assert any(e["surface"] == "entities" for e in response["errors"])
        assert any("incomplete results" in w for w in response["warnings"])

    @pytest.mark.parametrize(
        "exception_cls,exception_msg,expected_code",
        [
            (ValueError, "invalid input", "VALIDATION_FAILED"),
            (TimeoutError, "timed out", "TIMEOUT_OPERATION"),
            (RuntimeError, "unexpected failure", "INTERNAL_ERROR"),
        ],
    )
    async def test_different_exception_types_produce_correct_error_codes(
        self,
        ha_search_tool: Any,
        exception_cls: type[Exception],
        exception_msg: str,
        expected_code: str,
    ) -> None:
        failing = AsyncMock(side_effect=exception_cls(exception_msg))

        with patch.object(SmartSearchTools, "deep_search", failing):
            response = await ha_search_tool(query="test_query")

        err = _extract_deep_search_error(response)
        assert err["success"] is False
        assert err["error"]["code"] == expected_code
