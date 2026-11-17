"""Which server package the in-process server runs, and when it is replaced (#1527).

Each test starts the manager on a real ``hass`` and a real server entry that has
no credential, so a start that gets through the package step fails at the token
step (``package_step``). ``PackageIndex`` plays ``uv`` and the installed
distributions; the manifest requirement the component pins is set with ``pin``.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    CHANNEL_DEV,
    DATA_LAST_PIP_SPEC,
    DATA_REINSTALL_REQUESTED,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
)

from ._embedded_server_core import (
    DEV,
    INDEX_VERSION,
    STABLE,
    PackageIndex,
    flag_values,
    new_manager,
    package_failure,
    package_step,
    pin,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import package_index as package_index

pytestmark = pytest.mark.usefixtures("embedded_server_env")

PIN = f"{STABLE}=={INDEX_VERSION}"
TARBALL = "https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1/head.tar.gz"
WHEEL = f"{STABLE} @ file:///config/ha_mcp-{INDEX_VERSION}-py3-none-any.whl"
DEV_PIN = f"{DEV}==8.7.0.dev3"


async def test_the_port_option_is_the_port_the_server_listens_on(
    hass: HomeAssistant,
) -> None:
    custom, _ = await new_manager(hass, options={OPT_SERVER_PORT: 9999})
    default, _ = await new_manager(hass)

    assert custom.port == 9999
    assert default.port == DEFAULT_SERVER_PORT
    assert not default.is_running


# -- which requirement is installed ------------------------------------------


async def test_the_pip_override_wins_over_the_manifest_pin(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: "ha-mcp==8.5.0"})

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == ["ha-mcp==8.5.0"]


async def test_the_manifest_pin_is_installed_without_an_override(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [PIN]


async def test_the_default_pip_spec_is_not_an_override(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: DEFAULT_PIP_SPEC})

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [PIN]


async def test_a_manifest_without_a_server_pin_installs_the_bare_distribution(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pin(hass)
    mgr, entry = await new_manager(hass)

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [DEFAULT_PIP_SPEC]
    assert entry.data[DATA_LAST_PIP_SPEC] == DEFAULT_PIP_SPEC
    assert "manifest pins no ha-mcp server" in caplog.text


async def test_the_retired_channel_option_is_ignored(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[DEV] = "8.6.0.dev2893"
    mgr, _ = await new_manager(
        hass, options={"channel": CHANNEL_DEV, "auto_update": False}
    )

    await package_step(mgr)

    assert package_index.dists == {STABLE: INDEX_VERSION}


@pytest.mark.parametrize(
    ("requirements", "expected"),
    [
        (["mcp>=1.24.0", PIN], PIN),
        (["ha_mcp_dev==8.7.0.dev3"], "ha_mcp_dev==8.7.0.dev3"),
        (["mcp>=1.24.0"], DEFAULT_PIP_SPEC),
        (["not a requirement ==", PIN], PIN),
    ],
    ids=["among others", "dev distribution", "none", "after an unparseable one"],
)
async def test_the_server_requirement_is_read_from_the_manifest(
    hass: HomeAssistant,
    package_index: PackageIndex,
    requirements: list[str],
    expected: str,
) -> None:
    pin(hass, *requirements)
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [expected]


@pytest.mark.parametrize(
    ("spec", "removed"),
    [
        (PIN, DEV),
        (DEV_PIN, STABLE),
        ("ha-mcp @ file:///config/w.whl", DEV),
        ("https://files.example/ha_mcp_dev-8.7.0.dev3-py3-none-any.whl", STABLE),
        ("file:///config/ha_mcp-8.6.0-py3-none-any.whl", DEV),
        ("https://example/t.tgz", None),
        ("acme-ha-mcp==1.0", None),
    ],
    ids=[
        "stable pin",
        "dev pin",
        "named url",
        "dev wheel url",
        "stable wheel url",
        "unnamed archive",
        "another distribution",
    ],
)
async def test_the_other_server_distribution_is_removed_when_both_are_installed(
    hass: HomeAssistant,
    package_index: PackageIndex,
    spec: str,
    removed: str | None,
) -> None:
    """Both distributions own the ``ha_mcp`` files, so one must go."""
    package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev1"})
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: spec})

    await package_step(mgr)

    assert package_index.uninstalls == ([removed] if removed else [])


# -- why an install happens --------------------------------------------------

_REASONS: list[Any] = [
    pytest.param(
        {STABLE: "8.6.0"},
        {OPT_PIP_SPEC: "acme-ha-mcp==1.0"},
        {},
        "names no known server distribution",
        id="not a server distribution",
    ),
    pytest.param({}, {}, {}, "no importable ha_mcp package", id="nothing installed"),
    pytest.param(
        {DEV: "8.6.0.dev1"}, {}, {}, "ha-mcp is not installed", id="target missing"
    ),
    pytest.param(
        {STABLE: "8.6.0", DEV: "8.6.0.dev1"},
        {},
        {},
        "ha-mcp-dev is installed beside ha-mcp",
        id="other distribution beside",
    ),
    pytest.param(
        {STABLE: "8.6.0"},
        {OPT_PIP_SPEC: WHEEL},
        {},
        "is a direct URL, which always installs",
        id="direct url",
    ),
    pytest.param(
        {STABLE: "8.5.0"},
        {},
        {},
        f"8.5.0 does not satisfy '{PIN}'",
        id="pin not satisfied",
    ),
    pytest.param(
        {STABLE: "7.9.0"},
        {OPT_PIP_SPEC: "ha-mcp>=7.0"},
        {DATA_LAST_PIP_SPEC: "ha-mcp>=7.0"},
        "is older than",
        id="build too old",
    ),
    pytest.param(
        {STABLE: "8.6.0"},
        {},
        {DATA_LAST_PIP_SPEC: TARBALL},
        f"the spec changed from {TARBALL!r}",
        id="source changed",
    ),
    pytest.param(
        {STABLE: "8.6.0"},
        {},
        {DATA_REINSTALL_REQUESTED: True},
        "a reinstall was requested",
        id="repair asked",
    ),
]


@pytest.mark.parametrize(("dists", "options", "data", "reason"), _REASONS)
async def test_the_install_log_names_the_first_reason(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    dists: dict[str, str],
    options: dict[str, Any],
    data: dict[str, Any],
    reason: str,
) -> None:
    caplog.set_level(logging.INFO, logger=es.__name__)
    pin(hass, PIN)
    package_index.dists.update(dists)
    mgr, _ = await new_manager(hass, options=options, data=data)

    await package_step(mgr)

    assert reason in caplog.text


@pytest.mark.parametrize("stored", [None, PIN, "ha-mcp"])
async def test_a_satisfied_pin_installs_nothing(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    stored: str | None,
) -> None:
    caplog.set_level(logging.INFO, logger=es.__name__)
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(
        hass, data={DATA_LAST_PIP_SPEC: stored} if stored else None
    )

    await package_step(mgr)

    assert package_index.installs == []
    assert "Reinstalling" not in caplog.text


# -- the paired pin ----------------------------------------------------------


async def test_a_pin_home_assistant_already_installed_needs_no_install(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, entry = await new_manager(hass)

    await package_step(mgr)

    assert package_index.installs == []
    assert package_index.uninstalls == []
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_an_unchanged_pin_writes_nothing_to_the_entry(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(hass, data={DATA_LAST_PIP_SPEC: PIN})
    writes = MagicMock(wraps=hass.config_entries.async_update_entry)
    monkeypatch.setattr(hass.config_entries, "async_update_entry", writes)

    await package_step(mgr)

    assert package_index.installs == []
    writes.assert_not_called()


async def test_the_bare_spec_a_2x_install_stored_counts_as_satisfied(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, entry = await new_manager(hass, data={DATA_LAST_PIP_SPEC: "ha-mcp"})

    await package_step(mgr)

    assert package_index.installs == []
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_an_unsatisfied_pin_upgrades_only_the_servers_own_distribution(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = "8.5.0"
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    [install] = package_index.installs
    assert install.spec == PIN
    assert flag_values(install.args, "--upgrade-package") == [STABLE]
    assert "--reinstall-package" not in install.args
    assert package_index.dists[STABLE] == INDEX_VERSION


async def test_the_other_distribution_beside_the_pin_is_removed_then_reinstalled(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Removing it deletes the files the pinned build owns too (Core 2026.10.0b0).

    An upgrade of a version that already matches would install nothing and leave
    ``No module named 'ha_mcp.config'``, so the pin is reinstalled.
    """
    pin(hass, PIN)
    package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev2893"})
    mgr, entry = await new_manager(hass, data={DATA_LAST_PIP_SPEC: DEV})

    await package_step(mgr)

    assert package_index.calls == [f"uninstall {DEV}", "install"]
    assert package_index.sources[STABLE] == PIN
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_an_override_on_the_other_distribution_removes_the_pin_distribution(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: DEV_PIN})

    await package_step(mgr)

    assert package_index.uninstalls == [STABLE]
    assert package_index.dists == {DEV: "8.7.0.dev3"}


