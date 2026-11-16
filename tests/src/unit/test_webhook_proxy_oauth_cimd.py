"""Client metadata documents and the client_id identity the proxy presents to Core.

The authorize and token legs must forward the same client_id for one client,
so each case here runs both legs against a document served by ``cimd_web``.
"""

from __future__ import annotations

import socket
from types import ModuleType

import pytest

from ._webhook_proxy_oauth_core import (
    GOOGLE_REDIRECT_URIS,
    KEY,
    PUBLIC_ADDRESS,
    CimdWeb,
    submodule,
)
from ._webhook_proxy_oauth_core import (
    cimd_web as cimd_web,
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

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def test_redirect_matching_includes_loopback_port_variance(
    component: ModuleType,
) -> None:
    """Non-loopback matches are exact; loopback runtime ports may vary."""
    indirect = submodule(component, "oauth_indirect")

    assert indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/cb"
    )
    assert not indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/other"
    )
    assert indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/callback"
    )
    assert not indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/other"
    )


async def test_unreachable_client_is_negative_cached(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """Repeated requests for a dead identity do not repeat name resolution."""
    indirect = submodule(component, "oauth_indirect")
    cimd_web.dns.side_effect = socket.gaierror
    client_id = "https://dead.example/client.json"

    first = await indirect.fetch_cimd_redirects(cimd_web.session, client_id)
    second = await indirect.fetch_cimd_redirects(cimd_web.session, client_id)

    assert first is None and second is None
    cimd_web.dns.assert_awaited_once()


async def test_valid_document_is_fetched_once_then_served_from_cache(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """The positive twin of the negative cache: a good document is cached."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://good.example/client.json"
    cimd_web.serve(client_id, ["https://good.example/cb"])

    first = await indirect.fetch_cimd_redirects(cimd_web.session, client_id)
    second = await indirect.fetch_cimd_redirects(cimd_web.session, client_id)

    assert first == second == ["https://good.example/cb"]
    assert cimd_web.fetches == 1


async def test_invalid_document_is_not_negative_cached(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """Draft -00 section 4.4.3: an INVALID document is deliberately not
    cached, so a client that fixes its metadata recovers on the very next
    request (#2218 review by Patch76). The bodies go through the real fetch
    and parse path rather than a stubbed fetch."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://client.example/client.json"
    # No client_name, so the document is rejected.
    cimd_web.mocker.get(
        f"https://{PUBLIC_ADDRESS}/client.json",
        json={"client_id": client_id, "redirect_uris": ["https://client.example/cb"]},
    )

    assert await indirect.fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert cimd_web.fetches == 1

    cimd_web.serve(client_id, ["https://client.example/cb"])
    assert await indirect.fetch_cimd_redirects(cimd_web.session, client_id) == [
        "https://client.example/cb"
    ]
    assert cimd_web.fetches == 1


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (GOOGLE_REDIRECT_URIS[0], "https://oauth-redirect.googleusercontent.com"),
        (
            GOOGLE_REDIRECT_URIS[1],
            "https://oauth-redirect-sandbox.googleusercontent.com",
        ),
    ],
)
async def test_multi_origin_registration_translates_to_the_presented_origin(
    component: ModuleType, redirect_uri: str, expected: str
) -> None:
    """Each authorization leg translates to its matched presented origin."""
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    translated = await indirect.resolve_forward_client_id(
        session=None, dcr_key=KEY, client_id=client_id, redirect_uri=redirect_uri
    )

    assert translated == expected


async def test_explicit_default_port_translates_on_both_legs(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """#2218 review: a client_id with an explicit scheme-default port misses
    the raw-netloc fast path, so authorize mints the token under the
    TRANSLATED origin, and the redirect-less refresh must re-derive that same
    origin rather than pass the raw client_id through."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://claude.ai:443/oauth/client.json"
    cimd_web.serve(client_id, [CLAUDE_CALLBACK])

    authorize_id = await indirect.resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=CLAUDE_CALLBACK,
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert authorize_id == "https://claude.ai"
    assert refresh_id == "https://claude.ai"


async def test_same_case_uppercase_pair_passes_through_on_both_legs(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """#2219 review: an all-uppercase pair is equal under Core's raw netloc
    rule, so authorize passes it through, and refresh matches the client_id
    against the REGISTERED redirects raw, so it passes through too."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://CLAUDE.AI/oauth/client.json"
    callback = "https://CLAUDE.AI/api/mcp/auth_callback"
    cimd_web.serve(client_id, [callback])

    authorize_id = await indirect.resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=callback,
    )
    # The fast path decided authorize: no lookup.
    assert cimd_web.fetches == 0
    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH
    # Refresh DID fetch and matched the registered redirect: the passthrough is
    # the comparison's verdict, not a skipped lookup.
    assert cimd_web.fetches == 1


@pytest.mark.parametrize(
    ("presented_case", "registered_case"),
    [("upper", "lower"), ("lower", "lower"), ("lower", "upper")],
)
async def test_case_only_differences_pass_through_on_both_legs(
    component: ModuleType,
    cimd_web: CimdWeb,
    presented_case: str,
    registered_case: str,
) -> None:
    """#2219 review round 3: Core's authorize leg lowercases BOTH sides while
    its refresh leg compares byte-exact, so a case-only difference is
    same-origin to Core and the raw client_id is what both legs must forward:
    the document's casing is author-written and the presented redirect is
    client-runtime, so they need not agree."""
    indirect = submodule(component, "oauth_indirect")
    upper = "https://CLAUDE.AI/api/mcp/auth_callback"
    lower = "https://claude.ai/api/mcp/auth_callback"
    client_id = "https://CLAUDE.AI/oauth/client.json"
    cimd_web.serve(client_id, [upper if registered_case == "upper" else lower])

    authorize_id = await indirect.resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=upper if presented_case == "upper" else lower,
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


async def test_mixed_port_shapes_are_unreproducible(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """#2219 review round 3: port normalization makes these ONE canonical
    origin, but authorize passes through for the port-less redirect and
    translates the explicit-port one. Which was presented is unrecorded, so
    the honest answer is a local invalid_grant."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://h.example/client.json"
    cimd_web.serve(client_id, ["https://h.example/a", "https://h.example:443/b"])

    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert refresh_id is indirect.RefreshDisposition.UNREPRODUCIBLE


async def test_symmetric_explicit_port_passes_through_on_both_legs(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """#2219 review: when the client_id AND the registered redirect carry the
    same explicit default port, the authorize fast path fires (raw netlocs
    equal) and the token is bound to the full client_id, so refresh must pass
    through, not translate to the canonicalized origin."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://claude.ai:443/oauth/client.json"
    callback = "https://claude.ai:443/api/mcp/auth_callback"
    cimd_web.serve(client_id, [callback])

    authorize_id = await indirect.resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=callback,
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


async def test_portless_same_origin_client_passes_through_on_both_legs(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """claude.ai's real shape: raw netlocs match, so authorize forwards the
    full client_id untranslated and refresh passes through to match."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://claude.ai/oauth/client.json"
    cimd_web.serve(client_id, [CLAUDE_CALLBACK])

    authorize_id = await indirect.resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=CLAUDE_CALLBACK,
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


async def test_malformed_port_client_id_passes_through_for_core_to_reject(
    component: ModuleType,
) -> None:
    """#2218 review: a client_id with an invalid port must pass through for
    Core to reject. The fast path compares raw netlocs because reading the
    parsed port raises ValueError on an anonymous view."""
    indirect = submodule(component, "oauth_indirect")

    resolved = await indirect.resolve_forward_client_id(
        None,
        None,
        "https://client.example:99999/metadata.json",
        "https://client.example/cb",
    )

    assert resolved == "https://client.example:99999/metadata.json"


async def test_refresh_identity_is_derived_passed_through_or_refused(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """Refresh derives an origin, passes through, or rejects unreproducible IDs."""
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    reproducible = dcr.mint_client_id(KEY, ["https://a.example/cb"])
    multi_origin = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, reproducible)
        == "https://a.example"
    )
    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, multi_origin)
        is indirect.RefreshDisposition.UNREPRODUCIBLE
    )

    unknown = "https://unknown.example/client.json"
    cimd_web.serve(unknown, None, status=404)
    assert (
        await indirect.translated_client_id_for_refresh(cimd_web.session, None, unknown)
        is indirect.RefreshDisposition.PASSTHROUGH
    )


async def test_multi_origin_document_refreshes_as_unreproducible(
    component: ModuleType, cimd_web: CimdWeb
) -> None:
    """Verified CIMD multi-origin identities use UNREPRODUCIBLE too."""
    indirect = submodule(component, "oauth_indirect")
    client_id = "https://spark.example/client.json"
    cimd_web.serve(client_id, GOOGLE_REDIRECT_URIS)

    translated = await indirect.translated_client_id_for_refresh(
        cimd_web.session, None, client_id
    )

    assert translated is indirect.RefreshDisposition.UNREPRODUCIBLE
