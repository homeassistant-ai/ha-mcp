"""Best-effort collectors for ha_report_issue: logs, this instance's own
identifiers, and Home Assistant's install facts. None of them raises."""

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from .._version import is_running_in_addon
from ..client.supervisor_client import make_supervisor_httpx_client
from ..config import get_global_settings
from .bug_report_redaction import KnownIdentifiers, _sanitize_log_text
from .coercion import ANSI_ESCAPE_RE
from .helpers import get_connected_ws_client

logger = logging.getLogger(__name__)

# Max characters to include from addon container logs.
# 3000 chars ≈ 750 LLM tokens — keeps the tool response well below context budgets
# while still capturing enough recent output to diagnose most issues.
_ADDON_LOG_MAX_CHARS = 3000

# Max characters to include from the Home Assistant error log (home-assistant.log).
# Slightly larger than the addon cap: this log carries the highest-value
# diagnostic lines (auth failures, integration tracebacks) and the recent tail
# is where they land. ~5000 chars ≈ 1250 LLM tokens.
_CORE_LOG_MAX_CHARS = 5000

# Log window requested from the install for that tail. Only the last
# _CORE_LOG_MAX_CHARS survive into the report — a few dozen lines of typical
# HA log text — so anything deeper is fetched and then thrown away, which on
# Supervisor-backed installs is exactly the cost that made the unconditional
# 20,000-line fetch hang (#2279). 500 keeps an order of magnitude of headroom
# for short lines.
_CORE_LOG_WINDOW_LINES = 500


async def _fetch_addon_logs() -> str:
    """Fetch ha-mcp addon container logs via the Supervisor REST API.

    Only works when running as a Home Assistant add-on (SUPERVISOR_TOKEN set).
    Uses /addons/self/logs which resolves to the calling addon's own logs via
    the Supervisor's per-addon token binding — no slug interpolation needed.

    Direct httpx against ``http://supervisor`` is the documented add-on access
    pattern: it uses the Supervisor token directly (no extra HA hop) and
    preserves the ``self`` shortcut, which the WebSocket ``supervisor/api``
    proxy used by other tools may not.

    Returns sanitized log text (last _ADDON_LOG_MAX_CHARS chars, with a
    truncation marker prepended when truncation occurs), or empty string on
    failure.
    """
    # Redundant with the caller's `install_method == "addon"` gate, but kept
    # as a defensive guard for any direct callers added later.
    if not is_running_in_addon():
        return ""

    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=get_global_settings().verify_ssl
        ) as http_client:
            resp = await http_client.get("/addons/self/logs")
            if resp.status_code != 200:
                logger.info("Addon log fetch returned HTTP %s", resp.status_code)
                return ""

            # Strip ANSI escape codes first, then sanitize, then truncate.
            # Sanitizing before truncating prevents secrets that straddle the
            # truncation boundary from leaking through.
            cleaned = ANSI_ESCAPE_RE.sub("", resp.text)
            sanitized = _sanitize_log_text(cleaned)
            if len(sanitized) > _ADDON_LOG_MAX_CHARS:
                marker = (
                    f"[...truncated, showing last {_ADDON_LOG_MAX_CHARS} of "
                    f"{len(sanitized)} chars...]\n"
                )
                return marker + sanitized[-_ADDON_LOG_MAX_CHARS:]
            return sanitized
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch addon logs: {e}")

    return ""


async def _fetch_core_error_log(client: Any) -> str:
    """Fetch the Home Assistant error log (home-assistant.log) via the client.

    Works on every install type — ``HomeAssistantClient.get_error_log`` routes
    add-on installs to the Supervisor, supervised/HAOS to the hassio proxy, and
    container/pip to ``/api/error_log``. Captured over REST, which stays up even
    when the WebSocket path is failing; that is exactly the failure mode in
    issue #1694, where the high-value auth lines
    (``InsecureKeyLengthWarning``, "invalid authentication") only appeared in
    home-assistant.log, not in the add-on log this tool already captured.

    Best-effort: returns sanitized text (last ``_CORE_LOG_MAX_CHARS`` chars of a
    ``_CORE_LOG_WINDOW_LINES`` window, with a truncation marker prepended) or an
    empty string on any failure, so a log-fetch problem never breaks the
    bug-report path.
    """
    try:
        page = await client.get_error_log(lines=_CORE_LOG_WINDOW_LINES)
    except Exception as e:  # noqa: BLE001
        # Broad by design — the bug-report path must stay robust whatever the
        # client raises (auth, role, connection, transport). Logged at INFO so
        # a missing error log is visible without alarming on a routine 403.
        logger.info(
            "Could not fetch HA error log for bug report: %s (%s)",
            e,
            type(e).__name__,
        )
        return ""

    if not page.text:
        return ""

    # Strip ANSI, then sanitize, then truncate — sanitizing before truncating
    # keeps a secret straddling the cut boundary from leaking through.
    sanitized = _sanitize_log_text(ANSI_ESCAPE_RE.sub("", page.text))
    if len(sanitized) > _CORE_LOG_MAX_CHARS:
        marker = (
            f"[...truncated, showing last {_CORE_LOG_MAX_CHARS} of "
            f"{len(sanitized)} chars...]\n"
        )
        return marker + sanitized[-_CORE_LOG_MAX_CHARS:]
    return sanitized


