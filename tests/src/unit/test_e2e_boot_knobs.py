"""The container lanes' opt-in boot knobs must stay inert until they are set.

Measured on a slow PyPI window, the embedded lane's ``pip install`` of the
checkout wheel plus its dependency tree alone outlived the 480s API-ready gate
and killed the whole lane twice, while the same bring-up finishes in ~40s on a
warm network. Two knobs address that: ``HA_E2E_PIP_CACHE`` reuses a host pip
cache across runs, ``HA_E2E_READY_TIMEOUT`` raises the readiness budget.

Both are opt-in, so the property that matters as much as "the knob works" is
that an unset knob changes nothing: a lane with no environment set must build
the same container it always did. The tests below therefore run the session
fixture (``_e2e_session``) and check what it asked Docker for: the mounts and
the entrypoint of the container it created. The fixture's readiness gates ask
the unit tests' Home Assistant; only Docker is replaced.

``HA_E2E_BASE_HOST`` is the third knob and a different kind of setting: it
selects only the host in the testcontainer's ``base_url``, so a containerized
pytest process can reach the sibling container through the Docker host address
instead of its own ``localhost``. Its parsing is asserted directly; the fixture
using it is covered in ``test_e2e_session_fixture_cleanup.py``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from tests.src.e2e import _conftest_knobs

from ._core_client import refused_url
from ._e2e_session import Session, plain_container_session, running_home_assistant


@pytest.fixture(autouse=True)
def _unset_boot_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the stock, unconfigured lane."""
    monkeypatch.delenv(_conftest_knobs.PIP_CACHE_ENV_VAR, raising=False)
    monkeypatch.delenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv(_conftest_knobs.BASE_HOST_ENV_VAR, raising=False)


@pytest.fixture
def session(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Session:
    return plain_container_session(
        monkeypatch,
        request,
        tmp_path,
        home_assistant_url=core_client.base_url,
        token=core_client.token,
    )


async def _boot(hass: HomeAssistant, session: Session) -> Path:
    """Run the session fixture through its setup and teardown; the staged config."""
    generator = session.fixture()
    async with running_home_assistant(hass):
        info = await hass.async_add_executor_job(next, generator)
    generator.close()
    await hass.async_add_executor_job(session.request.finalize)
    return Path(info["config_path"])


def _preinstalls(script: str) -> list[str]:
    """Every ``pip install`` in the entrypoint, from the install to the next segment.

    One per manifest-requirement attempt (PyPI-first plus the wheels-index
    fallback), so the list is what the flag check has to cover — a count would
    restate the current command layout instead.
    """
    return script.split("pip install ")[1:]


def _run_entrypoint(
    script: str, tmp_path: Path, pip_status: int
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the entrypoint against stand-ins for the container's ``pip``,
    ``chown`` and ``stat``; its result and the tools' invocations."""
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
    result = subprocess.run(
        ["sh", "-c", script],
        check=False,
        env={**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


def _boot_into(script: str, tmp_path: Path) -> tuple[str, Path]:
    """The entrypoint with Home Assistant's ``/init`` replaced by a stand-in
    that leaves a mark and exits 7; the script and the mark."""
    booted = tmp_path / "booted"
    init_stub = tmp_path / "init"
    init_stub.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(booted))}\nexit 7\n", encoding="utf-8"
    )
    init_stub.chmod(0o755)
    return script.replace("exec /init", f"exec {shlex.quote(str(init_stub))}"), booted


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


def test_the_timeout_knob_is_the_budget_the_api_gate_waits(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    socket_enabled: None,
) -> None:
    """The knob names the readiness gate, so the gate must wait exactly that."""
    session = plain_container_session(
        monkeypatch, request, tmp_path, home_assistant_url=refused_url()
    )
    monkeypatch.setenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, "1")

    with pytest.raises(pytest.fail.Exception, match="within 1 seconds"):
        next(session.fixture())
    session.request.finalize()


# ---------------------------------------------------------------------------
# HA_E2E_PIP_CACHE
# ---------------------------------------------------------------------------


async def test_unset_cache_knob_keeps_the_preinstall_ephemeral(
    hass: HomeAssistant, session: Session
) -> None:
    """Stock behaviour: one /config mount, and pip caches nothing."""
    staged = await _boot(hass, session)

    assert session.mounts() == {str(staged): "/config"}
    installs = _preinstalls(session.entrypoint_script())
    assert installs
    assert all(install.startswith("--no-cache-dir ") for install in installs)


