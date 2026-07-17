"""
Developer-mode tools for managing the ha-mcp server itself.

These tools are hidden behind the ``enable_dev_mode`` setting (the
"Developer" section at the bottom of the web settings UI's Server
Settings tab, or the ``HAMCP_ENABLE_DEV_MODE`` env var). When the flag
is off — the default — ``register_dev_tools`` registers nothing, so the
tools do not exist for MCP clients at all.

Feature Flag: Set HAMCP_ENABLE_DEV_MODE=true to enable these tools.
"""

import asyncio
import logging
import sys
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from .._version import get_version, is_dev_version, is_embedded, is_running_in_addon
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

# Feature flag - disabled by default; the toggle lives in the Developer
# section of the web settings UI (Server Settings tab, bottom).
FEATURE_FLAG = "HAMCP_ENABLE_DEV_MODE"

# Domain of the ha_mcp_tools custom component. Its "server" config entry
# runs the ha-mcp server in-process inside HA and exposes channel /
# pip-spec options that ha_dev_manage_server drives.
COMPONENT_DOMAIN = "ha_mcp_tools"

# The component's own in-process command for locating its "server" config
# entry (see ``_fetch_server_entry_via_component``) — replaces probing every
# ha_mcp_tools-domain entry's options-flow schema from the outside.
WS_SERVER_ENTRY = "ha_mcp_tools/server_entry"

# Options-flow field names of the component's server entry
# (custom_components/ha_mcp_tools/const.py OPT_CHANNEL / OPT_PIP_SPEC).
_OPT_CHANNEL = "channel"
_OPT_PIP_SPEC = "pip_spec"
_OPT_SERVER_URL = "server_url"
_OPT_EXTERNAL_URL = "external_url"
_OPT_WEBHOOK_ID_OVERRIDE = "webhook_id_override"
_OPT_SECRET_PATH_OVERRIDE = "secret_path_override"
_VALID_CHANNELS = ("stable", "dev")

# Optional text fields the component's options flow pre-fills via
# suggested_value (so the UI can clear them). Because an OMITTED optional field
# reads as "cleared" rather than "unchanged", a partial update_source submit
# must resend these at their current values or it would blank the user's
# server-URL / connect-secret overrides.
_PRESERVED_OPTION_KEYS = (
    _OPT_PIP_SPEC,
    _OPT_SERVER_URL,
    _OPT_EXTERNAL_URL,
    _OPT_WEBHOOK_ID_OVERRIDE,
    _OPT_SECRET_PATH_OVERRIDE,
)

# Delay before a self-affecting action (embedded entry reload / options
# submit) fires, so this tool's JSON response flushes to the MCP client
# before the serving thread is torn down. Mirrors
# settings_ui._supervisor._SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S, with more headroom
# because the embedded response may traverse the HA ingress/webhook hop.
_SELF_ACTION_FLUSH_DELAY_S = 1.0

# Strong references to in-flight fire-and-forget tasks so the event
# loop's weakref-only task table can't garbage-collect them mid-run.
# Same pattern as settings_ui._supervisor._BACKGROUND_RESTART_TASKS.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Sentinel marking a key for removal in _merge_file_override (reset action).
_REMOVE = object()


def is_dev_mode_enabled() -> bool:
    """Check if developer mode is enabled.

    Reads through :func:`config.get_global_settings` so the same
    env-var / override-file / default precedence path applies as
    every other runtime-editable Settings field. ``getattr`` with a
    False default, not attribute access: during an in-process package
    update the cached settings singleton can predate this field
    (issues #1783/#1785), and that stale read must mean "dev mode
    off", never AttributeError.
    """
    from ..config import get_global_settings

    return bool(getattr(get_global_settings(), "enable_dev_mode", False))


def _spawn_background(coro: Any) -> None:
    """Run ``coro`` as a strongly-referenced fire-and-forget task."""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _field_prefill(item: dict[str, Any]) -> Any:
    """Return a serialized options-flow field's current value.

    Reads ``description.suggested_value`` first: a persisted option is
    serialized there (as ``add_suggested_values_to_schema`` does; this component
    sets it directly on the ``vol.Optional`` marker), and the clearable text
    fields carry their value there rather than as a schema ``default`` (a
    ``default`` equal to the value would make the field impossible to clear).
    Falls back to ``default`` then ``value`` for the dropdown/toggle fields.
    Mirrors ``tools_integrations.options_from_form_flow``.
    """
    description = item.get("description")
    if isinstance(description, dict) and description.get("suggested_value") is not None:
        return description["suggested_value"]
    return item.get("default", item.get("value"))


