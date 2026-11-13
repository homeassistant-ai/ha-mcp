"""The approval-response channel's connections, opened as the server's client.

``HomeAssistantSmartMCPServer._apply_tool_security_policies`` binds these to
the server and hands them to ``ApprovalResponseListener``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def approval_ws_client(server: Any) -> Any:
    """Return a WebSocket client that authenticates as ``server.client``."""
    # Imported at call time: the WebSocket stack is only needed once a
    # gated call is actually announced, which may never happen.
    from ..client.websocket_client import get_websocket_client

    # Keyed to the credentials the announcement itself goes out
    # with, the way ``HomeAssistantClient.send_websocket_message``
    # does it. In OAuth mode ``server.client`` is a proxy resolving to
    # the current request's client, while the global settings hold
    # only the ``oauth-mode-token`` placeholder -- so an
    # unparameterised call would authenticate the response
    # subscription as nobody, and a request could be announced over
    # REST on a channel that can never carry the answer back. With no
    # request context the proxy raises HomeAssistantAuthError, which
    # must propagate rather than fall back to the pooled connection.
    client = server.client
    return await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )


async def emit_approval_result_as(
    server: Any,
    token: str,
    decision: str,
    *,
    applied: bool,
    reason: str,
    tool_name: str | None = None,
) -> None:
    from ..client.rest_client import HomeAssistantClient
    from .events import emit_approval_result

    # Credentials come from ``server.client`` for the same reason as in
    # ``approval_ws_client``: in OAuth mode it is a proxy to the current
    # request's client. This runs in a bus handler, not in a request,
    # so the proxy resolves to nobody and raises rather than handing
    # back a usable client. Unlike there, that failure is caught and
    # logged on purpose: the decision is already applied, and only its
    # result event is lost. A fresh REST client over the snapshot keeps
    # the result going out as the same identity the request was
    # announced with.
    client = server.client
    url: str | None
    token_value: str | None
    verify_ssl: bool | None
    known_is_admin: bool | None
    admin_route_refused: bool
    try:
        url = client.base_url
        token_value = client.token
        verify_ssl = client.verify_ssl
        known_is_admin = client.known_is_admin
        admin_route_refused = client.admin_route_refused is True
    except Exception:
        logger.debug(
            "policy decisions: no credentials for the result event; "
            "the decision itself is unaffected",
            exc_info=True,
        )
        return
    # Owned here, so closed here: this client is built per event and
    # carries its own httpx connection pool, which nothing else will
    # ever reclaim. A wrong PIN retried by a chatty automation would
    # otherwise open one per attempt.
    async with HomeAssistantClient(
        url,
        token_value,
        verify_ssl=verify_ssl,
        is_admin=known_is_admin,
        admin_route_refused=admin_route_refused,
    ) as result_client:
        await emit_approval_result(
            result_client,
            token,
            decision,
            applied=applied,
            reason=reason,
            tool_name=tool_name,
        )
