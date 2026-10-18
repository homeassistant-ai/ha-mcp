"""Shared handler seam types for the ``ha_mcp_tools/*`` command surface.

Most commands return a JSON-serializable result and let the shared handler
encode it. A command whose payload can be large encodes it off the event loop
instead (Core does this for logbook rows) and returns
:class:`PreSerializedResult` so the handler sends the finished bytes without
running ``connection.send_result``'s synchronous encode on the loop.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreSerializedResult:
    """A command result already serialized to its final wire message.

    ``payload`` must be the complete ``result_message`` bytes for the request's
    message id (``json_bytes(messages.result_message(msg_id, result))``),
    produced off the event loop. The shared handler forwards it verbatim.
    """

    payload: bytes
