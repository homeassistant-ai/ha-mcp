"""ha_config_list_helpers shows what the component could not give it: options it
withheld, a degraded secret scrub, and helper flows its failed loader read left
out."""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_helpers import listing as helper_listing
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES

from ._stub_helper_flows import STUB_HELPER_FLOWS

_SCRUB = helper_listing._SCRUB_DEGRADED_WARNING
_CUSTOM = {
    "kind": "flow",
    "helper_type": "my_helper",
    "entry_id": "e2",
    "options": None,
    "options_withheld": "custom_integration",
}


def _result(**flags: bool) -> dict[str, Any]:
    return {
        "helpers": [
            {
                "kind": "flow",
                "helper_type": "template",
                "entry_id": "e1",
                "options": {},
            },
            dict(_CUSTOM),
        ],
        "covered_types": sorted(STUB_HELPER_FLOWS | SIMPLE_HELPER_TYPES),
        **flags,
    }


async def _no_legacy(helper_type: str) -> dict[str, Any]:
    raise AssertionError(f"unexpected legacy listing of {helper_type}")


@pytest.mark.parametrize(
    "helper_type,flags,warnings",
    [
        ("template", {"secret_scrub_degraded": True}, [_SCRUB]),
        ("template", {"helper_flows_degraded": True}, []),
        ("template", {"secret_scrub_degraded": False}, []),
        ("template", {}, []),
        ("input_boolean", {"secret_scrub_degraded": True}, []),
    ],
)
def test_single_type_listing_warns_when_the_flow_helper_read_degraded(
    helper_type: str, flags: dict[str, bool], warnings: list[str]
) -> None:
    response = helper_listing._shape_component_helpers_response(
        helper_type, _result(**flags)
    )
    assert response.get("warnings", []) == warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flags,warnings",
    [
        ({"secret_scrub_degraded": True}, [_SCRUB]),
        ({"helper_flows_degraded": True}, []),
        ({"secret_scrub_degraded": False}, []),
        ({}, []),
    ],
)
async def test_all_types_listing_warns_when_the_flow_helper_read_degraded(
    flags: dict[str, bool], warnings: list[str]
) -> None:
    response = await helper_listing.shape_all_helpers_response(
        _result(**flags), _no_legacy, flow_types=STUB_HELPER_FLOWS
    )
    assert response.get("warnings", []) == warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flags,code",
    [
        ({"helper_flows_degraded": True}, "SERVICE_CALL_FAILED"),
        ({}, "COMPONENT_NOT_INSTALLED"),
    ],
)
async def test_a_helper_flow_left_out_by_a_failed_loader_read_is_not_blamed_on_the_component(
    flags: dict[str, bool], code: str
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await helper_listing.shape_all_helpers_response(
            _result(**flags),
            _no_legacy,
            flow_types=STUB_HELPER_FLOWS | {"my_custom_helper"},
        )
    assert code in str(exc_info.value)
    assert "my_custom_helper" in str(exc_info.value)


@pytest.mark.asyncio
async def test_all_types_listing_marks_a_custom_helper_as_withheld_not_empty() -> None:
    """An agent must be able to tell withheld options from a helper without any."""
    response = await helper_listing.shape_all_helpers_response(
        _result(), _no_legacy, flow_types=STUB_HELPER_FLOWS
    )
    by_type = {h["helper_type"]: h for h in response["helpers"]}
    assert by_type["my_helper"] == {
        "helper_type": "my_helper",
        "entry_id": "e2",
        "options_withheld": "custom_integration",
    }
    assert "options_withheld" not in by_type["template"]


def test_single_type_listing_marks_a_custom_helper_as_withheld() -> None:
    response = helper_listing._shape_component_helpers_response(
        "template", {"helpers": [dict(_CUSTOM, helper_type="template")]}
    )
    (record,) = response["helpers"]
    assert record["options_withheld"] == "custom_integration"
    assert "options" not in record
