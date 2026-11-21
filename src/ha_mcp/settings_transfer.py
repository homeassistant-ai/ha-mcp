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
INCOMPLETE_ROLLBACK_NOTE = "Settings import rollback incomplete"
# The component's settings_transfer_schema validates the same envelope.
FORMAT = "ha-mcp-settings"
VERSION = 1
MAX_BYTES = 4 * 1024 * 1024
_ENVELOPE_KEYS = {
    "format",
    "version",
    "contains_credentials",
    "entries",
    "server_settings",
}

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


class LocalSettingsUnusable(ValueError):
    """A settings file on this installation cannot be read or validated."""


class ServerSettingsRollbackIncomplete(RuntimeError):
    """One or more server settings could not be restored."""


class IncompatibleVersion(ValueError):
    """The file is a settings export of another format version."""


@dataclass
class Backup:
    """The parts of an export a server outside Home Assistant can apply."""

    server_settings: dict[str, Any]
    tools: dict[str, list[str]] | None
    has_connection: bool


def read_backup(bundle: Any) -> Backup:
    """Check an uploaded export's envelope; the settings are checked on restore.

    Only the HA-MCP Server entry in Home Assistant can apply the connection
    section, so it is reported and left out here.
    """
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise ValueError("Not an HA-MCP settings export")
    if type(bundle.get("version")) is not int or bundle["version"] != VERSION:
        raise IncompatibleVersion("Unsupported settings export version")
    entries = bundle.get("entries", {})
    if (
        bundle.keys() - _ENVELOPE_KEYS
        or "server_settings" not in bundle
        or not isinstance(entries, dict)
        or entries.keys() - {"server", "tools"}
        or type(bundle.get("contains_credentials")) is not bool
        or bundle["contains_credentials"] != ("server" in entries)
    ):
        raise ValueError("Invalid settings export")
    return Backup(
        bundle["server_settings"],
        _read_tools(entries.get("tools")),
        "server" in entries,
    )


def _read_tools(tools: Any) -> dict[str, list[str]] | None:
    """The File & YAML Tools lists to apply; None when absent or disabled."""
    if tools is None:
        return None
    names = ("allowed_dirs", "extra_yaml_keys")
    if (
        not isinstance(tools, dict)
        or tools.keys() - {*names, "disabled"}
        or not isinstance(tools.get("disabled", False), bool)
        or any(
            not isinstance(tools.get(k), list)
            or not all(isinstance(i, str) for i in tools[k])
            for k in names
        )
    ):
        raise ValueError("Invalid File & YAML Tools settings")
    return None if tools.get("disabled") else {k: tools[k] for k in names}


def app_option_fields(section: str) -> frozenset[str]:
    """The ``features`` or ``backup`` overrides the app keeps as its options."""
    types = _FEATURE_TYPES if section == "features" else _BACKUP_TYPES
    return frozenset(
        name
        for name in types
        if (meta := setting_of(Settings.model_fields[name])) and meta.app
    )


def _override_error(name: str, value: Any, types: Mapping[str, type]) -> str | None:
    field_type = types.get(name)
    if field_type is None:
        return "Unknown settings override"
    if type(value) is not field_type and not (
        field_type is float and type(value) is int
    ):
        return "Invalid settings override"
    meta = setting_of(Settings.model_fields[name])
    if meta is None:
        return "Unknown settings override"
    if meta.range and value != meta.off_value:
        low, high = meta.range
        if not isinstance(value, int | float) or not low <= value <= high:
            return "Settings override outside its permitted range"
    if meta.choices and value not in meta.choices:
        return "Invalid settings choice"
    if isinstance(value, str):
        if "\x00" in value:
            return "Unexpected null character in settings"
        if name == "ipv6_allowed_networks" and invalid_allowed_networks(value):
            return "Invalid IPv6 networks"
    return None


def _drop(file_key: str, entry: str) -> None:
    # The runtime loaders skip what they cannot apply, so a stored file can
    # keep a retired or out-of-range entry that no settings save removes.
    _LOGGER.warning("Left %s out of the %s settings", entry, file_key)


def _validate_overrides(
    file_key: str, values: dict[str, Any], types: Mapping[str, type], *, strict: bool
) -> dict[str, Any]:
    kept = {}
    for name, value in values.items():
        if (error := _override_error(name, value, types)) is None:
            kept[name] = value
        elif strict:
            raise ValueError(error)
        else:
            _drop(file_key, repr(name))
    return kept