# Kept from system_health's ``homeassistant`` block. The rest (user,
# config_dir, timezone) describes the person's setup rather than the install.
_HA_INSTALL_KEYS = (
    "installation_type",
    "os_name",
    "os_version",
    "arch",
    "python_version",
    "container_arch",
    "docker",
    "hassio",
    "dev",
)


def _add_host(host: str, hosts: set[str], addresses: set[str]) -> None:
    # A configured URL may spell the name as an absolute FQDN (trailing dot);
    # logs never do, so the stored name must not carry it.
    host = host.rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A single-label name (``supervisor`` for the app's proxy URL,
        # ``homeassistant``, ``localhost``) is generic, and redacting it would
        # blank every ordinary use of the word in the logs.
        if "." in host:
            hosts.add(host.lower())
        return
    if not address.is_loopback:
        addresses.add(host.lower())


def _add_url_host(url: Any, hosts: set[str], addresses: set[str]) -> None:
    try:
        host = urlparse(url).hostname if isinstance(url, str) else None
    except ValueError:
        # A malformed URL has no usable host; the report is still built.
        return
    if host:
        _add_host(host, hosts, addresses)


class _UnsuccessfulReply(Exception):
    """A WebSocket command Home Assistant answered with an error."""


# Each read gets its own short deadline: a silent socket must not add the
# default 30 s command wait, twice, before the report is assembled. The reply
# wait is the command's own; the outer deadline also covers connecting, which
# the pooled client bounds separately at about 20 s.
_IDENTIFIER_READ_TIMEOUT_S = 10.0
_IDENTIFIER_READ_DEADLINE_S = 15.0


async def _ws_result(client: Any, command: str) -> Any:
    reply = await client.send_websocket_message(
        {"type": command, "_wait_timeout": _IDENTIFIER_READ_TIMEOUT_S}
    )
    if not reply.get("success"):
        raise _UnsuccessfulReply(reply.get("error_code") or "unsuccessful reply")
    return reply.get("result")


async def _read_identifiers(
    client: Any, command: str, consume: Callable[[Any], None]
) -> str | None:
    """Run one read and hand its result to ``consume``; a failure becomes the
    reason text for the caller's warning instead of an exception."""
    try:
        consume(
            await asyncio.wait_for(
                _ws_result(client, command), timeout=_IDENTIFIER_READ_DEADLINE_S
            )
        )
    except Exception as e:  # noqa: BLE001
        # The error text could carry the very hostnames being collected, so
        # only Home Assistant's error code or the exception type is reported.
        reason = str(e) if isinstance(e, _UnsuccessfulReply) else type(e).__name__
        logger.info("Known-identifier read failed at %s: %s", command, reason)
        return f"{command}: {reason}"
    return None


async def collect_known_identifiers(
    client: Any,
) -> tuple[KnownIdentifiers, str | None]:
    """Read this instance's URLs and interface addresses for redaction.

    Returns the identifiers and, when a read failed, one warning for the
    caller. The reads are independent and whatever was collected is kept: the
    server's own Home Assistant host needs no read at all, ``network/url``
    answers a non-admin token while ``network`` (``require_admin`` in Core)
    does not, and a transient failure of one read must not drop the other.
    Loopback addresses are left out: they identify nobody.
    """
    hosts: set[str] = set()
    addresses: set[str] = set()
    _add_url_host(getattr(client, "base_url", None), hosts, addresses)

    def _urls(urls: Any) -> None:
        for key in ("internal", "external", "cloud"):
            _add_url_host(urls.get(key), hosts, addresses)

    def _adapters(network: Any) -> None:
        for adapter in network.get("adapters") or []:
            for entry in (adapter.get("ipv4") or []) + (adapter.get("ipv6") or []):
                if entry.get("address"):
                    _add_host(str(entry["address"]), hosts, addresses)

    failures = [
        failure
        for failure in (
            await _read_identifiers(client, "network/url", _urls),
            await _read_identifiers(client, "network", _adapters),
        )
        if failure
    ]
    warning = (
        f"Known-identifier redaction is partial ({'; '.join(failures)}); "
        "review hostnames and addresses by hand."
        if failures
        else None
    )
    return KnownIdentifiers(frozenset(hosts), frozenset(addresses)), warning


async def fetch_ha_install(client: Any) -> dict[str, Any] | None:
    """Read Home Assistant's install facts from ``system_health/info``.

    ``None`` when the WebSocket cannot be opened or the reply does not arrive.
    """
    ws_client = None
    try:
        ws_client, _ = await get_connected_ws_client(
            client.base_url, client.token, verify_ssl=client.verify_ssl
        )
        if ws_client is None:
            return None
        _, event_response = await ws_client.send_command_with_event(
            "system_health/info", wait_timeout=10.0
        )
        info = event_response["event"]["data"]["homeassistant"]["info"]
        return {key: info[key] for key in _HA_INSTALL_KEYS if key in info}
    except Exception as e:  # noqa: BLE001
        logger.info("system_health read for the report failed: %s", e)
        return None
    finally:
        if ws_client is not None:
            with contextlib.suppress(Exception):
                await ws_client.disconnect()
