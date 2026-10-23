"""Config-entry diagnostics: the "Download diagnostics" file (#2584).

A report from a broken install needs the facts ``ha_report_issue`` collects,
but that tool runs only inside a working MCP session. This download works
without one, from the integration page.

Home Assistant's diagnostics component already wraps the result with the Core
version and installation type, every custom integration's version, this
component's manifest and the domain's repair issues (ids only for
non-persistent ones), so none of that is repeated here.

The output is built from an explicit set of fields and never dumps
``entry.data`` / ``entry.options``: those hold the provisioned access token,
OAuth credentials, signing keys and the secret webhook id and path, and a
secret key added later must not leak by default. None of those values is
emitted; of the options that can embed credentials, the file reports two
(package override, external URL), and only as "is it set" booleans. Both entry
types return the same component-wide summary, so the tools entry's download
still shows a server entry whose setup raised (HA offers the download only for
a loaded entry).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir
from packaging.version import Version

from .const import (
    CONF_ENTRY_TYPE,
    DATA_ACCESS_TOKEN,
    DATA_DCR_SIGNING_KEY,
    DATA_LAST_PIP_SPEC,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DATA_WEBHOOK_ID,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_PIP_SPEC,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_NONE,
)
from .embedded_entry import access_enabled, bring_up_in_progress, server_running
from .spec_dist import version_dist
from .websocket_api.registry import _enum_value

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_REDACTED = "**REDACTED**"

# Keys whose values must never appear in the file (a plain server pin
# excepted, see _is_server_pin). The startup error detail is free text built
# from an exception message (a pip failure echoes the package spec, a
# connection failure the URL), so these values are also scrubbed from that
# text.
_SECRET_DATA_KEYS = (
    DATA_WEBHOOK_ID,
    DATA_SECRET_PATH,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_DCR_SIGNING_KEY,
    DATA_SERVER_USER_ID,
    DATA_REFRESH_TOKEN_ID,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
)
_SECRET_OPTION_KEYS = (
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_PIP_SPEC,
    OPT_SERVER_URL,
    OPT_EXTERNAL_URL,
    OPT_WEBHOOK_ID_OVERRIDE,
    OPT_SECRET_PATH_OVERRIDE,
)
_STARTUP_ISSUE_IDS = (ISSUE_PACKAGE_FAILED, ISSUE_START_FAILED)
# Credentials in a URL's userinfo, e.g. a pip index or tarball URL that never
# reached the entry (pip echoes its index URLs in errors). Up to the last "@"
# before the path, so a raw "@" in a password is covered. It does not stop at
# "?" or "#": urlsplit would end the userinfo there, but stopping would leave
# the rest of a password holding one, so an "@" in a query costs the host.
_URL_USERINFO = re.compile(r"(://)[^/\s'\"]+@")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the component-wide summary for either entry type."""
    entries = hass.config_entries.async_entries(DOMAIN)
    server_entry = next(
        (e for e in entries if _entry_type(e) == ENTRY_TYPE_SERVER), None
    )
    return {
        "entries": [
            {
                "entry_type": _entry_type(e),
                "state": _enum_value(e.state),
                "disabled_by": _enum_value(e.disabled_by),
            }
            for e in entries
        ],
        "server": (
            await _async_server_summary(hass, server_entry)
            if server_entry is not None
            else None
        ),
    }


def _entry_type(entry: ConfigEntry) -> str:
    """Entry-type marker; a missing value means the tools entry (const.py)."""
    return str(entry.data.get(CONF_ENTRY_TYPE) or ENTRY_TYPE_TOOLS)


