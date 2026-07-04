"""HAOS-lane E2E for the in-process ha_mcp_server integration (issue #1527).

The testcontainer embedded-server test
(``tests/src/e2e/workflows/embedded/test_embedded_server.py``, ``@container_only``)
proves the mechanism on the docker / unsupervised HA flavor. This module is its
HAOS counterpart: it covers the HAOS-specific deltas that live testing found
broken and that the container lane cannot reach —

* ``SUPERVISOR_TOKEN`` IS present in the HAOS core container, so the in-process
  server's ``is_running_in_addon()`` would report True unless the integration
  set ``HA_MCP_EMBEDDED`` first. A tool call succeeding here — end to end,
  against a real Supervised Home Assistant with ``SUPERVISOR_TOKEN`` in the env
  — is the regression signal that the embedded-mode routing
  (``src/ha_mcp/_version.py`` / ``config.py``) takes its non-add-on branch.
* Real-HAOS ``install_package`` behavior: enabling the entry runs HA's runtime
  requirement install of the fastmcp tree inside the HAOS core container.
* Host networking / webhook ingress: the request path is
  ``http://<vm>:8123/api/webhook/<id>`` → the mcp_webhook handler →
  ``127.0.0.1:9584`` (the in-process server), all inside HAOS.
* On the ``haos_inaddon`` lane the dev ha-mcp add-on is already running on 9583;
  the in-process server defaults to 9584, so the two coexist — this module
  asserts both respond.

How the entry gets here: the bake seeds a DISABLED ``ha_mcp_server`` config entry
into the qcow2 (``build_image._stage_embedded_server_integration``) and the
conftest HAOS branch delivers a wheel built from the checkout into ``/config`` +
rewrites the entry's ``pip_spec`` to it
(``haos_runtime.stage_embedded_server_wheel_in_qcow2``). The module fixture below
enables the entry (``config_entries/disable`` with ``disabled_by=null``) so the
multi-minute bring-up only runs on this test's session, then waits for the
server to install itself, start, and register the webhook.

Every unit of the webhook / manager / flow logic is covered hermetically in
``tests/src/unit/test_{embedded_server,mcp_webhook,embedded_setup,...}.py``; this
is the real-HAOS proof of the mechanism end to end.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from haos_runtime import (
    HA_MCP_SERVER_ENTRY_ID,
    HA_MCP_SERVER_WEBHOOK_ID,
    enable_config_entry,
)

LOG = logging.getLogger(__name__)

# Enabling the entry kicks off a runtime pip install of the whole fastmcp tree
# inside HAOS — minutes on the resource-constrained QEMU guest — before the
# webhook answers. Poll the webhook, never sleep-then-assert.
_READY_TIMEOUT_S = 600
_READY_POLL_S = 5

# The module-scoped fixture's bring-up wait runs during the FIRST test item, so
# that item's pytest-timeout must exceed the poll budget. pytest.ini's global
# timeout=300 is fine on the fast container lane (which never actually waits that
# long) but would kill a legitimately slower HAOS install mid-poll; override it
# to sit just above _READY_TIMEOUT_S. The fixture's own 600s AssertionError still
# fires first with an actionable message; this is only the safety ceiling above it.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.haos_only,
    pytest.mark.timeout(_READY_TIMEOUT_S + 120),
]


def _mcp_post(
    base_url: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(
        f"{base_url}/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}",
        headers=headers,
        data=json.dumps(payload),
        timeout=60,
    )


def _parse_mcp(resp: requests.Response) -> dict[str, Any] | None:
    """Parse a Streamable-HTTP MCP response (JSON body or SSE) to a JSON-RPC dict."""
    ctype = resp.headers.get("Content-Type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    return obj
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _initialize(base_url: str) -> tuple[bool, str | None]:
    """Run the MCP initialize handshake; return ``(ok, session_id)``."""
    resp = _mcp_post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ha_mcp_server-haos-e2e", "version": "1.0"},
            },
        },
    )
    parsed = _parse_mcp(resp)
    if not parsed or "result" not in parsed:
        return False, None
    session_id = resp.headers.get("Mcp-Session-Id")
    if session_id:
        # Some servers require the initialized notification before accepting
        # further requests — best-effort.
        _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
    return True, session_id


@pytest.fixture(scope="module")
def embedded_server(
    ha_container_with_fresh_config: dict[str, Any],
) -> Iterator[tuple[str, str | None, dict[str, Any]]]:
    """Enable the baked ha_mcp_server entry and wait for its webhook to answer.

    Yields ``(base_url, session_id, container_info)`` once the in-process server
    has installed itself, started, and registered its ingress webhook.
    """
    info = ha_container_with_fresh_config
    base_url = info["base_url"]
    token = info["token"]

    # Enable the baked-disabled entry. Raises on a WS-level failure (e.g. the
    # entry id is absent because the bake didn't seed it) so the cause is clear
    # rather than surfacing as a webhook timeout below.
    enable_config_entry(base_url, token, HA_MCP_SERVER_ENTRY_ID)
    LOG.info(
        "Enabled %s; waiting for the in-process server bring-up", HA_MCP_SERVER_ENTRY_ID
    )

    deadline = time.monotonic() + _READY_TIMEOUT_S
    session_id: str | None = None
    ready = False
    while time.monotonic() < deadline:
        try:
            ready, session_id = _initialize(base_url)
        except requests.exceptions.RequestException:
            # Bring-up still installing/starting — the webhook 404s / connection
            # resets until mcp_webhook registers. Retry until the deadline.
            ready = False
        if ready:
            break
        time.sleep(_READY_POLL_S)
    if not ready:
        raise AssertionError(
            "The in-process ha_mcp_server did not become reachable via its "
            f"webhook within {_READY_TIMEOUT_S}s of enabling {HA_MCP_SERVER_ENTRY_ID}. "
            "The runtime pip install may have failed, the server thread may not "
            "have started, or the wheel delivery "
            "(haos_runtime.stage_embedded_server_wheel_in_qcow2) did not land — "
            "check the ha-core-runtime.log in the HAOS diagnostics artifact."
        )
    yield base_url, session_id, info


class TestEmbeddedServerOnHaos:
    def test_initialize_and_list_tools(
        self, embedded_server: tuple[str, str | None, dict[str, Any]]
    ) -> None:
        """tools/list returns the full ha-mcp inventory over the HAOS webhook."""
        base_url, session_id, _ = embedded_server
        resp = _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        parsed = _parse_mcp(resp)
        assert parsed is not None, f"unparseable tools/list response: {resp.text[:500]}"
        assert "result" in parsed, parsed
        tools = parsed["result"].get("tools", [])
        names = {t.get("name") for t in tools}
        # The full inventory is present (well above the selection-accuracy
        # threshold); a truncated / wrong server would return a handful.
        assert len(tools) > 60, f"expected the full tool inventory, got {len(tools)}"
        assert "ha_get_state" in names

    def test_read_only_tool_call_with_supervisor_token_present(
        self, embedded_server: tuple[str, str | None, dict[str, Any]]
    ) -> None:
        """A read-only tool call succeeds despite SUPERVISOR_TOKEN in the core env.

        This is the HAOS regression signal: the HA core container carries
        ``SUPERVISOR_TOKEN``, so the in-process server would take the add-on code
        path (Supervisor-direct routing) unless the integration set
        ``HA_MCP_EMBEDDED`` before the first ``ha_mcp`` import. The tool ran
        against the real HA instance and returned content, which means the whole
        embedded chain — runtime install, server thread, loopback REST/WS client,
        webhook ingress — works end to end on real HAOS.
        """
        base_url, session_id, _ = embedded_server
        resp = _mcp_post(
            base_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ha_get_state",
                    "arguments": {"entity_id": "sun.sun"},
                },
            },
            session_id=session_id,
        )
        parsed = _parse_mcp(resp)
        assert parsed is not None, f"unparseable tools/call response: {resp.text[:500]}"
        assert "result" in parsed, parsed
        assert parsed["result"].get("content"), parsed

    def test_addon_and_embedded_server_coexist(
        self, embedded_server: tuple[str, str | None, dict[str, Any]]
    ) -> None:
        """The in-process server coexists with the dev add-on (inaddon lane).

        On ``haos_inaddon`` the dev ha-mcp add-on is already running on its own
        port (9583) while the in-process server runs on 9584; assert BOTH the
        add-on's MCP endpoint and the in-process server's webhook respond. On the
        external ``haos`` lane there is no add-on, so assert the harness reports
        none (``addon_mcp_url is None``) and re-confirm the embedded webhook —
        documenting that external mode is embedded-only.
        """
        base_url, session_id, info = embedded_server
        backend = info["backend"]

        # The embedded server answers on both lanes (the fixture already proved
        # bring-up; re-confirm it's still live alongside the coexistence check).
        embedded_ok, _ = _initialize(base_url)
        assert embedded_ok, "in-process server webhook stopped responding"

        if backend == "haos_inaddon":
            addon_url = info.get("addon_mcp_url")
            assert addon_url, (
                f"inaddon lane but addon_mcp_url is {addon_url!r} — the dev "
                f"add-on should be running for the coexistence check"
            )
            # OPTIONS is the safe verb: uvicorn answers it (often 405) even
            # though the streamable-HTTP handler RSTs a bare GET. Any non-5xx
            # response proves the add-on's listener is up alongside the embedded
            # server. wait_for_addon_mcp_ready already gated on this at setup.
            resp = requests.options(addon_url, timeout=10)
            assert resp.status_code < 500, (
                f"dev add-on MCP endpoint {addon_url} returned {resp.status_code}; "
                f"expected it to coexist with the in-process server"
            )
        else:
            assert info.get("addon_mcp_url") is None, (
                f"external haos lane should have no add-on running, but "
                f"addon_mcp_url={info.get('addon_mcp_url')!r}"
            )
