"""The signed refresh envelope the dev proxy wraps around Core's refresh token (#2248)."""

from __future__ import annotations

from types import ModuleType

import pytest

from ._webhook_proxy_oauth_core import (
    KEY,
    b64url,
    hand_signed_envelope,
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

ROTATED_KEY = b"r" * 32


def _tampered(envelope: str) -> str:
    body, _, signature = envelope.rpartition(".")
    return f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"


def test_envelope_round_trips_and_is_disjoint_from_the_registration_blob(
    component: ModuleType,
) -> None:
    """#2248: relabelling one blob family as the other does not make it verify.

    Both are HMAC-signed under the DCR key, and the only thing separating them
    is that the envelope's MAC covers its prefix while the blob's covers the
    bare body. Swapping the prefixes is exactly the attack that difference
    exists to stop, so both directions are driven here.
    """
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    states = indirect.EnvelopeState
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, client_id) == (
        "core-refresh",
        "http://127.0.0.1:54321",
    )
    assert (
        indirect.unwrap_refresh_token(KEY, envelope, "other-client") is states.INVALID
    )
    assert indirect.unwrap_refresh_token(KEY, client_id, client_id) is states.ABSENT
    assert dcr.client_redirect_uris(KEY, envelope) is None
    # Relabelled: same signed bodies, the other family's prefix.
    relabelled_blob = f"hamcp-rt-{client_id.removeprefix('hamcp-dcr-')}"
    relabelled_envelope = f"hamcp-dcr-{envelope.removeprefix('hamcp-rt-')}"
    assert (
        indirect.unwrap_refresh_token(KEY, relabelled_blob, client_id) is states.INVALID
    )
    assert dcr.client_redirect_uris(KEY, relabelled_envelope) is None


def test_tampering_a_rotated_key_and_bad_payloads_are_invalid(
    component: ModuleType,
) -> None:
    """A bad MAC, a rotated signing key, or a wrong payload shape is INVALID.

    The signature only proves WE minted the blob, so the version guard behind
    it needs a valid MAC to be exercised at all, hence the hand-signed payload.
    """
    indirect = submodule(component, "oauth_indirect")
    states = indirect.EnvelopeState
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid"
    )

    assert indirect.unwrap_refresh_token(KEY, _tampered(envelope), "cid") is (
        states.INVALID
    )
    assert indirect.unwrap_refresh_token(b"x" * 32, envelope, "cid") is states.INVALID
    future_version = hand_signed_envelope(
        KEY, {"v": 2, "t": "core-refresh", "c": "https://a.example", "p": "x"}
    )
    assert indirect.unwrap_refresh_token(KEY, future_version, "cid") is states.INVALID


def test_a_missing_presenter_unwraps_any_intact_envelope(
    component: ModuleType,
) -> None:
    """A None presenter skips the client binding (RFC 7009 revocation).

    Revocation authorizes the BEARER of the token, not a client identity, so
    the revoke path recovers Core's token without knowing which client_id the
    envelope was minted alongside.
    """
    indirect = submodule(component, "oauth_indirect")
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid-a"
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, None) == (
        "core-refresh",
        "https://a.example",
    )


def test_revocation_reads_verified_and_unverifiable_envelopes(
    component: ModuleType,
) -> None:
    """#2249 review: revocation recovers Core's token even on a failed MAC.

    The rotated key is the case that matters: removing and re-adding the
    integration mints a new DCR key, invalidating every envelope already in a
    client's hands. Forwarding a body we did not verify is sound here and only
    here: RFC 7009 authorizes the bearer, and Core's revoke endpoint is
    anonymous and idempotent, so a forger gains nothing they could not get by
    POSTing to Core themselves.
    """
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    envelope = indirect.wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")
    rotated = indirect.wrap_refresh_token(
        ROTATED_KEY, "core-rt", "https://a.example", "cid"
    )

    assert indirect.core_token_for_revocation(KEY, envelope) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, _tampered(envelope)) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, rotated) == "core-rt"
    # No key configured means no envelope of ours to recognise at all.
    assert indirect.core_token_for_revocation(None, envelope) is None
    # And the registration blob family stays disjoint from the envelope family.
    blob = dcr.mint_client_id(KEY, ["https://a.example/cb"])
    assert indirect.core_token_for_revocation(KEY, blob) is None


