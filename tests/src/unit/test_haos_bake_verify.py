"""Unit tests for the HAOS bake's local-add-on source verification.

Guards the #1594 root-cause fix: ``build_image._verify_local_addon_sources``
must fail the bake (rather than silently save a poisoned image) whenever a
staged ``/supervisor/addons/local/<dir>/config.yaml`` is absent from the
qcow2. The check runs ``guestfish exists`` per dir; these tests mock the
subprocess so they need no qcow2 / libguestfs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.haos_image_build.build_image import (
    _STAGED_LOCAL_ADDON_DIRS,
    _verify_local_addon_sources,
)

_QCOW2 = Path("/tmp/does-not-need-to-exist.qcow2")


def _proc(returncode: int) -> subprocess.CompletedProcess[str]:
    # Presence is signalled by the guestfish exit code: ``stat`` exits 0 on a
    # present file, non-zero on a missing path (or a mount/guestfish failure).
    return subprocess.CompletedProcess(
        args=["guestfish"], returncode=returncode, stdout="", stderr=""
    )


def test_all_sources_present_passes() -> None:
    """Every dir exiting 0 → no raise, one guestfish call per dir."""
    with patch(
        "tests.haos_image_build.build_image.subprocess.run",
        return_value=_proc(0),
    ) as mock_run:
        _verify_local_addon_sources(_QCOW2)
    assert mock_run.call_count == len(_STAGED_LOCAL_ADDON_DIRS)


def test_missing_source_raises_and_lists_only_missing() -> None:
    """A dir whose stat exits non-zero is listed; present dirs are not."""

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        return _proc(0 if "ha_mcp_dev/config.yaml" in joined else 1)

    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run", side_effect=fake_run
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)

    msg = str(exc.value)
    assert "ha_mcp_webhook_proxy/config.yaml" in msg
    assert "puppet/config.yaml" in msg
    # The present dir must NOT be reported missing.
    assert "ha_mcp_dev/config.yaml" not in msg
    assert "#1594" in msg


def test_guestfish_nonzero_exit_treated_as_missing() -> None:
    """A non-zero guestfish exit (missing path, mount failure etc.) → missing."""
    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run",
            return_value=_proc(1),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)

    for addon_dir in _STAGED_LOCAL_ADDON_DIRS:
        assert f"local/{addon_dir}/config.yaml" in str(exc.value)
