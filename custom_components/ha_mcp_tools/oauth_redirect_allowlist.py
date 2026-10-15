"""Callback allowlist for the none-mode auto-approve ``/authorize`` (#2427).

None mode's auto-approve endpoint is anonymous, so without a list it would
302 any visitor to any spec-valid URL: an open redirector on the Home
Assistant origin. Only callbacks an administrator listed are honoured.

Matching is exact, with one exception from RFC 8252 §7.3: a listed ``http``
loopback callback matches the same callback on any port, because native
clients pick a free port per sign-in. Nothing a client sends — dynamic client
registration included — adds to the list.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse, urlsplit

from .const import (
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    MAX_OAUTH_CALLBACK_LENGTH,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
)

_LOGGER = logging.getLogger(__name__)

# RFC 8252 §7.3: native/CLI OAuth clients (e.g. GitHub Copilot CLI) receive the
# authorization code on a loopback redirect, for which the spec explicitly
# permits a plain http scheme. Every non-loopback redirect must still be https.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def _is_loopback_host(hostname: str) -> bool:
    """True for the loopback hosts RFC 8252 §7.3/§8.3 allows over plain http."""
    if hostname in _LOOPBACK_HOSTNAMES:
        return True
    try:
        # Covers all of 127.0.0.0/8 and ::1, not just the literal 127.0.0.1.
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


# RFC 3986 §3.2 authority charset (unreserved / pct-encoded / sub-delims /
# ':' '@' and IPv6 brackets). Anything outside it (a backslash, a space, raw
# unicode) is an illegal authority that downstream URL builders reject.
_AUTHORITY_CHARS_RE = re.compile(r"[A-Za-z0-9._~%!$&'()*+,;=:@\[\]-]*")


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    """Spec-floor validation for OAuth redirect_uri: an https:// URL — or an
    http:// loopback URL (RFC 8252 §7.3, for native/CLI clients) — with a
    non-empty host, a valid port, and no fragment. A shape check only; whether
    the callback may receive a code is :func:`is_redirect_allowed`'s job."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
        # Accessing .port validates it: urlparse defers the range/format check
        # until access, so a crafted ':999999' or ':abc' port raises ValueError
        # HERE (→ clean 400) instead of later in yarl inside _redirect_with,
        # where it would escape as an uncaught 500 on an unauthenticated view.
        _ = parsed.port
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    if not _AUTHORITY_CHARS_RE.fullmatch(parsed.netloc):
        # Same contract as the .port access above: urlparse and yarl split
        # authorities differently (a backslash before '@', a zero-width
        # character in the host), so an RFC 3986-illegal authority must fail
        # HERE rather than escape as a 500 out of _redirect_with
        # (#2219 codex review).
        return False
    if parsed.scheme == "http":
        # Plain http only for loopback callbacks (native-client flow).
        if not _is_loopback_host(parsed.hostname):
            return False
    elif parsed.scheme != "https":
        return False
    # Fragments are not allowed in OAuth redirect URIs (RFC 6749 §3.1.2).
    return not parsed.fragment


def effective_allowlist(options: Mapping[str, Any]) -> list[str]:
    """The list in force for an entry's options (the default when unset)."""
    saved = options.get(OPT_OAUTH_REDIRECT_ALLOWLIST)
    if saved is None:
        return list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    if not isinstance(saved, list) or not all(isinstance(e, str) for e in saved):
        # Only validated writers store this option; anything else is corrupt,
        # and refusing every callback is safer than widening to the default.
        _LOGGER.warning("Ignoring a malformed OAuth callback allowlist: %r", saved)
        return (
            [e for e in saved if isinstance(e, str)] if isinstance(saved, list) else []
        )
    return list(saved)


def stored_allowlist(
    options: Mapping[str, Any], entries: list[str]
) -> list[str] | None:
    """The value to save for ``entries``, or None to keep following the default.

    An entry that has never saved a list keeps following the shipped default
    while it submits that default unchanged, so a later release's additions
    reach it. Once saved, a list (even an empty one) is kept as written.
    """
    if OPT_OAUTH_REDIRECT_ALLOWLIST not in options and entries == list(
        DEFAULT_OAUTH_REDIRECT_ALLOWLIST
    ):
        return None
    return entries


def normalize_allowlist(values: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Return ``(entries, invalid)``: trimmed, de-duplicated, in input order.

    An entry is invalid when no authorization request could use it: it fails
    the same spec floor as ``/authorize`` (:func:`_is_valid_redirect_uri`), or
    it is longer than ``MAX_OAUTH_CALLBACK_LENGTH``.
    """
    entries: list[str] = []
    invalid: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in entries or text in invalid:
            continue
        valid = len(text) <= MAX_OAUTH_CALLBACK_LENGTH and _is_valid_redirect_uri(text)
        (entries if valid else invalid).append(text)
    return entries, invalid


def _loopback_key(uri: str) -> tuple[str, str, str] | None:
    parts = urlsplit(uri)
    if (
        parts.scheme != "http"
        or "@" in parts.netloc
        or not _is_loopback_host(parts.hostname or "")
    ):
        return None
    return (parts.hostname or "", parts.path, parts.query)


def is_redirect_allowed(redirect_uri: str, allowlist: Iterable[str]) -> bool:
    """True when ``redirect_uri`` may receive a none-mode authorization code."""
    if not _is_valid_redirect_uri(redirect_uri):
        return False
    listed = list(allowlist)
    if redirect_uri in listed:
        return True
    key = _loopback_key(redirect_uri)
    return key is not None and any(_loopback_key(entry) == key for entry in listed)
