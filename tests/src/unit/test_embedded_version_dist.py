"""Which distribution the in-process server reads its installed version from (#2765).

An override can leave another server distribution's metadata beside the one it
installed: ``ha-mcp`` beside an ``ha-mcp-dev`` override when the conflicting
uninstall fails, and always the manifest pin's distribution beside an override
under another name, which skips that uninstall. Bring-up, the post-start
staleness check and the worker's recorded version must all read the
distribution the spec installs. The ``ha-mcp-dev`` cases are covered in
``test_embedded_server.py`` (``TestWarmCacheVersionAgreement``,
``TestRunningVersionStalenessWarning``, ``TestServeRunningVersionCapture``).
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install, no_direct_listener_helpers  # noqa: F401

install()

import custom_components.ha_mcp_tools.embedded_server as es  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_LAST_PIP_SPEC,
    DATA_SECRET_PATH,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    OPT_PIP_SPEC,
    OPT_SERVER_URL,
)
from custom_components.ha_mcp_tools.spec_dist import version_dist  # noqa: E402

from .test_embedded_server import _manager, _stub_ha_mcp_surface  # noqa: E402

OTHER_DIST = "acme-ha-mcp"
OTHER_SPEC = f"{OTHER_DIST} @ file:///pkg.whl"
# The manifest pin stays installed beside the override: ha-mcp, or ha-mcp-dev
# for a pre-release component. ha_mcp.__version__ reads only those two, so it
# reports the pin's version.
PIN_VERSION = "8.7.0"
OVERRIDE_VERSION = "9.0.0"
pin_dists = pytest.mark.parametrize("pin_dist", [DIST_NAME_STABLE, DIST_NAME_DEV])
SERVER_FILES = [importlib.metadata.PackagePath("ha_mcp/__init__.py")]


def _dist(
    files: list[importlib.metadata.PackagePath] | None = None,
    top_level: str | None = None,
) -> SimpleNamespace:
    """An installed distribution's file list and ``top_level.txt``."""
    return SimpleNamespace(
        files=files,
        read_text=lambda name: top_level if name == "top_level.txt" else None,
    )


