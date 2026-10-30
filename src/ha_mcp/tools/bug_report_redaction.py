"""Secret and identifier scrubbing for ha_report_issue output.

Dependency-free on purpose: only the standard library, so the rules can be
exercised in isolation.
"""

import ipaddress
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class KnownIdentifiers:
    """This instance's own hostnames and addresses, read from Home Assistant."""

    hosts: frozenset[str] = frozenset()
    addresses: frozenset[str] = frozenset()


_NO_IDENTIFIERS = KnownIdentifiers()

# A ContextVar rather than a parameter: the scrubber is called from a dozen
# synchronous sites while one report is assembled.
_known_identifiers: ContextVar[KnownIdentifiers] = ContextVar(
    "bug_report_known_identifiers", default=_NO_IDENTIFIERS
)


@contextmanager
def known_identifiers_scope(identifiers: KnownIdentifiers) -> Iterator[None]:
    """Make ``_sanitize_log_text`` redact ``identifiers`` within the block."""
    token = _known_identifiers.set(identifiers)
    try:
        yield
    finally:
        _known_identifiers.reset(token)


# Two or more colon-separated hex groups, optionally ending in a dotted quad
# (``::ffff:192.0.2.1``); ``hh:mm:ss`` and other non-addresses fail to parse in
# the callback and are left alone. A preceding label colon (``address:``) is
# allowed: the leftmost match always consumes a whole address, so a candidate
# never starts inside a longer one.
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![\w.])(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{0,4})(?![\w:]|\.\d)"
)


def _parse_v6(value: str) -> ipaddress.IPv6Address | None:
    try:
        return ipaddress.IPv6Address(value)
    except ValueError:
        return None


def _alternation(values: frozenset[str]) -> str:
    # Longest first, so an alternative never stops short inside a longer one.
    return "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))


@lru_cache(maxsize=8)
def _known_identifier_rules(
    identifiers: KnownIdentifiers,
) -> tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]:
    """Whole-token patterns: a known value never matches inside a longer
    address or hostname, but does match before ``:port``, ``/``, a quote,
    a bracket, a comma or a sentence-ending dot."""
    rules: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = []
    ipv4 = frozenset(a for a in identifiers.addresses if ":" not in a)
    ipv6 = identifiers.addresses - ipv4
    if ipv4:
        rules.append(
            (
                re.compile(rf"(?<![\w.-])(?:{_alternation(ipv4)})(?![\w-]|\.[\w-])"),
                "[IP]",
            )
        )
    if ipv6:
        # An IPv6 address has many spellings (``::`` vs zero groups, leading
        # zeros), so candidates are parsed and compared, not matched as text.
        known_v6 = frozenset(_parse_v6(a) for a in ipv6) - {None}

        def _redact_v6(match: re.Match[str]) -> str:
            try:
                parsed = ipaddress.IPv6Address(match.group(0))
            except ValueError:
                return match.group(0)
            return "[IP]" if parsed in known_v6 else match.group(0)

        if known_v6:
            rules.append((_IPV6_CANDIDATE_RE, _redact_v6))
    if identifiers.hosts:
        rules.append(
            (
                re.compile(
                    rf"(?<![\w.-])(?:{_alternation(identifiers.hosts)})(?![\w-]|\.[\w-])",
                    re.IGNORECASE,
                ),
                "[HA_HOST]",
            )
        )
    return tuple(rules)


# IPv4 sanitization: only redact addresses with strong network context so that
# four-segment version strings (e.g. "ha-mcp version 1.2.3.4") are preserved.
_IPV4_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

_IPV4 = rf"(?:{_IPV4_OCTET}\.){{3}}{_IPV4_OCTET}"

# IP followed by :port or /CIDR — always a network address, never a version.
_IPV4_WITH_PORT_OR_CIDR_RE = re.compile(rf"\b{_IPV4}(?::\d+|/\d{{1,2}})\b(?!\.\d)")

# IP preceded by a network keyword (from, to, host=, host='...', addr=, etc.).
_IPV4_AFTER_KEYWORD_RE = re.compile(
    rf"\b((?:from|to|host|hostname|addr|address|ip|src|dst|server|client|peer|via)\b\s*[=:]?\s*['\"]?){_IPV4}\b(?!\.\d)",
    re.IGNORECASE,
)

# IP appearing inside a URL (`scheme://1.2.3.4...`).
_IPV4_IN_URL_RE = re.compile(rf"(://){_IPV4}\b(?!\.\d)")

# Plex wildcard DNS puts the server address in ``1-2-3-4.<hex>.plex.direct``.
_PLEX_DIRECT_DASHED_IPV4_RE = re.compile(
    rf"\b(?:{_IPV4_OCTET}-){{3}}{_IPV4_OCTET}(?=\.[^\s.]+\.plex\.direct\b)",
    re.IGNORECASE,
)
# Internal hostnames. A match starts only where a name starts, so a long dotted
# run stays linear instead of being rescanned from every label.
_LAN_HOST_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+lan(?![\w-]|\.[\w-])", re.IGNORECASE
)
_TAILNET_HOST_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+ts\.net(?![\w-]|\.[\w-])", re.IGNORECASE
)
# Microsoft Graph calendar ID in a URL path.
_GRAPH_CALENDAR_ID_RE = re.compile(r"(/me/calendars/)[^/?#\s'\"]+", re.IGNORECASE)


