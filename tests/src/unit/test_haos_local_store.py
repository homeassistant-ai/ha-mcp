"""Unit tests for ``haos_runtime._resolve_local_store_dir``.

Supervisor 2026.06.0 (home-assistant/supervisor#6865) renamed the local
add-on store ``addons/local`` to ``apps/local`` and migrates the legacy path
on boot, so the inaddon refresh must write to whichever path the bake-produced
base image actually carries. These tests mock the guestfish ``ll`` probe so
they need no qcow2 / libguestfs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.src.haos_runtime import _resolve_local_store_dir

_QCOW2 = Path("/tmp/does-not-need-to-exist.qcow2")
_APPS = "/supervisor/apps/local"
_ADDONS = "/supervisor/addons/local"


def _proc(returncode: int) -> subprocess.CompletedProcess[str]:
    # ``ll <dir>`` exits 0 iff the dir exists.
    return subprocess.CompletedProcess(
        args=["guestfish"], returncode=returncode, stdout="", stderr=""
    )


def test_prefers_apps_local_when_present() -> None:
    """apps/local is probed first; a hit short-circuits before addons/local."""
    with patch(
        "tests.src.haos_runtime.subprocess.run", return_value=_proc(0)
    ) as mock_run:
        assert _resolve_local_store_dir(_QCOW2) == _APPS
    assert mock_run.call_count == 1


def test_falls_back_to_addons_local() -> None:
    """When apps/local is absent but addons/local exists (older Supervisor)."""

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # cmd[-1] is the ``ll`` target path.
        return _proc(1 if cmd[-1] == "/supervisor/apps/local" else 0)

    with patch("tests.src.haos_runtime.subprocess.run", side_effect=fake_run):
        assert _resolve_local_store_dir(_QCOW2) == _ADDONS


def test_defaults_to_apps_local_when_neither_exists() -> None:
    """A poisoned base with neither path defaults to apps/local (fresh bakes
    run current Supervisor)."""
    with patch("tests.src.haos_runtime.subprocess.run", return_value=_proc(1)):
        assert _resolve_local_store_dir(_QCOW2) == _APPS