def _validate_tools(values: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    if strict and values.keys() - {"tools", LLM_API_CONFIG_KEY}:
        raise ValueError("Invalid tool configuration")
    kept: dict[str, Any] = {}
    for section, valid in (
        ("tools", lambda state: isinstance(state, str) and state in _VALID_STATES),
        (LLM_API_CONFIG_KEY, lambda value: isinstance(value, bool)),
    ):
        if section not in values:
            continue
        entries = values[section]
        if not isinstance(entries, dict):
            if strict:
                raise ValueError("Invalid tool configuration")
            _drop("tools", repr(section))
            continue
        kept[section] = {}
        for name, value in entries.items():
            if valid(value):
                kept[section][name] = value
            elif strict:
                raise ValueError("Invalid tool state")
            else:
                _drop("tools", repr(name))
    return kept


def _revisioned_content(
    key: str, value: dict[str, Any], *, strict: bool
) -> dict[str, Any] | None:
    if key == "policy":
        policy = Policy.model_validate(value, strict=True)
        if "schema_version" not in value:
            policy, _ = unpack_legacy_policy(policy)
        model: Policy | VisibilityConfig = policy
    else:
        if unknown := value.keys() - VisibilityConfig.model_fields.keys():
            if strict:
                raise ValueError("Unknown entity visibility option")
            # VisibilityConfig ignores unknown keys when the server loads it.
            for name in sorted(unknown):
                _drop("visibility", repr(name))
            value = {k: v for k, v in value.items() if k not in unknown}
        model = VisibilityConfig.model_validate(value, strict=True)
    content = model.model_dump(mode="json", exclude={"version"})
    default = _REVISIONED_DEFAULTS[key].model_dump(mode="json", exclude={"version"})
    return None if content == default else content


def validate_server_settings(
    settings: dict[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """Validate the complete document and strip destination-owned edit revisions.

    An uploaded document is strict. A stored one (``strict=False``) drops the
    entries the runtime loaders would also skip.
    """
    if not isinstance(settings, dict) or settings.keys() != _FILES.keys():
        raise ValueError("Expected the complete set of server settings")
    portable = dict(settings)
    for key, value in settings.items():
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("Invalid server settings section")
        if key in _REVISIONED_DEFAULTS:
            portable[key] = _revisioned_content(key, value, strict=strict)
        elif key == "tools":
            portable[key] = _validate_tools(value, strict=strict)
        else:
            types = _FEATURE_TYPES if key == "features" else _BACKUP_TYPES
            portable[key] = _validate_overrides(key, value, types, strict=strict)
    return portable


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        _LOGGER.error("Could not read %s (%s)", path.name, type(err).__name__)
        raise LocalSettingsUnusable(path.name) from err
    if not isinstance(value, dict):
        _LOGGER.error("Could not read %s (not a JSON object)", path.name)
        raise LocalSettingsUnusable(path.name)
    return value


def _read_settings(directory: Path) -> dict[str, Any]:
    return {key: _read_document(directory / name) for key, name in _FILES.items()}


def _stored_settings(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the raw stored documents and their portable content."""
    raw = _read_settings(directory)
    try:
        return raw, validate_server_settings(raw, strict=False)
    except ValueError as err:
        # Only the policy or visibility model can still refuse a stored file.
        _LOGGER.error("Stored server settings are invalid (%s)", type(err).__name__)
        raise LocalSettingsUnusable("server settings") from err


def export_server_settings(data_dir: Path) -> dict[str, Any]:
    """Read portable documents, failing explicitly on an unreadable setting."""
    with config_file_lock(data_dir):
        return _stored_settings(data_dir)[1]


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
) -> bool:
    """Attempt every undo under the caller's file lock and report completeness."""
    complete = True
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
                    complete = False
                    continue
                previous = _next_revision(change.key, previous, change.written)
            _replace(path, previous)
        except (OSError, ValueError) as err:
            complete = False
            # Exception messages may include settings or paths supplied by the
            # operator. Report only the fixed filename and exception class.
            _LOGGER.error(
                "Settings import rollback failed for %s (%s)",
                path.name,
                type(err).__name__,
            )
    return complete


def rollback_server_settings(directory: Path, changes: ServerSettingsChanges) -> None:
    """Undo only writes from this import that no later save has replaced."""
    with config_file_lock(directory):
        if not _undo_documents(directory, changes.documents, published=True):
            raise ServerSettingsRollbackIncomplete(INCOMPLETE_ROLLBACK_NOTE)


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
        previous, old_content = _stored_settings(data_dir)
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
        except (OSError, ValueError) as err:
            if not _undo_documents(data_dir, changed, published=False):
                err.add_note(INCOMPLETE_ROLLBACK_NOTE)
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
