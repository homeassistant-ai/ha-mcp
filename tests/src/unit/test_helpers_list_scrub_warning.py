"""ha_config_list_helpers warns when the component's secret scrub degraded."""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.tools.config_entry_flow import FLOW_HELPER_TYPES
from ha_mcp.tools.config_helpers import listing as helper_listing
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES

_WARNING = helper_listing._SCRUB_DEGRADED_WARNING


def _result(degraded: bool | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "helpers": [
            {"kind": "flow", "helper_type": "template", "entry_id": "e1", "options": {}}
        ],
        "covered_types": sorted(FLOW_HELPER_TYPES | SIMPLE_HELPER_TYPES),
    }
    if degraded is not None:
        result["secret_scrub_degraded"] = degraded
    return result


@pytest.mark.parametrize(
    "helper_type,degraded,warned",
    [
        ("template", True, True),
        ("template", False, False),
        ("template", None, False),
        ("input_boolean", True, False),
    ],
)
def test_single_type_listing_warns_only_for_a_degraded_flow_listing(
    helper_type: str, degraded: bool | None, warned: bool
) -> None:
    response = helper_listing._shape_component_helpers_response(
        helper_type, _result(degraded)
    )
    assert (_WARNING in response.get("warnings", [])) is warned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "degraded,warned", [(True, True), (False, False), (None, False)]
)
async def test_all_types_listing_warns_for_a_degraded_scrub(
    degraded: bool | None, warned: bool
) -> None:
    async def _no_legacy(helper_type: str) -> dict[str, Any]:
        raise AssertionError(f"unexpected legacy listing of {helper_type}")

    response = await helper_listing.shape_all_helpers_response(
        _result(degraded), _no_legacy
    )
    assert (_WARNING in response.get("warnings", [])) is warned
