"""Shared symlink helper for unit tests that create symlinks.

Windows only permits ``os.symlink`` for elevated processes or with
Developer Mode enabled; without it every symlink-creating test dies with
``OSError: [WinError 1314]``. Tests route creation through
``symlink_or_skip`` so they skip cleanly there instead of failing —
on Linux (CI) the helper is a plain passthrough and nothing is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def symlink_or_skip(link: Path, target: Path) -> None:
    """Create ``link`` pointing at ``target``, or skip the running test
    when the platform refuses symlink creation."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")