async def _fetch_server_entry_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/server_entry`` read; ``None`` ⇒ use the legacy probe.

    Returns the component's ``{entry_id, channel, pip_spec}`` payload —
    ``entry_id`` is ``None`` when no server entry exists, an AUTHORITATIVE
    verdict the component reaches in-process (its own ``DOMAIN`` entries),
    not a "try the legacy path" signal. Returns ``None`` (⇒ legacy probe) on
    capability miss, downgrade (``unknown_command`` → invalidate the cached
    caps), command error/timeout (logged), or an empty-string ``entry_id``
    (a malformed reply shape — not the component's real "no entry" signal,
    which is ``None`` — so it is not trusted as authoritative either) — same
    taxonomy as ``component_devices.fetch_device_via_component``. A
    ``HomeAssistantConnectionError`` is not caught here; it propagates, since
    the legacy path shares the same socket and would fail identically.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "server_entry"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_SERVER_ENTRY)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_SERVER_ENTRY, exc)
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "entry_id" not in result:
        return None
    if result.get("entry_id") == "":
        return None
    return result


def _fields_from_flow_schema(flow: dict[str, Any]) -> dict[str, Any]:
    """Map an options-flow's ``data_schema`` field names to their current values.

    Shared by ``_open_server_entry_flow`` (the component-identified entry)
    and ``find_server_config_entry``'s legacy per-candidate probe — both open
    a flow and need the same ``{field_name: current_value}`` shape, via
    ``_field_prefill``.
    """
    schema = flow.get("data_schema") or []
    return {
        str(item["name"]): _field_prefill(item)
        for item in schema
        if isinstance(item, dict) and item.get("name")
    }


async def _open_server_entry_flow(
    client: Any, entry_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """Open the options flow for a KNOWN server ``entry_id``; ``None`` on failure.

    Builds ``current_options`` from the freshly-opened flow's own schema (via
    ``_fields_from_flow_schema``) rather than the component's narrower
    ``{channel, pip_spec}`` shape, so callers like ``_update_source`` that
    resend ``_PRESERVED_OPTION_KEYS`` (``server_url`` / ``external_url`` /
    ``webhook_id_override`` / ``secret_path_override`` — fields the
    ``server_entry`` capability does not carry) still see them.
    """
    try:
        flow = await client.start_options_flow(entry_id)
    except HomeAssistantAPIError as exc:
        # The component-identified entry couldn't open its flow (e.g. a race
        # where the entry vanished between the two calls) — the caller falls
        # back to the legacy per-candidate probe rather than treating this as
        # authoritative "no server entry".
        logger.debug("Options-flow open failed for %s: %s", entry_id, exc)
        return None
    return str(entry_id), flow, _fields_from_flow_schema(flow)


async def find_server_config_entry(
    client: Any,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """Find the ha_mcp_tools in-process "server" config entry.

    When the component advertises ``server_entry``, one
    ``ha_mcp_tools/server_entry`` read identifies the entry in-process (no
    per-candidate options-flow probing, no ``pip_spec`` schema-shape
    heuristic); an authoritative "no server entry" verdict from that read
    returns ``None`` directly. Only ONE options flow is then opened — for the
    identified entry — because the write path (``_update_source``) submits
    it; a component read never substitutes for that flow.

    On capability miss, component error, or a failure to open the identified
    entry's flow, this falls back to probing each ``ha_mcp_tools`` entry's
    options flow directly: the server entry's flow is a form whose schema
    carries the ``pip_spec`` field; the tools (services) entry's flow is an
    informational form with no ``pip_spec`` field, so it never matches.

    Returns ``(entry_id, open_flow, current_options)`` with the options flow
    left OPEN (callers must submit or abort it), or ``None`` when no server
    entry exists. ``current_options`` maps schema field names to their
    current values (persisted ``suggested_value`` first, else the schema
    ``default`` or ``value``, via ``_field_prefill``).

    Module-level (not a DevTools method) so the settings UI's embedded
    restart handler can share it.
    """
    via_component = await _fetch_server_entry_via_component(client)
    if via_component is not None:
        entry_id = via_component.get("entry_id")
        if entry_id is None:
            # Authoritative: the component confirmed no server entry exists.
            return None
        found = await _open_server_entry_flow(client, str(entry_id))
        if found is not None:
            return found
        # Fall through to the legacy probe below as a defensive retry.

    response = await client.send_websocket_message(
        {"type": "config_entries/get", "domain": COMPONENT_DOMAIN}
    )
    if not response.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"config_entries/get failed: {response.get('error')}",
                suggestions=["Check the Home Assistant WebSocket connection"],
            )
        )
    result = response.get("result", [])
    entries = result if isinstance(result, list) else []
    for entry in entries:
        entry_id = entry.get("entry_id")
        if not entry_id:
            continue
        try:
            flow = await client.start_options_flow(entry_id)
        except HomeAssistantAPIError as exc:
            # This entry's flow can't open (e.g. an entry type without an
            # options flow) — skip it and keep probing. Connection/auth
            # errors deliberately propagate: swallowing them here would
            # make a broken connection indistinguishable from "no server
            # entry exists" and steer the caller toward reinstalling a
            # component that is already running.
            logger.debug("Options-flow probe failed for %s: %s", entry_id, exc)
            continue
        fields = _fields_from_flow_schema(flow)
        if flow.get("type") == "form" and _OPT_PIP_SPEC in fields:
            return str(entry_id), flow, fields
        # Not the server entry — close the probe flow if one opened.
        await abort_options_flow_quietly(client, flow)
    return None


