"""Unit tests for KnxTools — covers the success path plus error/edge branches.

End-to-end tests would require a live Home Assistant with the KNX integration
loaded and an uploaded ETS project; mocking ``send_websocket_message`` keeps
these hermetic while still exercising every branch of the tool. The sample
``knx/get_knx_project`` response below mirrors the real upstream schema
(``xknxproject.models.KNXProject`` / ``GroupAddress``) with several group
addresses across different DPTs, a group-range hierarchy, and project info.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_knx import KnxTools, _is_knx_not_loaded_error

# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def tools():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return KnxTools(client)


def _sample_knxproject() -> dict:
    """A realistic ``knx/get_knx_project`` result payload.

    Keys mirror ``xknxproject.models.KNXProject``; group-address entries mirror
    ``GroupAddress`` (address, name, dpt, description, ...). DPTs are varied:
    switch (1.001), dimming step (3.007), scaling (5.001), 2-byte float /
    temperature (9.001), and one address with no DPT assigned.
    """
    return {
        "info": {
            "name": "Demo Project",
            "last_modified": "2026-01-15T10:00:00",
            "tool_version": "6.1.0",
            "xknxproject_version": "3.8.1",
        },
        "group_addresses": {
            "1/0/1": {
                "name": "Living Room Light Switch",
                "identifier": "GA-1",
                "raw_address": 2049,
                "address": "1/0/1",
                "project_uid": None,
                "dpt": {"main": 1, "sub": 1},
                "data_secure": False,
                "communication_object_ids": ["O-1_R-1"],
                "description": "On/Off",
                "comment": "",
            },
            "1/0/2": {
                "name": "Living Room Light Dim Step",
                "identifier": "GA-2",
                "raw_address": 2050,
                "address": "1/0/2",
                "project_uid": None,
                "dpt": {"main": 3, "sub": 7},
                "data_secure": False,
                "communication_object_ids": ["O-2_R-2"],
                "description": "Relative dimming",
                "comment": "",
            },
            "1/0/3": {
                "name": "Living Room Light Brightness",
                "identifier": "GA-3",
                "raw_address": 2051,
                "address": "1/0/3",
                "project_uid": None,
                "dpt": {"main": 5, "sub": 1},
                "data_secure": False,
                "communication_object_ids": ["O-3_R-3"],
                "description": "Brightness %",
                "comment": "",
            },
            "2/1/0": {
                "name": "Living Room Temperature",
                "identifier": "GA-4",
                "raw_address": 4352,
                "address": "2/1/0",
                "project_uid": None,
                "dpt": {"main": 9, "sub": 1},
                "data_secure": False,
                "communication_object_ids": ["O-4_R-4"],
                "description": "Actual temperature",
                "comment": "",
            },
            "3/3/3": {
                "name": "Unassigned DPT Address",
                "identifier": "GA-5",
                "raw_address": 6915,
                "address": "3/3/3",
                "project_uid": None,
                "dpt": None,
                "data_secure": False,
                "communication_object_ids": [],
                "description": "",
                "comment": "",
            },
        },
        "group_ranges": {
            "1/-/-": {
                "name": "Lighting",
                "address_start": 2048,
                "address_end": 4095,
                "comment": "",
                "group_addresses": [],
                "group_ranges": {},
            }
        },
        "devices": {},
    }


# -----------------------------------------------------------------------------
# _is_knx_not_loaded_error
# -----------------------------------------------------------------------------


class TestIsKnxNotLoadedError:
    def test_matches_upstream_sentinel(self):
        assert _is_knx_not_loaded_error("Command failed: KNX integration not loaded.")

    def test_does_not_match_other_errors(self):
        assert not _is_knx_not_loaded_error("Command failed: something else")
        assert not _is_knx_not_loaded_error("")


# -----------------------------------------------------------------------------
# ha_knx_get_project
# -----------------------------------------------------------------------------


class TestGetKnxProject:
    async def test_returns_group_addresses_and_metadata(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _sample_knxproject(),
        }

        result = await tools.ha_knx_get_project()

        assert result["success"] is True
        assert result["count"] == 5
        assert set(result["group_addresses"]) == {
            "1/0/1",
            "1/0/2",
            "1/0/3",
            "2/1/0",
            "3/3/3",
        }
        # DPTs are preserved verbatim, including the null-DPT entry.
        assert result["group_addresses"]["1/0/1"]["dpt"] == {"main": 1, "sub": 1}
        assert result["group_addresses"]["2/1/0"]["dpt"] == {"main": 9, "sub": 1}
        assert result["group_addresses"]["3/3/3"]["dpt"] is None
        # Names/descriptions and structural tree pass through.
        assert result["group_addresses"]["1/0/1"]["name"] == "Living Room Light Switch"
        assert result["group_ranges"]["1/-/-"]["name"] == "Lighting"
        assert result["info"]["name"] == "Demo Project"

        tools._client.send_websocket_message.assert_awaited_once_with(
            {"type": "knx/get_knx_project"}
        )

    async def test_no_project_uploaded_returns_empty_with_note(self, tools):
        """Integration loaded but no .knxproj uploaded → get_knxproject() is
        None. The tool returns an empty result with a note, not an error."""
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": None,
        }

        result = await tools.ha_knx_get_project()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["group_addresses"] == {}
        assert result["group_ranges"] == {}
        assert "note" in result

    async def test_integration_not_loaded_raises_component_not_installed(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "Command failed: KNX integration not loaded.",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_knx_get_project()

        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        assert "COMPONENT_NOT_INSTALLED" in json.dumps(err)

    async def test_other_ws_failure_raises_service_call_failed(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "Command failed: something broke",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_knx_get_project()

        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        assert "SERVICE_CALL_FAILED" in json.dumps(err)

    async def test_unexpected_exception_is_wrapped(self, tools):
        tools._client.send_websocket_message.side_effect = RuntimeError("boom")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_knx_get_project()

        err = json.loads(str(exc_info.value))
        assert err["success"] is False
