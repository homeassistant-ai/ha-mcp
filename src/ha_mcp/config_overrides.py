"""Feature-flag and advanced-setting overrides read from ``feature_flags.json``."""

import logging
import os
from typing import Any

from ha_mcp._version import is_running_in_addon
from ha_mcp.config_registry import (
    _ADVANCED_SETTINGS_BOUNDS,
    _ADVANCED_SETTINGS_CHOICES,
    _ADVANCED_SETTINGS_SENTINELS,
    _FEATURE_FLAG_INT_BOUNDS,
    _FEATURE_FLAG_OVERRIDE_FILENAME,
    ADVANCED_SETTINGS_FIELDS,
    BETA_FEATURE_FIELDS,
    FEATURE_FLAG_FIELDS,
    RegistryFieldType,
)
from ha_mcp.config_settings import Settings

# All config modules log under the ``ha_mcp.config`` logger name.
logger = logging.getLogger("ha_mcp.config")


# Names of beta sub-flags the master gate has already logged a
# force-False line for in this process. Used to dedup the gate's
# INFO log so we don't spam addon logs on every Settings rebuild
# now that the cascade-clear is gone and the file may carry truthy
# sub-flag values long-term. Reset alongside
# the Settings singleton in ``_reset_global_settings``.
_BETA_GATE_LOGGED: set[str] = set()


def get_feature_flag_origin(env_name: str) -> str:
    """Return where the live value for ``env_name`` is sourced from.

    Used by the web UI to label each feature-flag field with its
    source and decide whether the field is editable from the web UI:

    - ``"addon"``: running inside the HA add-on AND the env var is
      currently set. ``start.py`` writes env vars from ``config.yaml``
      on every addon start; the override file is ignored. Web UI
      edits are routed through Supervisor ``/addons/self/options`` so
      ``config.yaml`` stays authoritative.
    - ``"env"``: env var explicitly set in the process environment
      (includes values loaded from ``.env`` via ``load_dotenv`` at
      module import — those land in ``os.environ`` and are
      indistinguishable from ``docker -e`` / shell-set values,
      which is intentional). Web UI shows the field read-only;
      user must unset the env var to edit.
    - ``"file"``: standalone deployment with a value persisted in
      ``<data_dir>/feature_flags.json``. Web UI edits update the
      file in place.
    - ``"default"``: no env var and no override file entry; the
      pydantic field default applies. Web UI edits create the file.

    Addon-mode handling for the master and beta sub-flags:

    - ``ENABLE_BETA_FEATURES`` (master) is in the DEV addon schema
      only. In dev addon mode, ``start.py`` writes
      the env var from ``/data/options.json`` when the key is present;
      that signals "Supervisor authoritative" and ``"addon"`` is
      returned here. In stable addon mode the key is absent from
      schema, ``start.py`` doesn't write the env var, and the master
      falls through to env / file / default precedence so the
      standalone web UI master path remains the gate.
    - The ``BETA_FEATURE_FIELDS`` (sub-flags) follow the same
      shape — present in dev addon schema, absent from stable. Same
      env-var-presence signal distinguishes them at runtime.
    """
    field_name = next(
        (fname for fname, ename, _ in FEATURE_FLAG_FIELDS if ename == env_name),
        None,
    )
    is_master = field_name == "enable_beta_features"
    is_beta_sub = field_name in BETA_FEATURE_FIELDS
    # is_running_in_addon() (not a raw SUPERVISOR_TOKEN read) so the in-process
    # embedded server — which carries SUPERVISOR_TOKEN on HAOS but is not an
    # add-on — is treated as a standalone deployment: its settings-UI edits
    # persist to override files under HA_MCP_CONFIG_DIR instead of being routed
    # to a Supervisor add-on that does not exist.
    in_addon = is_running_in_addon()

    if in_addon:
        if is_master or is_beta_sub:
            # Dev addon: start.py wrote the env var from options.json
            # → Supervisor is the source of truth, mark addon-editable.
            # Stable addon: env var never written → fall through to
            # file/default. The master moved from "never schema-bound"
            # to "schema-bound on dev only"; the same env-var-presence
            # signal now distinguishes both for the master and the
            # beta sub-flags.
            if os.environ.get(env_name) is not None:
                return "addon"
            # else: stable / legacy-dev-no-master-key, fall through.
        else:
            return "addon"
    if os.environ.get(env_name) is not None:
        return "env"
    if field_name is None:
        return "default"
    overrides = _read_feature_flag_override_file()
    if field_name in overrides:
        return "file"
    return "default"


