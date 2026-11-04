"""Separate a component's displayed build from its compatibility version."""

from __future__ import annotations

import re


def compatibility_version(version: str) -> str:
    """Keep dev builds at their release's existing component capability floor.

    Older external servers parse the bootstrap version as dotted integers.
    The embedded server must also accept a dev build at its plain release
    floor. Strip only the suffix our release pipeline emits; leave other
    versions unchanged for the caller's existing validation/comparison.
    """
    match = re.fullmatch(r"(\d+\.\d+\.\d+)\.dev\d+", version)
    return match[1] if match else version
