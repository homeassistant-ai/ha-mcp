"""The two flow walkers for tests whose walks know no ``FlowRef``.

They keep the raw error and abort keys, and a progress step raises
``ValueError``; a test passes ``flow=`` itself to get Core's text or a poll.
"""

from collections.abc import Awaitable
from typing import Any

from ha_mcp.tools import config_entry_flow_walker


def _handle_flow_steps(*args: Any, **kwargs: Any) -> Awaitable[Any]:
    return config_entry_flow_walker._handle_flow_steps(*args, flow=None, **kwargs)


def _handle_config_subentry_flow_steps(*args: Any, **kwargs: Any) -> Awaitable[Any]:
    return config_entry_flow_walker._handle_config_subentry_flow_steps(
        *args, flow=None, **kwargs
    )
