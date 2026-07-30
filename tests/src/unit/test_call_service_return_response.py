"""Regression tests for ha_call_service's return_response placement (issue #2085).

Home Assistant's REST reply to ``return_response=true`` is an envelope:
``{"changed_states": [...], "service_response": {...}}``. The legacy REST path in
``ha_call_service`` used to hand that whole envelope to ``_project_service_result``
(which returns dicts unchanged) *and* copy ``service_response`` to the top level,
so the service's response shipped twice, byte-identical, doubling its token cost.

The envelope is now split before projection: ``result`` carries only the changed
states and ``service_response`` appears exactly once at the top level — the same
placement ``_build_component_call_response`` already used for the component path.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.tools_service import ServiceTools

_SHELL_RESPONSE = {"stdout": "abc", "stderr": "", "returncode": 0}

_CHANGED_STATE = {
    "entity_id": "light.x",
    "state": "on",
    "attributes": {},
    "context": {"id": "ctx-1"},
    "last_changed": "t",
    "last_updated": "t",
    "last_reported": "t",
}


def _make_tools(call_service_return: Any) -> ServiceTools:
    client = MagicMock()
    client.call_service = AsyncMock(return_value=call_service_return)
    tools = ServiceTools.__new__(ServiceTools)
    tools._client = client
    tools._device_tools = MagicMock()
    return tools


def _occurrences(response: dict[str, Any], needle: str) -> int:
    return json.dumps(response).count(needle)


@pytest.fixture(autouse=True)
def _no_component():
    """Pin every test to the legacy REST path (component advertises nothing).

    ``component_supports(None, ...)`` is False, so ``_call_service_via_component``
    returns None and ``ha_call_service`` falls through to the REST POST.
    """
    with patch(
        "ha_mcp.tools.tools_service.get_component_caps",
        new=AsyncMock(return_value=None),
    ):
        yield


class TestReturnResponsePlacement:
    async def test_service_response_appears_once_at_top_level(self):
        """The envelope splits: response data top-level, changed states in result."""
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == _SHELL_RESPONSE
        result = response["result"]
        assert isinstance(result, list), (
            f"result must be the projected changed_states list, got: {result!r}"
        )
        assert len(result) == 1
        assert result[0]["entity_id"] == "light.x"
        assert "service_response" not in result[0]
        assert _occurrences(response, "stdout") == 1, (
            f"service_response must be serialized exactly once: {response!r}"
        )

    async def test_verbose_still_returns_one_copy(self):
        """verbose=True bypasses projection but must not resurrect the duplicate."""
        tools = _make_tools(
            {"changed_states": [_CHANGED_STATE], "service_response": _SHELL_RESPONSE}
        )

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True, verbose=True
        )

        assert response["service_response"] == _SHELL_RESPONSE
        # verbose returns the changed states raw (metadata kept), never the envelope.
        assert response["result"] == [_CHANGED_STATE]
        assert _occurrences(response, "stdout") == 1, (
            f"service_response must be serialized exactly once: {response!r}"
        )

    async def test_envelope_without_service_response_key_surfaces_whole_dict(self):
        """No ``service_response`` key: the whole dict goes top-level, once."""
        tools = _make_tools({"changed_states": []})

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert response["service_response"] == {"changed_states": []}
        assert response["result"] == []
        assert _occurrences(response, "changed_states") == 1, (
            f"the fallback dict must be serialized exactly once: {response!r}"
        )

    async def test_null_service_response_is_still_set(self):
        """A legitimately null response data is reported, not dropped."""
        tools = _make_tools({"changed_states": [], "service_response": None})

        response = await tools.ha_call_service(
            domain="shell_command", service="test", return_response=True
        )

        assert "service_response" in response
        assert response["service_response"] is None
        assert response["result"] == []

    async def test_return_response_false_has_no_service_response_key(self):
        """Without return_response the REST reply is a plain changed-states list."""
        tools = _make_tools([_CHANGED_STATE])

        response = await tools.ha_call_service(domain="shell_command", service="test")

        assert "service_response" not in response
        result = response["result"]
        assert isinstance(result, list)
        assert result[0]["entity_id"] == "light.x"
