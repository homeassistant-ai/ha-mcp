"""Compatibility checks for screenshot-engine API differences."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

from .provision import EngineKind


def is_legacy_puppet(engine_kind: EngineKind | None, version: str | None) -> bool:
    """Return whether a verified Puppet version predates WIDTHxauto support."""
    if engine_kind != "puppet" or not version:
        return False
    try:
        return Version(version) < Version("2.5.0")
    except InvalidVersion:
        return False
