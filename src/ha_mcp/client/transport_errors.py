"""Recognise HTTP transport outcomes that mean Home Assistant was not reached."""

import ssl

import httpx


def is_ssl_error(exc: BaseException) -> bool:
    """True if ``exc`` (or anything in its cause chain) is an SSL error.

    httpx wraps ``ssl.SSLError`` inside ``httpx.ConnectError``; the only
    reliable check is to walk ``__cause__`` / ``__context__``.
    """
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def redirect_message(base_url: str, response: httpx.Response) -> str:
    """Explain a 3xx reply: redirects are not followed, so Home Assistant was
    not reached (a write must not be replayed, nor the token sent elsewhere)."""
    location = response.headers.get("location") or "(no location)"
    return (
        f"Home Assistant was not reached at {base_url}: HTTP "
        f"{response.status_code} redirect to {location}. Set HOMEASSISTANT_URL "
        "to the address Home Assistant answers on."
    )
