"""Unit tests for the HAOS bake's local-add-on source verification.

Guards the #1594 root-cause fix: ``build_image._verify_local_addon_sources``
must fail the bake (rather than silently save a poisoned image) when a staged
``/supervisor/addons/local/<dir>`` is absent from the qcow2. It runs a single
``guestfish ... ls /supervisor/addons/local`` and checks the expected dir
names appear; these tests mock the subprocess so they need no qcow2 /
libguestfs. A non-zero guestfish exit (mount/appliance failure) is a distinct
"could not verify" error, NOT a per-dir "missing" signal — that distinction
is what prevented the false-positive that blocked both HAOS tiers.
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


def _proc(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["guestfish"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _listing(*names: str) -> str:
    """guestfish ``ls`` prints one entry per line."""
    return "".join(f"{n}\n" for n in names)


def test_all_sources_present_passes() -> None:
    """All expected dirs present in the ls output → no raise, one guestfish call."""
    listing = _listing(*_STAGED_LOCAL_ADDON_DIRS)
    with patch(
        "tests.haos_image_build.build_image.subprocess.run",
        return_value=_proc(stdout=listing),
    ) as mock_run:
        _verify_local_addon_sources(_QCOW2)
    # Single ls-the-parent call, not one per dir.
    assert mock_run.call_count == 1


def test_missing_source_raises_and_lists_only_missing() -> None:
    """A dir absent from the listing is reported; present dirs are not."""
    # ha_mcp_dev present, the other two absent (plus an unrelated entry).
    listing = _listing("ha_mcp_dev", "some_other_addon")
    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run",
            return_value=_proc(stdout=listing),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)

    msg = str(exc.value)
    assert "ha_mcp_webhook_proxy" in msg
    assert "puppet" in msg
    # The present dir must NOT be reported missing (only as a present entry).
    assert "missing local add-on source dir(s): ha_mcp_dev" not in msg
    assert "#1594" in msg


def test_substring_dir_name_not_false_matched() -> None:
    """An entry that merely contains a dir name as a substring is not a match."""
    # ``puppet_old`` must not satisfy the ``puppet`` requirement.
    listing = _listing("ha_mcp_dev", "ha_mcp_webhook_proxy", "puppet_old")
    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run",
            return_value=_proc(stdout=listing),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)
    assert "puppet" in str(exc.value)


def test_guestfish_failure_raises_distinct_error_with_stderr() -> None:
    """A non-zero guestfish exit is a 'could not verify' system error, not 'missing'."""
    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run",
            return_value=_proc(
                returncode=1, stderr="libguestfs: error: appliance closed"
            ),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)

    msg = str(exc.value)
    assert "Could not verify" in msg
    assert "appliance closed" in msg
    # Must NOT misattribute a system failure to a poisoned image.
    assert "missing local add-on source dir(s)" not in msg


def test_empty_listing_is_system_error_not_all_missing() -> None:
    """ls exiting 0 but empty → 'could not verify', NOT 'everything missing'.

    Guards the residual false-positive: a silent mount failure that still
    exits 0 would yield an empty listing; reporting that as all-dirs-missing
    would block every bake. A real poisoned image still lists the other dirs.
    """
    with (
        patch(
            "tests.haos_image_build.build_image.subprocess.run",
            return_value=_proc(stdout="\n  \n"),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        _verify_local_addon_sources(_QCOW2)

    msg = str(exc.value)
    assert "Could not verify" in msg
    assert "no entries" in msg
    assert "missing local add-on source dir(s)" not in msg
