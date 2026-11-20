"""Portable settings stored alongside the embedded server's config entries."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_meta import setting_of
from .config_registry import (
    _BACKUP_OVERRIDE_FILENAME,
    _FEATURE_FLAG_OVERRIDE_FILENAME,
    ADVANCED_RESTART_REQUIRED,
    ADVANCED_SETTINGS_FIELDS,
    BACKUP_OVERRIDE_FIELDS,
    FEATURE_FLAG_FIELDS,
)
from .config_settings import Settings
from .ipv6_lan_guard import invalid_allowed_networks
from .llm_exposure import LLM_API_CONFIG_KEY
from .policy.decision_pin import is_pin_set
from .policy.model import Policy
from .policy.persistence import POLICY_FILENAME, unpack_legacy_policy
from .settings_ui._persistence import _atomic_write_json
from .settings_ui._tools_meta import _VALID_STATES
from .utils.config_write_lock import config_file_lock
from .visibility.model import VisibilityConfig
from .visibility.persistence import VISIBILITY_FILENAME

_LOGGER = logging.getLogger(__name__)

# Never use a path from the uploaded document. A null section records that
# the source used defaults and must clear any override on the destination.
_FILES = {
    "tools": "tool_config.json",
    "policy": POLICY_FILENAME,
    "visibility": VISIBILITY_FILENAME,
    "features": _FEATURE_FLAG_OVERRIDE_FILENAME,
    "backup": _BACKUP_OVERRIDE_FILENAME,
}
_FEATURE_TYPES = {row.field: row.ftype for row in FEATURE_FLAG_FIELDS}
_FEATURE_TYPES.update(
    {row.field: row.ftype for row in ADVANCED_SETTINGS_FIELDS if row.editable}
)
_BACKUP_TYPES = {row.field: row.ftype for row in BACKUP_OVERRIDE_FIELDS}
_REVISIONED_DEFAULTS: dict[str, Policy | VisibilityConfig] = {
    "policy": Policy(),
    "visibility": VisibilityConfig(),
}


class DecisionPinRequired(ValueError):
    """The source accepts approval events and the destination needs its own PIN."""


def _validate_overrides(values: dict[str, Any], types: Mapping[str, type]) -> None:
    for name, value in values.items():
        field_type = types.get(name)
        if type(value) is not field_type and not (
            field_type is float and type(value) is int
        ):
            raise ValueError("Invalid settings override")
        meta = setting_of(Settings.model_fields[name])
        assert meta is not None
        if meta.range and value != meta.off_value:
            low, high = meta.range
            if not isinstance(value, int | float) or not low <= value <= high:
                raise ValueError("Settings override outside its permitted range")
        if meta.choices and value not in meta.choices:
            raise ValueError("Invalid settings choice")
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("Unexpected null character in settings")
            if name == "ipv6_allowed_networks" and invalid_allowed_networks(value):
                raise ValueError("Invalid IPv6 networks")


def _validate_tools(values: dict[str, Any]) -> None:
    if values.keys() - {"tools", LLM_API_CONFIG_KEY}:
        raise ValueError("Invalid tool configuration")
    states = values.get("tools", {})
    if not isinstance(states, dict) or any(
        not isinstance(state, str) or state not in _VALID_STATES
        for state in states.values()
    ):
        raise ValueError("Invalid tool state")
    exposure = values.get(LLM_API_CONFIG_KEY, {})
    if not isinstance(exposure, dict) or any(
        not isinstance(value, bool) for value in exposure.values()
    ):
        raise ValueError("Invalid tool exposure")


def _revisioned_content(key: str, value: dict[str, Any]) -> dict[str, Any] | None:
    if key == "policy":
        policy = Policy.model_validate(value, strict=True)
        if "schema_version" not in value:
            policy, _ = unpack_legacy_policy(policy)
        model: Policy | VisibilityConfig = policy
    else:
        if value.keys() - VisibilityConfig.model_fields.keys():
            raise ValueError("Unknown entity visibility option")
        model = VisibilityConfig.model_validate(value, strict=True)
    content = model.model_dump(mode="json", exclude={"version"})
    default = _REVISIONED_DEFAULTS[key].model_dump(mode="json", exclude={"version"})
    return None if content == default else content


def validate_server_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete document and strip destination-owned edit revisions."""
    if not isinstance(settings, dict) or settings.keys() != _FILES.keys():
        raise ValueError("Expected the complete set of server settings")
    portable = dict(settings)
    for key, value in settings.items():
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("Invalid server settings section")
        if key in _REVISIONED_DEFAULTS:
            portable[key] = _revisioned_content(key, value)
        elif key == "tools":
            _validate_tools(value)
        else:
            _validate_overrides(
                value, _FEATURE_TYPES if key == "features" else _BACKUP_TYPES
            )
    return portable


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in the server settings file")
    return value