async def abort_options_flow_quietly(client: Any, flow: dict[str, Any]) -> None:
    """Abort an open options flow, ignoring failures."""
    flow_id = flow.get("flow_id")
    if not flow_id:
        return
    try:
        await client.abort_options_flow(flow_id)
    except Exception as exc:
        logger.debug("Options-flow abort failed: %s", exc)


def schedule_deferred_entry_reload(client: Any, entry_id: str) -> None:
    """Reload a config entry after the response-flush delay, fire-and-forget.

    Self-restart path for the embedded server: the reload tears down the
    very worker answering the current request, so it must not run until the
    response has flushed. Failures can only be logged — there is no caller
    left to answer.
    """

    async def _reload() -> None:
        await asyncio.sleep(_SELF_ACTION_FLUSH_DELAY_S)
        try:
            await client._request(
                "POST", f"/config/config_entries/entry/{entry_id}/reload"
            )
            logger.info("Deferred reload of entry %s requested", entry_id)
        except Exception:
            logger.exception("Deferred config-entry reload failed")

    _spawn_background(_reload())


class DevTools:
    """Developer-mode tools for server introspection, update, and settings."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ----- settings management helpers -----

    @staticmethod
    def _advanced_origin(fname: str, env_name: str, overrides: dict[str, Any]) -> str:
        """Origin for an ADVANCED_SETTINGS_FIELDS entry.

        Mirrors the settings UI's ``_origin_for_advanced_field``:
        addon-synced fields report ``addon`` in add-on mode (writes
        route through Supervisor); an explicitly-set env var wins
        (``env``, locked); a value in the override file is ``file``;
        otherwise ``default``.
        """
        import os

        from ..config import ADDON_SYNCED_ADVANCED_FIELDS

        if is_running_in_addon() and fname in ADDON_SYNCED_ADVANCED_FIELDS:
            return "addon"
        if os.environ.get(env_name) is not None:
            return "env"
        if fname in overrides:
            return "file"
        return "default"

    def _settings_rows(self) -> list[dict[str, Any]]:
        """Build the full settings matrix from both registries."""
        from ..config import (
            _ADVANCED_SETTINGS_BOUNDS,
            _ADVANCED_SETTINGS_CHOICES,
            _ADVANCED_SETTINGS_SENTINELS,
            _FEATURE_FLAG_INT_BOUNDS,
            ADVANCED_SETTINGS_FIELDS,
            FEATURE_FLAG_FIELDS,
            OAUTH_MODE_TOKEN,
            _read_feature_flag_override_file,
            get_feature_flag_origin,
            get_global_settings,
        )

        settings = get_global_settings()
        overrides = _read_feature_flag_override_file()
        rows: list[dict[str, Any]] = []
        bounds: tuple[float, float] | None
        for fname, env_name, ftype in FEATURE_FLAG_FIELDS:
            origin = get_feature_flag_origin(env_name)
            row: dict[str, Any] = {
                "setting": fname,
                "env_var": env_name,
                "value": getattr(settings, fname),
                "type": ftype.__name__,
                "registry": "features",
                "origin": origin,
                "editable": origin in ("addon", "file", "default"),
            }
            bounds = _FEATURE_FLAG_INT_BOUNDS.get(fname)
            if bounds is not None:
                row["min"], row["max"] = bounds
            rows.append(row)
        for (
            fname,
            env_name,
            ftype,
            section,
            registry_editable,
        ) in ADVANCED_SETTINGS_FIELDS:
            origin = self._advanced_origin(fname, env_name, overrides)
            value: Any = getattr(settings, fname, None)
            # Never echo the real long-lived token; the OAuth-mode
            # sentinel survives so deployment mode stays visible.
            if fname == "homeassistant_token":
                value = "*****" if value and value != OAUTH_MODE_TOKEN else value
            row = {
                "setting": fname,
                "env_var": env_name,
                "value": value,
                "type": ftype.__name__,
                "registry": "advanced",
                "section": section,
                "origin": origin,
                "editable": registry_editable and origin != "env",
            }
            bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
            if bounds is not None:
                row["min"], row["max"] = bounds
                sentinel = _ADVANCED_SETTINGS_SENTINELS.get(fname)
                if sentinel is not None:
                    row["min"] = sentinel
            choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
            if choices is not None:
                row["choices"] = list(choices)
            rows.append(row)
        return rows

    @staticmethod
    def _coerce_setting_value(fname: str, raw: Any, ftype: type) -> Any:
        """Coerce/validate ``raw`` against the registry field type.

        Accepts the natural JSON type plus common MCP-client
        stringifications ("true"/"false" for bools, numeric strings
        for int/float). Raises ``ToolError`` on mismatch.
        """
        if ftype is bool:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
                return raw.strip().lower() == "true"
        elif ftype is int:
            if isinstance(raw, bool):
                pass  # bool is an int subclass; reject below
            elif isinstance(raw, int):
                return raw
            elif isinstance(raw, str):
                try:
                    return int(raw.strip())
                except ValueError:
                    pass
        elif ftype is float:
            if isinstance(raw, bool):
                pass
            elif isinstance(raw, int | float):
                return float(raw)
            elif isinstance(raw, str):
                try:
                    return float(raw.strip())
                except ValueError:
                    pass
        elif ftype is str:
            if isinstance(raw, str):
                if "\x00" in raw:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{fname!r} value contains a null byte",
                        )
                    )
                return raw
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{fname!r} must be of type {ftype.__name__}, got {type(raw).__name__}",
                context={"setting": fname, "value": raw},
            )
        )
        return None  # unreachable; explicit for CodeQL

    @staticmethod
    async def _merge_file_override(changes: dict[str, Any]) -> None:
        """Read-merge-write ``changes`` into the shared override file.

        Uses the settings UI's override-file lock and atomic-write
        helper so a concurrent web-UI save can't interleave and
        clobber this write (or vice versa). Refuses to overwrite an
        unreadable or corrupt existing file — same data-loss guard as
        the web UI save handlers.
        """
        import json

        from ..config import _FEATURE_FLAG_OVERRIDE_FILENAME
        from ..settings_ui._persistence import (
            _atomic_write_json,
            _get_override_file_lock,
        )
        from ..utils.data_paths import get_data_dir

        path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
        async with _get_override_file_lock():
            existing: dict[str, Any] = {}
            try:
                # Executor thread: file I/O must not block the event loop.
                existing_raw = await asyncio.to_thread(path.read_text)
            except FileNotFoundError:
                existing_raw = None
            except OSError as exc:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Could not read existing override file "
                        f"({type(exc).__name__}: {exc}); refusing to "
                        "overwrite to preserve prior settings.",
                        suggestions=["Check filesystem permissions and retry"],
                    )
                )
            if existing_raw is not None:
                try:
                    parsed = json.loads(existing_raw)
                except json.JSONDecodeError as exc:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.CONFIG_INVALID,
                            f"Existing override file at {path} is not valid "
                            f"JSON ({exc}); refusing to overwrite to preserve "
                            "prior settings.",
                            suggestions=[
                                "Inspect or delete the file manually and retry"
                            ],
                        )
                    )
                if isinstance(parsed, dict):
                    existing = parsed
            for key, val in changes.items():
                if val is _REMOVE:
                    existing.pop(key, None)
                else:
                    existing[key] = val
            await asyncio.to_thread(_atomic_write_json, path, existing)

    # ----- server-entry helpers -----

    async def _delayed_submit_options(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> None:
        """Submit an options flow after the response-flush delay.

        Fire-and-forget path for embedded mode: applying the options
        reloads the config entry, which tears down the very server
        thread answering this tool call, so the submit must happen
        after our JSON response has flushed. Failures can only be
        logged — there is no caller left to answer.
        """
        await asyncio.sleep(_SELF_ACTION_FLUSH_DELAY_S)
        try:
            result = await self._client.submit_options_flow_step(flow_id, user_input)
            if result.get("type") == "create_entry":
                logger.info("Deferred options submit applied")
            else:
                # Fire-and-forget: no caller is left to raise to, so a rejected
                # self-restart must at least be discoverable in the log.
                logger.warning(
                    "Deferred options submit was not applied (type=%s, errors=%s)",
                    result.get("type"),
                    result.get("errors") or result.get("reason"),
                )
        except Exception:
            logger.exception("Deferred options-flow submit failed")

    # ----- tools -----

    @tool(
        name="ha_dev_manage_settings",
        tags={"Developer"},
        annotations={
            "openWorldHint": False,
            "title": "Manage Server Settings (dev)",
            "destructiveHint": True,
        },
    )
    @log_tool_usage
    async def ha_dev_manage_settings(
        self,
        action: Annotated[
            Literal["list", "set", "reset"],
            Field(
                description="list all settings, set one value, or reset one override"
            ),
        ],
        setting: Annotated[
            str | None,
            Field(default=None, description="Setting name (required for set/reset)"),
        ] = None,
        value: Annotated[
            bool | int | float | str | None,
            Field(default=None, description="New value (required for set)"),
        ] = None,
    ) -> dict[str, Any]:
        """Manage ha-mcp server settings directly (developer mode).

        When NOT to use: for HA entity/automation configuration use the
        ha_config_* tools; for enabling/disabling individual MCP tools
        use the web settings UI (Tools tab).

        When to use: reading or changing the server's own settings (the
        same matrix as the web UI's Server Settings tab) during
        development/testing — feature flags, advanced fields, and their
        env/file/addon origins.

        Caveats: changed values persist to the server's override file
        (or the add-on options via Supervisor) but most settings only
        take effect after a server restart (ha_dev_manage_server
        action="restart").
        Env-pinned settings are read-only until the env var is unset.
        This can flip security-sensitive flags; treat with the same
        care as editing the web UI.

        EXAMPLES:
        ha_dev_manage_settings("list")
        ha_dev_manage_settings("set", setting="log_level", value="DEBUG")
        ha_dev_manage_settings("reset", setting="log_level")
        """
        try:
            if action == "list":
                return {
                    "success": True,
                    "data": {
                        "settings": await asyncio.to_thread(self._settings_rows),
                        "is_addon": is_running_in_addon(),
                        "is_embedded": is_embedded(),
                        "note": (
                            "Most settings need a server restart to take "
                            "effect (ha_dev_manage_server action='restart')."
                        ),
                    },
                }

            if not setting:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        f"'setting' is required for action={action!r}",
                    )
                )

            from ..config import (
                _ADVANCED_SETTINGS_BOUNDS,
                _ADVANCED_SETTINGS_CHOICES,
                _ADVANCED_SETTINGS_SENTINELS,
                _FEATURE_FLAG_INT_BOUNDS,
                ADVANCED_SETTINGS_FIELDS,
                BETA_FEATURE_FIELDS,
                FEATURE_FLAG_FIELDS,
                _read_feature_flag_override_file,
                _reset_global_settings,
                get_feature_flag_origin,
                get_global_settings,
            )

            features = {f: (e, t) for f, e, t in FEATURE_FLAG_FIELDS}
            advanced = {f: (e, t, s, ed) for f, e, t, s, ed in ADVANCED_SETTINGS_FIELDS}
            bounds: tuple[float, float] | None
            sentinel: int | None
            choices: tuple[str, ...] | None
            if setting in features:
                env_name, ftype = features[setting]
                origin = get_feature_flag_origin(env_name)
                editable = origin in ("addon", "file", "default")
                bounds = _FEATURE_FLAG_INT_BOUNDS.get(setting)
                sentinel = None
                choices = None
            elif setting in advanced:
                env_name, ftype, _section, registry_editable = advanced[setting]
                overrides = await asyncio.to_thread(_read_feature_flag_override_file)
                origin = self._advanced_origin(setting, env_name, overrides)
                editable = registry_editable and origin != "env"
                bounds = _ADVANCED_SETTINGS_BOUNDS.get(setting)
                sentinel = _ADVANCED_SETTINGS_SENTINELS.get(setting)
                choices = _ADVANCED_SETTINGS_CHOICES.get(setting)
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Unknown setting: {setting!r}",
                        suggestions=[
                            "Call ha_dev_manage_settings('list') for valid names"
                        ],
                    )
                )

            if action == "reset":
                if origin == "env":
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{setting!r} is pinned by the {env_name} env var; "
                            "unset the env var instead.",
                        )
                    )
                if origin == "addon":
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{setting!r} is managed by the add-on "
                            "configuration; change it there or via "
                            "action='set'.",
                        )
                    )
                had_override = origin == "file"
                if had_override:
                    await self._merge_file_override({setting: _REMOVE})
                    _reset_global_settings()
                return {
                    "success": True,
                    "data": {
                        "setting": setting,
                        "removed_override": had_override,
                        "restart_required": had_override,
                    },
                }

            # action == "set"
            if value is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "'value' is required for action='set'",
                    )
                )
            if not editable:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{setting!r} is locked by {origin}. "
                        + (
                            f"Unset the {env_name} env var to edit it here."
                            if origin == "env"
                            else "It is display-only on this surface."
                        ),
                    )
                )
            coerced = self._coerce_setting_value(setting, value, ftype)
            if (
                bounds is not None
                and coerced != sentinel
                and not (bounds[0] <= coerced <= bounds[1])
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{setting!r} must be between {bounds[0]} and {bounds[1]}",
                    )
                )
            if choices is not None and coerced not in choices:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{setting!r} must be one of {list(choices)}",
                    )
                )
            # Master beta gate: enabling a beta sub-flag while the
            # effective master is off would be silently forced back off
            # at runtime — reject loudly instead (same rule as the web
            # UI save handler).
            if (
                setting in BETA_FEATURE_FIELDS
                and bool(coerced)
                and not get_global_settings().enable_beta_features
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Cannot enable beta sub-flag {setting!r} while "
                        "'enable_beta_features' is off.",
                        suggestions=["Set enable_beta_features=true first, then retry"],
                    )
                )

            if origin == "addon":
                from ..settings_ui._supervisor import _supervisor_merge_and_post_options

                ok, err = await _supervisor_merge_and_post_options(
                    get_global_settings().verify_ssl, {setting: coerced}
                )
                if not ok:
                    message = err.message if err else "Supervisor update failed"
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Supervisor rejected the options update: {message}",
                            suggestions=["Check the Supervisor and add-on logs"],
                        )
                    )
                mode = "addon"
            else:
                await self._merge_file_override({setting: coerced})
                _reset_global_settings()
                mode = "file"
            return {
                "success": True,
                "data": {
                    "setting": setting,
                    "value": coerced,
                    "mode": mode,
                    "restart_required": True,
                },
            }
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "setting": setting},
                suggestions=["Check server logs for details"],
            )
            return None  # unreachable; explicit for CodeQL

    @tool(
        name="ha_dev_manage_server",
        tags={"Developer"},
        annotations={
            "openWorldHint": True,
            "title": "Manage MCP Server (dev)",
            "destructiveHint": True,
        },
    )
    @log_tool_usage
    async def ha_dev_manage_server(
        self,
        action: Annotated[
            Literal["info", "update_source", "restart"],
            Field(
                description=(
                    "info: deployment/version report; update_source: point the "
                    "in-process server at a channel or pip spec and reinstall; "
                    "restart: restart this server"
                )
            ),
        ],
        channel: Annotated[
            str | None,
            Field(
                default=None,
                description="Release channel for update_source: 'stable' or 'dev'",
            ),
        ] = None,
        pip_spec: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Explicit pip requirement for update_source — a version pin "
                    "(ha-mcp==7.9.0) or a GitHub tarball URL such as "
                    "https://github.com/homeassistant-ai/ha-mcp/archive/refs/"
                    "pull/<PR>/head.tar.gz. Empty string clears the override."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage the running ha-mcp server itself (developer mode).

        When NOT to use: to restart Home Assistant use ha_restart; to
        update HA add-ons or HACS packages use ha_manage_addon /
        ha_manage_hacs.

        When to use: development/testing workflows — inspecting how this
        server is deployed, switching the in-process (custom component)
        server to another release channel or an arbitrary pip spec such
        as a PR tarball, and restarting the server so config or code
        changes take effect.

        Caveats: restart interrupts this MCP connection in embedded and
        add-on deployments (the reply arrives just before the server
        goes down); update_source only self-interrupts in embedded mode
        — elsewhere it reloads the separate in-process server entry
        without dropping this connection. Reinstalls can take minutes.
        update_source requires the ha_mcp_tools component's in-process
        server entry; restart supports embedded and add-on deployments
        only (standalone processes must be restarted externally).

        EXAMPLES:
        ha_dev_manage_server("info")
        ha_dev_manage_server("update_source", channel="dev")
        ha_dev_manage_server("update_source", pip_spec="https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1234/head.tar.gz")
        ha_dev_manage_server("restart")
        """
        try:
            if action == "info":
                return await self._server_info()
            if action == "update_source":
                return await self._update_source(channel, pip_spec)
            return await self._restart_server()
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "channel": channel, "pip_spec": pip_spec},
                suggestions=["Check server and Home Assistant logs for details"],
            )
            return None  # unreachable; explicit for CodeQL

    async def _server_info(self) -> dict[str, Any]:
        from ..utils.data_paths import get_data_dir

        version = await asyncio.to_thread(get_version)
        if is_embedded():
            mode = "embedded"
        elif is_running_in_addon():
            mode = "addon"
        else:
            mode = "standalone"
        data: dict[str, Any] = {
            "server_version": version,
            "is_dev_build": is_dev_version(version),
            "deployment_mode": mode,
            "python_version": sys.version.split()[0],
            "data_dir": str(get_data_dir()),
        }
        warnings: list[str] = []
        try:
            ha_config = await self._client.get_config()
            data["ha_version"] = ha_config.get("version")
        except Exception as exc:
            warnings.append(f"Could not read HA version: {exc}")
        try:
            found = await find_server_config_entry(self._client)
            if found is None:
                data["component_server_entry"] = None
            else:
                entry_id, flow, options = found
                await abort_options_flow_quietly(self._client, flow)
                data["component_server_entry"] = {
                    "entry_id": entry_id,
                    "channel": options.get(_OPT_CHANNEL),
                    "pip_spec": options.get(_OPT_PIP_SPEC),
                }
        except Exception as exc:
            # Best-effort probe: a failure here (including the ToolError the
            # entry discovery raises on a config_entries/get failure) must
            # degrade the info report to a warning, mirroring the HA-version
            # probe above — not hard-fail the whole diagnostic.
            warnings.append(f"Could not inspect component server entry: {exc}")
        result: dict[str, Any] = {"success": True, "data": data}
        if warnings:
            result["warnings"] = warnings
        return result

    async def _update_source(
        self, channel: str | None, pip_spec: str | None
    ) -> dict[str, Any]:
        if channel is None and pip_spec is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "update_source needs 'channel' and/or 'pip_spec'",
                )
            )
        if channel is not None and channel not in _VALID_CHANNELS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"channel must be one of {list(_VALID_CHANNELS)}",
                )
            )
        if pip_spec is not None and (
            len(pip_spec) > 500 or any(ord(c) < 32 for c in pip_spec)
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "pip_spec must be a single-line string under 500 chars",
                )
            )

        found = await find_server_config_entry(self._client)
        if found is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.COMPONENT_NOT_INSTALLED,
                    "No ha_mcp_tools in-process server entry found. "
                    "update_source drives that entry's channel/pip-spec "
                    "options, so it needs the entry to exist.",
                    suggestions=[
                        "Install the ha_mcp_tools component and add its "
                        + "'server' entry (Settings > Devices & Services)",
                        "Setup guide: https://github.com/homeassistant-ai/"
                        + "ha-mcp/blob/master/docs/in-process-server.md",
                    ],
                )
            )
        entry_id, flow, current = found
        # Resend the user's current overrides (see _PRESERVED_OPTION_KEYS) so a
        # channel/pip-spec change here does not blank them — an omitted optional
        # field reads as "cleared", not "unchanged".
        user_input: dict[str, Any] = {
            key: current[key] for key in _PRESERVED_OPTION_KEYS if current.get(key)
        }
        if channel is not None:
            user_input[_OPT_CHANNEL] = channel
        if pip_spec is not None:
            user_input[_OPT_PIP_SPEC] = pip_spec
        flow_id = flow.get("flow_id")
        if not flow_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Options flow opened without a flow_id",
                )
            )

        if is_embedded():
            # Applying options reloads the entry that runs THIS server;
            # defer the submit so this response reaches the client first.
            _spawn_background(self._delayed_submit_options(flow_id, user_input))
            return {
                "success": True,
                "data": {
                    "scheduled": True,
                    "entry_id": entry_id,
                    "applying": user_input,
                    "previous": {
                        _OPT_CHANNEL: current.get(_OPT_CHANNEL),
                        _OPT_PIP_SPEC: current.get(_OPT_PIP_SPEC),
                    },
                    "note": (
                        "The in-process server will reinstall and restart "
                        "now; this connection will drop. Reconnect in 1-5 "
                        "minutes and verify with ha_dev_manage_server('info')."
                    ),
                },
            }

        result = await self._client.submit_options_flow_step(flow_id, user_input)
        if result.get("type") != "create_entry":
            errors = result.get("errors") or result.get("reason")
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    f"Options flow did not apply: {errors}",
                    context={"flow_result_type": result.get("type")},
                )
            )
        return {
            "success": True,
            "data": {
                "entry_id": entry_id,
                "applied": user_input,
                "previous": {
                    _OPT_CHANNEL: current.get(_OPT_CHANNEL),
                    _OPT_PIP_SPEC: current.get(_OPT_PIP_SPEC),
                },
                "note": (
                    "The component is reloading the in-process server with "
                    "the new source; installs can take a few minutes."
                ),
            },
        }

    async def _restart_server(self) -> dict[str, Any]:
        if is_embedded():
            found = await find_server_config_entry(self._client)
            if found is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.COMPONENT_NOT_INSTALLED,
                        "Embedded mode detected but no in-process server "
                        "entry was found to reload.",
                        suggestions=["Reload the ha_mcp_tools integration in HA"],
                    )
                )
            entry_id, flow, _options = found
            await abort_options_flow_quietly(self._client, flow)
            schedule_deferred_entry_reload(self._client, entry_id)
            return {
                "success": True,
                "data": {
                    "scheduled": True,
                    "mode": "embedded",
                    "note": (
                        "Config entry reload scheduled; this connection will "
                        "drop. Reconnect in ~1 minute."
                    ),
                },
            }
        if is_running_in_addon():
            from ..config import get_global_settings
            from ..settings_ui._supervisor import _schedule_supervisor_self_restart

            _schedule_supervisor_self_restart(get_global_settings().verify_ssl)
            return {
                "success": True,
                "data": {
                    "scheduled": True,
                    "mode": "addon",
                    "note": (
                        "Supervisor add-on self-restart scheduled; this "
                        "connection will drop. Reconnect in ~1 minute."
                    ),
                },
            }
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "restart is only available in embedded (custom component) "
                "or add-on deployments; restart this standalone process "
                "externally.",
                suggestions=[
                    "Docker: docker restart <container>",
                    "Desktop MCP clients: relaunch the client to respawn "
                    + "the stdio server",
                ],
            )
        )
        return None  # unreachable; explicit for CodeQL


def register_dev_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register developer-mode tools.

    Registers NOTHING unless developer mode is enabled — the tools are
    invisible to MCP clients by default. Enable via the Developer
    section of the web settings UI or HAMCP_ENABLE_DEV_MODE=true.
    """
    if not is_dev_mode_enabled():
        logger.debug(f"Dev tools disabled (set {FEATURE_FLAG}=true to enable)")
        return

    logger.warning(
        "Developer mode is ON: ha_dev_manage_server / ha_dev_manage_settings "
        "are registered. These tools can change server settings and replace "
        "the running server version."
    )
    register_tool_methods(mcp, DevTools(client))
