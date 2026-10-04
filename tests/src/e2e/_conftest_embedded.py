"""Lane selectors and helpers for the embedded server backend of the E2E fixtures."""

import json
import logging
import os
import subprocess
import time
import warnings
from pathlib import Path
from typing import Any

import requests
from test_constants import TEST_TOKEN
from urllib3.exceptions import InsecureRequestWarning

from .utilities.streamable_http import parse_mcp_response
from .utilities.topology import tools_entry_absent

logger = logging.getLogger(__name__)


# --- Embedded backend (in-process server entry of ha_mcp_tools, #1527) --------
# Same testcontainer HA as the ``container`` backend, but the server-under-test is
# the ha_mcp_tools component's in-process "server" config entry running IN-PROCESS
# inside the container (not an in-process FastMCP server in the pytest process).
# Selected with ``E2E_BACKEND=embedded``; ``mcp_client`` then speaks Streamable
# HTTP to the entry's ingress webhook instead of using an in-memory transport. The
# component is installed once (for all testcontainer backends) by
# ``_install_custom_component``; the embedded backend additionally seeds the
# server entry via ``_install_embedded_server``.
_EMBEDDED_DOMAIN = "ha_mcp_tools"
_EMBEDDED_ENTRY_ID = "e2e_embedded_ha_mcp_server_entry"
# unique_id of the single-instance server entry (matches config_flow's
# ``_SERVER_UNIQUE_ID = f"{DOMAIN}-server"``), distinct from the tools entry's.
_EMBEDDED_UNIQUE_ID = "ha_mcp_tools-server"
# Stable webhook id + secret so the fixture knows the connect URL up front (a
# generated one would live only in the container's .storage after bring-up).
_EMBEDDED_WEBHOOK_ID = "mcp_e2e_embedded_0123456789abcdef"
_EMBEDDED_SECRET_PATH = "/private_e2e_embedded_server"
_EMBEDDED_SERVER_PORT = 9584
# Persistent data dir the server entry hands ha_mcp via HA_MCP_CONFIG_DIR (mirrors
# the component's const.SERVER_CONFIG_SUBDIR); the feature-flag override file
# lives directly under it.
_EMBEDDED_SERVER_CONFIG_SUBDIR = ".ha_mcp"
# Feature flags the container backend injects as pytest-process env vars for its
# in-process server (see the testcontainer path below). The embedded server runs
# inside the container and cannot read the pytest process env, so the SAME values
# are written to the component's data-dir override file (feature_flags.json) —
# ha_mcp.config reads that override layer in a standalone deployment, and the
# embedded server IS standalone (is_running_in_addon() is False in embedded mode,
# so the file is honored rather than short-circuited by Supervisor). Keys are
# ha_mcp.config Settings field names, not env-var names: both
# config.FEATURE_FLAG_FIELDS and, via _apply_advanced_overrides reading the
# same feature_flags.json, config.ADVANCED_SETTINGS_FIELDS. READ_ONLY_MODE /
# ENABLE_TOOL_SECURITY_POLICIES are deliberately absent:
# the tests that need them build their own in-process server
# (test_readonly_mode / test_approval_flow), so enabling them on the shared
# embedded server would break the default-catalog tests.
_EMBEDDED_FEATURE_FLAGS: dict[str, bool | str] = {
    "enable_beta_features": True,
    "enable_yaml_config_editing": True,
    "enable_yaml_packages_automation": True,
    "enable_yaml_packages_script": True,
    "enable_yaml_packages_scene": True,
    "enable_filesystem_tools": True,
    # Strict best-practices gate (#1779) defaults ON with its parent
    # (enable_mandatory_bps); pin it OFF here the same way the testcontainer
    # backend pins ENABLE_STRICT_MANDATORY_BPS=false, so the embedded server's
    # keyless writes aren't hard-blocked.
    "enable_strict_mandatory_bps": False,
    # Operator extra YAML write keys (#1887): an ADVANCED_SETTINGS_FIELDS
    # value (str, not a bool flag) so the embedded/haos_embedded backends can
    # exercise a successful non-built-in-key write. The container backend sets
    # the HA_MCP_EXTRA_YAML_KEYS env var directly instead (below).
    "extra_yaml_write_keys": "alert2",
}
# BACKUP_OVERRIDE_FIELDS (not FEATURE_FLAG_FIELDS) values for the embedded
# server — a separate override file (backup_settings.json) from
# _EMBEDDED_FEATURE_FLAGS's feature_flags.json, since ha_mcp.config reads the
# two registries from different files. Mirrors the ENABLE_SNAPSHOT_DELETE env
# var the container backend sets directly (#1861); the embedded server can't
# read that env, so it goes through this file instead, same rationale as
# _EMBEDDED_FEATURE_FLAGS above.
_EMBEDDED_BACKUP_OVERRIDES: dict[str, bool] = {
    "enable_snapshot_delete": True,
}


