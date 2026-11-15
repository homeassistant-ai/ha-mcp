"""The embedded server's start: dependency audit, refusals and credentials, module purge.

Audit (#2245): the stand-in (``embedded_server_env``) records the root it is asked to
audit; each case starts the manager on a real ``hass`` and a server entry with no
credential, so the start passes the package and audit steps and stops at the token step.

Lifecycle (#2427): a start that cannot proceed fails before the package step, and
removing the entry releases only what an older release created for itself.

Purge (#1904): all workers are threads of the one Home Assistant process and Python
imports ``ha_mcp`` from ``sys.modules``, so a reload after an install only serves new
code if the cached modules are dropped. Whether a start dropped them is read from the
identity of the ``ha_mcp`` package in ``sys.modules`` (``generation``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import timedelta
from types import ModuleType
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DATA_ADMIN_TOKEN,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
    OPT_PIP_SPEC,
)

from ._embedded_server_core import (
    DEV,
    INDEX_VERSION,
    REAL_VERSION,
    STABLE,
    ImportHook,
    PackageIndex,
    issue_admin_token,
    new_manager,
    package_step,
    pin,
    running_server,
    server_options,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import import_hook as import_hook
from ._embedded_server_core import package_index as package_index

# Every test here needs the stand-in; the tests that start a real worker also need sockets.
pytestmark = pytest.mark.usefixtures("embedded_server_env")

DEV_VERSION = "8.7.0.dev3"
PIN = f"{STABLE}=={INDEX_VERSION}"
DEV_PIN = f"{DEV}=={DEV_VERSION}"

# An entry with an override always purges.
OVERRIDE = {OPT_PIP_SPEC: "ha-mcp==99.0.0"}
RESTART_WARNING = "restart Home Assistant"


def generation() -> Any:
    return sys.modules["ha_mcp"]


# -- which distribution the start's dependency audit walks (#2245) -----------


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


# -- what a start refuses before it touches anything, and the credential (#2427) --


async def test_a_start_without_a_secret_path_fails_before_anything_else(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER})
    entry.add_to_hass(hass)

    with pytest.raises(es.EmbeddedServerError, match="secret path missing"):
        await es.EmbeddedServerManager(hass, entry).async_start()

    assert package_index.calls == []


@pytest.mark.parametrize(
    ("version", "message"),
    [
        (
            "2025.9.4",
            rf"requires Home Assistant {re.escape(MIN_EMBEDDED_HOME_ASSISTANT_VERSION)} or newer",
        ),
        ("custom-build", "could not determine whether Home Assistant custom-build"),
    ],
    ids=["too old", "not a version"],
)
async def test_an_unsupported_home_assistant_is_refused_before_the_install(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    message: str,
) -> None:
    monkeypatch.setattr(es, "HA_VERSION", version)
    pin(hass, f"{STABLE}=={INDEX_VERSION}")
    mgr, _entry = await new_manager(hass)

    with pytest.raises(es.EmbeddedServerError, match=message) as err:
        await mgr.async_start()

    # Not "package": that repair offers a reinstall, which cannot fix this.
    assert err.value.kind == "start"
    assert package_index.calls == []
    package_index.requirements.assert_not_awaited()


async def test_a_missing_credential_asks_for_one_and_creates_no_account(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Older releases created an administrator here; none is created now."""
    pin(hass, f"{STABLE}=={INDEX_VERSION}")
    package_index.dists[STABLE] = INDEX_VERSION
    users = len(await hass.auth.async_get_users())
    mgr, _entry = await new_manager(hass)

    with pytest.raises(es.EmbeddedServerError) as err:
        await mgr.async_start()

    assert err.value.kind == "token"
    assert len(await hass.auth.async_get_users()) == users


async def test_removing_the_entry_deletes_the_account_an_older_release_created(
    hass: HomeAssistant, hass_owner_user: MockUser
) -> None:
    user = await hass.auth.async_create_user(
        "HA-MCP server", group_ids=[GROUP_ID_ADMIN]
    )
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name="HA-MCP",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=365),
    )
    mgr, entry = await new_manager(
        hass,
        data={DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: refresh_token.id},
    )
    stored = dict(entry.data)

    await mgr.async_revoke_credentials()

    assert await hass.auth.async_get_user(user.id) is None
    assert hass.auth.async_get_refresh_token(refresh_token.id) is None
    # Home Assistant removed the entry first; writing to it would raise.
    assert dict(entry.data) == stored


async def test_removing_the_entry_leaves_a_pasted_token_valid(
    hass: HomeAssistant,
) -> None:
    """A token the user pasted belongs to them, who revokes it in their profile."""
    token = await issue_admin_token(hass)
    mgr, _entry = await new_manager(hass, data={DATA_ADMIN_TOKEN: token})

    await mgr.async_revoke_credentials()

    assert hass.auth.async_validate_access_token(token) is not None


# -- which ha-mcp code a start serves: the module purge (#1904) --------------


@pytest.mark.usefixtures("socket_enabled")
async def test_a_start_with_no_server_modules_cached_imports_them_fresh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [n for n in sys.modules if n == "ha_mcp" or n.startswith("ha_mcp.")]:
        monkeypatch.delitem(sys.modules, name)

    async with running_server(hass, options=OVERRIDE) as (mgr, _entry):
        assert mgr.is_running
        assert "ha_mcp.config" in sys.modules


@pytest.mark.usefixtures("socket_enabled")
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


@pytest.mark.usefixtures("socket_enabled")
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


@pytest.mark.usefixtures("socket_enabled")
async def test_an_override_replaces_the_cache_even_when_the_version_matches(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-pointed tarball or pin can change the code under the same version
    string, which a version-keyed skip would serve stale; without the purge an
    entry reload keeps serving the old code until Home Assistant restarts.
    ``ha_mcp_other`` is not a server module and stays."""
    async with running_server(hass):
        warm = generation()
    monkeypatch.setitem(sys.modules, "ha_mcp.stale_probe", ModuleType("probe"))
    other = ModuleType("ha_mcp_other")
    monkeypatch.setitem(sys.modules, "ha_mcp_other", other)

    async with running_server(hass, options=OVERRIDE):
        assert generation() is not warm
        assert "ha_mcp.stale_probe" not in sys.modules
        assert sys.modules["ha_mcp_other"] is other


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


@pytest.mark.usefixtures("socket_enabled")
async def test_a_worker_running_older_code_than_is_installed_asks_for_a_restart(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    await start_with_a_new_install_after_the_import(
        hass, package_index, caplog, installed_after="99.0.0"
    )

    assert f"running version {REAL_VERSION}" in caplog.text
    assert "version 99.0.0 is installed" in caplog.text
    assert RESTART_WARNING in caplog.text


@pytest.mark.usefixtures("socket_enabled")
async def test_a_worker_running_the_installed_code_is_quiet(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    await start_with_a_new_install_after_the_import(
        hass, package_index, caplog, installed_after=None
    )

    assert RESTART_WARNING not in caplog.text


@pytest.mark.usefixtures("socket_enabled")
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
