"""Pin the aiohttp routing property the two PRM views rely on to coexist.

The Webhook Proxy app binds its RFC 9728 path-scoped document at an EXACT
path (``/.well-known/oauth-protected-resource/api/webhook/<its id>``) while
the in-process component binds a ``{webhook_id}`` route parameter under the
same prefix and 404s any id that is not its own. Both can be installed on one
Home Assistant, so the app's document must win for the app's id no matter
which integration registered first. aiohttp's ``UrlDispatcher`` guarantees
that: ``resolve`` walks the request path from the most specific segment
upwards, and an exact-path resource is indexed under the full path while a
dynamic one is indexed under its static prefix, so registration order only
breaks ties between resources that share an index key. This test exercises the
real router in both registration orders so a future aiohttp change surfaces
here rather than as a dead discovery URL on a both-installed instance.
"""

from __future__ import annotations

import pytest

aiohttp_web = pytest.importorskip("aiohttp.web")
from aiohttp.test_utils import make_mocked_request  # noqa: E402

PREFIX = "/.well-known/oauth-protected-resource/api/webhook"
APP_ID = "mcp_app_id_aaaa"
COMPONENT_ID = "mcp_component_id_bbbb"


async def _exact(request):  # the app's view: bound at its own id
    return aiohttp_web.Response(text="app")


async def _dynamic(request):  # the component's view: route parameter
    return aiohttp_web.Response(text=f"component:{request.match_info['webhook_id']}")


@pytest.mark.parametrize("dynamic_first", [False, True])
async def test_exact_path_wins_regardless_of_registration_order(dynamic_first):
    router = aiohttp_web.UrlDispatcher()
    if dynamic_first:
        router.add_get(f"{PREFIX}/{{webhook_id}}", _dynamic)
        router.add_get(f"{PREFIX}/{APP_ID}", _exact)
    else:
        router.add_get(f"{PREFIX}/{APP_ID}", _exact)
        router.add_get(f"{PREFIX}/{{webhook_id}}", _dynamic)

    match = await router.resolve(make_mocked_request("GET", f"{PREFIX}/{APP_ID}"))
    assert match.handler is _exact

    match = await router.resolve(make_mocked_request("GET", f"{PREFIX}/{COMPONENT_ID}"))
    assert match.handler is _dynamic
    assert match.get("webhook_id") == COMPONENT_ID
