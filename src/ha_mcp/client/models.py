"""Frozen value types shared by the client and its tool consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """One bounded window of the error log, plus whether older history exists.

    ``has_more`` cannot be derived by the caller from ``text``: a journald
    window that comes back full may or may not have anything behind it (see
    ``_journald_error_log_page``), and only the fetching route knows which.
    """

    text: str
    has_more: bool


@dataclass(frozen=True)
class SceneResolution:
    """Outcome of resolving a scene identifier via the entity registry.

    ``registry_hit`` records whether ``config/entity_registry/get`` returned a
    usable ``unique_id`` - i.e. the entity actually EXISTS. It is the signal that
    separates a genuinely-missing scene from one that exists but is not editable
    through HA's scene config API (a Hue/vendor scene, or a raw-YAML scene): both
    resolve fine yet 404 on ``config/scene/config/{id}``, because that endpoint is
    backed only by the managed ``scenes.yaml`` (issue #1971). ``platform`` carries
    the registry entry's integration domain (``"hue"``, ``"homeassistant"``, …)
    for error-message enrichment only - it never drives the classification, since
    a raw-YAML scene is ``platform="homeassistant"`` yet still 404s.
    """

    storage_key: str
    registry_hit: bool
    platform: str | None