# URL userinfo, anchored on "://" rather than on the scheme: a match that began
# at the scheme made every character of a long unbroken word a candidate start,
# quadratic in its length (about a minute for a 200 kB base64 blob).
_URL_USERINFO_RE = re.compile(r"(://)([^:/?#\s@]+):([^@/\s]+)@")


def _sanitize_log_text(text: str) -> str:
    """Best-effort secret scrubber for log text.

    Defense-in-depth, not exhaustive — bug reports still pass through human
    review (see ``_generate_anonymization_guide``). Rules cover the most common
    leak shapes seen in HA add-on logs:
    JWTs, bearer tokens, long hex tokens, ``key=value`` style credentials,
    URL userinfo, IPv4 addresses with network context (including quoted and
    ``*.plex.direct`` dashed forms), ``.lan`` and tailnet ``.ts.net`` hostnames,
    Microsoft Graph calendar IDs, and the MCP connect-URL secret path (the
    add-on's LAN auth). Inside known_identifiers_scope this instance's own
    hostnames and addresses are redacted first, wherever they appear.
    """
    for pattern, replacement in _known_identifier_rules(_known_identifiers.get()):
        text = pattern.sub(replacement, text)
    # JWT tokens (header.payload.signature)
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[REDACTED_JWT]",
        text,
    )
    # Bearer tokens — match any casing (BEARER, Bearer, bearer, BeArEr, …)
    # via re.IGNORECASE, but preserve the original casing in the output by
    # echoing m.group(1) back through the lambda.
    text = re.sub(
        r"\b(bearer)\s+\S+",
        lambda m: f"{m.group(1)} [REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Authorization values in any other scheme (Basic, Digest, ...). The
    # Bearer rule above has already handled Bearer, so it is skipped here.
    text = re.sub(
        r"\b(authorization['\"]?\s*[:=]\s*['\"]?)(?!bearer\b)(?:[A-Za-z]+\s+)?[^\s'\",}]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Generic key=value credentials (api_key, token, secret, password, etc.).
    # Negative lookbehind for a letter so OPENAI_API_KEY=... still matches
    # (underscore is a word-char, so \b doesn't fire there).
    # "authorization" has its own rule above.
    # A quoted key and value, as in JSON or a Python dict repr, are covered
    # too: {"token": "a b"} and {'password': 'x'}.
    text = re.sub(
        r"(?<![A-Za-z])(api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd)\b"
        r"(['\"]?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1\2[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # URL userinfo: scheme://user:password@host -> scheme://user:[REDACTED]@host
    text = _URL_USERINFO_RE.sub(r"\1\2:[REDACTED]@", text)
    # Long hex strings (API keys, tokens) - 32+ contiguous hex chars
    text = re.sub(
        r"(?<![a-fA-F0-9])[a-fA-F0-9]{32,}(?![a-fA-F0-9])",
        "[REDACTED_HEX]",
        text,
    )
    # IPv4 addresses — only when there's strong network context, so that
    # four-segment version strings (e.g. "version 1.2.3.4") survive intact.
    text = _IPV4_WITH_PORT_OR_CIDR_RE.sub("[IP]", text)
    text = _IPV4_IN_URL_RE.sub(r"\1[IP]", text)
    text = _IPV4_AFTER_KEYWORD_RE.sub(r"\1[IP]", text)
    text = _PLEX_DIRECT_DASHED_IPV4_RE.sub("[IP]", text)
    text = _TAILNET_HOST_RE.sub("[TAILNET_HOST]", text)
    text = _LAN_HOST_RE.sub("[HOST]", text)
    text = _GRAPH_CALENDAR_ID_RE.sub(r"\1[CALENDAR_ID]", text)
    # MCP connect-URL secret path — the add-on's LAN auth. Redact by the
    # configured value (catches custom paths) and the generated
    # ``/private_<token>`` convention. Only bug-report output is scrubbed here;
    # the raw logs this text is copied from are left intact.
    # The dedicated settings-UI secret path (OAuth/OIDC) is a second secret whose
    # leak grants the same unauthenticated settings access, so redact a custom
    # value the same way. The auto-generated /private_<token> form is caught by
    # the generic pattern below.
    for env_name in ("MCP_SECRET_PATH", "MCP_SETTINGS_SECRET_PATH"):
        # Strip before rstrip: the resolver strips MCP_SETTINGS_SECRET_PATH before
        # mounting, so a whitespace-bearing value mounts at the stripped path — the
        # redaction pattern must match that, not the raw value (GHSA-mx64-982r-65vg).
        configured = os.getenv(env_name, "").strip().rstrip("/")
        if configured and configured != "/mcp":
            # Anchor on a path-segment boundary so a short/substring-prone
            # configured value (e.g. "/ha") cannot corrupt unrelated text
            # (e.g. "/happy").
            text = re.sub(
                re.escape(configured) + r"(?![A-Za-z0-9_-])",
                "[REDACTED_SECRET_PATH]",
                text,
            )
    text = re.sub(r"/private_[A-Za-z0-9_-]+", "[REDACTED_SECRET_PATH]", text)
    return text
