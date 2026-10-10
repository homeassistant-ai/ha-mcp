"""Auto-backup settings overrides read from ``backup_settings.json``."""

import logging
import os
from typing import Any

from ha_mcp._version import is_running_in_addon
from ha_mcp.config_registry import (
    _BACKUP_OVERRIDE_FILENAME,
    BACKUP_OVERRIDE_FIELDS,
    SETTING_BOUNDS,
    RegistryFieldType,
)
from ha_mcp.config_settings import Settings

# All config modules log under the ``ha_mcp.config`` logger name.
logger = logging.getLogger("ha_mcp.config")


def get_backup_setting_origin(env_name: str) -> str:
    """Return where the live value for ``env_name`` is sourced from.

    Used by the web UI to label each auto-backup field with its source
    and decide whether the field is editable from the web UI:

    - ``"addon"``: running inside the HA add-on. ``start.py`` always
      writes these env vars from ``config.yaml`` on every addon start;
      the override file is ignored. Web UI edits are routed through
      Supervisor ``/addons/self/options`` so ``config.yaml`` stays
      authoritative.
    - ``"env"``: env var explicitly set in the process environment
      (includes values loaded from ``.env`` via ``load_dotenv`` at
      module import — those land in ``os.environ`` and are
      indistinguishable from ``docker -e`` / shell-set values, which is
      intentional per the deployment design). Web UI shows the field
      read-only; user must unset / remove the env var to edit.
    - ``"file"``: standalone deployment with a value persisted in
      ``<data_dir>/backup_settings.json``. Web UI edits update the
      file in place.
    - ``"default"``: no env var and no override file entry; the
      pydantic field default applies. Web UI edits create the file.
    """
    # is_running_in_addon() rather than a raw SUPERVISOR_TOKEN read so the
    # embedded in-process server (SUPERVISOR_TOKEN present on HAOS, but not an
    # add-on) is labeled a standalone deployment and its backup settings persist
    # to the override file instead of a non-existent Supervisor add-on.
    if is_running_in_addon():
        return "addon"
    if os.environ.get(env_name) is not None:
        return "env"
    field_name = next(
        (fname for fname, ename, _ in BACKUP_OVERRIDE_FIELDS if ename == env_name),
        None,
    )
    if field_name is None:
        return "default"
    overrides = _read_backup_override_file()
    if field_name in overrides:
        return "file"
    return "default"


def _read_backup_override_file() -> dict[str, object]:
    """Return the contents of the auto-backup override file, or ``{}``.

    Best-effort: a corrupt file MUST NOT break Settings loading. The
    failure modes split into two categories that need different
    treatment (mirrors ``_read_feature_flag_override_file``):

    * **Silent**: file does not exist. The override layer is opt-in;
      a missing file is the normal "user has never edited" state and
      should not log.
    * **Loud (WARNING)**: file exists but is unreadable
      (``PermissionError``, broken filesystem) or unparseable
      (``JSONDecodeError``). The user toggled something, the UI said
      "Saved", and the value is silently being ignored. Without a
      log line they have no diagnostic; with one, the sidecar/server
      log tells them exactly what to fix.

    Reads are not cached — callers (Settings construction, the GET
    endpoint) hit disk each time, which is fine for a small JSON file
    behind a singleton-cached Settings.
    """
    import json
    from pathlib import Path

    try:
        from .utils.data_paths import get_data_dir

        path: Path = get_data_dir() / _BACKUP_OVERRIDE_FILENAME
    except (RuntimeError, OSError):
        # Couldn't resolve the data dir at all — user has no override
        # file by definition. Silent.
        return {}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning(
            "Auto-backup override file at %s exists but is unreadable; "
            "falling back to defaults. Check filesystem permissions.",
            path,
            exc_info=True,
        )
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(
            "Auto-backup override file at %s is not valid JSON; "
            "falling back to defaults. Delete or fix the file to "
            "re-enable persisted toggles.",
            path,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Auto-backup override file at %s is not a JSON object "
            "(got %s); falling back to defaults.",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _coerce_backup_int_value(field_name: str, raw: object) -> tuple[bool, Any]:
    """Coerce + range-check one int-typed BACKUP_OVERRIDE_FIELDS value.

    Split out of ``_coerce_backup_override_value`` (mccabe complexity).
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "backup_settings.json: %s expects int, got %s; ignoring",
            field_name,
            type(raw).__name__,
        )
        return False, None
    coerced = int(raw)
    bounds = SETTING_BOUNDS.get(field_name)
    if bounds is not None and not bounds[0] <= coerced <= bounds[1]:
        logger.warning(
            "backup_settings.json: %s=%d out of range %g..%g; ignoring",
            field_name,
            coerced,
            bounds[0],
            bounds[1],
        )
        return False, None
    return True, coerced


def _coerce_backup_override_value(
    field_name: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce + range-check one override-file value for BACKUP_OVERRIDE_FIELDS.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "backup_settings.json: %s expects bool, got %s; ignoring",
                field_name,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        return _coerce_backup_int_value(field_name, raw)
    elif ftype is str:
        if not isinstance(raw, str):
            logger.warning(
                "backup_settings.json: %s expects str, got %s; ignoring",
                field_name,
                type(raw).__name__,
            )
            return False, None
        if "\x00" in raw:
            logger.warning(
                "backup_settings.json: %s contains null byte; ignoring",
                field_name,
            )
            return False, None
        return True, raw
    return False, None


def _apply_one_backup_override(
    settings: "Settings",
    field_name: str,
    env_name: str,
    ftype: RegistryFieldType,
    overrides: dict[str, object],
) -> None:
    """Apply a single BACKUP_OVERRIDE_FIELDS override-file entry, if eligible."""
    if os.environ.get(env_name) is not None:
        return
    if field_name not in overrides:
        return
    ok, coerced = _coerce_backup_override_value(
        field_name, ftype, overrides[field_name]
    )
    if not ok:
        return
    try:
        setattr(settings, field_name, coerced)
    except (ValueError, TypeError) as err:
        logger.warning(
            "backup_settings.json: setattr(%s, %r) rejected by Settings (%s); ignoring",
            field_name,
            coerced,
            err,
        )


def _apply_backup_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with values from the override file, in place.

    Honors the "env var wins" contract: a field whose env var is set in
    the process environment is never overwritten. Addon mode short-
    circuits — ``start.py`` already wrote these env vars from
    ``config.yaml`` and the override file is ignored in that mode.
    Range / type clamping mirrors the pydantic Field bounds so a
    corrupt override file can't push values out of range; out-of-range
    or untypable entries are silently skipped.
    """
    # is_running_in_addon() (embedded-aware) so the in-process server applies
    # its backup override file like a standalone deployment; see the matching
    # rationale in _apply_feature_flag_overrides / get_backup_setting_origin.
    if is_running_in_addon():
        return
    overrides = _read_backup_override_file()
    if not overrides:
        return
    for field_name, env_name, ftype in BACKUP_OVERRIDE_FIELDS:
        _apply_one_backup_override(settings, field_name, env_name, ftype, overrides)