@pytest.mark.parametrize(
    "token",
    [
        # Not ours at all: no prefix, so nothing is parsed.
        pytest.param("core-opaque-refresh-token", id="pre-envelope-token"),
        pytest.param("", id="empty"),
        # Our prefix, nothing readable behind it.
        pytest.param("hamcp-rt-", id="no-body"),
        pytest.param("hamcp-rt-nodot", id="no-separator"),
        pytest.param("hamcp-rt-###.not-a-real-signature", id="undecodable-base64"),
    ],
)
def test_revocation_finds_nothing_to_substitute_in_unusable_values(
    component: ModuleType, token: str
) -> None:
    """Nothing to substitute, so the caller forwards the value as presented."""
    indirect = submodule(component, "oauth_indirect")

    assert indirect.core_token_for_revocation(KEY, token) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'["not","an","object"]', id="json-array"),
        pytest.param(b'{"v":1,"t":42}', id="int-t"),
        pytest.param(b'{"v":1,"c":"https://a.example"}', id="missing-t"),
        pytest.param(b"[" * 1500 + b"]" * 1500, id="deeply-nested"),
    ],
)
def test_revocation_refuses_unusable_bodies(
    component: ModuleType, payload: bytes
) -> None:
    """A body that yields no string token is refused, never guessed at.

    This parse runs BEFORE any MAC check, so the JSON parser sees caller-chosen
    nesting (#2218).
    """
    indirect = submodule(component, "oauth_indirect")
    token = f"hamcp-rt-{b64url(payload)}.not-a-real-signature"

    assert len(token) <= indirect.MAX_REVOKE_ENVELOPE_LEN  # not the cap at work
    assert indirect.core_token_for_revocation(KEY, token) is None


def test_revocation_caps_the_unverified_parse(component: ModuleType) -> None:
    """An oversized prefixed value is refused BEFORE it is decoded.

    Core's refresh tokens are short, so nothing legitimate approaches the cap;
    it exists because this parse is the one place an anonymous view hands
    attacker-chosen bytes to base64 and the JSON parser. It guards only that
    path: an envelope the proxy can still VERIFY is honoured at any size.
    """
    indirect = submodule(component, "oauth_indirect")
    huge = "T" * indirect.MAX_REVOKE_ENVELOPE_LEN
    rotated = indirect.wrap_refresh_token(ROTATED_KEY, huge, "https://a.example", "cid")
    ours = indirect.wrap_refresh_token(KEY, huge, "https://a.example", "cid")

    assert len(rotated) > indirect.MAX_REVOKE_ENVELOPE_LEN
    assert indirect.core_token_for_revocation(KEY, rotated) is None
    assert indirect.core_token_for_revocation(KEY, ours) == huge


@pytest.mark.parametrize(
    "body",
    [
        b'{"access_token":"x","token_type":"Bearer"}',  # Core's refresh response
        b'{"error":"invalid_grant"}',
        b'{"refresh_token":null}',
        b'{"refresh_token":42}',
        b'["not","an","object"]',
        b"not json at all",
        b"",
        b'{"refresh_token":"\xff\xfe"}',  # not UTF-8
    ],
)
def test_bodies_without_a_string_refresh_token_are_relayed_byte_identical(
    component: ModuleType, body: bytes
) -> None:
    """Relay anything with no string refresh_token to wrap, byte for byte."""
    indirect = submodule(component, "oauth_indirect")

    rewritten = indirect.rewrite_token_response_body(
        KEY, body, "https://a.example", "cid"
    )

    assert rewritten is body
