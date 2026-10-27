"""The container lanes' opt-in boot knobs must stay inert until they are set.

Measured on a slow PyPI window, the embedded lane's ``pip install`` of the
checkout wheel plus its dependency tree alone outlived the 480s API-ready gate
and killed the whole lane twice, while the same bring-up finishes in ~40s on a
warm network. Two knobs address that: ``HA_E2E_PIP_CACHE`` reuses a host pip
cache across runs, ``HA_E2E_READY_TIMEOUT`` raises the readiness budget.

Both are opt-in, so the property that matters as much as "the knob works" is
that an unset knob changes nothing: a lane with no environment set must build
the same container it always did. The assertions below therefore go through
``_build_ha_testcontainer`` and check the assembled mounts and entrypoint
rather than the helpers' own return values, and no Docker daemon is involved —
constructing a ``DockerContainer`` only records configuration.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer

from tests.src.e2e import _conftest_knobs, _conftest_testcontainer

_WHEEL = "ha_mcp-9.9.9-py3-none-any.whl"


@pytest.fixture(autouse=True)
def _unset_boot_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the stock, unconfigured lane."""
    monkeypatch.delenv(_conftest_knobs.PIP_CACHE_ENV_VAR, raising=False)
    monkeypatch.delenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, raising=False)


def _config_dir(tmp_path: Path, requirements: list[str]) -> Path:
    """A staged config dir with one component manifest, as the fixture stages it."""
    config = tmp_path / "config"
    component = config / "custom_components" / "ha_mcp_tools"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(json.dumps({"requirements": requirements}))
    return config


def _entrypoint_script(container: DockerContainer) -> str:
    return container._kwargs["entrypoint"][-1]


def _preinstalls(script: str) -> list[str]:
    """Every ``pip install`` in the entrypoint, from the install to the next segment.

    One per manifest-requirement attempt (PyPI-first plus the wheels-index
    fallback) and two for the embedded wheel (constraints file present or
    absent), so the list is what the flag check has to cover — a count would
    restate the current command layout instead.
    """
    return script.split("pip install ")[1:]


def _mount_targets(container: DockerContainer) -> dict[str, str]:
    return {str(host): mapping["bind"] for host, mapping in container.volumes.items()}


def _stub_container_tools(tmp_path: Path, pip_status: int) -> tuple[Path, Path]:
    """Stand-ins for the container's ``pip``/``chown``/``stat`` and their call log."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    calls = tmp_path / "calls.log"
    log = shlex.quote(str(calls))
    stubs = {
        "pip": f'#!/bin/sh\necho "pip $*" >> {log}\nexit {pip_status}\n',
        "chown": f'#!/bin/sh\necho "chown $*" >> {log}\nexit 0\n',
        "stat": "#!/bin/sh\necho 1000:1000\n",
    }
    for name, body in stubs.items():
        stub = stub_bin / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return stub_bin, calls


def _run_entrypoint(script: str, stub_bin: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", script],
        check=False,
        env={**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# HA_E2E_READY_TIMEOUT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("default", [1, 480, 3600])
def test_unset_timeout_knob_keeps_the_lane_budget(default: int) -> None:
    """An absent knob must not move any lane's budget."""
    assert _conftest_knobs.resolve_ready_timeout(default) == default


@pytest.mark.parametrize("raw", ["900", " 900 "])
def test_timeout_knob_raises_the_lane_budget(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, raw)
    assert _conftest_knobs.resolve_ready_timeout(480) == 900


@pytest.mark.parametrize("raw", ["abc", "480s", "480.5", "0", "-1", "", "   "])
def test_an_unusable_timeout_is_rejected_rather_than_ignored(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back to the default would reinstate the budget a slow run
    deliberately raised, and the lane would die looking like a slow boot."""
    monkeypatch.setenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, raw)
    with pytest.raises(ValueError, match=_conftest_knobs.READY_TIMEOUT_ENV_VAR):
        _conftest_knobs.resolve_ready_timeout(480)


def test_both_container_lanes_keep_their_historical_budgets() -> None:
    """The embedded lane pays for the wheel preinstall; the plain container does not.

    The 60s value is the plain container lane's long-standing gate — a change
    here would either halve the embedded lane's budget or hand the container
    lane a budget it never needed.
    """
    embedded = _conftest_testcontainer._container_api_ready_timeout(True)
    plain = _conftest_testcontainer._container_api_ready_timeout(False)

    assert embedded == _conftest_testcontainer._EMBEDDED_API_READY_TIMEOUT
    assert plain == 60
    assert embedded > plain


def test_timeout_knob_overrides_both_container_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob names the readiness gate, so it must not silently skip a lane."""
    monkeypatch.setenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, "1200")
    assert _conftest_testcontainer._container_api_ready_timeout(True) == 1200
    assert _conftest_testcontainer._container_api_ready_timeout(False) == 1200


# ---------------------------------------------------------------------------
# HA_E2E_PIP_CACHE
# ---------------------------------------------------------------------------


def test_unset_cache_knob_keeps_the_preinstall_ephemeral(tmp_path: Path) -> None:
    """Stock behaviour: one /config mount, and pip caches nothing."""
    config = _config_dir(tmp_path, ["ruamel.yaml"])

    container = _conftest_testcontainer._build_ha_testcontainer(config, True, _WHEEL)

    assert _mount_targets(container) == {str(config): "/config"}
    # Both preinstalls — the component manifest requirements and the checkout
    # wheel — must keep discarding their downloads.
    installs = _preinstalls(_entrypoint_script(container))
    assert len(installs) >= 2
    assert all(install.startswith("--no-cache-dir ") for install in installs)


