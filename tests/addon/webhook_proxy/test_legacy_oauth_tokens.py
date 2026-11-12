"""The legacy provider's signed tokens: a forged, foreign or expired one is refused.

These build an ``OAuthProvider`` on the real ``hass`` with a known signing key,
so a test can craft the token a forger or a stale client would present.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from ._legacy_oauth import CLIENT_ID, CLIENT_SECRET, WEBHOOK_ID, b64url, token_claims

SIGNING_KEY = b"\x00" * 32


def _provider(
    component: ModuleType,
    hass: HomeAssistant,
    *,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    signing_key: bytes = SIGNING_KEY,
) -> Any:
    return component.oauth.OAuthProvider(
        hass=hass,
        client_id=client_id,
        client_secret=client_secret,
        webhook_id=WEBHOOK_ID,
        signing_key=signing_key,
    )


def _flip_first_char(segment: str) -> str:
    # The first base64url character carries 6 full data bits, so changing it
    # always changes the decoded bytes.
    return ("A" if segment[0] != "A" else "B") + segment[1:]


@pytest.mark.parametrize("segment", [0, 1], ids=["payload", "signature"])
def test_tampered_token_is_rejected(
    hass: HomeAssistant, component: ModuleType, segment: int
) -> None:
    """Editing either half of a token must invalidate it, or a client could
    extend its own expiry or forge a signature."""
    provider = _provider(component, hass)
    parts = provider.issue_access_token().rsplit(".", 1)
    parts[segment] = _flip_first_char(parts[segment])

    assert provider.validate_access_token(".".join(parts)) is False


def test_rotating_the_client_id_revokes_outstanding_tokens(
    hass: HomeAssistant, component: ModuleType
) -> None:
    """An admin who regenerates the client id expects every token issued to
    the old client to stop working, even under the same signing key."""
    token = _provider(component, hass).issue_access_token()
    rotated = _provider(component, hass, client_id="id-bbbbbbbbbbbbbbbbb")

    assert rotated.validate_access_token(token) is False


def test_validly_signed_non_object_payload_is_rejected(
    hass: HomeAssistant, component: ModuleType
) -> None:
    """A correctly signed body that is not a JSON object must be refused, not
    crash the claim lookup behind the webhook's bearer gate."""
    body = b64url(json.dumps([1, 2, 3]).encode())
    signature = hmac.new(SIGNING_KEY, body.encode("ascii"), hashlib.sha256).digest()

    provider = _provider(component, hass)

    assert provider.validate_access_token(f"{body}.{b64url(signature)}") is False


@pytest.mark.parametrize(
    ("offset", "valid"), [(-1, True), (0, False)], ids=["before-exp", "at-exp"]
)
def test_access_token_is_valid_until_its_exp_claim(
    hass: HomeAssistant,
    component: ModuleType,
    freezer: FrozenDateTimeFactory,
    offset: int,
    valid: bool,
) -> None:
    """A token is valid strictly before ``exp`` (RFC 7519 §4.1.4): accepting it
    at ``exp`` would honour an expired token, refusing it a second early would
    cut every token's life short."""
    provider = _provider(component, hass)
    token = provider.issue_access_token()

    freezer.move_to(datetime.fromtimestamp(token_claims(token)["exp"] + offset, UTC))

    assert provider.validate_access_token(token) is valid


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"client_id": ""}, "client_id"),
        ({"client_id": "too-short"}, "client_id"),
        ({"client_secret": ""}, "client_secret"),
        ({"signing_key": b"too-short"}, "signing_key"),
    ],
    ids=["blank-client-id", "short-client-id", "blank-secret", "short-key"],
)
def test_provider_refuses_credentials_that_would_weaken_auth(
    hass: HomeAssistant,
    component: ModuleType,
    overrides: dict[str, Any],
    field: str,
) -> None:
    """A future caller that skips start.py's checks must fail loudly instead of
    serving OAuth with a guessable client id, no secret or a weak key."""
    with pytest.raises(ValueError, match=field):
        _provider(component, hass, **overrides)
