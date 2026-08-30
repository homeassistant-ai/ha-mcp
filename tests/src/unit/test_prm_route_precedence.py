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

import importlib
import sys

import pytest

PREFIX = "/.well-known/oauth-protected-resource/api/webhook"
APP_ID = "mcp_app_id_aaaa"
COMPONENT_ID = "mcp_component_id_bbbb"


def _module_is(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


@pytest.fixture
def real_aiohttp():
    """Import the REAL aiohttp for this test, then restore whatever was there.

    Other unit modules install a stub ``aiohttp`` in ``sys.modules`` (see
    ``_embedded_stubs``) at import time, and under xdist this file may collect
    after one of them in the same worker — so the real package has to be loaded
    behind the stub's back and put back afterwards, exactly as
    ``test_oauth_autoapprove.unified_view_client_factory`` does.
    """
    package_roots = ("aiohttp", "yarl")
    saved = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        yield web, test_utils
    finally:
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.mark.parametrize("dynamic_first", [False, True])
async def test_exact_path_wins_regardless_of_registration_order(
    real_aiohttp, dynamic_first
):
    web, test_utils = real_aiohttp

    async def exact(request):  # the app's view: bound at its own id
        return web.Response(text="app")

    async def dynamic(request):  # the component's view: route parameter
        return web.Response(text=f"component:{request.match_info['webhook_id']}")

    router = web.UrlDispatcher()
    if dynamic_first:
        router.add_get(f"{PREFIX}/{{webhook_id}}", dynamic)
        router.add_get(f"{PREFIX}/{APP_ID}", exact)
    else:
        router.add_get(f"{PREFIX}/{APP_ID}", exact)
        router.add_get(f"{PREFIX}/{{webhook_id}}", dynamic)

    match = await router.resolve(
        test_utils.make_mocked_request("GET", f"{PREFIX}/{APP_ID}")
    )
    assert match.handler is exact

    match = await router.resolve(
        test_utils.make_mocked_request("GET", f"{PREFIX}/{COMPONENT_ID}")
    )
    assert match.handler is dynamic
    assert match.get("webhook_id") == COMPONENT_ID