_EMBEDDED_READY_POLL_S = 5


# ``pip list --format=freeze`` snapshots written by the embedded backend's
# container entrypoint, bracketing the ha-mcp wheel preinstall, plus a copy
# of the image's own package_constraints.txt. Land in the bind-mounted
# /config so workflows/embedded/test_embedded_no_stomp.py can read them
# host-side and assert the install never replaced anything HA governs
# (#2135/#2146).
EMBEDDED_FREEZE_BEFORE = ".embedded_preinstall_freeze_before.txt"
EMBEDDED_FREEZE_AFTER = ".embedded_preinstall_freeze_after.txt"
EMBEDDED_HA_CONSTRAINTS_COPY = ".embedded_ha_package_constraints.txt"


def _is_embedded_backend_selected() -> bool:
    """Return True when ``E2E_BACKEND=embedded`` selects the in-process server.

    The embedded backend is a variant of the testcontainer path (not HAOS), so it
    is orthogonal to ``is_haos_backend_selected()`` — both are never true at once.
    """
    return os.environ.get("E2E_BACKEND", "").strip().lower() == "embedded"


def _is_no_tools_entry_selected() -> bool:
    """Return True when ``E2E_NO_TOOLS_ENTRY`` selects a no-tools lane (#2292).

    On these lanes the component's "HA-MCP File & YAML Tools" config entry is
    absent, so the privileged filesystem / YAML services never register. It is
    orthogonal to the backend selectors: each backend has its own no-tools
    shape (see ``_prepare_testcontainer_config`` / ``_prepare_haos_image``).

    Delegates to ``utilities.topology.tools_entry_absent`` — the staging here
    and the markers / assertions there must never disagree about which
    topology a lane is running, so there is one parser, not two. It raises on
    an unrecognized value rather than guessing.
    """
    return tools_entry_absent()


def _build_embedded_server_wheel(dest_dir: Path) -> Path:
    """Build a ha-mcp wheel from the checkout into ``dest_dir`` via ``uv build``.

    The wheel carries the PR's own ``src/ha_mcp`` (checkout fidelity for the
    embedded-mode routing under test); its dependencies still resolve in the
    container (preinstalled in the entrypoint, see the testcontainer path).

    Uses ``uv build`` rather than ``python -m pip wheel`` (as the
    workflows/embedded smoke test and ``haos_runtime`` helpers do): the project's
    uv-managed venv does not seed ``pip``/``setuptools``, so ``python -m pip
    wheel`` fails with "No module named pip". Those helpers catch the failure and
    skip/warn, but this backend must ERROR loudly if the wheel can't be built —
    the whole lane depends on it — so it must use a mechanism that actually works
    in the venv. ``uv`` is always on PATH (the suite runs under ``uv run``) and
    builds in an isolated env without needing pip in the venv.
    """
    repo_root = Path(__file__).parent.parent.parent.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dest_dir), str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wheels = list(dest_dir.glob("ha_mcp-*.whl"))
    if not wheels:
        raise RuntimeError(f"no ha_mcp wheel built in {dest_dir}")
    return wheels[0]


