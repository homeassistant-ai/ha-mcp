"""Which distribution the in-process server reads its installed version from (#2765).

An override can leave another server distribution's metadata beside the one it
installed: always the manifest pin's distribution beside an override under
another name, which skips the conflicting uninstall. Bring-up, the post-start
staleness check and the worker's recorded version must all read the
distribution the spec installs. The ``ha-mcp-dev`` overrides are covered in
``test_embedded_server.py`` and ``test_embedded_server_package.py``.

The override ``acme-ha-mcp`` is a real ``.dist-info`` on ``sys.path``, so
whether it ships the server is decided by the installed metadata itself.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC
from custom_components.ha_mcp_tools.spec_dist import version_dist

from ._embedded_server_core import (
    DEV,
    REAL_VERSION,
    STABLE,
    PackageIndex,
    new_manager,
    package_step,
    pin,
    running_server,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import package_index as package_index

OTHER_DIST = "acme-ha-mcp"
OTHER_SPEC = f"{OTHER_DIST} @ file:///pkg.whl"
PIN_VERSION = "8.7.0"
OVERRIDE_VERSION = "9.0.0"
NO_SERVER_WARNING = "installs no ha_mcp package"
RESTART_WARNING = "restart Home Assistant"
PIN_DISTS = pytest.mark.parametrize("pin_dist", [STABLE, DEV])
# Name -> (files the distribution lists, its top_level.txt); None omits the file.
SERVER_SHAPES = {
    "lists-the-server-files": (["ha_mcp/__init__.py"], None),
    "editable-declares-ha-mcp": (None, "ha_mcp\n"),
}
NO_SERVER_SHAPES = {
    "lists-another-package": (["ha_mcp_client/__init__.py"], None),
    "lists-nothing": (None, None),
    "declares-another-package": (["ha_mcp/__init__.py"], "acme\n"),
}

InstallOverride = Callable[[tuple[list[str] | None, str | None]], None]


@pytest.fixture
def install_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> InstallOverride:
    """Install the override as ``(files, top_level)`` behind the real ``ha_mcp``."""

    def install(shape: tuple[list[str] | None, str | None]) -> None:
        files, top_level = shape
        info = tmp_path / f"acme_ha_mcp-{OVERRIDE_VERSION}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {OTHER_DIST}\nVersion: {OVERRIDE_VERSION}\n"
        )
        # Core's metadata reader lists only files that exist.
        for name in files or []:
            (tmp_path / name).parent.mkdir(exist_ok=True)
            (tmp_path / name).touch()
        if files is not None:
            (info / "RECORD").write_text("".join(f"{name},,\n" for name in files))
        if top_level is not None:
            (info / "top_level.txt").write_text(top_level)
        monkeypatch.setattr(sys, "path", [*sys.path, str(tmp_path)])
        importlib.invalidate_caches()

    return install


@pytest.mark.parametrize(
    ("spec", "dist"),
    [
        ("HA_MCP_Dev==8.7.0.dev3", DEV),
        (OTHER_SPEC, OTHER_DIST),
        ("file:///config/acme_ha_mcp-1.0-py3-none-any.whl#sha256=ab", OTHER_DIST),
        # A repository tarball names no distribution and installs ha-mcp.
        ("https://example/t.tgz", STABLE),
        ("https://github.com/o/r/archive/refs/pull/12/head.tar.gz", STABLE),
        ("https://github.com/o/r/archive/refs/heads/fix-2427.tar.gz", STABLE),
        # An sdist names its distribution (PEP 625), like a wheel (#2787).
        ("https://files.example/ha_mcp_dev-8.8.0.dev3.tar.gz", DEV),
    ],
)
def test_version_is_read_from_what_the_spec_installs(spec: str, dist: str) -> None:
    assert version_dist(spec) == dist


@PIN_DISTS
@pytest.mark.parametrize("shape", SERVER_SHAPES.values(), ids=list(SERVER_SHAPES))
@pytest.mark.usefixtures("embedded_server_env")
async def test_the_override_version_is_not_masked_by_the_pin_beside_it(
    hass: HomeAssistant,
    package_index: PackageIndex,
    install_override: InstallOverride,
    caplog: pytest.LogCaptureFixture,
    pin_dist: str,
    shape: tuple[list[str] | None, str | None],
) -> None:
    """Bring-up must report the override's build, not the pin's leftover metadata."""
    caplog.set_level(logging.INFO, logger=es.__name__)
    install_override(shape)
    package_index.dists[pin_dist] = PIN_VERSION
    package_index.install_version = OVERRIDE_VERSION
    pin(hass, f"{pin_dist}=={PIN_VERSION}")
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: OTHER_SPEC})

    await package_step(mgr)

    assert f"package ready (version {OVERRIDE_VERSION})" in caplog.text
    assert NO_SERVER_WARNING not in caplog.text


@PIN_DISTS
@pytest.mark.parametrize("shape", NO_SERVER_SHAPES.values(), ids=list(NO_SERVER_SHAPES))
@pytest.mark.usefixtures("embedded_server_env")
async def test_an_override_that_ships_no_server_leaves_the_pin_serving_with_a_warning(
    hass: HomeAssistant,
    package_index: PackageIndex,
    install_override: InstallOverride,
    caplog: pytest.LogCaptureFixture,
    pin_dist: str,
    shape: tuple[list[str] | None, str | None],
) -> None:
    """The pin's version is the one that runs, and the user is told the override
    is wrong; reading the override's version would report a server that is not."""
    caplog.set_level(logging.INFO, logger=es.__name__)
    install_override(shape)
    package_index.dists[pin_dist] = PIN_VERSION
    package_index.install_version = OVERRIDE_VERSION
    pin(hass, f"{pin_dist}=={PIN_VERSION}")
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: OTHER_SPEC})

    await package_step(mgr)

    assert f"package ready (version {PIN_VERSION})" in caplog.text
    assert NO_SERVER_WARNING in caplog.text


@pytest.mark.parametrize(
    ("pin_dist", "shape"),
    [
        (STABLE, SERVER_SHAPES["lists-the-server-files"]),
        (DEV, SERVER_SHAPES["editable-declares-ha-mcp"]),
        (STABLE, NO_SERVER_SHAPES["lists-another-package"]),
    ],
    ids=["stable pin, override ships server", "dev pin, editable", "no server shipped"],
)
@pytest.mark.usefixtures("embedded_server_env", "socket_enabled")
async def test_a_started_worker_runs_the_version_bring_up_installed(
    hass: HomeAssistant,
    package_index: PackageIndex,
    install_override: InstallOverride,
    caplog: pytest.LogCaptureFixture,
    pin_dist: str,
    shape: tuple[list[str] | None, str | None],
) -> None:
    """The worker imports the real ha-mcp, whose own version lookup only knows the
    pin's distributions; a restart warning here means it recorded the pin's
    version for an override build, or the override's for a build that is the pin."""
    caplog.set_level(logging.WARNING, logger=es.__name__)
    install_override(shape)
    package_index.dists[pin_dist] = REAL_VERSION
    package_index.install_version = OVERRIDE_VERSION
    pin(hass, f"{pin_dist}=={REAL_VERSION}")

    async with running_server(hass, options={OPT_PIP_SPEC: OTHER_SPEC}) as (mgr, _):
        assert mgr.is_running

    assert RESTART_WARNING not in caplog.text
