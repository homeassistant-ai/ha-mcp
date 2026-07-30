"""Unit tests for the HAOS addon container resolution in ``haos_runtime``.

Supervisor renamed addon containers from ``addon_<slug>`` to ``app_<slug>``
and HAOS CI VMs self-update Supervisor at boot, so which prefix a VM uses
depends on the build it booted with. ``docker_exec_in_addon`` therefore
resolves the name from docker rather than assuming a prefix, and recovers
from a resolution that went stale between the listing and the exec.

These paths used to be observable only through the HAOS in-addon lane (a
~40 min VM boot), so a regression in them cost a full CI round-trip to see.
Covered here directly: prefix resolution, running-only listing, the fixed
candidate preference, exact-name matching, the single re-resolve on a stale
name, and the degradation when the listing itself is unreadable.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from tests.src.haos_runtime import _resolve_addon_container, docker_exec_in_addon

SLUG = "local_ha_mcp_dev"
APP = f"app_{SLUG}"
ADDON = f"addon_{SLUG}"

_LIST = ("list", None)


def _miss(container: str) -> RuntimeError:
    """The RuntimeError ``ssh_exec`` raises when the name matches nothing."""
    return RuntimeError(
        f"ssh_exec failed (exit 1, attempt=1): cmd=['docker', 'exec'] "
        f'stderr="Error response from daemon: No such container: {container}"'
    )


def _not_running(container: str) -> RuntimeError:
    """The RuntimeError ``ssh_exec`` raises for a stopped container."""
    return RuntimeError(
        f"ssh_exec failed (exit 1, attempt=1): cmd=['docker', 'exec'] "
        f'stderr="Error response from daemon: Container {container} is not running"'
    )


class _FakeSSH:
    """Scripted ``ssh_exec`` stand-in recording listing and exec calls.

    ``listings`` and ``execs`` are consumed in order; a queued
    ``BaseException`` is raised instead of returned. Running past the end of
    either queue is a test-authoring bug, so it raises ``AssertionError``
    rather than silently succeeding.
    """

    def __init__(
        self, listings: list[Any] | None = None, execs: list[Any] | None = None
    ) -> None:
        self._listings = list(listings or [])
        self._execs = list(execs or [])
        self.calls: list[tuple[str, str | None]] = []
        self.listing_cmds: list[list[str]] = []

    def __call__(
        self, cmd: list[str], *, timeout: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["docker", "ps"]:
            self.listing_cmds.append(list(cmd))
            self.calls.append(_LIST)
            return self._serve(self._listings, cmd)
        assert cmd[:2] == ["docker", "exec"], f"unexpected command: {cmd!r}"
        self.calls.append(("exec", cmd[2]))
        return self._serve(self._execs, cmd)

    @staticmethod
    def _serve(queue: list[Any], cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if not queue:
            raise AssertionError(f"unscripted call: {cmd!r}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return subprocess.CompletedProcess(cmd, 0, stdout=item, stderr="")

    @property
    def exec_targets(self) -> list[str | None]:
        return [name for kind, name in self.calls if kind == "exec"]


def _run(fake: _FakeSSH) -> str:
    with patch("tests.src.haos_runtime.ssh_exec", fake):
        return docker_exec_in_addon(SLUG, ["chmod", "444", "/data/saved_tools.json"])


def test_resolves_app_prefix() -> None:
    """A VM on the renamed prefix execs against ``app_<slug>``."""
    fake = _FakeSSH([f"{APP}\nhomeassistant\n"], ["ok"])
    assert _run(fake) == "ok"
    assert fake.calls == [_LIST, ("exec", APP)]


def test_resolves_legacy_addon_prefix() -> None:
    """A VM still on the old prefix execs against ``addon_<slug>``."""
    fake = _FakeSSH([f"{ADDON}\nhomeassistant\n"], ["ok"])
    assert _run(fake) == "ok"
    assert fake.calls == [_LIST, ("exec", ADDON)]


def test_resolution_lists_running_containers_only() -> None:
    """Resolution must not pass ``-a`` -- exec needs a RUNNING container.

    The regression this guards: with ``-a`` the listing also carries Exited
    leftovers, and the ``app_``-first preference would then select a dead
    ``app_`` container over the ``addon_`` one that is actually serving.
    """
    fake = _FakeSSH([f"{ADDON}\nhomeassistant\n"], ["ok"])
    assert _run(fake) == "ok"
    assert fake.listing_cmds == [["docker", "ps", "--format", "{{.Names}}"]]
    assert "-a" not in fake.listing_cmds[0]
    assert fake.exec_targets == [ADDON]


def test_prefers_app_over_legacy_when_both_running() -> None:
    """Preference is the fixed candidate order, not the listing's order."""
    fake = _FakeSSH([f"{ADDON}\n{APP}\n"])
    with patch("tests.src.haos_runtime.ssh_exec", fake):
        assert _resolve_addon_container(SLUG) == APP


