"""How the server package is installed, removed and read (#2146, #2135, #1891).

The command lines are the ones ``PackageIndex`` receives in place of ``uv``; the
start is the real ``EmbeddedServerManager.async_start`` on a real ``hass``,
stopped at the missing token once the package step has passed.
"""

from __future__ import annotations

import logging
import re
import site
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    DATA_LAST_PIP_SPEC,
    OPT_PIP_SPEC,
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
WHEELS_HOST = "wheels.home-assistant.io"


async def install_pin(
    hass: HomeAssistant, package_index: PackageIndex, **options: Any
) -> list[str]:
    """Start with nothing installed; return the install command line."""
    pin(hass, PIN)
    mgr, _ = await new_manager(hass, options=options)
    await package_step(mgr)
    return package_index.installs[0].args


def use_pip_kwargs(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    """Home Assistant's pip arguments for this Core version: ``config/deps`` or none."""
    monkeypatch.setattr(es, "pip_kwargs", lambda _config_dir: kwargs)


# -- the install command -----------------------------------------------------


async def test_an_index_spec_upgrades_only_its_own_distribution(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """uv's bare ``--upgrade`` re-resolves the whole graph and replaces packages
    Home Assistant ships (the #2135/#2146 torn-install window)."""
    args = await install_pin(hass, package_index)

    assert args[:6] == [sys.executable, "-m", "uv", "pip", "install", "--quiet"]
    assert flag_values(args, "--upgrade-package") == [STABLE]
    assert "--upgrade" not in args
    assert "--reinstall-package" not in args


async def test_a_forked_index_spec_is_upgraded_not_reinstalled(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Reinstalling an index requirement would uninstall-then-extract on every start."""
    package_index.dists[STABLE] = INDEX_VERSION
    pin(hass, PIN)
    mgr, _ = await new_manager(hass, options={OPT_PIP_SPEC: "my-fork==1.0"})

    await package_step(mgr)

    [install] = package_index.installs
    assert flag_values(install.args, "--upgrade-package") == ["my-fork"]
    assert "--reinstall-package" not in install.args


@pytest.mark.parametrize(
    ("spec", "scoped"),
    [
        ("https://example.invalid/ha_mcp.tar.gz", [STABLE, DEV]),
        (
            "https://example.invalid/ha_mcp_dev-8.7.0.dev3-py3-none-any.whl",
            [DEV, STABLE],
        ),
    ],
    ids=["unnamed archive", "dev wheel"],
)
async def test_a_url_without_a_name_is_reinstalled_for_every_known_distribution(
    hass: HomeAssistant, package_index: PackageIndex, spec: str, scoped: list[str]
) -> None:
    """A repository tarball installs as ``ha-mcp`` and a dev wheel as ``ha-mcp-dev``;
    guessing wrong has uv report success and leave the real package running."""
    args = await install_pin(hass, package_index, **{OPT_PIP_SPEC: spec})

    assert flag_values(args, "--reinstall-package") == scoped
    assert "--upgrade-package" not in args


async def test_home_assistants_constraints_and_download_timeout_are_threaded(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_pip_kwargs(monkeypatch, constraints="/hacons.txt", timeout=5)
    await install_pin(hass, package_index)

    [install] = package_index.installs
    assert flag_values(install.args, "--constraint") == ["/hacons.txt"]
    assert int(install.env["HTTP_TIMEOUT"]) > 5, "a cold install pulls the fastmcp tree"
    assert "--target" not in install.args


async def test_the_install_is_time_bounded(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """A wedged uv must not hold the install slot, and with it every next start."""
    await install_pin(hass, package_index)

    assert package_index.installs[0].kwargs["timeout"] > 0


async def test_a_dependency_target_directory_is_installed_into(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    use_pip_kwargs(monkeypatch, target=str(tmp_path))
    args = await install_pin(hass, package_index)

    assert flag_values(args, "--target") == [str(tmp_path)]


async def test_a_non_virtualenv_install_goes_to_the_user_site(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """uv has no ``--user``; the branch a real Home Assistant Core install takes."""
    use_pip_kwargs(monkeypatch)
    monkeypatch.setattr(es, "is_virtual_env", lambda: False)
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path))
    monkeypatch.delenv("UV_SYSTEM_PYTHON", raising=False)
    monkeypatch.delenv("UV_PYTHON", raising=False)
    args = await install_pin(hass, package_index)

    assert flag_values(args, "--python") == [sys.executable]
    assert flag_values(args, "--target") == [str(tmp_path)]


async def test_an_explicit_uv_python_selection_skips_the_user_site(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    use_pip_kwargs(monkeypatch)
    monkeypatch.setattr(es, "is_virtual_env", lambda: False)
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path))
    monkeypatch.setenv("UV_SYSTEM_PYTHON", "1")
    args = await install_pin(hass, package_index)

    assert "--target" not in args


@pytest.mark.parametrize(
    "outcome",
    [
        "exit status 2",
        subprocess.TimeoutExpired(cmd="uv", timeout=1),
        OSError("no uv"),
    ],
    ids=["non-zero exit", "timeout", "uv missing"],
)
async def test_a_failed_install_stops_the_start_with_a_package_error(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    outcome: str | BaseException,
) -> None:
    package_index.install_outcome = lambda _install: outcome
    pin(hass, PIN)
    mgr, entry = await new_manager(hass)

    await package_failure(mgr)

    assert DATA_LAST_PIP_SPEC not in entry.data
    assert "Could not install" in caplog.text


# -- an extra index that is down ----------------------------------------------


def _is_wheels_host(url: str) -> bool:
    try:
        return urlparse(url).hostname == WHEELS_HOST
    except ValueError:
        return False


def _fail_on_wheels_host(install: Any) -> str | None:
    hosts = install.env.get("UV_EXTRA_INDEX_URL", "").split()
    if any(_is_wheels_host(url) for url in hosts):
        return f"error: failed to fetch https://{WHEELS_HOST}/x"
    return None


async def test_a_failing_extra_index_host_is_dropped_and_retried(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv treats a failing extra index as fatal where pip skips it, so a wheels
    outage must not fail an install PyPI can satisfy."""
    monkeypatch.setenv(
        "UV_EXTRA_INDEX_URL",
        f"https://{WHEELS_HOST}/simple https://healthy.example/simple",
    )
    package_index.install_outcome = _fail_on_wheels_host
    await install_pin(hass, package_index)

    first, retry = package_index.installs
    assert WHEELS_HOST in first.env["UV_EXTRA_INDEX_URL"]
    assert retry.env["UV_EXTRA_INDEX_URL"] == "https://healthy.example/simple"


async def test_a_sole_failing_extra_index_is_removed_entirely(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", f"https://{WHEELS_HOST}/simple")
    package_index.install_outcome = _fail_on_wheels_host
    await install_pin(hass, package_index)

    assert len(package_index.installs) == 2
    assert "UV_EXTRA_INDEX_URL" not in package_index.installs[1].env


async def test_a_malformed_extra_index_url_does_not_hide_the_install_failure(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd index URL is only parsed after an install failed; it must not
    replace uv's stderr with a traceback, and it is kept in the retry."""
    monkeypatch.setenv(
        "UV_EXTRA_INDEX_URL", f"https://[bad/simple https://{WHEELS_HOST}/simple"
    )
    package_index.install_outcome = _fail_on_wheels_host
    await install_pin(hass, package_index)

    retry = package_index.installs[1].env["UV_EXTRA_INDEX_URL"]
    assert WHEELS_HOST not in retry
    assert "https://[bad/simple" in retry


async def test_a_failure_that_names_no_extra_index_is_not_retried(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://wheels.example/simple")
    package_index.install_outcome = lambda _install: "No matching distribution"
    pin(hass, PIN)
    mgr, _ = await new_manager(hass)

    await package_failure(mgr)

    assert len(package_index.installs) == 1


# -- the uninstall command ---------------------------------------------------


async def test_the_other_distribution_is_uninstalled_from_this_interpreter(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_pip_kwargs(monkeypatch)
    pin(hass, PIN)
    package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev1"})
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    [args] = package_index.uninstall_args
    assert args == [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "uninstall",
        "--python",
        sys.executable,
        DEV,
    ]


async def test_the_uninstall_targets_the_dependency_directory_the_install_uses(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_pip_kwargs(monkeypatch, target="/config/deps")
    pin(hass, PIN)
    package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev1"})
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    [args] = package_index.uninstall_args
    assert flag_values(args, "--target") == ["/config/deps"]
    assert "--python" not in args


@pytest.mark.parametrize(
    "outcome", ["exit status 1", OSError("no uv")], ids=["non-zero exit", "uv missing"]
)
async def test_a_failed_removal_of_the_other_distribution_does_not_stop_the_install(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    outcome: str | BaseException,
) -> None:
    pin(hass, PIN)
    package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev1"})
    package_index.uninstall_outcome = lambda _dist: outcome
    mgr, _ = await new_manager(hass)

    await package_step(mgr)

    assert [i.spec for i in package_index.installs] == [PIN]
    assert "Could not uninstall" in caplog.text or "Uninstall of" in caplog.text


# -- a package Home Assistant does not manage (skip_pip) ---------------------


async def test_skip_pip_uses_a_compatible_externally_managed_package(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Every package mutation is bypassed and the stored markers are kept."""
    pin(hass, PIN)
    package_index.dists[STABLE] = "7.12.1"
    hass.config.skip_pip = True
    data = {DATA_LAST_PIP_SPEC: "ha-mcp==7.11.0"}
    mgr, entry = await new_manager(
        hass, options={OPT_PIP_SPEC: "ha-mcp==99.0.0"}, data=data
    )
    stored = dict(entry.data)

    await package_step(mgr)

    assert package_index.installs == []
    assert package_index.uninstalls == []
    package_index.requirements.assert_not_awaited()
    assert dict(entry.data) == stored


@pytest.mark.parametrize(
    ("dists", "message"),
    [
        ({DEV: "7.12.1.dev1"}, r"expects ha-mcp, but only ha-mcp-dev"),
        (
            {},
            rf"skip_pip.*system package manager.*{re.escape(es.MIN_EMBEDDED_SERVER_VERSION)}",
        ),
        (
            {STABLE: "7.9.0"},
            rf"externally managed ha-mcp 7\.9\.0.*{re.escape(es.MIN_EMBEDDED_SERVER_VERSION)} or newer",
        ),
        (
            {STABLE: "7.12.1", DEV: "7.13.0.dev1"},
            r"Both ha-mcp 7\.12\.1 and ha-mcp-dev 7\.13\.0\.dev1",
        ),
    ],
    ids=["other distribution", "missing", "too old", "both installed"],
)
async def test_skip_pip_names_what_the_system_package_manager_must_fix(
    hass: HomeAssistant,
    package_index: PackageIndex,
    dists: dict[str, str],
    message: str,
) -> None:
    pin(hass, PIN)
    package_index.dists.update(dists)
    hass.config.skip_pip = True
    mgr, _ = await new_manager(hass)

    err = await package_failure(mgr)

    assert re.search(message, str(err))
    assert package_index.installs == []


# -- an import system that breaks on Python 3.14 (#1891, #1985) --------------

_EDITABLE_KEY = "__editable__.homeassistant-2026.7.2.finder.__path_hook__"


async def start_with_cache_sweep(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    sweep: Any,
    cache_entries: dict[str, Any] | None = None,
) -> None:
    pin(hass, PIN)
    package_index.dists[STABLE] = INDEX_VERSION
    cache = {**sys.path_importer_cache, **(cache_entries or {})}
    monkeypatch.setattr(sys, "path_importer_cache", cache)
    monkeypatch.setattr("importlib.invalidate_caches", sweep)
    mgr, _ = await new_manager(hass)
    await package_step(mgr)


async def test_a_broken_cache_sweep_is_completed_after_pruning_the_stale_entries(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CPython 3.14 raises ``KeyError`` on the editable finder's placeholder, which
    aborted every version check and with it the bring-up."""
    calls: list[int] = []

    def sweep() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise KeyError(_EDITABLE_KEY)

    stale = {
        "/abs/deps/dir": object(),
        _EDITABLE_KEY: None,
        "": None,
        "rel/not/absolute": object(),
    }
    await start_with_cache_sweep(hass, package_index, monkeypatch, sweep, stale)

    assert "pruning stale" in caplog.text
    assert _EDITABLE_KEY not in sys.path_importer_cache
    assert "" not in sys.path_importer_cache
    assert "rel/not/absolute" not in sys.path_importer_cache
    assert "/abs/deps/dir" in sys.path_importer_cache


async def test_a_working_cache_sweep_logs_nothing(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=es.__name__)

    await start_with_cache_sweep(hass, package_index, monkeypatch, lambda: None)

    assert "invalidate_caches" not in caplog.text


async def test_a_second_cache_sweep_failure_does_not_stop_the_start(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A concurrent import can re-add the placeholder between prune and retry."""

    def sweep() -> None:
        raise KeyError(_EDITABLE_KEY)

    await start_with_cache_sweep(hass, package_index, monkeypatch, sweep)

    assert "still raised KeyError" in caplog.text


async def test_any_other_cache_sweep_failure_is_not_swallowed(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sweep() -> None:
        raise RuntimeError("unrelated import-system failure")

    with pytest.raises(RuntimeError, match="unrelated import-system failure"):
        await start_with_cache_sweep(hass, package_index, monkeypatch, sweep)
