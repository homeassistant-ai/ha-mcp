"""Home Assistant's own write operations on the recorder, its statistics and the logs."""

import asyncio
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .component_api import (
    ask_component,
    component_unusable_warning,
    get_component_caps,
)
from .config_entry_flow_errors import ReconfigureStatus
from .core_contract import command_payload, include_core_contract
from .helpers import (
    core_error_message,
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
)
from .history_coverage import MIGRATING, when
from .response_helpers import build_pagination_metadata
from .statistics_search import matching_statistics
from .tool_hints import write_hints
from .tools_services import _fetch_catalog_and_translations, _process_services

# The tool's permission boundary: Core's own names, a dot for a service and a
# slash for a WebSocket command.
LogsAction = Literal[
    "recorder.purge",
    "recorder.purge_entities",
    "recorder.enable",
    "recorder.disable",
    "system_log.clear",
    "system_log.write",
    "logger.set_level",
    "logger.set_default_level",
    "logbook.log",
    "recorder/list_statistic_ids",
    "recorder/validate_statistics",
    "recorder/update_statistics_issues",
    "recorder/clear_statistics",
    "recorder/update_statistics_metadata",
    "recorder/change_statistics_unit",
    "recorder/adjust_sum_statistics",
    "recorder/import_statistics",
]

# How long a recorder write waits for the recorder to finish it.
RECORDER_DRAIN_SECONDS = 30.0
_DRAIN_POLL_SECONDS = 0.2
_PAGED_ACTION = "recorder/list_statistic_ids"
_SETTLED_FIELDS = {"settled", "purge_keep_days", "auto_purge", "oldest_state"}
# The client's wait for the bridge's reply outlasts the wait asked of it.
_REPLY_MARGIN_SECONDS = 10.0
# The recorder actions that change nothing, so nothing is waited for; Read
# Only Mode allows exactly these (read_only._logs_write).
RECORDER_READS = (_PAGED_ACTION, "recorder/validate_statistics")


def _still_working() -> str:
    return (
        f"The recorder was still working after {RECORDER_DRAIN_SECONDS:g} s; the "
        "operation may not have finished yet"
    )


def _raise_core_refusal(action: str, reply: dict[str, Any]) -> NoReturn:
    raise_core_refusal(
        f"{action} failed",
        reply,
        {"action": action},
        [
            f"Call ha_manage_logs(action='{action}', describe=True) for the "
            "fields Home Assistant accepts"
        ],
    )


# Core declares a statistic reference as a plain string, so the statistics a
# command names are found under Core's own keys for them, the naming convention
# docs/agents/native-core-contracts.md allows for optional enrichment. A
# reference Core puts under another key is not read back.
_STATISTIC_KEYS = ("statistic_id", "statistic_ids")
_UNIT_CHANGE = "recorder/change_statistics_unit"


def _named_statistics(value: Any) -> list[str]:
    """The statistic ids a request holds under Core's keys for them, at any depth."""
    if isinstance(value, list):
        return [found for item in value for found in _named_statistics(item)]
    if not isinstance(value, dict):
        return []
    found: list[str] = []
    for key, inner in value.items():
        if key not in _STATISTIC_KEYS:
            found += _named_statistics(inner)
        elif isinstance(inner, str):
            found.append(inner)
        elif isinstance(inner, list):
            found += [item for item in inner if isinstance(item, str)]
    return found


def _unapplied_unit(data: dict[str, Any], after: list[dict[str, Any]]) -> list[str]:
    """Core answers a unit change it did not apply with success; the unit it
    stores afterwards says whether the requested one took."""
    statistic_id = data.get("statistic_id")
    wanted = data.get("new_unit_of_measurement")
    stored = [
        row["statistics_unit_of_measurement"]
        for row in after
        if row["statistic_id"] == statistic_id
    ]
    if not stored:
        return [
            f"Core accepted the command, but {statistic_id} was not found when "
            "read back."
        ]
    if stored[0] == wanted:
        return []
    return [
        f"Core accepted the command, but {statistic_id} is stored in "
        f"{stored[0]}, not {wanted}."
    ]