def test_similar_name_does_not_match_slug() -> None:
    """Matching is exact: ``app_<slug>2`` is a different addon, not ours."""
    fake = _FakeSSH([f"{APP}2\nhomeassistant\n"], ["ok"])
    assert _run(fake) == "ok"
    # Falls back to the legacy name rather than hijacking the 2-suffixed one.
    assert fake.exec_targets == [ADDON]
    assert f"{APP}2" not in fake.exec_targets


def test_stale_resolution_reresolves_and_retries_once() -> None:
    """A listing taken mid-restart pins the legacy name; the miss recovers.

    This is the case that made the fallback dangerous: on an ``app_`` VM the
    legacy fallback names a container that will never exist, so without the
    re-resolve the exec fails even though the real container came back.
    """
    fake = _FakeSSH(["homeassistant\n", f"{APP}\n"], [_miss(ADDON), "ok"])
    assert _run(fake) == "ok"
    assert fake.calls == [_LIST, ("exec", ADDON), _LIST, ("exec", APP)]


def test_not_running_error_takes_the_reresolve_path() -> None:
    """A container that stopped between listing and exec re-resolves too."""
    fake = _FakeSSH([f"{ADDON}\n", f"{APP}\n"], [_not_running(ADDON), "ok"])
    assert _run(fake) == "ok"
    assert fake.calls == [_LIST, ("exec", ADDON), _LIST, ("exec", APP)]


def test_unchanged_reresolve_reraises_without_second_exec() -> None:
    """An unchanged name is not worth re-running -- fail fast, one exec only."""
    fake = _FakeSSH(["homeassistant\n", "homeassistant\n"], [_miss(ADDON)])
    with pytest.raises(RuntimeError, match="No such container"):
        _run(fake)
    assert fake.calls == [_LIST, ("exec", ADDON), _LIST]
    assert fake.exec_targets == [ADDON]


def test_unreadable_listing_degrades_to_both_names() -> None:
    """A broken listing must not block an exec that would have worked."""
    fake = _FakeSSH(
        [RuntimeError("ssh_exec failed: connection closed")], [_miss(ADDON), "ok"]
    )
    assert _run(fake) == "ok"
    # One listing attempt only -- the second name comes from the prefix pair.
    assert fake.calls == [_LIST, ("exec", ADDON), ("exec", APP)]


def test_listing_timeout_degrades_to_both_names() -> None:
    """``ssh_exec`` does not wrap TimeoutExpired; it must still degrade."""
    fake = _FakeSSH([subprocess.TimeoutExpired("ssh", 20.0)], [_miss(ADDON), "ok"])
    assert _run(fake) == "ok"
    assert fake.calls == [_LIST, ("exec", ADDON), ("exec", APP)]


def test_non_miss_failure_propagates_without_reresolve() -> None:
    """An unrelated exec failure is a real failure -- no retry, no re-resolve."""
    fake = _FakeSSH(
        [f"{APP}\n"],
        [RuntimeError('ssh_exec failed (exit 1): stderr="chmod: permission denied"')],
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        _run(fake)
    assert fake.calls == [_LIST, ("exec", APP)]