def _read_settings(directory: Path) -> dict[str, Any]:
    return {key: _read_document(directory / name) for key, name in _FILES.items()}


def export_server_settings(data_dir: Path) -> dict[str, Any]:
    """Read portable documents, failing explicitly on an unreadable setting."""
    with config_file_lock(data_dir):
        settings = _read_settings(data_dir)
    return validate_server_settings(settings)


def _replace(path: Path, value: dict[str, Any] | None) -> None:
    if value is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_json(path, value)


def _file_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class _DocumentChange:
    key: str
    previous: dict[str, Any] | None
    written: dict[str, Any] | None
    stamp: tuple[int, int, int, int, int] | None = None


@dataclass
class ServerSettingsChanges:
    documents: list[_DocumentChange]
    restart_required: bool


def _next_revision(
    key: str, value: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any] | None:
    if key not in _REVISIONED_DEFAULTS:
        return value
    # A reset must also advance the edit revision to invalidate open editors.
    default = _REVISIONED_DEFAULTS[key]
    version = (current or {}).get("version", default.version)
    return {**(value or default.model_dump(mode="json")), "version": version + 1}


def _undo_documents(
    directory: Path, changes: list[_DocumentChange], *, published: bool
) -> None:
    """Attempt every undo step under the caller's file lock."""
    for change in reversed(changes):
        path = directory / _FILES[change.key]
        try:
            previous = change.previous
            if published:
                if (
                    _file_stamp(path) != change.stamp
                    or _read_document(path) != change.written
                ):
                    _LOGGER.warning(
                        "Kept %s because it changed after settings import", path.name
                    )
                    continue
                if (
                    change.key == "policy"
                    and previous
                    and previous.get("event_decisions_enabled")
                    and not is_pin_set(directory)
                ):
                    _LOGGER.error(
                        "Could not roll back %s because the decision PIN was removed",
                        path.name,
                    )
                    continue
                previous = _next_revision(change.key, previous, change.written)
            _replace(path, previous)
        except (OSError, ValueError) as err:
            # Exception messages may include settings or paths supplied by the
            # operator. Report only the fixed filename and exception class.
            _LOGGER.error(
                "Settings import rollback failed for %s (%s)",
                path.name,
                type(err).__name__,
            )


def rollback_server_settings(directory: Path, changes: ServerSettingsChanges) -> None:
    """Undo only writes from this import that no later save has replaced."""
    with config_file_lock(directory):
        _undo_documents(directory, changes.documents, published=True)


def restore_server_settings(
    data_dir: Path, settings: dict[str, Any]
) -> ServerSettingsChanges | None:
    """Return a rollback record for committed changes, or None on a no-op."""
    incoming = validate_server_settings(settings)
    data_dir.mkdir(parents=True, exist_ok=True)
    with config_file_lock(data_dir):
        policy = incoming["policy"]
        if (
            policy
            and policy.get("event_decisions_enabled")
            and not is_pin_set(data_dir)
        ):
            raise DecisionPinRequired("Set the approval decision PIN locally first")
        previous = _read_settings(data_dir)
        old_content = validate_server_settings(previous)
        changed: list[_DocumentChange] = []
        try:
            for key, name in _FILES.items():
                value = incoming[key]
                if old_content[key] == value:
                    continue
                path = data_dir / name
                change = _DocumentChange(
                    key, previous[key], _next_revision(key, value, previous[key])
                )
                _replace(path, change.written)
                changed.append(change)
                change.stamp = _file_stamp(path)
        except OSError:
            _undo_documents(data_dir, changed, published=False)
            raise
    if not changed:
        return None
    old_features = old_content["features"] or {}
    new_features = incoming["features"] or {}
    restart = any(
        old_features.get(key) != new_features.get(key)
        for key in ADVANCED_RESTART_REQUIRED
    )
    return ServerSettingsChanges(changed, restart)
