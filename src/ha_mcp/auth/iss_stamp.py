"""RFC 9207 ``iss`` stamping for the SDK's own error redirects."""

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REDIRECT_STATUSES = frozenset({302, 303, 307})


class StampIssOnErrorRedirects:
    """ASGI wrapper that stamps ``iss`` on the error redirects of the app it wraps.

    The SDK serves ``/authorize`` as an ASGI app, so the stamp is applied to the
    headers as they are sent. A class, not a function, so Starlette's ``Route``
    mounts it as raw ASGI instead of wrapping it in ``request_response``.
    """

    def __init__(self, app: ASGIApp, stamp: Callable[[str], str]) -> None:
        self.app = app
        self.stamp = stamp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_stamped(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] in _REDIRECT_STATUSES
            ):
                headers = MutableHeaders(scope=message)
                location = headers.get("location", "")
                if location and "error" in parse_qs(urlparse(location).query):
                    headers["location"] = self.stamp(location)
            await send(message)

        await self.app(scope, receive, send_stamped)
