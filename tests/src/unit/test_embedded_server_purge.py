"""Which ha-mcp code a start serves: the module purge and the stale-code warning (#1904).

All workers are threads of the one Home Assistant process and Python imports
``ha_mcp`` from ``sys.modules``, so a reload after an install only serves new
code if the cached modules are dropped. Whether a start dropped them is read
from the identity of the ``ha_mcp`` package in ``sys.modules`` (``generation``).
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC

from ._embedded_server_core import (
    DEV,
    REAL_VERSION,
    STABLE,
    PackageIndex,
    pin,
    running_server,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import package_index as package_index

pytestmark = pytest.mark.usefixtures("embedded_server_env", "socket_enabled")

# An entry with an override always purges.
OVERRIDE = {OPT_PIP_SPEC: "ha-mcp==99.0.0"}
RESTART_WARNING = "restart Home Assistant"


def generation() -> Any:
    return sys.modules["ha_mcp"]


async def test_a_start_with_an_override_replaces_the_cached_server_modules(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it an entry reload reinstalls the package and keeps serving the old
    code until Home Assistant restarts; ``ha_mcp_other`` is not a server module."""
    before = generation()
    monkeypatch.setitem(sys.modules, "ha_mcp.stale_probe", ModuleType("probe"))
    other = ModuleType("ha_mcp_other")
    monkeypatch.setitem(sys.modules, "ha_mcp_other", other)

    async with running_server(hass, options=OVERRIDE):
        assert generation() is not before
        assert "ha_mcp.stale_probe" not in sys.modules
        assert sys.modules["ha_mcp_other"] is other


async def test_a_start_with_no_server_modules_cached_imports_them_fresh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [n for n in sys.modules if n == "ha_mcp" or n.startswith("ha_mcp.")]:
        monkeypatch.delitem(sys.modules, name)

    async with running_server(hass, options=OVERRIDE) as (mgr, _entry):
        assert mgr.is_running
        assert "ha_mcp.config" in sys.modules


async def test_a_retry_with_the_same_install_keeps_the_warm_module_cache(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Purging on every attempt made each retry pay the whole cold import again,
    so hardware that missed the readiness window once could never recover."""
    caplog.set_level(logging.DEBUG, logger=es.__name__)
    async with running_server(hass):
        warm = generation()

    async with running_server(hass):
        assert generation() is warm
    assert "skipping the module purge (warm start)" in caplog.text


async def test_a_changed_install_replaces_the_warm_module_cache(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    hass.config.skip_pip = True
    package_index.dists[STABLE] = REAL_VERSION
    async with running_server(hass):
        warm = generation()

    package_index.dists[STABLE] = "99.0.0"
    async with running_server(hass):
        assert generation() is not warm


async def test_an_override_replaces_the_cache_even_when_the_version_matches(
    hass: HomeAssistant,
) -> None:
    """A re-pointed tarball or pin can change the code under the same version
    string, which a version-keyed skip would serve stale."""
    async with running_server(hass):
        warm = generation()

    async with running_server(hass, options=OVERRIDE):
        assert generation() is not warm


# -- the running code against the installed one ------------------------------


async def start_with_a_new_install_after_the_import(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    installed_after: str | None,
) -> None:
    caplog.set_level(logging.WARNING, logger=es.__name__)
    pin(hass, f"{STABLE}=={REAL_VERSION}")
    package_index.dists[STABLE] = REAL_VERSION
    if installed_after:
        package_index.after_worker_read = (STABLE, installed_after)
    async with running_server(hass):
        pass


async def test_a_worker_running_older_code_than_is_installed_asks_for_a_restart(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    await start_with_a_new_install_after_the_import(
        hass, package_index, caplog, installed_after="99.0.0"
    )

    assert f"running version {REAL_VERSION}" in caplog.text
    assert "version 99.0.0 is installed" in caplog.text
    assert RESTART_WARNING in caplog.text


async def test_a_worker_running_the_installed_code_is_quiet(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    await start_with_a_new_install_after_the_import(
        hass, package_index, caplog, installed_after=None
    )

    assert RESTART_WARNING not in caplog.text


async def test_leftover_stable_metadata_does_not_make_a_dev_install_look_stale(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    """``ha_mcp.__version__`` reads the stable metadata first, so after a failed
    best-effort uninstall freshly imported dev code reports the stale stable
    version; the start must compare against the dev distribution it installed."""
    caplog.set_level(logging.WARNING, logger=es.__name__)
    dev_version = "7.13.0.dev1"
    package_index.dists.update({STABLE: "6.2.0", DEV: dev_version})
    package_index.uninstall_outcome = lambda _dist: "permission denied"
    pin(hass, f"{DEV}=={dev_version}")

    async with running_server(hass, options={OPT_PIP_SPEC: f"{DEV}=={dev_version}"}):
        pass

    assert RESTART_WARNING not in caplog.text
