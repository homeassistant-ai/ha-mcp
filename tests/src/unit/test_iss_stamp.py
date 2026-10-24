"""The RFC 9207 ``iss`` stamp must reach every redirect status the SDK emits."""

from urllib.parse import parse_qs, urlparse

import pytest
from starlette.responses import RedirectResponse, Response

from ha_mcp.auth.iss_stamp import StampIssOnErrorRedirects


async def _response_start(app) -> dict:
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {"type": "http", "method": "GET", "path": "/authorize", "headers": []},
        receive,
        send,
    )
    return next(m for m in messages if m["type"] == "http.response.start")


def _stamp(url: str) -> str:
    return url + "&iss=https://as.example/"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 303, 307])
async def test_error_redirect_is_stamped_on_every_redirect_status(status):
    """A client reading an error redirect of any status must see ``iss``."""
    app = RedirectResponse(
        "https://client.example/cb?error=invalid_scope&state=s", status_code=status
    )

    start = await _response_start(StampIssOnErrorRedirects(app, _stamp))

    params = parse_qs(urlparse(dict(start["headers"])[b"location"].decode()).query)
    assert params["iss"] == ["https://as.example/"]
    assert params["error"] == ["invalid_scope"]


@pytest.mark.asyncio
async def test_redirect_without_location_is_left_alone():
    """A redirect status with no Location header must pass through untouched."""
    start = await _response_start(
        StampIssOnErrorRedirects(Response(status_code=303), _stamp)
    )

    assert b"location" not in dict(start["headers"])
