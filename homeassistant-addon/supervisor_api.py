"""Supervisor API calls made at app startup: option writes and MCP discovery.

Imported by ``start.py``; kept separate so it does not import it back. The
caller passes its own log functions, so output keeps ``start.py``'s format.
"""

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# First Core release whose Model Context Protocol integration handles app
# discovery (home-assistant/core#180378). Older cores turn the message into a
# discovery card that drops the URL and asks for one by hand.
MCP_DISCOVERY_MIN_CORE = (2026, 10)


def supervisor_get(path: str, supervisor_token: str) -> dict[str, Any]:
    """GET a Supervisor endpoint and return its ``data`` object."""
    req = urllib.request.Request(
        f"http://supervisor{path}",
        headers={"Authorization": f"Bearer {supervisor_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read()).get("data")
    return data if isinstance(data, dict) else {}


def supervisor_post(path: str, supervisor_token: str, body: dict[str, Any]) -> None:
    """POST a JSON body to a Supervisor endpoint, discarding the response.

    Raises the underlying `urllib.error.HTTPError` / `URLError` / `OSError`
    on failure — callers decide how loudly to surface the problem.
    """
    req = urllib.request.Request(
        f"http://supervisor{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def core_supports_mcp_discovery(version: str) -> bool:
    """Whether a Core version string (``2026.10.0``, ``2026.10.0b1``) is new enough."""
    try:
        year, month = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (year, month) >= MCP_DISCOVERY_MIN_CORE


def announce_mcp_discovery(
    secret_path: str,
    port: int,
    supervisor_token: str,
    log_info: Callable[[str], None],
    log_warning: Callable[[str], None],
) -> None:
    """Offer this server to Home Assistant's Model Context Protocol integration.

    The Supervisor keys the message by (app, service), so re-announcing on every
    start replaces the URL in place. It is never withdrawn on stop: Core removes
    the discovered entry when the message disappears. Failures never block
    startup.
    """
    try:
        version = str(supervisor_get("/core/info", supervisor_token).get("version"))
        if not core_supports_mcp_discovery(version):
            log_info(
                f"Home Assistant {version} cannot discover MCP apps (needs "
                "2026.10+); restart this app after updating Home Assistant."
            )
            return
        hostname = supervisor_get("/addons/self/info", supervisor_token).get("hostname")
        if not hostname:
            log_warning("Supervisor reported no app hostname; MCP discovery skipped.")
            return
        # Starlette redirects a trailing slash, and Core's client does not
        # follow redirects.
        url = f"http://{hostname}:{port}{secret_path.rstrip('/')}"
        supervisor_post(
            "/discovery",
            supervisor_token,
            {"service": "mcp", "config": {"url": url}},
        )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as e:
        log_warning(f"Could not announce this server to Home Assistant: {e!r}")
        return
    log_info(
        "Announced to Home Assistant: Settings > Devices & services offers to "
        "add this server to the Model Context Protocol integration."
    )


def start_mcp_discovery(
    secret_path: str,
    port: int,
    supervisor_token: str,
    log_info: Callable[[str], None],
    log_warning: Callable[[str], None],
) -> None:
    """Announce in a daemon thread: each Supervisor call may wait its full timeout."""
    threading.Thread(
        target=announce_mcp_discovery,
        args=(secret_path, port, supervisor_token, log_info, log_warning),
        name="mcp-discovery",
        daemon=True,
    ).start()