@pytest.fixture(autouse=True)
def dists(monkeypatch: pytest.MonkeyPatch) -> dict[str, SimpleNamespace]:
    """Fake installed distributions; the override ships the server."""
    fakes = {OTHER_DIST: _dist(SERVER_FILES)}
    real = importlib.metadata.distribution

    def distribution(name: str) -> object:
        return fakes[name] if name in fakes else real(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    return fakes


def _metadata(versions: dict[str, str]) -> Callable[[str | None], str]:
    """``importlib.metadata.version`` over ``versions``; others are absent."""

    def version(name: str | None) -> str:
        if not name:  # as the real one: a name is required
            raise ValueError("A distribution name is required.")
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    return version


@pytest.mark.parametrize(
    ("spec", "dist"),
    [
        ("HA_MCP_Dev==8.7.0.dev3", DIST_NAME_DEV),
        (OTHER_SPEC, OTHER_DIST),
        ("file:///config/acme_ha_mcp-1.0-py3-none-any.whl#sha256=ab", OTHER_DIST),
        # A repository tarball names no distribution and installs ha-mcp.
        ("https://example/t.tgz", DIST_NAME_STABLE),
        ("https://github.com/o/r/archive/refs/pull/12/head.tar.gz", DIST_NAME_STABLE),
        ("https://github.com/o/r/archive/refs/heads/fix-2427.tar.gz", DIST_NAME_STABLE),
        # An sdist names its distribution (PEP 625), like a wheel (#2787).
        ("https://files.example/ha_mcp_dev-8.8.0.dev3.tar.gz", DIST_NAME_DEV),
    ],
)
def test_version_is_read_from_what_the_spec_installs(spec: str, dist: str) -> None:
    assert version_dist(spec) == dist


@pytest.mark.parametrize(
    ("preferred", "expected"),
    [(OTHER_DIST, OVERRIDE_VERSION), (None, PIN_VERSION)],
    ids=["override-masked-by-the-pin", "no-preference-reads-stable-first"],
)
def test_installed_version_is_not_masked_by_leftover_metadata(
    monkeypatch: pytest.MonkeyPatch, preferred: str | None, expected: str
) -> None:
    monkeypatch.setattr(es.importlib.util, "find_spec", lambda name: object())
    versions = {
        DIST_NAME_STABLE: PIN_VERSION,
        DIST_NAME_DEV: "8.7.0.dev1",
        OTHER_DIST: OVERRIDE_VERSION,
    }
    monkeypatch.setattr(importlib.metadata, "version", _metadata(versions))
    assert es._installed_ha_mcp_version(preferred) == expected


@pytest.mark.parametrize(
    "dist",
    [
        _dist([importlib.metadata.PackagePath("ha_mcp_client/__init__.py")]),
        _dist(None),
        _dist(SERVER_FILES, top_level="acme\n"),
        None,
    ],
    ids=["other-package", "no-record", "declares-another-package", "not-installed"],
)
def test_override_without_ha_mcp_is_not_read_as_the_server(
    monkeypatch: pytest.MonkeyPatch,
    dists: dict[str, SimpleNamespace],
    dist: SimpleNamespace | None,
) -> None:
    # An override that is not the server, or not installed, ships no ha_mcp; the
    # manifest pin keeps serving, so its version is the installed and running one.
    versions = {DIST_NAME_STABLE: PIN_VERSION, OTHER_DIST: OVERRIDE_VERSION}
    if dist is None:
        del dists[OTHER_DIST], versions[OTHER_DIST]
    else:
        dists[OTHER_DIST] = dist
    monkeypatch.setattr(es.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(importlib.metadata, "version", _metadata(versions))
    fake = ModuleType("ha_mcp")
    fake.__version__ = PIN_VERSION
    monkeypatch.setitem(sys.modules, "ha_mcp", fake)

    assert es._installed_ha_mcp_version(OTHER_DIST) == PIN_VERSION
    assert es._running_ha_mcp_version(OTHER_DIST) == PIN_VERSION


def test_override_declaring_ha_mcp_is_read_without_its_files(
    monkeypatch: pytest.MonkeyPatch, dists: dict[str, SimpleNamespace]
) -> None:
    # An editable install lists no package files; its top_level.txt names ha_mcp.
    dists[OTHER_DIST] = _dist(None, top_level="ha_mcp\n")
    monkeypatch.setattr(es.importlib.util, "find_spec", lambda name: object())
    versions = {DIST_NAME_STABLE: PIN_VERSION, OTHER_DIST: OVERRIDE_VERSION}
    monkeypatch.setattr(importlib.metadata, "version", _metadata(versions))

    assert es._installed_ha_mcp_version(OTHER_DIST) == OVERRIDE_VERSION


@pytest.mark.parametrize(
    ("installed", "warns"),
    [(_dist(SERVER_FILES), False), (_dist(None), True), (None, False)],
    ids=["ships-the-server", "ships-no-server", "not-installed"],
)
async def test_bring_up_warns_when_the_override_installs_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dists: dict[str, SimpleNamespace],
    installed: SimpleNamespace | None,
    warns: bool,
) -> None:
    if installed is None:
        del dists[OTHER_DIST]
    else:
        dists[OTHER_DIST] = installed
    mgr, _hass, _entry = _manager(
        tmp_path,
        options={OPT_PIP_SPEC: OTHER_SPEC},
        data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: OTHER_SPEC},
    )
    monkeypatch.setattr(es, "_force_install_package", MagicMock(return_value=True))
    monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
    monkeypatch.setattr(
        es, "_installed_ha_mcp_version", lambda preferred_dist=None: PIN_VERSION
    )
    monkeypatch.setattr(es, "_installed_dist_version", lambda name: None)
    monkeypatch.setattr(es, "_dist_installed", lambda name: name in dists)
    monkeypatch.setattr(es, "_uninstall_distribution", MagicMock(return_value=False))

    with caplog.at_level("WARNING"):
        await mgr._async_ensure_package()

    assert ("installs no ha_mcp package" in caplog.text) is warns


