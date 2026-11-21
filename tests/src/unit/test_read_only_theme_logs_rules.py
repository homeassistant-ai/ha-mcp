"""Read Only Mode's read/write rules for ``ha_manage_theme`` and ``ha_manage_logs``.

Each rule sees a call's raw arguments and names the write it blocks, or
returns None for a read.
"""

import pytest

from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS


@pytest.mark.parametrize(
    "args,allowed",
    [
        ({"action": "list"}, True),
        # No pure-read duplicate exists, and Read Only Mode still surfaces
        # the capture warning naming this value -- so it stays inspectable.
        ({"action": "get_engine_theme"}, True),
        # Both writes stay blocked: the backend default and the
        # engine-account restore.
        ({"action": "set", "theme_name": "nord"}, False),
        ({"action": "set_engine_theme", "value": {"theme": ""}}, False),
        # A missing action fails closed -- never a silent read.
        ({}, False),
    ],
)
def test_manage_theme(args, allowed):
    rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_theme"].blocked_write
    assert (rule(args) is None) is allowed


@pytest.mark.parametrize(
    ("args", "allowed"),
    [
        # describe sends nothing, whatever the action.
        ({"action": "recorder.purge", "describe": True}, True),
        # A describe that is not exactly true is a write: the predicate
        # sees the arguments before schema coercion.
        ({"action": "recorder.purge", "describe": "false"}, False),
        ({}, False),
    ],
)
def test_manage_logs(args, allowed):
    rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_logs"].blocked_write
    assert (rule(args) is None) is allowed