async def _async_server_summary(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Version, bring-up and webhook facts for the server entry.

    Option defaults are read exactly as bring-up reads them, so the file shows
    what the server was started with.
    """
    # Lazy import, the embedded chain's convention (embedded_entry.py): only a
    # server-entry install needs the embedded server module.
    from .embedded_server import (
        _async_paired_server_requirement,
        _installed_ha_mcp_version,
    )

    options = entry.options
    pip_spec = str(options.get(OPT_PIP_SPEC) or "").strip()
    # Same test as EmbeddedServerManager: the default spec is no override.
    override = pip_spec if pip_spec != DEFAULT_PIP_SPEC else ""
    # The effective spec, as EmbeddedServerManager._resolve_pip_spec resolves it.
    spec = override or await _async_paired_server_requirement(hass) or DEFAULT_PIP_SPEC
    return {
        # Blocking dist-info read; the spec's distribution first, as bring-up.
        "installed_server_version": await hass.async_add_executor_job(
            _installed_ha_mcp_version, version_dist(spec)
        ),
        "package_override_set": bool(override),
        "access_enabled": access_enabled(entry),
        "running": server_running(hass),
        "bring_up_in_progress": bring_up_in_progress(hass),
        "startup_error": _startup_error(hass, entry),
        "webhook_enabled": bool(options.get(OPT_ENABLE_WEBHOOK, True)),
        "webhook_auth": str(options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE)),
        "external_url_set": bool(str(options.get(OPT_EXTERNAL_URL) or "").strip()),
    }


def _startup_error(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str] | None:
    """The failed bring-up's repair issue and its scrubbed detail, if any.

    Bring-up tears the manager down on failure, so the repair issue's
    ``detail`` is the only copy of the error outside the log: in the issue's
    ``data`` for a package failure, whose fix flow reads it, and in its
    translation placeholders for a start failure. HA's own ``issues`` field
    omits both for non-persistent issues, so they are read here. Bring-up
    clears both issues at its start, so a hit belongs to the latest attempt.
    After a restart Core restores a non-persistent issue as inactive and
    without either; that one is skipped.
    """
    registry = ir.async_get(hass)
    for issue_id in _STARTUP_ISSUE_IDS:
        issue = registry.async_get_issue(DOMAIN, issue_id)
        if issue is None or not issue.active:
            continue
        fields = issue.data or issue.translation_placeholders or {}
        detail = fields.get("detail", "")
        return {"issue_id": issue_id, "detail": _scrub(str(detail), entry)}
    return None


def _scrub(text: str, entry: ConfigEntry) -> str:
    """Replace every stored secret value, and URL credentials, in ``text``.

    The server strips the package override before it reaches an error message,
    while the options store it as entered, so the stripped form is replaced too.
    """
    stored = [entry.data.get(key) for key in _SECRET_DATA_KEYS]
    stored += [entry.options.get(key) for key in _SECRET_OPTION_KEYS]
    values = {s for v in stored if isinstance(v, str) for s in (v, v.strip())}
    # The install error quotes the spec with repr(), which escapes backslashes
    # and quotes, so the quoted form is replaced as well.
    values |= {repr(v)[1:-1] for v in values}
    # Longest first, so a value that contains another is replaced whole.
    for value in sorted(
        (v for v in values if v and not _is_server_pin(v)), key=len, reverse=True
    ):
        text = text.replace(value, _REDACTED)
    return _URL_USERINFO.sub(rf"\1{_REDACTED}@", text)


def _is_server_pin(value: str) -> bool:
    """True for a plain server pin (``ha-mcp``, ``ha-mcp-dev==1.2``).

    ``last_pip_spec`` holds one of these when no override is set: the
    component's manifest pin, or bare ``ha-mcp`` without one. It is not a
    secret, and keeping it leaves the package name and version in the error.
    Only the whole value counts: an override that merely starts with one
    carries whatever was typed after it. The version must be a public one, as
    PyPI releases are: a local label (``+...``) can carry arbitrary
    alphanumeric text, such as a token.
    """
    dist, pinned, version = value.strip().partition("==")
    if dist not in (DIST_NAME_STABLE, DIST_NAME_DEV):
        return False
    if not pinned:
        return True
    try:
        parsed = Version(version)
    except ValueError:  # InvalidVersion, or a digit run past int()'s limit
        return False
    return parsed.local is None
