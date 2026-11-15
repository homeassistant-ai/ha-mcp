"""The dev proxy's dynamic client registration, over Core's real HTTP server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from ._webhook_proxy_oauth_core import (
    GOOGLE_REDIRECT_URIS,
    ProxyStarter,
    submodule,
)
from ._webhook_proxy_oauth_core import (
    component as component,
)
from ._webhook_proxy_oauth_core import (
    fs_root as fs_root,
)
from ._webhook_proxy_oauth_core import (
    proxy_components_root as proxy_components_root,
)
from ._webhook_proxy_oauth_core import (
    start_proxy_in_mode as start_proxy_in_mode,
)


async def test_registered_client_id_keeps_every_presented_redirect(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """A client registering two origins can use either one: its client_id is
    signed with the proxy's key and still names both after the round trip."""
    proxy = await start_proxy_in_mode("ha_auth")
    dcr = proxy.module("oauth_dcr")

    client_id = await proxy.register(GOOGLE_REDIRECT_URIS)

    assert client_id.startswith("hamcp-dcr-")
    assert dcr.client_redirect_uris(proxy.dcr_key, client_id) == GOOGLE_REDIRECT_URIS
    assert dcr.client_redirect_uris(b"x" * 32, client_id) is None


async def test_none_mode_registration_promises_no_refresh_grant(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """None mode has no refresh cycle, so a client must not be told it has one."""
    proxy = await start_proxy_in_mode("none")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": ["https://a.example/cb"]}
    )

    assert response.status == 201
    assert (await response.json())["grant_types"] == ["authorization_code"]


@pytest.mark.parametrize(
    "redirect_uris",
    [
        pytest.param(["https://a.example/cb"], id="single-origin"),
        pytest.param(GOOGLE_REDIRECT_URIS, id="multi-origin"),
        pytest.param(
            ["http://127.0.0.1/callback", "http://localhost/callback"],
            id="loopback-only",
        ),
        pytest.param(
            ["https://a.example/cb", "http://localhost/callback"], id="hybrid"
        ),
    ],
)
async def test_ha_auth_registration_is_refreshable_whatever_its_shape(
    start_proxy_in_mode: ProxyStarter, redirect_uris: list[str]
) -> None:
    """#2248: every ha_auth registration gets a refresh grant, including
    loopback and multi-origin clients, because the signed refresh envelope
    records the identity Core bound the grant to."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": redirect_uris}
    )

    body = await response.json()
    assert response.status == 201
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    registered = proxy.module("oauth_dcr").client_redirect_uris(
        proxy.dcr_key, body["client_id"]
    )
    assert registered == redirect_uris


async def test_registration_keeps_an_explicit_zero_port_distinct(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """Port zero is falsy: a registration naming ``:0`` next to the default
    port must still hand both redirect URIs back, not collapse them."""
    proxy = await start_proxy_in_mode("ha_auth")
    redirect_uris = ["https://a.example/cb", "https://a.example:0/cb"]

    client_id = await proxy.register(redirect_uris)

    assert (
        proxy.module("oauth_dcr").client_redirect_uris(proxy.dcr_key, client_id)
        == redirect_uris
    )


def test_origin_identity_keeps_an_explicit_zero_port_distinct(
    component: ModuleType,
) -> None:
    """``:0`` and the scheme default must never compare equal, or the authorize
    leg would translate a client's two origins to one."""
    dcr = submodule(component, "oauth_dcr")

    assert dcr.normalized_origin("https://a.example:0/cb") == ("https", "a.example", 0)
    assert dcr.normalized_origin("https://a.example/cb") == ("https", "a.example", 443)


async def test_registration_rejects_a_non_loopback_http_redirect(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """Only HTTPS and RFC 8252 loopback HTTP callbacks are accepted."""
    proxy = await start_proxy_in_mode("none")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": ["http://evil.example/cb"]}
    )

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_redirect_uri"


async def test_registration_answers_a_deeply_nested_body_with_400(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2218: an anonymous endpoint must call a body nested thousands deep
    malformed metadata, never a 500. Whether the parser overflows or returns a
    list depends on the interpreter's stack, so only the 400 is pinned."""
    proxy = await start_proxy_in_mode("ha_auth")
    payload = b"[" * 32000 + b"]" * 32000
    assert len(payload) < proxy.module("oauth_dcr").MAX_DCR_BODY_BYTES

    response = await proxy.client.post(proxy.url("register"), data=payload)

    body = await response.json()
    assert response.status == 400
    assert body["error"] == "invalid_client_metadata"


async def test_registration_answers_a_parser_stack_overflow_with_400(
    start_proxy_in_mode: ProxyStarter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overflow is forced through the module's own ``json`` name: the
    endpoint calls it malformed metadata, never a 500."""
    proxy = await start_proxy_in_mode("ha_auth")
    overflowing = SimpleNamespace(
        loads=Mock(side_effect=RecursionError), dumps=json.dumps
    )
    monkeypatch.setattr(proxy.module("oauth_dcr"), "json", overflowing)

    response = await proxy.client.post(proxy.url("register"), data=b"[[]]")

    body = await response.json()
    assert response.status == 400
    assert body["error_description"] == "body must be JSON"


async def test_registration_rejects_text_that_is_not_json(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """A body that does not parse is malformed metadata, not a server error."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.client.post(proxy.url("register"), data=b"redirect_uris=")

    body = await response.json()
    assert response.status == 400
    assert body["error_description"] == "body must be JSON"


async def test_registration_reassembles_a_chunked_body(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2219: a body arriving in small fragments must be read to EOF; one
    ``read()`` may return early and truncate the document."""
    proxy = await start_proxy_in_mode("ha_auth")
    body = json.dumps({"redirect_uris": ["https://a.example/cb"]}).encode()

    async def fragments() -> AsyncIterator[bytes]:
        for start in range(0, len(body), 7):
            yield body[start : start + 7]

    response = await proxy.client.post(proxy.url("register"), data=fragments())

    assert response.status == 201


async def test_registration_rejects_an_oversized_body(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2219: a conforming registration is a few KB, so the read is capped
    rather than riding Home Assistant's 16 MiB request limit."""
    proxy = await start_proxy_in_mode("ha_auth")
    oversized = b"x" * (proxy.module("oauth_dcr").MAX_DCR_BODY_BYTES + 1)

    response = await proxy.client.post(proxy.url("register"), data=oversized)

    body = await response.json()
    assert response.status == 400
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body is too large"
