"""The two flow walkers for tests whose walks know no ``FlowRef``.

They keep the raw error and abort keys, and a progress step raises
``ValueError``; a test passes ``flow=`` itself to get Core's text or a poll.
"""

from functools import partial

from ha_mcp.tools import config_entry_flow_walker

_handle_flow_steps = partial(config_entry_flow_walker._handle_flow_steps, flow=None)
_handle_config_subentry_flow_steps = partial(
    config_entry_flow_walker._handle_config_subentry_flow_steps, flow=None
)