async def test_a_mounted_pip_cache_is_used_by_every_preinstall(
    hass: HomeAssistant,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without this, a reused run pays the full PyPI cost again every time."""
    cache_dir = tmp_path / "pip-cache"
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(cache_dir))

    staged = await _boot(hass, session)

    # /root/.cache/pip is where pip inside the HA image resolves its cache: the
    # upstream image runs as root and sets neither PIP_CACHE_DIR nor
    # XDG_CACHE_HOME, so a mount elsewhere would be a directory pip never reads.
    assert session.mounts() == {
        str(staged): "/config",
        str(cache_dir): "/root/.cache/pip",
    }
    assert await hass.async_add_executor_job(cache_dir.is_dir)
    installs = _preinstalls(session.entrypoint_script())
    assert installs
    # Dropping --no-cache-dir is not enough: the image's pip.conf sets
    # no-cache-dir = false, which pip disables the cache for just the same.
    assert all(
        install.startswith(f"--cache-dir {_conftest_knobs.PIP_CACHE_MOUNT_PATH} ")
        for install in installs
    )


async def test_the_mounted_cache_is_claimed_for_root_and_handed_back(
    hass: HomeAssistant,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """pip as root only caches into a uid-0 directory; the host keeps its owner.

    The bind mount carries the invoking user's uid, so the entrypoint has to
    hand the directory to root before the first install and back afterwards —
    otherwise pip disables the cache, or the developer's directory is left
    undeletable.
    """
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(tmp_path / "pip-cache"))
    await _boot(hass, session)

    script = session.entrypoint_script()
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
async def test_a_failed_preinstall_still_hands_the_cache_back(
    hass: HomeAssistant,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An install failure must not leave the host cache root-owned.

    ``chown`` only succeeds as root, so the failure path cannot be exercised
    for real here; the generated script runs against stub ``pip``/``chown``/
    ``stat`` binaries instead. The install fails, the hand-back still runs, and
    the install's status is what the container exits with.
    """
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(tmp_path / "pip-cache"))
    await _boot(hass, session)

    result, invocations = await hass.async_add_executor_job(
        _run_entrypoint, session.entrypoint_script(), tmp_path, 3
    )

    assert result.returncode == 3, result.stderr
    assert invocations[0] == f"chown 0:0 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    assert invocations[-1] == (
        f"chown -R 1000:1000 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    )
    assert any(call.startswith("pip install ") for call in invocations)


@pytest.mark.skipif(
    sys.platform == "win32", reason="the container lanes are POSIX-only"
)
async def test_a_successful_preinstall_still_boots_home_assistant(
    hass: HomeAssistant,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Handing the cache back must not replace booting HA after a good install."""
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, str(tmp_path / "pip-cache"))
    await _boot(hass, session)
    script, booted = await hass.async_add_executor_job(
        _boot_into, session.entrypoint_script(), tmp_path
    )

    result, invocations = await hass.async_add_executor_job(
        _run_entrypoint, script, tmp_path, 0
    )

    assert result.returncode == 7, result.stderr
    assert await hass.async_add_executor_job(booted.exists)
    assert invocations[-1] == (
        f"chown -R 1000:1000 {_conftest_knobs.PIP_CACHE_MOUNT_PATH}"
    )


async def test_a_relative_cache_path_is_resolved_before_it_is_mounted(
    hass: HomeAssistant,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker needs an absolute host path; a relative value must not stay one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_conftest_knobs.PIP_CACHE_ENV_VAR, "pip-cache")

    await _boot(hass, session)

    assert session.mounts()[str(tmp_path / "pip-cache")] == "/root/.cache/pip"


# ---------------------------------------------------------------------------
# HA_E2E_BASE_HOST
# ---------------------------------------------------------------------------


def test_the_base_host_defaults_to_localhost() -> None:
    """An unset knob keeps the URL every lane has always used."""
    assert _conftest_knobs.resolve_base_host() == "localhost"


@pytest.mark.parametrize("raw", ["192.0.2.10", " 192.0.2.10 "])
def test_the_base_host_uses_the_override(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The literal documented name, not BASE_HOST_ENV_VAR: spelling the knob in
    # the test is what fails when the constant and the docs drift apart.
    monkeypatch.setenv("HA_E2E_BASE_HOST", raw)
    assert _conftest_knobs.resolve_base_host() == raw.strip()


def test_a_blank_base_host_falls_back_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whitespace-only covers the strip() and the ``or "localhost"`` fallback;
    # an empty string takes the same path, so it needs no separate case.
    monkeypatch.setenv("HA_E2E_BASE_HOST", "   ")
    assert _conftest_knobs.resolve_base_host() == "localhost"
