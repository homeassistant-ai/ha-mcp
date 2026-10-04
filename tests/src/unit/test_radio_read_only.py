"""Read-only exemptions for radio management actions."""

from typing import Any

import pytest

from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS


@pytest.mark.parametrize(
    ("args", "allowed"),
    [
        ({"radio": "matter", "action": "diagnostics", "device_id": "d1"}, True),
        ({"radio": "zwave", "action": "network_status"}, True),
        (
            {"radio": "zwave", "action": "get_config_params", "device_id": "d1"},
            True,
        ),
        (
            {
                "radio": "zwave",
                "action": "get_config_param",
                "device_id": "d1",
                "params": {"property": 3, "refresh": True},
            },
            True,
        ),
        ({"radio": "matter", "action": "ping", "device_id": "d1"}, True),
        ({"radio": "zigbee", "action": "cluster_read", "device_id": "d1"}, True),
        ({"radio": "thread", "action": "list_datasets"}, True),
        ({"radio": "zwave", "action": "add"}, False),
        ({"radio": "matter", "action": "remove_fabric", "device_id": "d1"}, False),
        ({"radio": "thread", "action": "set_network", "confirm": True}, False),
        ({"radio": "zwave", "action": "firmware_update", "device_id": "d1"}, False),
        # Non-mutating but intentionally blocked: network_backup creates a
        # backup artifact (mirrors ha_manage_backup), discover_routers starts
        # a long-running mDNS scan.
        ({"radio": "zigbee", "action": "network_backup"}, False),
        ({"radio": "thread", "action": "discover_routers"}, False),
        # A missing action fails closed — never a silent read.
        ({"radio": "zwave"}, False),
        ({}, False),
    ],
)
def test_manage_radio(args: dict[str, Any], allowed: bool) -> None:
    rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_radio"].blocked_write
    assert (rule(args) is None) is allowed
