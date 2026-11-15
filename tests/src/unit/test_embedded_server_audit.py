"""Which distribution the start's dependency audit walks (#2245).

The audit's stand-in (``embedded_server_env``) records the root it is asked to
audit; each case starts the manager on a real ``hass`` and a server entry with
no credential, so the start passes the package and audit steps and stops at the
token step.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC

from ._embedded_server_core import (
    DEV,
    INDEX_VERSION,
    STABLE,
    ImportHook,
    PackageIndex,
    new_manager,
    package_step,
    pin,
    server_options,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import import_hook as import_hook
from ._embedded_server_core import package_index as package_index

DEV_VERSION = "8.7.0.dev3"
PIN = f"{STABLE}=={INDEX_VERSION}"
DEV_PIN = f"{DEV}=={DEV_VERSION}"


@pytest.mark.parametrize(
    ("requirements", "override", "installed", "root"),
    [
        pytest.param([PIN], "ha-mcp==9.9.9", [DEV], STABLE, id="pin-override"),
        pytest.param(
            [PIN],
            "https://example.invalid/ha-mcp.tar.gz",
            [STABLE, DEV],
            STABLE,
            id="tarball-url-with-both-installed",
        ),
        pytest.param(
            [PIN],
            "https://example.invalid/ha_mcp_dev-8.7.0-py3-none-any.whl",
            [DEV],
            DEV,
            id="dev-wheel-url-not-the-absent-stable",
        ),
        pytest.param(
            [PIN],
            "acme-ha-mcp @ file:///package.whl",
            [STABLE],
            "acme-ha-mcp",
            id="named-url-names-its-own-distribution",
        ),
        pytest.param([PIN], None, [STABLE, DEV], STABLE, id="manifest-pin"),
        pytest.param([DEV_PIN], None, [STABLE], DEV, id="dev-manifest-pin"),
    ],
)
async def test_the_audit_walks_the_distribution_that_runs(
    hass: HomeAssistant,
    package_index: PackageIndex,
    embedded_server_env: list[str],
    requirements: list[str],
    override: str | None,
    installed: list[str],
    root: str,
) -> None:
    pin(hass, *requirements)
    package_index.dists = {
        dist: INDEX_VERSION if dist == STABLE else DEV_VERSION for dist in installed
    }
    options = {} if override is None else {OPT_PIP_SPEC: override}
    mgr, _ = await new_manager(hass, options=options)

    await package_step(mgr)

    assert embedded_server_env == [root]


@pytest.mark.usefixtures("socket_enabled")
async def test_a_deferred_start_audits_the_distribution_that_is_installed(
    hass: HomeAssistant,
    import_hook: ImportHook,
    package_index: PackageIndex,
    embedded_server_env: list[str],
) -> None:
    package_index.dists[STABLE] = INDEX_VERSION
    import_hook.block("ha_mcp.server")
    pin(hass, PIN)
    first, _ = await new_manager(
        hass, token=True, options=server_options(**{OPT_PIP_SPEC: PIN})
    )
    start = asyncio.create_task(first.async_start())
    try:
        assert await hass.async_add_executor_job(import_hook.entered.wait, 30)
        package_index.dists = {DEV: DEV_VERSION}
        embedded_server_env.clear()
        second, _ = await new_manager(hass)

        await package_step(second)

        assert embedded_server_env == [DEV]
    finally:
        import_hook.release.set()
        try:
            await asyncio.wait_for(start, timeout=60)
        finally:
            await first.async_stop()