async def test_an_index_override_the_installed_version_satisfies_needs_no_install(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = "8.5.0"
    mgr, _ = await new_manager(
        hass,
        options={OPT_PIP_SPEC: "ha-mcp==8.5.0"},
        data={DATA_LAST_PIP_SPEC: "ha-mcp==8.5.0"},
    )

    await package_step(mgr)

    assert package_index.installs == []


async def test_an_override_is_applied_again_after_core_restored_the_pin(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Home Assistant reinstalls the manifest pin at boot, over the override."""
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(
        hass,
        options={OPT_PIP_SPEC: "ha-mcp==8.5.0"},
        data={DATA_LAST_PIP_SPEC: "ha-mcp==8.5.0"},
    )

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == ["ha-mcp==8.5.0"]
    assert package_index.dists[STABLE] == "8.5.0"


# -- a requested reinstall ---------------------------------------------------


async def test_a_requested_reinstall_replaces_a_satisfied_pin_once(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """The package repair's fix: a dependency was downgraded under the pin."""
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    data = {DATA_LAST_PIP_SPEC: PIN, DATA_REINSTALL_REQUESTED: True}
    mgr, entry = await new_manager(hass, data=data)

    await package_step(mgr)

    [install] = package_index.installs
    assert flag_values(install.args, "--reinstall-package") == [STABLE]
    assert DATA_REINSTALL_REQUESTED not in entry.data
    await package_step(mgr)
    assert len(package_index.installs) == 1


async def test_a_failed_requested_reinstall_is_not_retried_every_start(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    package_index.install_outcome = lambda _install: "no matching distribution"
    data = {DATA_LAST_PIP_SPEC: PIN, DATA_REINSTALL_REQUESTED: True}
    mgr, entry = await new_manager(hass, data=data)

    await package_failure(mgr)
    assert DATA_REINSTALL_REQUESTED not in entry.data
    await package_step(mgr)

    assert len(package_index.installs) == 1


# -- a changed source --------------------------------------------------------


async def test_a_url_override_is_reinstalled_on_every_start(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(
        hass, options={OPT_PIP_SPEC: WHEEL}, data={DATA_LAST_PIP_SPEC: WHEEL}
    )

    await package_step(mgr)

    [install] = package_index.installs
    assert install.spec == WHEEL
    assert flag_values(install.args, "--reinstall-package") == [STABLE]
    assert package_index.uninstalls == []


async def test_clearing_a_url_override_replaces_the_build_of_the_same_version(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Issue #1914: a PR tarball installs with the release's own version string,
    so the pin reads as satisfied by the PR code until that code is removed."""
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    package_index.sources[STABLE] = TARBALL
    mgr, entry = await new_manager(hass, data={DATA_LAST_PIP_SPEC: TARBALL})

    await package_step(mgr)

    assert package_index.calls == [f"uninstall {STABLE}", "install"]
    assert package_index.sources[STABLE] == PIN
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_a_replaced_source_that_cannot_be_removed_keeps_the_old_spec_stored(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Installing over it would report the new source satisfied and keep the old code."""
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    package_index.uninstall_outcome = lambda _dist: "permission denied"
    mgr, entry = await new_manager(hass, data={DATA_LAST_PIP_SPEC: TARBALL})

    await package_failure(mgr)

    assert package_index.installs == []
    assert entry.data[DATA_LAST_PIP_SPEC] == TARBALL


async def test_a_failed_uninstall_that_removed_the_distribution_still_installs(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """A timeout after the files went is not proof the distribution survived."""
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    package_index.uninstall_outcome = lambda _dist: "timed out"
    package_index.uninstall_leaves_dist_gone = True
    mgr, entry = await new_manager(hass, data={DATA_LAST_PIP_SPEC: TARBALL})

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [PIN]
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_moving_from_one_url_to_another_keeps_the_working_build_installed(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Removing it first would leave no server if the new URL cannot be fetched."""
    new_url = f"{STABLE} @ file:///config/ha_mcp-8.7.0-py3-none-any.whl"
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    mgr, _ = await new_manager(
        hass, options={OPT_PIP_SPEC: new_url}, data={DATA_LAST_PIP_SPEC: WHEEL}
    )

    await package_step(mgr)

    assert package_index.uninstalls == []
    assert [i.spec for i in package_index.installs] == [new_url]


@pytest.mark.parametrize(
    ("spec", "installed", "uninstalled_first"),
    [
        ("ha-mcp==8.1.0", "8.1.0+local", True),
        ("ha-mcp==8.1.0", "8.1.0", True),
        ("ha-mcp==8.1.0", "8.0.0", False),
        ("ha-mcp>=8.0.0", "8.1.0", True),
    ],
    ids=["local version label", "same version", "different version", "range"],
)
async def test_the_working_build_is_removed_first_unless_the_install_is_provably_real(
    hass: HomeAssistant,
    package_index: PackageIndex,
    spec: str,
    installed: str,
    uninstalled_first: bool,
) -> None:
    """A pin the installed build cannot satisfy cannot be skipped as satisfied, so
    removing the build first only loses the fallback; ``==1.0`` does match
    ``1.0+local``, so that build is removed."""
    package_index.dists[STABLE] = installed
    mgr, _ = await new_manager(
        hass, options={OPT_PIP_SPEC: spec}, data={DATA_LAST_PIP_SPEC: TARBALL}
    )

    await package_step(mgr)

    assert package_index.uninstalls == ([STABLE] if uninstalled_first else [])
    assert [i.spec for i in package_index.installs] == [spec]


# -- first install, failures -------------------------------------------------


async def test_an_install_failure_raises_and_records_nothing(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.install_outcome = lambda _install: "no matching distribution"
    mgr, entry = await new_manager(hass)

    await package_failure(mgr)

    assert DATA_LAST_PIP_SPEC not in entry.data


async def test_a_legacy_server_is_removed_before_the_pin_is_installed(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = "6.2.0"
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    assert package_index.calls == [f"uninstall {STABLE}", "install"]
    assert package_index.dists[STABLE] == INDEX_VERSION


async def test_a_legacy_version_left_by_the_installer_is_rejected(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    pin(hass)
    package_index.install_version = "6.2.0"
    mgr, entry = await new_manager(hass)

    err = await package_failure(mgr)

    assert "installed ha-mcp 6.2.0" in str(err)
    assert "custom_components.ha_mcp_tools.embedded_server" in str(err)
    assert DATA_LAST_PIP_SPEC not in entry.data


async def test_metadata_without_an_importable_package_is_not_an_install(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted channel switch can leave ``.dist-info`` while the files are gone."""
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> Any:
        return None if name == "ha_mcp" else real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    pin(hass, PIN)
    mgr, entry = await new_manager(hass)

    err = await package_failure(mgr)

    assert "not importable" in str(err)
    assert DATA_LAST_PIP_SPEC not in entry.data