def test_a_mounted_pip_cache_is_used_by_every_preinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, a reused run pays the full PyPI cost again every time."""
    config = _config_dir(tmp_path, ["ruamel.yaml"])
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))

    container = _conftest_testcontainer._build_ha_testcontainer(config, True, _WHEEL)

    # /root/.cache/pip is where pip inside the HA image resolves its cache: the
    # upstream image runs as root and sets neither PIP_CACHE_DIR nor
    # XDG_CACHE_HOME, so a mount elsewhere would be a directory pip never reads.
    assert _mount_targets(container) == {
        str(config): "/config",
        str(cache_dir): "/root/.cache/pip",
    }
    assert cache_dir.is_dir()
    installs = _preinstalls(_entrypoint_script(container))
    assert len(installs) >= 2
    # Dropping --no-cache-dir is not enough: the image's pip.conf sets
    # no-cache-dir = false, which pip disables the cache for just the same.
    assert all(
        install.startswith(f"--cache-dir {_conftest_knobs.PIP_CACHE_MOUNT_PATH} ")
        for install in installs
    )


def test_the_mounted_cache_is_claimed_for_root_and_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip as root only caches into a uid-0 directory; the host keeps its owner.

    The bind mount carries the invoking user's uid, so the entrypoint has to
    hand the directory to root before the first install and back afterwards —
    otherwise pip disables the cache, or the developer's directory is left
    undeletable.
    """
    config = _config_dir(tmp_path, ["ruamel.yaml"])
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))

    script = _entrypoint_script(
        _conftest_testcontainer._build_ha_testcontainer(config, True, _WHEEL)
    )

    claim = script.index("chown 0:0 /root/.cache/pip")
    first_install = script.index("pip install ")
    last_install = script.rindex("pip install ")
    hand_back = script.index('chown -R "$(stat -c %u:%g /config)" /root/.cache/pip')

    assert claim < first_install
    assert hand_back > last_install
    # Both are guarded so a file-sharing layer that refuses chown costs the
    # cache, not the lane.
    assert script.count("|| echo") >= 2


@pytest.mark.skipif(
    sys.platform == "win32", reason="the container lanes are POSIX-only"
)
def test_a_failed_preinstall_still_hands_the_cache_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install failure must not leave the host cache root-owned.

    ``chown`` only succeeds as root, so the failure path cannot be exercised
    for real here; the generated script runs against stub ``pip``/``chown``/
    ``stat`` binaries instead. The install fails, the hand-back still runs, and
    the install's status is what the container exits with.
    """
    config = _config_dir(tmp_path, ["ruamel.yaml"])
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))
    script = _entrypoint_script(
        _conftest_testcontainer._build_ha_testcontainer(config, False, None)
    )

    stub_bin, calls = _stub_container_tools(tmp_path, pip_status=3)
    result = _run_entrypoint(script, stub_bin)

    assert result.returncode == 3, result.stderr
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert invocations[0] == f"chown 0:0 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    assert invocations[-1] == (
        f"chown -R 1000:1000 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    )
    assert any(call.startswith("pip install ") for call in invocations)


@pytest.mark.skipif(
    sys.platform == "win32", reason="the container lanes are POSIX-only"
)
def test_a_successful_preinstall_still_boots_home_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing the cache back must not replace booting HA after a good install."""
    config = _config_dir(tmp_path, ["ruamel.yaml"])
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))
    script = _entrypoint_script(
        _conftest_testcontainer._build_ha_testcontainer(config, False, None)
    )
    stub_bin, calls = _stub_container_tools(tmp_path, pip_status=0)
    booted = tmp_path / "booted"
    init_stub = tmp_path / "init"
    init_stub.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(booted))}\nexit 7\n", encoding="utf-8"
    )
    init_stub.chmod(0o755)
    script = script.replace("exec /init", f"exec {shlex.quote(str(init_stub))}")

    result = _run_entrypoint(script, stub_bin)

    assert result.returncode == 7, result.stderr
    assert booted.exists()
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert invocations[-1] == (
        f"chown -R 1000:1000 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    )


def test_a_relative_cache_path_is_resolved_before_it_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker needs an absolute host path; a relative value must not stay one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, "pip-cache")

    mount = _conftest_knobs.pip_cache_mount()

    assert mount is not None
    host_dir, container_dir = mount
    assert host_dir == tmp_path / "pip-cache"
    assert host_dir.is_absolute()
    assert container_dir == _conftest_knobs.PIP_CACHE_MOUNT_PATH


def test_the_plain_container_lane_gets_the_cache_without_the_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plain container lane shares this builder; it caches, it has no wheel."""
    config = _config_dir(tmp_path, ["ruamel.yaml"])
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))

    container = _conftest_testcontainer._build_ha_testcontainer(config, False, None)

    assert _mount_targets(container) == {
        str(config): "/config",
        str(cache_dir): "/root/.cache/pip",
    }
    script = _entrypoint_script(container)
    installs = _preinstalls(script)
    assert len(installs) >= 1
    assert all(
        install.startswith(f"--cache-dir {_conftest_knobs.PIP_CACHE_MOUNT_PATH} ")
        for install in installs
    )
    assert _WHEEL not in script
