"""Run both component topologies in each worker's existing Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from ._conftest_embedded import _wait_for_embedded_webhook_ready
from ._conftest_readiness import (
    _wait_for_core_state_running,
    _wait_for_entries_loaded,
    _wait_for_ha_api_ready,
)
from .utilities.topology import (
    combined_topologies,
    component_surface_available,
    item_topology,
    tools_entry_absent,
)

logger = logging.getLogger(__name__)
WITHOUT_TOOLS = "topology_without_tools"
WITH_TOOLS = "topology_with_tools"


def pytest_configure(config: Any) -> None:
    if combined_topologies():
        # All workers boot the absent topology, including a worker whose first
        # assigned scope belongs to the second phase.
        os.environ["E2E_NO_TOOLS_ENTRY"] = "1"
        if config.getoption("dist", "no") not in ("no", "loadscope"):
            raise pytest.UsageError("Combined topologies require --dist loadscope")
        config.option.loadscopereorder = False
        # xdist requeues a crashed worker's unfinished scopes onto the other
        # workers, and one that has installed cannot run an absent scope again.
        # Stop at the first crash instead of failing unrelated scopes.
        config.option.maxworkerrestart = "0"
    elif tools_entry_absent():
        # order_topologies puts the entry-enabling tests last; xdist's default
        # largest-scope-first order would undo that.
        config.option.loadscopereorder = False


def pytest_generate_tests(metafunc: Any) -> None:
    if combined_topologies() and "e2e_topology" in metafunc.fixturenames:
        markers = {marker.name for marker in metafunc.definition.iter_markers()}
        has_surface = component_surface_available(no_tools_entry=True)
        if markers & {"requires_tools_entry", "installs_component_surface"}:
            phases = [WITH_TOOLS]
        elif "requires_component_surface" in markers:
            phases = [WITHOUT_TOOLS if has_surface else WITH_TOOLS]
        elif "component_topology" in markers and not has_surface:
            phases = [WITHOUT_TOOLS, WITH_TOOLS]
        else:
            phases = [WITHOUT_TOOLS]
        metafunc.parametrize("e2e_topology", phases, indirect=True, scope="session")


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_make_scheduler(config: Any, log: Any) -> Any:
    if not combined_topologies():
        return None
    from xdist.scheduler.loadscope import LoadScopeScheduling

    class TopologyScheduling(LoadScopeScheduling):
        # loadscope hands out scopes in collection order, which
        # order_topologies made absent-first. Prefixing the phase keeps a
        # module's two phases in separate scopes, so no scope straddles the
        # installation.
        def _split_scope(self, nodeid: str) -> str:
            phase = WITHOUT_TOOLS if WITHOUT_TOOLS in nodeid else WITH_TOOLS
            return f"{phase}:{super()._split_scope(nodeid)}"

    return TopologyScheduling(config, log)


def order_topologies(items: list[Any]) -> None:
    if combined_topologies():
        # Stable sort preserves the final-TLS ordering within a phase, so this
        # must run after _move_haos_tls_items_last.
        items.sort(key=lambda item: item_topology(item) == WITH_TOOLS)
    elif tools_entry_absent():
        # A test that enables a component entry leaves it behind, and the app
        # reports the component once its cached absence expires (300 s). Run
        # those tests after every test that expects the entry absent.
        items.sort(
            key=lambda item: (
                item.get_closest_marker("installs_component_surface") is not None
            )
        )


def _pending_core_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    pending = []
    for job in jobs:
        name = job.get("name") or ""
        if not job["done"] and name.startswith("home_assistant_core_"):
            pending.append(name)
        pending.extend(_pending_core_jobs(job["child_jobs"]))
    return pending


def wait_for_core_job(env: dict[str, Any], timeout: float = 300) -> None:
    """A restored HTTP endpoint can precede Supervisor releasing its restart lock."""
    from haos_runtime import _home_assistant_ws_command

    deadline = time.monotonic() + timeout
    pending: list[str] = []
    while time.monotonic() < deadline:
        result = _home_assistant_ws_command(
            env["base_url"],
            env["token"],
            {"type": "supervisor/api", "endpoint": "/jobs/info", "method": "get"},
            timeout=min(30, max(deadline - time.monotonic(), 0.1)),
        )
        pending = _pending_core_jobs(result["jobs"])
        if not pending:
            return
        time.sleep(1)
    raise RuntimeError(
        f"Supervisor's previous Core job did not finish before restart: {pending}"
    )


def _check(response: requests.Response, action: str) -> requests.Response:
    """Raise with Core's reply body, which names the reason a request failed."""
    if not response.ok:
        raise RuntimeError(
            f"{action} failed: HTTP {response.status_code}: {response.text}"
        )
    return response