class LogsManageTools:
    """Core's recorder, statistics and log write operations."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _send(self, action: str, data: dict[str, Any]) -> Any:
        if "/" in action:
            message = command_payload(action, data)
        else:
            domain, service = action.split(".", 1)
            message = {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": data,
            }
        try:
            reply = await self._client.send_websocket_message(message)
        except HomeAssistantCommandNotSent:
            raise
        except HomeAssistantConnectionError as lost:
            if action in RECORDER_READS:
                raise
            # Sent, or possibly sent, with no answer back: it may have run.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"{action} got no answer from Home Assistant ({lost}); it "
                    "may have run",
                    context={
                        "action": action,
                        "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    },
                    suggestions=[
                        "Do not repeat it automatically: a repeat can apply the "
                        "change twice. Read its effect first with ha_get_history, "
                        "ha_get_logs or action='recorder/list_statistic_ids'."
                    ],
                )
            )
        # Core's clear and metadata commands stop waiting for their own job
        # after a while and answer `timeout`: the job may still be queued, or
        # the recorder may have skipped it.
        timed_out = action.startswith("recorder/") and (
            reply.get("error_code") == "timeout"
        )
        if not reply.get("success") and not timed_out:
            _raise_core_refusal(action, reply)
        return reply.get("result"), timed_out

    async def _describe(self, action: str, warnings: list[str]) -> dict[str, Any]:
        if "/" in action:
            described: dict[str, Any] = {}
            await include_core_contract(self._client, described, action)
            warnings.extend(described.get("warnings", []))
            contract: dict[str, Any] = described["core_contract"]
            return contract
        domain = action.split(".", 1)[0]
        services, translations = await _fetch_catalog_and_translations(
            self._client, domain, warnings
        )
        catalog = _process_services(
            services,
            translations,
            domain_filter=domain,
            query_filter=action,
            detail_level="full",
            warnings=warnings,
        )
        entry: dict[str, Any] | None = catalog["services"].get(action)
        if entry is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_NOT_FOUND,
                    f"Home Assistant has no {action} service; its integration "
                    "is not loaded",
                    context={"action": action},
                )
            )
        return entry

    async def _recorder_drain_warning(self) -> str | None:
        """Wait until Core's recorder reports an empty queue; a warning when it
        does not within the bound or cannot be asked."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RECORDER_DRAIN_SECONDS
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _still_working()
            try:
                reply = await self._client.send_websocket_message(
                    {"type": "recorder/info", "_wait_timeout": remaining}
                )
            except HomeAssistantCommandNotSent:
                raise
            except HomeAssistantConnectionError:
                if loop.time() < deadline:
                    raise
                return _still_working()
            if not reply.get("success"):
                return (
                    "Could not read recorder/info to wait for the recorder: "
                    f"{core_error_message(reply.get('error', 'Unknown error'))}"
                )
            if reply["result"]["backlog"] == 0:
                return None
            await asyncio.sleep(
                min(_DRAIN_POLL_SECONDS, max(deadline - loop.time(), 0))
            )

    async def _recorder_settled(
        self, warnings: list[str]
    ) -> tuple[dict[str, Any] | None, bool]:
        """Wait for the recorder to finish its work: its status, and whether it did.

        The component waits on the recorder itself. Core's own API shows only
        the queue length, and a job leaves the queue when it starts.
        """
        try:
            status = await ask_component(
                self._client,
                "recorder_status",
                entity_ids=[],
                wait=RECORDER_DRAIN_SECONDS,
                _wait_timeout=RECORDER_DRAIN_SECONDS + _REPLY_MARGIN_SECONDS,
            )
        except HomeAssistantConnectionError:
            status = None
        # A component read that fails, or a reply without the bridge's fields,
        # counts as no component (docs/agents/custom-component.md).
        if status is not None and status.keys() >= _SETTLED_FIELDS:
            settled: bool = status["settled"]
            if not settled:
                warnings.append(_still_working())
            # A component released before the field came answers without it.
            if status.get("migration_in_progress"):
                warnings.append(MIGRATING)
            oldest = status["oldest_state"]
            return {
                "purge_keep_days": status["purge_keep_days"],
                "auto_purge": status["auto_purge"],
                "oldest_state": None if oldest is None else when(oldest),
            }, settled
        if warning := await self._recorder_drain_warning():
            warnings.append(warning)
        warnings.append(
            component_unusable_warning(
                await get_component_caps(self._client),
                "only the recorder's queue was watched: a job leaves it when it "
                "starts and may still be running",
            )
        )
        return None, False

    async def _statistics_metadata(
        self, statistic_ids: list[str]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Core's metadata for ``statistic_ids``, or why there is none."""
        reply = await self._client.send_websocket_message(
            {"type": "recorder/get_statistics_metadata", "statistic_ids": statistic_ids}
        )
        if reply.get("success"):
            rows: list[dict[str, Any]] = reply["result"]
            return rows, None
        return None, core_error_message(reply.get("error", "Unknown error"))

    async def _outcome(
        self,
        action: str,
        data: dict[str, Any],
        named: list[str],
        response: dict[str, Any],
        warnings: list[str],
    ) -> None:
        """Wait for the recorder and read back what the operation left."""
        settled = False
        # Services outside the recorder write through it too (a logbook entry
        # is an event it stores), so every write waits, with no list of them.
        if action not in RECORDER_READS:
            status, settled = await self._recorder_settled(warnings)
            if status is not None and action.startswith("recorder."):
                response["recorder"] = status
        if not named:
            return
        after, failure = await self._statistics_metadata(named)
        if after is None:
            warnings.append(f"Could not read the statistics metadata back: {failure}")
            return
        response["statistics_metadata"] = after
        # Before the recorder is through, the stored unit is no verdict.
        if settled and action == _UNIT_CHANGE:
            warnings.extend(_unapplied_unit(data, after))

    async def _run(
        self,
        action: str,
        data: dict[str, Any],
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> dict[str, Any]:
        named = (
            _named_statistics(data)
            if action.startswith("recorder/") and action not in RECORDER_READS
            else []
        )
        result, timed_out = await self._send(action, data)
        response: dict[str, Any] = {"success": True, "action": action}
        warnings: list[str] = []
        if timed_out:
            warnings.append(
                f"Core stopped waiting for {action} and answered timeout: it may "
                "still run, or may not have run. Read its effect before "
                "repeating it."
            )
        try:
            await self._outcome(action, data, named, response, warnings)
        except HomeAssistantConnectionError as lost:
            # Core answered the operation. An error here would invite a
            # second run.
            warnings.append(
                f"Core answered {action}, but gave no answer when its outcome "
                f"was read ({lost}); do not repeat it on that account."
            )
        if action == _PAGED_ACTION and isinstance(result, list):
            if query:
                result, query_warnings = await matching_statistics(
                    self._client, result, query
                )
                warnings.extend(query_warnings)
            # Core lists stored statistics first, so a clear or a first sample
            # moves one; pages follow the IDs instead.
            result = sorted(result, key=lambda row: row["statistic_id"])
            page = result[offset : offset + limit]
            response.update(
                build_pagination_metadata(len(result), offset, limit, len(page))
            )
            result = page
        response["result"] = result
        if warnings:
            response["warnings"] = warnings
        return response

    @tool(
        name="ha_manage_logs",
        tags={"History & Statistics"},
        annotations=write_hints(
            "Manage Recorder, Statistics and Logs",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_manage_logs(
        self,
        action: Annotated[
            LogsAction,
            Field(
                description=(
                    "The Home Assistant operation, named as Core names it: a dot "
                    "is a service, a slash a WebSocket command (Developer Tools > "
                    "Statistics)."
                )
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "The operation's fields, sent to Core unchanged: a service's "
                    "service data, or the fields of the WebSocket command, "
                    "where 'id', 'type' and '_wait_timeout' are refused."
                )
            ),
        ] = None,
        describe: Annotated[
            bool,
            Field(
                description=(
                    "Send nothing and return Core's own description of the "
                    "action's fields instead."
                )
            ),
        ] = False,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Page size for 'recorder/list_statistic_ids' only.",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                ge=0, description="Page start for 'recorder/list_statistic_ids' only."
            ),
        ] = 0,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "For 'recorder/list_statistic_ids' only: keep the statistics "
                    "where every word is part of one of the row's values (id, "
                    "name, source, units) or, with the ha_mcp_tools component, "
                    "of the names Home Assistant gives the statistic's entity. "
                    "Core's own statistic_type filter goes in data."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant's recorder, its long-term statistics and its logs.

        WHEN NOT TO USE:
        For reading recorded states or statistics values use ha_get_history,
        for reading log entries ha_get_logs, and for one integration's debug
        logging ha_set_integration with log_level.

        WHEN TO USE:
        To purge or pause the recorder, to list the recorder's statistics and
        Core's issues with them, to fix a statistic (clear it, adjust a sum,
        change its unit, import rows), and to write to or clear the system
        log, set log levels or add a logbook entry.

        CAVEATS:
        Each call runs one Core operation; Core validates its fields and its
        refusal comes back with its own error code. Call with describe=True
        first when the fields are not known. With the HA-MCP component a
        recorder write returns once the recorder has finished it, and a
        recorder service returns the recorder's purge settings and oldest
        state as `recorder`; without it the tool watches the recorder's queue
        and says what that cannot show. A statistics command that names
        statistics (Core's `statistic_id` / `statistic_ids` fields) also
        returns Core's metadata for them as `statistics_metadata`, read
        after the write. Core also lists a statistic a sensor still
        provides, so after a clear read its statistics to see the rows went.
        Purges and statistics clears cannot be undone.

        EXAMPLES:
        - ha_manage_logs(action="recorder/adjust_sum_statistics", describe=True)
        - ha_manage_logs(action="recorder/clear_statistics", data={"statistic_ids": ["sensor.old_meter"]})
        - ha_manage_logs(action="recorder/list_statistic_ids", query="gas meter")
        - ha_manage_logs(action="system_log.write", data={"message": "checkpoint"})
        """
        try:
            if describe:
                warnings: list[str] = []
                response: dict[str, Any] = {
                    "success": True,
                    "action": action,
                    "result": await self._describe(action, warnings),
                }
                if warnings:
                    response["warnings"] = warnings
                return response
            return await self._run(action, data or {}, limit, offset, query)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(e, context={"action": action})
            return {}  # unreachable: exception_to_structured_error always raises


def register_logs_manage_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the recorder, statistics and log management tool."""
    register_tool_methods(mcp, LogsManageTools(client))