@pin_dists
async def test_bring_up_and_worker_versions_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_dist: str
) -> None:
    mgr, _hass, _entry = _manager(
        tmp_path,
        options={OPT_PIP_SPEC: OTHER_SPEC},
        data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: OTHER_SPEC},
    )
    versions = {pin_dist: PIN_VERSION, OTHER_DIST: OVERRIDE_VERSION}

    def installed_version(preferred_dist: str | None = None) -> str | None:
        return versions.get(preferred_dist or DIST_NAME_STABLE) or versions.get(
            pin_dist
        )

    monkeypatch.setattr(es, "_force_install_package", MagicMock(return_value=True))
    monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
    monkeypatch.setattr(es, "_installed_ha_mcp_version", installed_version)
    monkeypatch.setattr(es, "_installed_dist_version", versions.get)
    monkeypatch.setattr(es, "_dist_installed", lambda name: False)
    monkeypatch.setattr(es, "_uninstall_distribution", MagicMock(return_value=False))
    fake = ModuleType("ha_mcp")
    fake.__version__ = PIN_VERSION
    monkeypatch.setitem(sys.modules, "ha_mcp", fake)

    ready_version = await mgr._async_ensure_package()

    # Read before the minimum-version check, which then checks this version.
    assert ready_version == OVERRIDE_VERSION
    assert es._running_ha_mcp_version(OTHER_DIST) == ready_version


async def test_no_false_restart_warning_for_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        es, "_async_paired_server_requirement", AsyncMock(return_value="ha-mcp==8.7.0")
    )
    mgr, _hass, _entry = _manager(tmp_path, options={OPT_PIP_SPEC: OTHER_SPEC})
    monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
    monkeypatch.setattr(mgr, "_async_access_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
    # The real purge would drop every live ha_mcp module in this process.
    monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
    monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
    versions = {DIST_NAME_STABLE: PIN_VERSION, OTHER_DIST: OVERRIDE_VERSION}
    monkeypatch.setattr(
        es,
        "_installed_ha_mcp_version",
        lambda preferred_dist=None: versions[preferred_dist or DIST_NAME_STABLE],
    )

    def _ready_with_current_worker() -> None:
        mgr._running_version = OVERRIDE_VERSION

    monkeypatch.setattr(
        mgr,
        "_async_wait_until_ready",
        AsyncMock(side_effect=_ready_with_current_worker),
    )

    with caplog.at_level("WARNING"):
        await mgr.async_start()
    if mgr._thread is not None:
        mgr._thread.join(timeout=2)

    assert "restart Home Assistant" not in caplog.text


@pytest.fixture
def _isolate_env() -> Iterator[None]:
    # _thread_main stages these into os.environ; restore them afterwards.
    keys = ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for key, value in saved.items():
        os.environ.pop(key, None)
        if value is not None:
            os.environ[key] = value


@pin_dists
@pytest.mark.usefixtures("_isolate_env")
def test_worker_records_the_override_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_dist: str
) -> None:
    mgr, _hass, _entry = _manager(
        tmp_path,
        options={OPT_PIP_SPEC: OTHER_SPEC, OPT_SERVER_URL: "http://ha.local:8123"},
    )

    class _StopServe(Exception):
        pass

    class _FakeMcp:
        def http_app(self, path: str, stateless_http: bool) -> None:
            raise _StopServe

    _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
    sys.modules["ha_mcp"].__version__ = PIN_VERSION
    versions = {pin_dist: PIN_VERSION, OTHER_DIST: OVERRIDE_VERSION}
    monkeypatch.setattr(es, "_installed_dist_version", versions.get)

    mgr._thread_main("tok")

    assert mgr._running_version == OVERRIDE_VERSION
    assert isinstance(mgr._thread_exc, _StopServe)
