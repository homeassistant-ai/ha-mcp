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
import json
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
from .util_helpers import JSON_STRING_COERCION

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

# The WRITE counterpart: applies a channel / pip_spec delta to the server entry via
# ``async_update_entry`` directly (embedded mode only — see
# ``_update_source_via_component``), collapsing update_source's options-flow start +
# submit round-trip. Gated on the ``server_entry_update`` capability.
WS_SERVER_ENTRY_UPDATE = "ha_mcp_tools/server_entry_update"

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

# Sentinel returned by the per-type ``_coerce_*_setting`` helpers when ``raw``
# is not coercible to that type — the caller then raises the generic
# type-mismatch error.
_COERCE_MISS = object()


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
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed
    (re)connect — is caught here and mapped to ``None``: the legacy probe
    (``find_server_config_entry``) rides the ``send_websocket_message`` bridge,
    which answers a component-side fault rather than dying with it — so a
    transport failure must fall back rather than escape.
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
    except Exception as exc:
        # HomeAssistantConnectionError / plain establish Exception → legacy probe
        # (which rides the send_websocket_message bridge).
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_SERVER_ENTRY, exc
        )
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

    def __init__(self, client: Any, server: Any | None = None) -> None:
        self._client = client
        # The live server object. The registry always passes it, so it is
        # present in every real deployment that registers these dev tools;
        # ``None`` is only a defensive fallback (no real path constructs
        # DevTools without a server). Used for the approval queue and the
        # unfiltered tool registry the Tools/Policies actions drive.
        self._server = server

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
            coerced = DevTools._coerce_bool_setting(raw)
        elif ftype is int:
            coerced = DevTools._coerce_int_setting(raw)
        elif ftype is float:
            coerced = DevTools._coerce_float_setting(raw)
        elif ftype is str:
            coerced = DevTools._coerce_str_setting(fname, raw)
        else:
            coerced = _COERCE_MISS
        if coerced is not _COERCE_MISS:
            return coerced
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{fname!r} must be of type {ftype.__name__}, got {type(raw).__name__}",
                context={"setting": fname, "value": raw},
            )
        )
        return None  # unreachable; explicit for CodeQL

    @staticmethod
    def _coerce_bool_setting(raw: Any) -> Any:
        """Coerce ``raw`` to bool, or ``_COERCE_MISS`` if not coercible."""
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        return _COERCE_MISS

    @staticmethod
    def _coerce_int_setting(raw: Any) -> Any:
        """Coerce ``raw`` to int, or ``_COERCE_MISS`` if not coercible."""
        if isinstance(raw, bool):
            pass  # bool is an int subclass; reject below
        elif isinstance(raw, int):
            return raw
        elif isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                pass
        return _COERCE_MISS

    @staticmethod
    def _coerce_float_setting(raw: Any) -> Any:
        """Coerce ``raw`` to float, or ``_COERCE_MISS`` if not coercible."""
        if isinstance(raw, bool):
            pass
        elif isinstance(raw, int | float):
            return float(raw)
        elif isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                pass
        return _COERCE_MISS

    @staticmethod
    def _coerce_str_setting(fname: str, raw: Any) -> Any:
        """Coerce ``raw`` to str, or ``_COERCE_MISS`` if not a string.

        Raises ``ToolError`` when a string contains a null byte.
        """
        if isinstance(raw, str):
            if "\x00" in raw:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} value contains a null byte",
                    )
                )
            return raw
        return _COERCE_MISS

    @staticmethod
    async def _merge_file_override(changes: dict[str, Any]) -> None:
        """Read-merge-write ``changes`` into the shared override file.

        Uses the settings UI's override-file lock and atomic-write
        helper so a concurrent web-UI save can't interleave and
        clobber this write (or vice versa). Refuses to overwrite an
        unreadable or corrupt existing file — same data-loss guard as
        the web UI save handlers.
        """
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
            Literal[
                "list",
                "set",
                "reset",
                "list_tools",
                "set_tool",
                "get_policy",
                "set_policy",
                "get_backup_config",
                "set_backup_config",
            ],
            Field(
                description=(
                    "Server-settings matrix: list / set / reset. Tools tab: "
                    "list_tools / set_tool (enable-disable-pin, LLM-API, security "
                    "gate). Security policies: get_policy / set_policy. Auto-backup "
                    "config: get_backup_config / set_backup_config."
                )
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
        tool: Annotated[
            str | None,
            Field(default=None, description="Tool name (required for set_tool)"),
        ] = None,
        state: Annotated[
            Literal["enabled", "disabled", "pinned"] | None,
            Field(
                default=None,
                description="set_tool: enable, disable, or pin the tool",
            ),
        ] = None,
        llm_api: Annotated[
            bool | None,
            Field(
                default=None,
                description=(
                    "set_tool: expose the tool to HA conversation agents "
                    "(effective only on the embedded custom-component server)"
                ),
            ),
        ] = None,
        gated: Annotated[
            bool | None,
            Field(
                default=None,
                description=(
                    "set_tool: require user approval before every call to this "
                    "tool (adds/removes an unconditional security-policy rule)"
                ),
            ),
        ] = None,
        policy: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "set_policy: the full policy object "
                    "{wait_seconds, approval_ttl_minutes, rules, version}"
                ),
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "set_policy: the version from your last get_policy, for "
                    "optimistic-concurrency safety (else the policy's own "
                    "version field is used)"
                ),
            ),
        ] = None,
        backup: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "set_backup_config: {field: value} of auto-backup settings "
                    "to change (see get_backup_config for field names)"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage ha-mcp server settings and the Tools/Policies/Backups surfaces (developer mode).

        Drives everything the web settings UI can change: the Server
        Settings matrix (list/set/reset), the Tools tab (enable/disable/pin,
        LLM-API exposure, and the per-tool security gate), the Tool Security
        Policies editor (get_policy/set_policy), and the auto-backup config
        (get_backup_config/set_backup_config). Use ha_dev_manage_server for
        the live approval queue and to restart.

        When NOT to use: for HA entity/automation configuration use the
        ha_config_* tools.

        Caveats: enable/disable/pin and most server settings take effect
        only after a restart (ha_dev_manage_server action="restart");
        LLM-API exposure, security gates, and policy edits apply live.
        Env-pinned settings/tools are read-only until the env var is unset.
        These actions can flip security-sensitive state — treat with the
        same care as editing the web UI.

        EXAMPLES:
        ha_dev_manage_settings("list_tools")
        ha_dev_manage_settings("set_tool", tool="ha_write_file", state="disabled")
        ha_dev_manage_settings("set_tool", tool="ha_call_service", gated=True)
        ha_dev_manage_settings("get_policy")
        ha_dev_manage_settings("set_backup_config", backup={"enable_auto_backup": False})
        """
        try:
            if action in ("list", "set", "reset"):
                return await self._manage_server_setting(action, setting, value)
            if action == "list_tools":
                return await self._list_tool_states()
            if action == "set_tool":
                return await self._apply_set_tool(tool, state, llm_api, gated)
            if action == "get_policy":
                return await self._get_policy()
            if action == "set_policy":
                return await self._apply_set_policy(policy, expected_version)
            if action == "get_backup_config":
                return await self._get_backup_config()
            return await self._apply_set_backup_config(backup)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "setting": setting, "tool": tool},
                suggestions=["Check server logs for details"],
            )
            return None  # unreachable; explicit for CodeQL

    async def _manage_server_setting(
        self,
        action: str,
        setting: str | None,
        value: bool | int | float | str | None,
    ) -> dict[str, Any]:
        """Handle the server-settings matrix actions (list / set / reset).

        Extracted from ``ha_dev_manage_settings``'s inline body so the tool
        method stays a thin dispatcher; the feature/advanced resolution and
        the env/addon/file origin logic live here.
        """
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
            FEATURE_FLAG_FIELDS,
            _read_feature_flag_override_file,
            get_feature_flag_origin,
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
                    suggestions=["Call ha_dev_manage_settings('list') for valid names"],
                )
            )

        if action == "reset":
            return await self._apply_setting_reset(setting, env_name, origin)

        # action == "set"
        return await self._apply_setting_set(
            setting,
            value,
            env_name,
            ftype,
            origin,
            editable,
            bounds,
            sentinel,
            choices,
        )

    # ----- Tools tab: enable/disable/pin, LLM-API, security gate -----

    async def _tool_metadata_rows(self) -> list[dict[str, Any]]:
        """Return unfiltered tool metadata from the live registry.

        Every real deployment that registers these developer tools passes a
        live server (the registry always does; the stdio settings sidecar is
        a separate process that never builds these tools), so the metadata
        comes from ``local_provider._list_tools()``. The on-disk cache is a
        defensive fallback for a server-less construction only.
        """
        from ..settings_ui._persistence import load_tool_metadata_cache
        from ..settings_ui._tools_meta import _get_tool_metadata

        if self._server is not None:
            return await _get_tool_metadata(self._server)
        return load_tool_metadata_cache()

    @staticmethod
    def _gated_tool_names() -> set[str]:
        """Tool names carrying the bare unconditional gate (the Tools-tab toggle).

        The Tools-tab per-tool gate toggle manages only the bare rule (no
        predicates); conditional rules are authored in the Policies tab. Keying
        this on the bare rule keeps the reported toggle state consistent with
        what set_tool(gated=...) writes.
        """
        from ..policy.persistence import load_policy
        from ..utils.data_paths import get_data_dir

        try:
            policy = load_policy(get_data_dir())
        except ValueError:
            return set()
        return {rule.tool_name for rule in policy.rules if not rule.when}

    async def _list_tool_states(self) -> dict[str, Any]:
        """List every tool with its state / LLM-API / gate + lock flags.

        Mirrors the web Tools tab payload: enabled/disabled/pinned (from
        tool_config.json + env overlay + default-pinned padding), effective
        LLM-API exposure, the per-tool security gate, and the env-pinned /
        mandatory / bps-locked flags that make a row read-only.
        """
        from ..config import get_global_settings
        from ..llm_exposure import effective_llm_api_exposed, load_llm_api_overrides
        from ..settings_ui._handlers_tools import _bps_locked_tools
        from ..settings_ui._persistence import effective_tool_config, env_pinned_tools
        from ..settings_ui._tools_meta import effective_mandatory_tools
        from ..transforms import DEFAULT_PINNED_TOOLS

        metadata = await self._tool_metadata_rows()
        settings = get_global_settings()
        states = dict(effective_tool_config().get("tools", {}))
        for name in DEFAULT_PINNED_TOOLS:
            states.setdefault(name, "pinned")
        env_pinned = env_pinned_tools()
        overrides = load_llm_api_overrides()
        gated = self._gated_tool_names()
        mandatory = effective_mandatory_tools(settings)
        bps_locked = set(_bps_locked_tools())

        rows = [
            {
                "name": t["name"],
                "category": t.get("category"),
                "state": states.get(t["name"], "enabled"),
                # Feature-gated tools appear as stubs carrying disabled_by (the
                # flag that would register them). Surface availability so the
                # caller doesn't read a stub's default state="enabled" as "this
                # tool is live" — it isn't until disabled_by is turned on.
                "available": t.get("disabled_by") is None,
                "disabled_by": t.get("disabled_by"),
                # Feature-gated stub rows carry their primary tag but not the
                # "beta" tag the registered tool declares; append it for
                # disabled_by stubs so the default exposure matches what the
                # real (beta) tool reports once enabled. Mirrors _get_tools.
                "llm_api": effective_llm_api_exposed(
                    t["name"],
                    [
                        *(t.get("tags") or []),
                        *(["beta"] if t.get("disabled_by") else []),
                    ],
                    overrides,
                ),
                "gated": t["name"] in gated,
                "env_pinned": t["name"] in env_pinned,
                "mandatory": t["name"] in mandatory,
                "bps_locked": t["name"] in bps_locked,
            }
            for t in metadata
        ]
        return {
            "success": True,
            "data": {
                "tools": rows,
                "count": len(rows),
                # configured: the saved flag. live: whether the gating
                # middleware/queue are actually wired (only at startup). They
                # diverge after toggling the flag until a restart.
                "policies_enabled": settings.enable_tool_security_policies,
                "policies_live": getattr(self._server, "approval_queue", None)
                is not None,
                "llm_api_available": is_embedded(),
            },
        }

    async def _apply_set_tool(
        self,
        tool: str | None,
        state: str | None,
        llm_api: bool | None,
        gated: bool | None,
    ) -> dict[str, Any]:
        """Apply a Tools-tab change to one tool (state / LLM-API / gate).

        All requested changes are validated (and the policy file read) BEFORE
        anything is written, so a validation failure on one field cannot leave
        another already persisted. ``tool='*'`` is rejected — it is a policy
        wildcard, not a specific tool; author wildcard rules via set_policy.
        """
        if not tool:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'tool' is required for action='set_tool'",
                )
            )
        if tool == "*":
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "tool='*' is a policy wildcard, not a specific tool; it "
                    "would gate every tool. Use set_policy to author a wildcard "
                    "rule deliberately.",
                )
            )
        if not any(v is not None for v in (state, llm_api, gated)):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "set_tool needs at least one of state / llm_api / gated",
                )
            )
        # Reject unknown tool names BEFORE persisting anything: a typo'd
        # security gate ("ha_call_servce") would otherwise save a rule for a
        # nonexistent tool and report success while the intended tool stays
        # ungated. The metadata list includes feature-gated stubs, so a
        # currently-unavailable tool is still configurable. Best-effort: a
        # failed/empty metadata read skips validation (the guard is a typo
        # net, not a security boundary — never brick set_tool over it).
        try:
            known = {t["name"] for t in await self._tool_metadata_rows()}
        except Exception:
            logger.debug(
                "set_tool name validation skipped: metadata unavailable",
                exc_info=True,
            )
            known = set()
        if known and tool not in known:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Unknown tool: {tool!r}",
                    suggestions=[
                        "Call ha_dev_manage_settings('list_tools') for valid names"
                    ],
                )
            )
        # Serialize the tool_config + policy read-modify-write against the web
        # save handlers and other set_tool calls via the shared asyncio lock
        # (held on the loop while the file I/O runs in the worker thread);
        # the worker additionally takes the cross-process file lock so the
        # stdio sidecar's handlers can't interleave from another process.
        from ..utils.config_write_lock import get_config_write_lock

        async with get_config_write_lock():
            return await asyncio.to_thread(
                self._with_file_lock, self._write_tool_all, tool, state, llm_api, gated
            )

    @staticmethod
    def _with_file_lock(fn: Any, /, *args: Any) -> Any:
        """Run ``fn(*args)`` holding the cross-process config file lock.

        Thread-side companion of ``config_write_guard()``: callers hold the
        asyncio lock on the loop, so the file lock never nests in-process.
        """
        from ..utils.config_write_lock import config_file_lock

        with config_file_lock():
            return fn(*args)

    def _write_tool_all(
        self, tool: str, state: str | None, llm_api: bool | None, gated: bool | None
    ) -> dict[str, Any]:
        """Preflight-validate every requested change, then write them.

        Validation and the fallible policy read happen before any write, so a
        rejected field (env-pin, mandatory/BPS lock, corrupt policy) cannot
        leave another field already persisted (Codex #1993). Cross-file
        atomicity across tool_config.json and tool_policy.json is still
        best-effort on a raw disk-write failure.
        """
        plan = self._preflight_set_tool(tool, state, llm_api, gated)
        return self._commit_set_tool(tool, state, plan)

    def _preflight_set_tool(
        self, tool: str, state: str | None, llm_api: bool | None, gated: bool | None
    ) -> dict[str, Any]:
        """Validate all requested changes without writing anything.

        Returns a plan dict (persist_state / llm_val / gate_val / new_policy /
        gate_changed); raises ToolError on the first invalid field.
        """
        from ..config import get_global_settings
        from ..policy.persistence import load_policy
        from ..settings_ui._persistence import env_pinned_tools
        from ..utils.data_paths import get_data_dir

        plan: dict[str, Any] = {
            "persist_state": False,
            "llm_val": None,
            "gate_val": None,
            "new_policy": None,
            "gate_changed": False,
        }
        if state is not None:
            plan["persist_state"] = self._validate_state_change(
                tool, state, get_global_settings(), env_pinned_tools()
            )
        if llm_api is not None:
            plan["llm_val"] = self._coerce_bool_or_raise(llm_api, "llm_api")
        if gated is not None:
            gate_val = self._coerce_bool_or_raise(gated, "gated")
            try:
                policy = load_policy(get_data_dir())
            except ValueError as exc:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONFIG_INVALID,
                        f"tool_policy.json is invalid: {exc}",
                        suggestions=["Inspect or delete the file, then retry"],
                    )
                )
            new_policy, changed = self._apply_gate_to_policy(policy, tool, gate_val)
            plan["gate_val"] = gate_val
            plan["new_policy"] = new_policy
            plan["gate_changed"] = changed
        return plan

    def _commit_set_tool(
        self, tool: str, state: str | None, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist a preflighted set_tool plan (validations already passed)."""
        from ..llm_exposure import LLM_API_CONFIG_KEY
        from ..settings_ui._persistence import load_tool_config, save_tool_config

        config = load_tool_config()
        tools_states = dict(config.get("tools", {}))
        llm_over = dict(config.get(LLM_API_CONFIG_KEY, {}))
        data: dict[str, Any] = {"tool": tool}
        warnings: list[str] = []
        restart_required = False
        if state is not None:
            data["state"] = state
            if plan["persist_state"]:
                tools_states[tool] = state
                restart_required = True
        if plan["llm_val"] is not None:
            llm_over[tool] = plan["llm_val"]
            data["llm_api"] = plan["llm_val"]
            if not is_embedded():
                warnings.append(
                    "LLM-API exposure only affects the embedded custom-component "
                    "server; it has no effect in this deployment."
                )
        config["tools"] = tools_states
        config[LLM_API_CONFIG_KEY] = llm_over
        if not save_tool_config(config):
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist tool config to disk",
                    suggestions=[
                        "Set HA_MCP_CONFIG_DIR to a writable path",
                        "Check the server logs for the underlying OSError",
                    ],
                )
            )
        if plan["gate_val"] is not None:
            partial = (state is not None and plan["persist_state"]) or plan[
                "llm_val"
            ] is not None
            warnings.extend(self._commit_gate_or_flag_partial(plan, data, partial))
        data["restart_required"] = restart_required
        result: dict[str, Any] = {"success": True, "data": data}
        if warnings:
            result["warnings"] = warnings
        return result

    def _commit_gate_or_flag_partial(
        self, plan: dict[str, Any], data: dict[str, Any], partial: bool
    ) -> list[str]:
        """Commit the gate; on a raw policy-write failure, surface that the
        tool_config (state/LLM-API) change already persisted so the caller
        isn't told the whole operation failed."""
        try:
            return self._commit_gate(plan, data)
        except ToolError:
            raise
        except Exception as exc:
            suffix = (
                " The state/LLM-API change WAS already saved — re-run set_tool "
                "with only gated= to finish the gate."
                if partial
                else ""
            )
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"The security-gate policy write failed: {exc}.{suffix}",
                    context={"partial_commit": partial, "persisted": data},
                )
            )
            return []  # unreachable; explicit for CodeQL

    def _commit_gate(self, plan: dict[str, Any], data: dict[str, Any]) -> list[str]:
        """Persist the gate portion of a set_tool plan; returns any warnings."""
        from ..config import get_global_settings
        from ..policy.persistence import save_policy
        from ..utils.data_paths import get_data_dir

        data["gated"] = bool(plan["gate_val"])
        data["policy_rules_changed"] = plan["gate_changed"]
        if plan["gate_changed"]:
            save_policy(get_data_dir(), plan["new_policy"])
            self._clear_remember_cache()
        warnings: list[str] = []
        if not get_global_settings().enable_tool_security_policies:
            warnings.append(
                "Tool security policies are disabled "
                "(enable_tool_security_policies=false); this gate is stored but "
                "won't enforce until policies are enabled and the server restarts."
            )
        return warnings

    @staticmethod
    def _validate_state_change(
        tool: str, state: str, settings: Any, env_pinned: dict[str, str]
    ) -> bool:
        """Validate a state change; return whether it should be persisted.

        Rejects invalid states, env-pinned flips, and disabling a mandatory
        tool (base MANDATORY_TOOLS always, plus the BPS-locked set while
        strict best-practices mode is on) — the web Tools tab locks those
        rows, so the headless path must refuse rather than persist a disable
        that apply_tool_visibility silently reverts at startup. Env-pinned
        no-op re-sends return False (nothing to persist); anything else that
        passes returns True.
        """
        from ..settings_ui._tools_meta import (
            _VALID_STATES,
            BPS_MANDATORY_TOOLS,
            effective_mandatory_tools,
        )

        if state not in _VALID_STATES:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"state must be one of {sorted(_VALID_STATES)}",
                )
            )
        if tool in env_pinned:
            if env_pinned[tool] != state:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{tool!r} is pinned by DISABLED_TOOLS / PINNED_TOOLS "
                        f"to {env_pinned[tool]!r}; unset the env var to change it.",
                    )
                )
            return False
        if state == "disabled" and tool in effective_mandatory_tools(settings):
            if tool in BPS_MANDATORY_TOOLS:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Refusing to disable {tool!r} while strict "
                        "best-practices mode (enable_strict_mandatory_bps) is on.",
                        suggestions=[
                            "Turn off strict best-practices mode first, then retry."
                        ],
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{tool!r} is a mandatory tool and is always kept enabled; "
                    "it cannot be disabled.",
                )
            )
        return True

    @staticmethod
    def _coerce_bool_or_raise(value: Any, field: str) -> bool:
        """Coerce ``value`` to bool or raise a ToolError naming ``field``."""
        coerced = DevTools._coerce_bool_setting(value)
        if coerced is _COERCE_MISS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{field} must be a boolean",
                )
            )
        return bool(coerced)

    @staticmethod
    def _apply_gate_to_policy(policy: Any, tool: str, gated: bool) -> tuple[Any, bool]:
        """Return (policy, changed) after adding/removing the bare gate rule.

        Pure (no I/O): manages only the bare unconditional rule (when == [])
        for ``tool``; predicate-bearing rules are preserved.
        """
        from ..policy.model import Rule

        has_bare = any(r.tool_name == tool and not r.when for r in policy.rules)
        if gated and not has_bare:
            updated = policy.model_copy(
                update={"rules": [*policy.rules, Rule(tool_name=tool)]}
            )
            return updated, True
        if not gated and has_bare:
            updated = policy.model_copy(
                update={
                    "rules": [r for r in policy.rules if r.tool_name != tool or r.when]
                }
            )
            return updated, True
        return policy, False

    def _clear_remember_cache(self) -> None:
        """Clear the approval remember-cache if a live queue exists."""
        queue = getattr(self._server, "approval_queue", None)
        if queue is not None:
            queue.clear_remember_cache()

    # ----- Tool security policies -----

    async def _get_policy(self) -> dict[str, Any]:
        """Return the full tool-security policy."""
        from ..config import get_global_settings
        from ..policy.persistence import load_policy
        from ..utils.data_paths import get_data_dir

        try:
            policy = load_policy(get_data_dir())
        except ValueError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_INVALID,
                    f"tool_policy.json is invalid: {exc}",
                    suggestions=["Inspect or delete the file, then retry"],
                )
            )
        return {
            "success": True,
            "data": {
                "policy": policy.model_dump(mode="json"),
                "policies_enabled": (
                    get_global_settings().enable_tool_security_policies
                ),
                "policies_live": getattr(self._server, "approval_queue", None)
                is not None,
            },
        }

    async def _apply_set_policy(
        self, policy: dict[str, Any] | None, expected_version: int | None
    ) -> dict[str, Any]:
        """Write the full tool-security policy (validated, version-guarded)."""
        from pydantic import ValidationError

        from ..policy.model import Policy
        from ..utils.config_write_lock import get_config_write_lock

        if not isinstance(policy, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'policy' (a policy object) is required for action='set_policy'",
                    suggestions=[
                        "Call ha_dev_manage_settings('get_policy') for the shape"
                    ],
                )
            )
        try:
            new_policy = Policy.model_validate(policy)
        except (ValidationError, ValueError) as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"policy failed schema validation: {exc}",
                )
            )
        # Compare against the COERCED model version so a JSON string "3" matches
        # the on-disk int 3; ``"version" in policy`` distinguishes an omitted
        # version (no concurrency check) from an explicit one.
        expected = (
            expected_version
            if expected_version is not None
            else (new_policy.version if "version" in policy else None)
        )
        # Serialize the load-check-save against the web PUT handler + set_tool
        # (asyncio lock) and other processes (file lock, in the thread).
        async with get_config_write_lock():
            return await asyncio.to_thread(
                self._with_file_lock, self._commit_policy, new_policy, expected
            )

    def _commit_policy(self, new_policy: Any, expected: int | None) -> dict[str, Any]:
        """Load current policy, version-check against ``expected``, save, report.

        MUST run while holding ``get_config_write_lock()`` (called via
        ``asyncio.to_thread`` from ``_apply_set_policy``).
        """
        from ..config import get_global_settings
        from ..policy.persistence import load_policy, save_policy
        from ..utils.data_paths import get_data_dir

        data_dir = get_data_dir()
        try:
            current = load_policy(data_dir)
        except ValueError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_INVALID,
                    f"existing tool_policy.json is invalid: {exc}",
                    suggestions=["Inspect or delete the file, then retry"],
                )
            )
        warnings: list[str] = []
        if expected is not None and expected != current.version:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "policy version mismatch — reload with get_policy before saving",
                    context={
                        "current_version": current.version,
                        "current_policy": current.model_dump(mode="json"),
                    },
                )
            )
        if expected is None:
            warnings.append(
                "No version supplied; wrote without an optimistic-concurrency check."
            )
        # Rebase onto the on-disk version so save_policy bumps to current+1.
        save_policy(
            data_dir, new_policy.model_copy(update={"version": current.version})
        )
        rules_changed = current.rules != new_policy.rules
        if rules_changed:
            self._clear_remember_cache()
        # Same "won't enforce" signal set_tool(gated=True) gives, so authoring
        # rules while the engine is off doesn't look like a live gate.
        if new_policy.rules and not get_global_settings().enable_tool_security_policies:
            warnings.append(
                "Tool security policies are disabled "
                "(enable_tool_security_policies=false); these rules are stored "
                "but won't enforce until policies are enabled and the server "
                "restarts."
            )
        result: dict[str, Any] = {
            "success": True,
            "data": {"version": current.version + 1, "rules_changed": rules_changed},
        }
        if warnings:
            result["warnings"] = warnings
        return result

    # ----- Auto-backup config -----

    async def _get_backup_config(self) -> dict[str, Any]:
        """Return the auto-backup config fields (shared with the web handler)."""
        from ..settings_ui._handlers_backups import backup_config_fields

        return {
            "success": True,
            "data": {
                "is_addon": is_running_in_addon(),
                "fields": backup_config_fields(),
            },
        }

    async def _apply_set_backup_config(
        self, backup: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Apply auto-backup config changes (same routing as the web UI)."""
        from ..settings_ui._handlers_backups import (
            _validate_backup_payload,
            apply_backup_config,
        )

        if not isinstance(backup, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'backup' (an object of {field: value}) is required for "
                    "action='set_backup_config'",
                    suggestions=[
                        "Call ha_dev_manage_settings('get_backup_config') for "
                        "field names"
                    ],
                )
            )
        clean, err = _validate_backup_payload(backup)
        if err is not None:
            raise_tool_error(
                create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, err)
            )
        response = await apply_backup_config(self._server, clean)
        # JSONResponse.body is typed bytes | memoryview; bytes() normalizes both
        # (no-op for bytes) so json.loads accepts it.
        body = json.loads(bytes(response.body))
        if response.status_code >= 400:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    self._backup_error_message(body),
                    context={"status": response.status_code, "response": body},
                )
            )
        return {"success": True, "data": body}

    @staticmethod
    def _backup_error_message(body: Any) -> str:
        """Pull a human message out of a backup-config error response body."""
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(
                err.get("message") or err.get("code") or "backup config update failed"
            )
        if isinstance(err, str):
            return err
        return "backup config update failed"

    async def _apply_setting_reset(
        self, setting: str, env_name: str, origin: str
    ) -> dict[str, Any]:
        """Handle ``ha_dev_manage_settings`` action='reset'.

        Extracted verbatim from the inline branch: rejects env/addon-managed
        settings, removes any file override, and reports whether one existed.
        """
        from ..config import _reset_global_settings

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

    async def _apply_setting_set(
        self,
        setting: str,
        value: bool | int | float | str | None,
        env_name: str,
        ftype: type,
        origin: str,
        editable: bool,
        bounds: tuple[float, float] | None,
        sentinel: int | None,
        choices: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        """Handle ``ha_dev_manage_settings`` action='set'.

        Extracted verbatim from the inline branch: validates presence +
        editability, coerces and range/choice/beta-checks the value, then
        persists via Supervisor (add-on mode) or the override file.
        """
        from ..config import (
            BETA_FEATURE_FIELDS,
            _reset_global_settings,
            get_global_settings,
        )

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
            Literal[
                "info",
                "update_source",
                "restart",
                "list_pending",
                "approve",
                "deny",
            ],
            Field(
                description=(
                    "info: deployment/version report; update_source: point the "
                    "ha_mcp_tools component's separate in-process server at a "
                    "channel or pip spec and reinstall it (never changes the "
                    "server serving this connection, unless embedded); "
                    "restart: restart this server; list_pending: list tool calls "
                    "blocked on a security-policy approval; approve / deny: decide "
                    "one blocked call by token"
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
        token: Annotated[
            str | None,
            Field(
                default=None,
                description="Approval token (required for approve/deny)",
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

        Caveats: update_source changes ONLY the ha_mcp_tools custom
        component's separate in-process server entry — it never updates
        the add-on, Docker, standalone, or PyPI server that may be
        serving this connection (update those via ha_manage_addon /
        docker pull / pip). In embedded mode that entry IS this server,
        so the update self-interrupts; elsewhere this connection is
        untouched and keeps its current version. Success means the
        entry's options were applied — the component then reinstalls in
        the background, which can take minutes and can still fail
        (check HA logs). update_source requires the component's
        in-process server entry to exist. restart interrupts this MCP
        connection in embedded and add-on deployments (the reply
        arrives just before the server goes down) and supports those
        two deployments only (standalone processes must be restarted
        externally).

        EXAMPLES:
        ha_dev_manage_server("info")
        ha_dev_manage_server("update_source", channel="dev")
        ha_dev_manage_server("update_source", pip_spec="https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1234/head.tar.gz")
        ha_dev_manage_server("restart")
        ha_dev_manage_server("list_pending")
        ha_dev_manage_server("approve", token="abc123")
        """
        try:
            if action == "info":
                return await self._server_info()
            if action == "update_source":
                return await self._update_source(channel, pip_spec)
            if action == "restart":
                return await self._restart_server()
            if action == "list_pending":
                return await self._list_pending()
            return await self._decide_approval(token, approve=action == "approve")
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
                    "role": (
                        "this server (embedded)"
                        if mode == "embedded"
                        else (
                            "separate in-process server run by the "
                            "ha_mcp_tools component; the update_source "
                            "target. server_version above describes the "
                            f"{mode} server handling this connection, not "
                            "this entry."
                        )
                    ),
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

    @staticmethod
    def _validate_update_source_args(channel: str | None, pip_spec: str | None) -> None:
        """Validate ``update_source``'s channel/pip_spec arguments.

        Extracted verbatim from ``_update_source``: raises a structured
        ToolError when neither is given, the channel is unknown, or the
        pip_spec is multi-line / over 500 chars.
        """
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

    async def _update_source(
        self, channel: str | None, pip_spec: str | None
    ) -> dict[str, Any]:
        self._validate_update_source_args(channel, pip_spec)

        if is_embedded():
            # Embedded self-reload: prefer the component's one-hop direct write
            # (async_update_entry) over the options-flow start+submit — and try it
            # BEFORE find_server_config_entry opens the legacy options flow, so the
            # fast path is NOT gated behind (or delayed/broken by) the very flow-open
            # it exists to bypass. The component locates the entry in-process itself,
            # so it needs no find. On ANY component error this returns None and we
            # fall through to the legacy find + options-flow submit below (unchanged).
            # Non-embedded deployments never route here — they reload the SEPARATE
            # in-process server entry synchronously and keep this connection, so the
            # collapse buys nothing there.
            component_result = await self._update_source_via_component(
                channel, pip_spec
            )
            if component_result is not None:
                return component_result

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
                    "target": (
                        "this server (the embedded ha_mcp_tools in-process entry)"
                    ),
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
                "target": "ha_mcp_tools in-process server entry",
                "applied": user_input,
                "previous": {
                    _OPT_CHANNEL: current.get(_OPT_CHANNEL),
                    _OPT_PIP_SPEC: current.get(_OPT_PIP_SPEC),
                },
                "note": (
                    "Applied to the ha_mcp_tools component's SEPARATE "
                    "in-process server entry, which is now reinstalling in "
                    "the background (can take minutes and can still fail — "
                    "check HA logs). The server handling this connection is "
                    "NOT affected and keeps its current version; verify the "
                    "in-process server on its own connect URL, not here."
                ),
            },
        }

    async def _update_source_via_component(
        self, channel: str | None, pip_spec: str | None
    ) -> dict[str, Any] | None:
        """Apply the channel/pip_spec delta via the component's direct write.

        Embedded-only fast path: when the component advertises
        ``server_entry_update``, one ``ha_mcp_tools/server_entry_update`` frame
        applies the delta through ``async_update_entry`` directly — the component
        merges it against its LIVE ``entry.options`` (so no preserved-key resend is
        needed) and schedules the resulting self-reload after a flush delay. Returns
        the final ``{success, data}`` envelope, or ``None`` to fall back to the
        legacy options-flow submit.

        The write is IDEMPOTENT (it targets a specific merged options set), so —
        unlike ``ha_call_service`` — a post-send ambiguity is harmless: EVERY
        component error (capability miss, unknown_command, connection/timeout, or a
        malformed reply) returns ``None``, and the legacy submit then re-applies the
        SAME delta, which the entry's ``DATA_LAST_OPTIONS`` guard collapses to a
        no-op reload if the component's write already landed. ``unknown_command``
        additionally invalidates the cached caps so the next call re-probes.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "server_entry_update"):
            return None
        try:
            # verify_ssl included so a verify_ssl=False client never establishes (or
            # keys) a default-verification pooled connection (mirrors the pooled
            # ``send_websocket_message`` path). ``getattr`` guards duck-typed clients
            # that omit the attribute — falling back to the pool's global default.
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
        except Exception as exc:
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_SERVER_ENTRY_UPDATE,
                exc,
            )
            return None
        deltas: dict[str, Any] = {}
        if channel is not None:
            deltas[_OPT_CHANNEL] = channel
        if pip_spec is not None:
            deltas[_OPT_PIP_SPEC] = pip_spec
        try:
            raw = await ws.send_command(WS_SERVER_ENTRY_UPDATE, **deltas)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                logger.warning(
                    "%s unknown_command; invalidating caps and falling back to "
                    "legacy: %r",
                    WS_SERVER_ENTRY_UPDATE,
                    exc,
                )
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "%s command error; falling back to legacy: %r",
                    WS_SERVER_ENTRY_UPDATE,
                    exc,
                )
            return None
        except Exception as exc:
            # HomeAssistantConnectionError (pooled-WS drop) or a plain post-send
            # transport failure. The write is idempotent, so a legacy re-apply is
            # safe (see docstring) — fall back rather than report a phantom error.
            logger.warning(
                "%s connection error; falling back to legacy: %r",
                WS_SERVER_ENTRY_UPDATE,
                exc,
            )
            return None
        result = raw.get("result")
        if not isinstance(result, dict) or not (
            result.get("scheduled") or result.get("unchanged")
        ):
            logger.warning(
                "%s returned an unusable result; falling back to legacy: %r",
                WS_SERVER_ENTRY_UPDATE,
                result,
            )
            return None
        return {"success": True, "data": self._component_update_data(result)}

    @staticmethod
    def _component_update_data(result: dict[str, Any]) -> dict[str, Any]:
        """Map a ``server_entry_update`` component reply into update_source's data.

        Mirrors the legacy embedded branch's ``scheduled:true`` shape
        (entry_id/target/applying/previous/note); a no-op reply carries ``unchanged``
        and its own note instead. This route is embedded-only, so ``target`` is
        always the embedded-server phrasing (matches the legacy embedded branch and
        preserves the #1929 target field).
        """
        data: dict[str, Any] = {
            "scheduled": bool(result.get("scheduled")),
            "entry_id": result.get("entry_id"),
            "target": "this server (the embedded ha_mcp_tools in-process entry)",
            "applying": result.get("applying"),
            "previous": result.get("previous"),
        }
        if result.get("unchanged"):
            data["unchanged"] = True
            data["note"] = (
                "No change: the requested channel/pip_spec already matches the "
                "current in-process server source."
            )
        else:
            data["note"] = (
                "The in-process server will reinstall and restart now; this "
                "connection will drop. Reconnect in 1-5 minutes and verify with "
                "ha_dev_manage_server('info')."
            )
        return data

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

    # ----- Approval queue (runtime) -----

    def _require_queue(self) -> Any:
        """Return the live approval queue or raise a clear error.

        The queue is created at server startup only when
        ``enable_tool_security_policies`` is on. These developer tools always
        run with a live server, so a missing queue means policies were off at
        startup (or were toggled on without the required restart) — not a
        deployment without a server.
        """
        queue = getattr(self._server, "approval_queue", None)
        if queue is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No active approval queue: tool security policies were not "
                    "enabled when the server started, so the gating middleware "
                    "and its queue are not wired.",
                    suggestions=[
                        "Enable enable_tool_security_policies "
                        "(ha_dev_manage_settings set) and restart the server, "
                        "then retry."
                    ],
                )
            )
        return queue

    async def _list_pending(self) -> dict[str, Any]:
        """List tool calls currently blocked on a security-policy approval."""
        queue = getattr(self._server, "approval_queue", None)
        if queue is None:
            return {
                "success": True,
                "data": {
                    "pending": [],
                    "count": 0,
                    "note": (
                        "No active approval queue — tool security policies were "
                        "not enabled at server startup; nothing is blocked."
                    ),
                },
            }
        pending = [
            {
                "token": e.token,
                "tool_name": e.tool_name,
                "args": e.args,
                "created_at": e.created_at.isoformat(),
                "expires_at": e.expires_at.isoformat(),
            }
            for e in queue.list_pending()
        ]
        return {"success": True, "data": {"pending": pending, "count": len(pending)}}

    async def _decide_approval(
        self, token: str | None, *, approve: bool
    ) -> dict[str, Any]:
        """Approve or deny one blocked tool call by token."""
        if not token:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'token' is required for approve/deny",
                )
            )
        queue = self._require_queue()
        entry = queue.get(token)
        if entry is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Unknown or expired approval token",
                    suggestions=[
                        "Call ha_dev_manage_server('list_pending') for live tokens"
                    ],
                )
            )
        decided = queue.approve(token) if approve else queue.deny(token)
        if not decided:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Token already decided as {entry.decision!r}",
                    context={"current_decision": entry.decision},
                )
            )
        return {
            "success": True,
            "data": {
                "token": token,
                "tool_name": entry.tool_name,
                "decision": "approved" if approve else "denied",
            },
        }


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
        "are registered. These tools can change server settings, tool "
        "visibility, security policies, and replace the running server version."
    )
    register_tool_methods(mcp, DevTools(client, server=kwargs.get("server")))