def _install_embedded_server(config_path: Path, wheel_name: str) -> None:
    """Seed the ha_mcp_tools "server" config entry + feature-flag overrides.

    The ha_mcp_tools component itself is already copied by
    ``_install_custom_component`` (which also seeds the "tools" services entry);
    the in-process server is a SECOND config entry of that same component,
    discriminated by ``data.entry_type == "server"``. Three pieces here, laid
    down before the container boots (so a post-boot host write to the bind
    mount doesn't have to propagate, matching the other pre-boot seeders):

    1. Seed the server config entry with the stable webhook id/secret (so the
       mcp_client fixture knows the connect URL) and a ``file://`` ``pip_spec``
       pointing at the checkout-built wheel copied into ``/config``. No
       ``last_pip_spec`` is stored, so bring-up takes the force-install path
       (proven by the workflows/embedded smoke test) — fast here because the
       entrypoint already installed the wheel + deps.
    2. Write the feature-flag override file. The container backend injects these
       as pytest-process env vars for its in-process server; the embedded server
       runs in the container and can't read that env, so the equivalent values go
       to ``<config>/.ha_mcp/feature_flags.json`` — the same override layer
       ``ha_mcp.config`` reads in a standalone deployment (the embedded server is
       standalone: ``is_running_in_addon()`` is False in embedded mode, so the
       file is honored rather than short-circuited by Supervisor).
    3. Write the backup-settings override file (``backup_settings.json``,
       ``_EMBEDDED_BACKUP_OVERRIDES``) the same way, for ``BACKUP_OVERRIDE_FIELDS``
       values (e.g. ``enable_snapshot_delete``) — a separate registry from
       ``FEATURE_FLAG_FIELDS``, read from a separate override file.
    """
    storage_file = config_path / ".storage" / "core.config_entries"
    data = json.loads(storage_file.read_text())
    entries = data.setdefault("data", {}).setdefault("entries", [])
    # Dedupe by entry_id, not domain: the domain (ha_mcp_tools) is shared with the
    # tools services entry seeded by _install_custom_component.
    if not any(
        isinstance(e, dict) and e.get("entry_id") == _EMBEDDED_ENTRY_ID for e in entries
    ):
        entries.append(
            {
                "created_at": "2025-09-07T23:56:28.040744+00:00",
                "data": {
                    "entry_type": "server",
                    "webhook_id": _EMBEDDED_WEBHOOK_ID,
                    "secret_path": _EMBEDDED_SECRET_PATH,
                    # The component no longer provisions an administrator
                    # (#2427); the server runs with the test admin's token.
                    "admin_token": TEST_TOKEN,
                },
                "disabled_by": None,
                "discovery_keys": {},
                "domain": _EMBEDDED_DOMAIN,
                "entry_id": _EMBEDDED_ENTRY_ID,
                "minor_version": 1,
                "modified_at": "2025-09-07T23:56:28.040747+00:00",
                "options": {
                    # file:// wheel (the pre-release/override channel) + deps
                    # resolved under HA's constraints on force-install.
                    "pip_spec": f"ha-mcp @ file:///config/{wheel_name}",
                    "server_port": _EMBEDDED_SERVER_PORT,
                    "bind_host": "127.0.0.1",
                    "webhook_auth": "none",
                },
                "pref_disable_new_entities": False,
                "pref_disable_polling": False,
                "source": "import",
                "subentries": [],
                "title": "HA-MCP Server",
                "unique_id": _EMBEDDED_UNIQUE_ID,
                "version": 1,
            }
        )
        storage_file.write_text(json.dumps(data, indent=2))

    server_data_dir = config_path / _EMBEDDED_SERVER_CONFIG_SUBDIR
    server_data_dir.mkdir(parents=True, exist_ok=True)
    (server_data_dir / "feature_flags.json").write_text(
        json.dumps(_EMBEDDED_FEATURE_FLAGS, indent=2)
    )
    (server_data_dir / "backup_settings.json").write_text(
        json.dumps(_EMBEDDED_BACKUP_OVERRIDES, indent=2)
    )
    logger.info(
        "Seeded ha_mcp_tools in-process server config entry + feature-flag "
        "+ backup-setting overrides"
    )


def _embedded_mcp_result(resp: requests.Response) -> dict[str, Any] | None:
    """Parse a Streamable-HTTP MCP response (JSON body or SSE) to a JSON-RPC dict."""
    return parse_mcp_response(resp.headers.get("Content-Type", ""), resp.content)


def _wait_for_embedded_webhook_ready(
    webhook_url: str, timeout: int, *, verify: bool = True
) -> bool:
    """Poll the embedded server's ingress webhook until MCP ``initialize`` works.

    A valid JSON-RPC ``result`` means the in-process MCP server has installed
    itself, started its worker thread, and registered the webhook. Returns False
    on timeout so the caller can dump diagnostics and fail with context.
    ``verify=False`` lets the TLS scenario poll the ``https://`` webhook while
    Core runs its trial certificate.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e-embedded-readiness", "version": "1.0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with warnings.catch_warnings():
                if not verify:
                    # This test deliberately uses Core's hostname-mismatched
                    # loopback certificate. Keep the suite's warnings-as-errors
                    # policy for every warning except this expected one.
                    warnings.simplefilter("ignore", InsecureRequestWarning)
                resp = requests.post(
                    webhook_url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=30,
                    verify=verify,
                )
            if resp.status_code == 200:
                parsed = _embedded_mcp_result(resp)
                if parsed is not None and "result" in parsed:
                    elapsed = int(time.monotonic() - (deadline - timeout))
                    logger.info(
                        "✅ Embedded MCP server webhook ready after ~%ds", elapsed
                    )
                    return True
        except requests.exceptions.RequestException:
            # Bring-up still in flight (pip force-install, thread start): retry.
            pass
        time.sleep(_EMBEDDED_READY_POLL_S)
    return False