def _restart_core(env: dict[str, Any]) -> None:
    if env["backend"].startswith("haos"):
        wait_for_core_job(env)
    headers = {"Authorization": f"Bearer {env['token']}"}
    base_url = env["base_url"]
    try:
        response = requests.post(
            f"{base_url}/api/services/homeassistant/restart",
            headers=headers,
            json={},
            timeout=30,
        )
        _check(response, "Core restart request")
    except requests.ConnectionError:
        logger.debug("Core closed the restart request during shutdown")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{base_url}/api/", headers=headers, timeout=5)
            if response.status_code != 200:
                break
        except requests.ConnectionError:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Core never went down after the topology restart")
    if not _wait_for_ha_api_ready(base_url, headers, timeout=600):
        raise RuntimeError("Core did not return after the topology restart")
    ready, _, state, _, _, snapshot_ok, unloaded = _wait_for_core_state_running(
        base_url, headers, timeout=600
    )
    if not ready:
        raise RuntimeError(f"Core did not finish restarting: {state}; {unloaded}")
    if env.get("container") is not None and (unloaded or not snapshot_ok):
        _wait_for_entries_loaded(env["container"], base_url, headers)


def _create_tools_entry(env: dict[str, Any]) -> str:
    """Use the same menu and confirmation flow as Add Integration."""
    url = f"{env['base_url']}/api/config/config_entries/flow"
    headers = {"Authorization": f"Bearer {env['token']}"}
    response = requests.post(
        url, headers=headers, json={"handler": "ha_mcp_tools"}, timeout=120
    )
    step = _check(response, "Starting the File & YAML Tools flow").json()
    for answer in ({"next_step_id": "tools"}, {}):
        if step["type"] not in ("menu", "form"):
            raise RuntimeError(f"Unexpected File & YAML Tools setup step: {step}")
        response = requests.post(
            f"{url}/{step['flow_id']}", headers=headers, json=answer, timeout=120
        )
        step = _check(response, f"File & YAML Tools step {step['step_id']}").json()
    if step["type"] != "create_entry":
        raise RuntimeError(f"File & YAML Tools setup did not create an entry: {step}")
    return step["result"]["entry_id"]


def install_tools_entry(env: dict[str, Any]) -> None:
    """Install the checked-out component, add its tools entry, and restart Core."""
    config_path = env.get("config_path")
    if config_path:
        target = Path(config_path) / "custom_components" / "ha_mcp_tools"
        if not target.exists():
            source = (
                Path(__file__).resolve().parents[3]
                / "custom_components"
                / "ha_mcp_tools"
            )
            shutil.copytree(source, target)
            # Core discovered the initial custom-component directory before
            # these files existed. Restart before opening the new flow.
            _restart_core(env)
    entry_id = _create_tools_entry(env)
    _restart_core(env)
    headers = {"Authorization": f"Bearer {env['token']}"}
    if env["backend"].startswith("haos"):
        from ._conftest_haos import _wait_for_haos_tools_entry_loaded

        _wait_for_haos_tools_entry_loaded(env["base_url"], headers)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = requests.get(
            f"{env['base_url']}/api/services", headers=headers, timeout=15
        )
        services = _check(response, "Listing services").json()
        if any(row["domain"] == "ha_mcp_tools" for row in services):
            break
        time.sleep(1)
    else:
        raise RuntimeError(
            f"File & YAML Tools entry {entry_id} did not load after restart"
        )
    if env.get("embedded_webhook_url"):
        if not _wait_for_embedded_webhook_ready(
            env["embedded_webhook_url"], timeout=600
        ):
            raise RuntimeError(
                "Embedded MCP server did not return after the topology restart"
            )
    if env["backend"] == "haos_inaddon":
        from haos_runtime import (
            HA_MCP_DEV_ADDON_SLUG,
            _home_assistant_ws_command,
            wait_for_addon_ha_link_ready,
            wait_for_addon_mcp_ready,
        )

        # The app outlives Core and caches absent component capabilities.
        _home_assistant_ws_command(
            env["base_url"],
            env["token"],
            {
                "type": "supervisor/api",
                "endpoint": f"/addons/{HA_MCP_DEV_ADDON_SLUG}/restart",
                "method": "post",
            },
            timeout=180,
        )
        env["addon_mcp_url"] = wait_for_addon_mcp_ready()
        if not wait_for_addon_ha_link_ready(env["addon_mcp_url"]):
            raise RuntimeError(
                "App did not reconnect to Core after the topology restart"
            )
    logger.info(
        "[E2E_TOPOLOGY] installed tools entry %s in the existing %s",
        entry_id,
        env["backend"],
    )


@pytest.fixture(scope="session", autouse=True)
async def e2e_topology(
    request: Any, ha_container_with_fresh_config: dict[str, Any]
) -> Any:
    phase = getattr(request, "param", None)
    if phase is None:
        yield
        return
    env = ha_container_with_fresh_config
    previous = env.get("topology_phase")
    if previous == WITH_TOOLS and phase == WITHOUT_TOOLS:
        raise RuntimeError(
            "E2E scheduler returned to the absent topology after installation"
        )
    if phase == WITH_TOOLS and previous != WITH_TOOLS:
        await asyncio.to_thread(install_tools_entry, env)
    os.environ["E2E_NO_TOOLS_ENTRY"] = "1" if phase == WITHOUT_TOOLS else "0"
    env["topology_phase"] = phase
    logger.info("[E2E_TOPOLOGY] %s on %s", phase, env["backend"])
    yield