def _read_feature_flag_override_file() -> dict[str, object]:
    """Return the contents of the feature-flag override file, or ``{}``.

    Best-effort: a corrupt file MUST NOT break Settings loading. But
    the failure modes split into two categories that need different
    treatment:

    * **Silent**: file does not exist. The override layer is opt-in;
      a missing file is the normal "user has never edited" state and
      should not log.
    * **Loud (WARNING)**: file exists but is unreadable
      (``PermissionError``, broken filesystem) or unparseable
      (``JSONDecodeError``). The user toggled something, the UI said
      "Saved", and the value is silently being ignored. Without a log
      line they have no diagnostic; with one, the sidecar/server log
      tells them exactly what to fix.

    Data-dir resolution itself can raise (``RuntimeError`` when
    ``Path.home()`` cannot determine a home directory — typical of
    pytest's ``patch.dict(os.environ, {}, clear=True)``), so the
    ``get_data_dir()`` call is inside the try/except too. That branch
    is treated as silent: the user could not have created an override
    file in a directory we cannot resolve.
    """
    import json
    from pathlib import Path

    try:
        from .utils.data_paths import get_data_dir

        path: Path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
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
            "Feature-flag override file at %s exists but is unreadable; "
            "falling back to defaults. Check filesystem permissions.",
            path,
            exc_info=True,
        )
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(
            "Feature-flag override file at %s is not valid JSON; "
            "falling back to defaults. Delete or fix the file to "
            "re-enable persisted toggles.",
            path,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Feature-flag override file at %s is not a JSON object "
            "(got %s); falling back to defaults.",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _coerce_feature_flag_value(
    field_name: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce + bounds-check one override-file value for FEATURE_FLAG_FIELDS.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it. Mirrors the original inline ``continue`` behavior.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "Override for %r is %s; expected bool — ignoring.",
                field_name,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            logger.warning(
                "Override for %r is %s; expected int — ignoring.",
                field_name,
                type(raw).__name__,
            )
            return False, None
        coerced = int(raw)
        bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
        if bounds is not None and not (bounds[0] <= coerced <= bounds[1]):
            logger.warning(
                "Override for %r is %d, outside %d-%d — ignoring.",
                field_name,
                coerced,
                bounds[0],
                bounds[1],
            )
            return False, None
        return True, coerced
    return False, None


def _apply_one_feature_flag_override(
    settings: "Settings",
    field_name: str,
    env_name: str,
    ftype: RegistryFieldType,
    overrides: dict[str, object],
    in_addon: bool,
    beta_fields: set[str],
) -> None:
    """Apply a single FEATURE_FLAG_FIELDS override-file entry, if eligible."""
    is_beta = field_name in beta_fields
    if in_addon and not is_beta:
        # Non-beta addon mode: start.py owns it. Skip.
        return
    if os.environ.get(env_name) is not None:
        # Explicit env var wins over file for that field.
        return
    if field_name not in overrides:
        return
    ok, coerced = _coerce_feature_flag_value(field_name, ftype, overrides[field_name])
    if not ok:
        return
    if not hasattr(settings, field_name):
        logger.warning(
            "Override for %r (value=%r) targets a field that does "
            "not exist on Settings; ignoring. Likely a stale entry "
            "after a field was renamed/removed.",
            field_name,
            coerced,
        )
        return
    try:
        setattr(settings, field_name, coerced)
    except (ValueError, TypeError) as err:
        logger.warning(
            "Override for %r (value=%r) rejected by Settings (%s); ignoring.",
            field_name,
            coerced,
            err,
        )


def _apply_beta_master_gate(settings: "Settings") -> None:
    """Force BETA_FEATURE_FIELDS to False when ``enable_beta_features`` is off.

    This is the "master toggle" semantics: even a power user who sets
    ENABLE_YAML_CONFIG_EDITING=true via env var still needs to flip the
    master before the flag takes effect.
    """
    if not getattr(settings, "enable_beta_features", False):
        for sub in BETA_FEATURE_FIELDS:
            if not hasattr(settings, sub):
                logger.warning(
                    "Beta gate: %s is not a Settings attribute; "
                    "BETA_FEATURE_FIELDS may have drifted from the "
                    "model. Skipping.",
                    sub,
                )
                continue
            current = getattr(settings, sub, False)
            if current and sub not in _BETA_GATE_LOGGED:
                # Dedup per-process: cascade-clear (an earlier behavior
                # that wrote False to the override file for every truthy
                # sub-flag whenever the master was saved off) was
                # removed, so the file now holds truthy sub-flag values
                # long-term and this gate runs on every Settings
                # rebuild. Logging the force-False line every time would
                # spam addon logs. First-time-per-process is enough to
                # leave an audit trail for operators debugging "why is
                # my beta tool off?".
                logger.info(
                    "Beta master toggle is off; forcing %s=False "
                    "(was True via env/file).",
                    sub,
                )
                _BETA_GATE_LOGGED.add(sub)
            try:
                setattr(settings, sub, False)
            except (ValueError, TypeError) as err:
                logger.warning(
                    "Could not force %s=False via master gate (%s); ignoring.",
                    sub,
                    err,
                )


def _apply_feature_flag_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with override-file values + apply the master beta gate.

    Two behaviors interleave:

    1. **Per-field override-file application**: reads
       ``feature_flags.json`` and applies values for each
       FEATURE_FLAG_FIELDS entry, subject to: explicit env var wins over
       file; addon mode (SUPERVISOR_TOKEN set) normally short-circuits
       this branch because start.py owns env vars from config.yaml.

       EXCEPTION: the beta-master + beta-sub-flag fields skip the
       addon-mode short-circuit. The master isn't in any addon schema;
       the sub-flags are in the dev-addon schema (where ``start.py``
       writes the env var from options.json — env-var-wins skips the
       file read here, leaving Supervisor authoritative) but NOT in the
       stable schema (where the env var is never written, so the file
       is read and applied). In standalone mode neither is addon-routed.

    2. **Beta master gate**: after the per-field pass, if
       ``enable_beta_features`` is False on the resolved Settings,
       force-set the BETA_FEATURE_FIELDS to False regardless of
       how they currently look. This is the "master toggle" semantics —
       even a power user who sets ENABLE_YAML_CONFIG_EDITING=true via
       env var still needs to flip the master before the flag takes
       effect.
    """
    # is_running_in_addon() (not a raw SUPERVISOR_TOKEN read) so the in-process
    # embedded server — which carries SUPERVISOR_TOKEN on HAOS but is not an
    # add-on — applies its settings-UI feature-flag saves like a standalone
    # deployment instead of short-circuiting here as if start.py owned the env.
    in_addon = is_running_in_addon()
    overrides = _read_feature_flag_override_file()

    known = {fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS}
    beta_fields = {"enable_beta_features", *BETA_FEATURE_FIELDS}

    for field_name, (env_name, ftype) in known.items():
        _apply_one_feature_flag_override(
            settings, field_name, env_name, ftype, overrides, in_addon, beta_fields
        )

    _apply_beta_master_gate(settings)


def _coerce_advanced_override_value(
    fname: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce one override-file value to its ADVANCED_SETTINGS_FIELDS type.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "Advanced override for %r is %s; expected bool — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            logger.warning(
                "Advanced override for %r is %s; expected int — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, int(raw)
    elif ftype is float:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            logger.warning(
                "Advanced override for %r is %s; expected float — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, float(raw)
    elif ftype is str:
        if not isinstance(raw, str):
            logger.warning(
                "Advanced override for %r is %s; expected str — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        if "\x00" in raw:
            logger.warning(
                "Advanced override for %r contains null byte; ignoring.",
                fname,
            )
            return False, None
        return True, raw
    return False, None


def _advanced_override_passes_constraints(fname: str, coerced: Any) -> bool:
    """Bounds/sentinel/choices gate for a coerced ADVANCED_SETTINGS_FIELDS value."""
    bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
    sentinel = _ADVANCED_SETTINGS_SENTINELS.get(fname)
    if (
        bounds is not None
        and coerced != sentinel
        and not (bounds[0] <= coerced <= bounds[1])
    ):
        logger.warning(
            "Advanced override for %r is %s, outside %s-%s — ignoring.",
            fname,
            coerced,
            bounds[0],
            bounds[1],
        )
        return False
    choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
    if choices is not None and coerced not in choices:
        logger.warning(
            "Advanced override for %r is %r, not in %s — ignoring.",
            fname,
            coerced,
            choices,
        )
        return False
    return True


def _apply_one_advanced_override(
    settings: "Settings",
    fname: str,
    env_name: str,
    ftype: RegistryFieldType,
    editable: bool,
    overrides: dict[str, object],
) -> None:
    """Apply a single ADVANCED_SETTINGS_FIELDS override-file entry, if eligible."""
    if not editable:
        # Display-only field somehow landed in the override file (UI
        # POST guard at /api/settings/advanced blocks this, so the
        # only way in is direct hand-edit or upgrade-time drift).
        # Log so the operator can see why the value is being ignored.
        if fname in overrides:
            logger.warning(
                "Override for %r is ignored: field is marked "
                "display-only in ADVANCED_SETTINGS_FIELDS (set via "
                "env var or addon configuration instead).",
                fname,
            )
        return
    if os.environ.get(env_name) is not None:
        return
    if fname not in overrides:
        return
    ok, coerced = _coerce_advanced_override_value(fname, ftype, overrides[fname])
    if not ok:
        return
    if not _advanced_override_passes_constraints(fname, coerced):
        return
    try:
        setattr(settings, fname, coerced)
    except (ValueError, TypeError):
        # Narrowed from bare ``Exception`` to match the parallel
        # _apply_feature_flag_overrides handler. Pydantic validation
        # surfaces failures as ValueError; an
        # attribute that doesn't exist on the model would be a
        # programming bug we want to crash, not silently swallow.
        logger.warning(
            "Advanced override for %r could not be applied; ignoring.",
            fname,
            exc_info=True,
        )


def _apply_advanced_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with advanced-section override values from
    ``feature_flags.json``.

    Mirrors ``_apply_feature_flag_overrides`` but iterates
    ``ADVANCED_SETTINGS_FIELDS`` and supports float / str in addition
    to bool / int. Display-only fields (``editable=False`` in the
    registry) are NEVER applied — chicken-and-egg safeguard for
    connection settings.

    Addon-mode behavior: two advanced fields are in addon ``config.yaml``
    schemas — ``backup_hint`` and ``verify_ssl`` (both stable and dev).
    For those, ``start.py`` exports the env var on every boot and the
    env-var-wins check below correctly skips them. All other advanced
    fields (code_mode_* sub-numerics, mcp_server_*, log_level, debug,
    enabled_tool_modules, fuzzy_threshold, etc.) are NOT in any addon
    schema; the override file is the authoritative source and applies
    in either deployment mode.
    """
    overrides = _read_feature_flag_override_file()
    if not overrides:
        return
    for fname, env_name, ftype, _section, editable in ADVANCED_SETTINGS_FIELDS:
        _apply_one_advanced_override(
            settings, fname, env_name, ftype, editable, overrides
        )
